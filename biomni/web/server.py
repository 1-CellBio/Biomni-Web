"""
Biomni Web UI -- FastAPI backend with SSE streaming.

Wraps the A1 agent's `app.stream()` loop and emits structured SSE events
that the React frontend consumes.

CRITICAL DESIGN NOTE:
  `agent.app.stream()` is a **synchronous blocking** generator (LangGraph).
  In an async SSE handler, calling it directly blocks the event-loop -- all
  `yield`ed SSE events queue up and are only flushed to the client once the
  synchronous iteration completes (i.e. after the *entire* conversation).
  To fix this we run the blocking stream in a background thread and bridge
  events via an `asyncio.Queue`.  The async SSE generator simply awaits the
  queue, so every event is flushed to the browser **immediately**.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import threading
import uuid
from pathlib import Path
from time import time
from typing import TYPE_CHECKING

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

if TYPE_CHECKING:
    from biomni.agent.a1 import A1

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_plan_steps(text: str) -> list[dict]:
    """Parse plan steps from reasoning text."""
    if not isinstance(text, str) or not text.strip():
        return []

    matches = re.findall(
        r"^\s*(?:[-*]|\d+[.)]\s*)\s*\[([ xX])\]\s+(.+)$",
        text, flags=re.MULTILINE,
    )
    if matches:
        steps = []
        for checked, label in matches:
            label = label.strip()
            if label:
                steps.append({"text": label, "done": checked.lower() == "x"})
        if steps:
            return steps

    numbered = re.findall(r"^\s*(\d+)[.)]\s+(.+)$", text, flags=re.MULTILINE)
    if len(numbered) >= 3:
        steps = []
        for _num, label in numbered:
            label = label.strip()
            if label:
                steps.append({"text": label, "done": False})
        if steps:
            return steps

    return []


def _summarize_thinking(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"```[\s\S]*?```", "", text)
    cleaned = re.sub(r"^\s*(?:[-*]|\d+[.)]\s*)\s*\[[ xX]\].*$", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"^\s*\d+[.)]\s+.*$", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"^[\s=\-*#]{4,}$", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\n{2,}", "\n", cleaned).strip()
    if not cleaned:
        return ""
    sentences = re.split(r"(?<=[.!?。！？])\s+", cleaned)
    result = ""
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if not result:
            result = s
        elif len(result) + len(s) + 1 < 200:
            result += " " + s
        else:
            break
    if len(result) > 250:
        result = result[:247] + "..."
    return result


def _summarize_observation(text: str) -> str:
    if not text:
        return ""
    lines = text.split("\n")
    meaningful: list[str] = []
    for line in lines:
        stripped = line.strip()
        if re.match(r"^[\s=\-*#]{4,}$", stripped):
            continue
        if not stripped:
            continue
        if len(stripped) > 300:
            stripped = stripped[:150] + "..."
        meaningful.append(stripped)
        if len(meaningful) >= 3:
            break
    result = "\n".join(meaningful)
    if len(result) > 300:
        result = result[:297] + "..."
    return result


# Patterns indicating the solution text is an internal directive, not user-facing
_INTERNAL_DIRECTIVE_RE = re.compile(
    r"^(?:tag\s+to\s+deliver|I\s+(?:will|should|need\s+to)\s|"
    r"Let\s+me\s|Now\s+I\s+(?:will|should)|"
    r"I(?:'ll|\s+will)\s+(?:now\s+)?(?:provide|present|show|deliver|send))",
    re.IGNORECASE
)

# Patterns indicating the text contains user-facing interactive content
_USER_FACING_RE = re.compile(
    r"(?:"
    r"^[A-E]\s*[-.):]\s*.{3,}"  # A. label / A - label
    r"|选项\s*\d+\s*[:：]"       # 选项1:
    r"|请告诉我|请选择|您想|你想|您希望"
    r"|继续.*?[？?]"
    r"|^\s*[-•*]\s*.{3,}[？?]\s*$"  # bullet + question
    r")",
    re.MULTILINE | re.IGNORECASE
)


def _enrich_solution_from_reasoning(solution: str, reasoning_texts: list[str]) -> str:
    """
    If the solution looks like an internal directive or is very short,
    and the accumulated reasoning contains rich user-facing content
    (options, suggestions, questions), use the last reasoning block
    as the solution instead, or prepend it.
    """
    if not reasoning_texts:
        return solution

    solution_stripped = solution.strip()

    # Check if solution is an internal directive or essentially empty
    is_internal = (
        not solution_stripped
        or _INTERNAL_DIRECTIVE_RE.search(solution_stripped)
        or len(solution_stripped) < 20
    )

    if not is_internal:
        return solution

    # Find the last reasoning block that has user-facing content
    for reasoning in reversed(reasoning_texts):
        if _USER_FACING_RE.search(reasoning):
            # This reasoning has interactive options — use it as the solution
            if solution_stripped and not _INTERNAL_DIRECTIVE_RE.search(solution_stripped):
                # Append solution after reasoning
                return reasoning.strip() + "\n\n" + solution_stripped
            return reasoning.strip()

    return solution


def _resolve_abs_path(file_path: str, agent_path: str | None = None) -> str | None:
    if os.path.isabs(file_path) and os.path.exists(file_path):
        return file_path
    cwd_candidate = os.path.join(os.getcwd(), file_path)
    if os.path.exists(cwd_candidate):
        return cwd_candidate
    if agent_path:
        path_candidate = os.path.join(agent_path, file_path)
        if os.path.exists(path_candidate):
            return path_candidate
    return None


_FILE_EXT_PATTERN = (
    r"\.png|\.jpg|\.jpeg|\.gif|\.bmp|\.webp|\.pdf|\.csv|\.tsv|\.xlsx|\.xls"
    r"|\.h5|\.h5ad|\.ipynb|\.svg|\.txt|\.json|\.rds|\.rdata|\.parquet|\.feather"
    r"|\.html|\.loom|\.zarr"
)

_IMG_EXTS = frozenset((".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg"))


def _extract_output_paths(text: str) -> list[str]:
    pattern = rf'["\']?([^\s"\']+?(?:{_FILE_EXT_PATTERN}))["\']?'
    matches = re.findall(pattern, text, flags=re.IGNORECASE)
    valid: list[str] = []
    seen: set[str] = set()
    for m in matches:
        if m.startswith(("Warning:", "Error:", "http://", "https://")):
            continue
        clean = m.strip("\"'()[]{}").strip().rstrip(".,;:")
        if clean and clean not in seen and not clean.startswith("."):
            seen.add(clean)
            valid.append(clean)
    return valid


def _extract_directory_paths(text: str) -> list[str]:
    dirs: list[str] = []
    seen: set[str] = set()
    for m in re.findall(
        r"""(?:output_dir|out_dir|save_dir|result_dir|results_dir)\s*=\s*(?:f?['"]([^'"]+)['"]|os\.path\.join\([^)]*,\s*['"]([^'"]+)['"]\))""",
        text,
    ):
        d = m[0] or m[1]
        if d and d not in seen:
            seen.add(d)
            dirs.append(d)
    for m in re.findall(r'os\.makedirs?\(\s*[f]?[\'"]([^\'"]+)[\'"]', text):
        if m and m not in seen:
            seen.add(m)
            dirs.append(m)
    for m in re.findall(r'saved?\s+(?:to|in)\s+[\'"]?([/.][\w/._-]+)[\'"]?', text, re.IGNORECASE):
        if m and ('/' in m) and not re.search(r'\.\w{1,5}$', m):
            if m not in seen:
                seen.add(m)
                dirs.append(m)
    return dirs


def _is_subpath(path: str, roots: list[str]) -> bool:
    try:
        rp = os.path.realpath(path)
    except Exception:
        return False
    for root in roots:
        try:
            rr = os.path.realpath(root)
            if os.path.commonpath([rp, rr]) == rr:
                return True
        except Exception:
            continue
    return False


def _normalize_files(
    files: list[dict], allowed_roots: list[str], excluded_paths: set[str],
    workspace_dir: str | None = None,
    data_lake_root: str | None = None,
) -> list[dict]:
    dl_real = os.path.realpath(data_lake_root) if data_lake_root else ""
    dedup: dict[str, dict] = {}
    for f in files:
        p = f.get("path")
        if not p or p in excluded_paths:
            continue
        # Exclude anything inside the read-only data lake
        if dl_real:
            try:
                rp = os.path.realpath(p)
                if os.path.commonpath([rp, dl_real]) == dl_real:
                    continue
            except (ValueError, OSError):
                pass
        if allowed_roots and not _is_subpath(p, allowed_roots):
            continue
        if workspace_dir and not f.get("relativePath"):
            f["relativePath"] = _make_relative(p, workspace_dir)
        dedup[p] = f
    out = list(dedup.values())
    out.sort(key=lambda x: (x.get("timestamp") or 0, x.get("name") or ""), reverse=True)
    return out


def _files_signature(files: list[dict]) -> tuple:
    return tuple((f.get("path"), f.get("timestamp")) for f in files)


def _make_relative(abs_path: str, workspace_dir: str) -> str:
    try:
        return os.path.relpath(abs_path, workspace_dir)
    except ValueError:
        return abs_path


def _make_file_entry(abs_path: str, workspace_dir: str | None = None) -> dict:
    ext = os.path.splitext(abs_path)[1].lower()
    ftype = "image" if ext in _IMG_EXTS else "file"
    try:
        mtime = int(os.path.getmtime(abs_path) * 1000)
    except OSError:
        mtime = int(time() * 1000)
    entry = {
        "path": abs_path, "name": os.path.basename(abs_path),
        "type": ftype, "ext": ext, "timestamp": mtime,
    }
    if workspace_dir:
        entry["relativePath"] = _make_relative(abs_path, workspace_dir)
    return entry


def _scan_directory(
    dir_path: str, files: list[dict],
    exclude_paths: set[str] | None = None,
    since_ts: float | None = None,
    workspace_dir: str | None = None,
) -> None:
    if not os.path.isdir(dir_path):
        return
    exclude = exclude_paths or set()
    try:
        for entry in sorted(os.scandir(dir_path), key=lambda e: e.name):
            if entry.name.startswith('.'):
                continue
            if entry.is_file():
                ap = entry.path
                if ap in exclude:
                    continue
                already = any(f["path"] == ap for f in files)
                if since_ts is not None and not already:
                    try:
                        if entry.stat().st_mtime < since_ts:
                            continue
                    except OSError:
                        continue
                if not already:
                    files.append(_make_file_entry(ap, workspace_dir))
            elif entry.is_dir():
                _scan_directory(entry.path, files, exclude,
                                since_ts=since_ts, workspace_dir=workspace_dir)
    except PermissionError:
        pass


def _create_session_workspace(session_id: str | None = None) -> tuple[str, str, str]:
    """Create a per-session workspace.  Returns (session_id, workspace_dir, upload_subdir)."""
    sid = session_id or uuid.uuid4().hex[:12]
    base = os.path.join(os.path.expanduser("~"), ".biomni", "sessions", sid)
    uploads = os.path.join(base, "uploads")
    os.makedirs(uploads, exist_ok=True)
    return sid, base, uploads


def _safe_upload_name(dest_dir: str, original_name: str) -> str:
    candidate = os.path.join(dest_dir, original_name)
    if not os.path.exists(candidate):
        return candidate
    stem, ext = os.path.splitext(original_name)
    n = 1
    while True:
        candidate = os.path.join(dest_dir, f"{stem}_{n}{ext}")
        if not os.path.exists(candidate):
            return candidate
        n += 1


def _new_session_state(workspace_dir: str) -> dict:
    """Return a fresh conversation-state dict for a session."""
    return {
        "messages": [],
        "result_files": [],
        "tracked_dirs": [],
        "uploaded_paths": set(),
        "allowed_result_roots": [workspace_dir],
        "workspace_dir": workspace_dir,
        "executor_log": [],
        "plan_steps": [],
        "active_plan_index": None,
    }


# ---------------------------------------------------------------------------
# Disk persistence helpers
# ---------------------------------------------------------------------------

_SESSIONS_BASE = os.path.join(os.path.expanduser("~"), ".biomni", "sessions")


def _save_session_to_disk(sess: dict) -> None:
    """Serialize session state to ``~/.biomni/sessions/<id>/session.json``."""
    try:
        ws = sess["workspace"]
        state = sess["state"]
        data = {
            "id": sess["id"],
            "title": sess.get("title", "New Task"),
            "created": sess.get("created", 0),
            "workspace": ws,
            "upload_dir": sess.get("upload_dir", ""),
            "state": {
                "messages": state["messages"],
                "result_files": state["result_files"],
                "tracked_dirs": state["tracked_dirs"],
                "uploaded_paths": list(state["uploaded_paths"]),
                "allowed_result_roots": state["allowed_result_roots"],
                "workspace_dir": state["workspace_dir"],
                "executor_log": state.get("executor_log", []),
                "plan_steps": state.get("plan_steps", []),
                "active_plan_index": state.get("active_plan_index"),
            },
        }
        os.makedirs(ws, exist_ok=True)
        path = os.path.join(ws, "session.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # best-effort


def _load_sessions_from_disk() -> dict[str, dict]:
    """Load all sessions from ``~/.biomni/sessions/*/session.json``."""
    loaded: dict[str, dict] = {}
    base = _SESSIONS_BASE
    if not os.path.isdir(base):
        return loaded
    for name in os.listdir(base):
        session_dir = os.path.join(base, name)
        json_path = os.path.join(session_dir, "session.json")
        if not os.path.isfile(json_path):
            continue
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            sid = data["id"]
            state_data = data.get("state", {})
            state_data["uploaded_paths"] = set(state_data.get("uploaded_paths", []))
            # Ensure new fields exist even for old sessions
            state_data.setdefault("executor_log", [])
            state_data.setdefault("plan_steps", [])
            state_data.setdefault("active_plan_index", None)
            sess = {
                "id": sid,
                "workspace": data.get("workspace", session_dir),
                "upload_dir": data.get("upload_dir", os.path.join(session_dir, "uploads")),
                "state": state_data,
                "title": data.get("title", "New Task"),
                "created": data.get("created", 0),
            }
            # Ensure uploads directory exists
            os.makedirs(sess["upload_dir"], exist_ok=True)
            loaded[sid] = sess
        except Exception:
            continue
    return loaded


_DONE = object()


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(agent: A1) -> FastAPI:
    app = FastAPI(title="Biomni Web UI")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"], allow_credentials=True,
        allow_methods=["*"], allow_headers=["*"],
    )

    # ---- Multi-session state ----
    # sessions: { session_id: { "workspace": str, "upload_dir": str, "state": dict, "title": str, "created": float } }
    sessions: dict[str, dict] = _load_sessions_from_disk()
    # active_session_id tracks the currently active session
    active_session: dict = {"id": None}
    # If we restored sessions, pick the most recent one as active
    if sessions:
        most_recent = max(sessions.values(), key=lambda s: s.get("created", 0))
        active_session["id"] = most_recent["id"]

    def _get_or_create_session(sid: str | None = None) -> dict:
        """Get existing session or create a new one.  Returns session dict."""
        if sid and sid in sessions:
            return sessions[sid]
        new_sid, ws, uploads = _create_session_workspace(sid)
        sess = {
            "id": new_sid,
            "workspace": ws,
            "upload_dir": uploads,
            "state": _new_session_state(ws),
            "title": "New Task",
            "created": time(),
        }
        sessions[new_sid] = sess
        active_session["id"] = new_sid
        _save_session_to_disk(sess)
        return sess

    def _active() -> dict:
        """Return the currently active session, creating one if needed."""
        sid = active_session.get("id")
        if sid and sid in sessions:
            return sessions[sid]
        return _get_or_create_session()

    def _session_summary(sess: dict) -> dict:
        """Return a JSON-serializable summary of a session for the frontend."""
        msgs = sess["state"]["messages"]
        title = sess.get("title", "New Task")
        # Auto-derive title from first user message
        if title == "New Task" and msgs:
            for m in msgs:
                if m["role"] == "user":
                    raw = m["content"].split("\n")[0].strip()
                    title = raw[:60] + ("..." if len(raw) > 60 else "")
                    sess["title"] = title
                    break
        return {
            "id": sess["id"],
            "title": title,
            "messageCount": len(msgs),
            "created": sess["created"],
            "isActive": sess["id"] == active_session.get("id"),
        }

    # ------------------------------------------------------------------
    # Session management endpoints
    # ------------------------------------------------------------------
    @app.get("/api/sessions")
    async def list_sessions():
        """Return all sessions sorted by creation time (newest first)."""
        summaries = [_session_summary(s) for s in sessions.values()]
        summaries.sort(key=lambda x: x["created"], reverse=True)
        return JSONResponse({"sessions": summaries, "activeId": active_session.get("id")})

    @app.post("/api/sessions")
    async def create_session():
        """Create a new session and set it as active."""
        sess = _get_or_create_session()
        return JSONResponse(_session_summary(sess))

    @app.post("/api/sessions/{session_id}/activate")
    async def activate_session(session_id: str):
        """Switch the active session."""
        if session_id not in sessions:
            return JSONResponse({"error": "Session not found"}, status_code=404)
        active_session["id"] = session_id
        sess = sessions[session_id]
        # Return full state so frontend can restore
        st = sess["state"]
        return JSONResponse({
            **_session_summary(sess),
            "messages": st["messages"],
            "resultFiles": st["result_files"],
            "executorLog": st.get("executor_log", []),
            "planSteps": st.get("plan_steps", []),
            "activePlanIndex": st.get("active_plan_index"),
        })

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str):
        """Delete a session and its workspace directory."""
        if session_id not in sessions:
            return JSONResponse({"error": "Session not found"}, status_code=404)
        # Remove workspace from disk
        ws = sessions[session_id].get("workspace")
        del sessions[session_id]
        if ws and os.path.isdir(ws):
            try:
                shutil.rmtree(ws)
            except Exception:
                pass
        # If deleted the active one, switch to another or create new
        if active_session.get("id") == session_id:
            if sessions:
                active_session["id"] = next(iter(sessions))
            else:
                active_session["id"] = None
        return JSONResponse({"ok": True})

    @app.patch("/api/sessions/{session_id}")
    async def update_session(session_id: str, request: Request):
        """Update session metadata (e.g. title)."""
        if session_id not in sessions:
            return JSONResponse({"error": "Session not found"}, status_code=404)
        body = await request.json()
        if "title" in body:
            sessions[session_id]["title"] = body["title"]
        return JSONResponse(_session_summary(sessions[session_id]))

    # ------------------------------------------------------------------
    # SSE streaming endpoint
    # ------------------------------------------------------------------
    @app.post("/api/chat")
    async def chat_stream(request: Request):
        body = await request.json()
        prompt = body.get("prompt", "")
        uploaded_files = body.get("files", [])
        # Allow specifying session; default to active
        req_sid = body.get("sessionId")
        sess = _get_or_create_session(req_sid) if req_sid else _active()
        conversation_state = sess["state"]
        ws_dir = sess["workspace"]
        ul_dir = sess["upload_dir"]

        for fp in uploaded_files:
            conversation_state["uploaded_paths"].add(fp)

        text_input = prompt
        for fp in uploaded_files:
            text_input += f"\n\n User uploaded this file: {fp}\n Please use it if needed."

        # Determine the read-only data-lake root so we can enforce sandbox
        _data_lake_root = os.path.realpath(getattr(agent, "path", "") or "")

        # Append sandbox instruction so the LLM writes outputs into the
        # session workspace (CWD) instead of the read-only data_lake.
        sandbox_hint = (
            "\n\n[System] Important: The data_lake path is READ-ONLY. "
            "You must save ALL output files (plots, processed data, downloads, "
            "extracted archives) to the current working directory (os.getcwd()) "
            "or a subdirectory of it. Do NOT write files into the data_lake path. "
            "Use work_dir = os.getcwd() for output."
        )
        text_input += sandbox_hint

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        cancel_event = threading.Event()
        # Store cancel_event so /api/stop can reach it
        sess["_cancel"] = cancel_event

        def _put(evt: dict) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, evt)

        def _producer() -> None:
            from langchain_core.messages import AIMessage, HumanMessage

            prev_cwd = os.getcwd()
            try:
                os.chdir(ws_dir)
            except OSError:
                pass

            # Inject session workspace into the persistent REPL namespace
            # so LLM-generated code using relative paths resolves here.
            try:
                from biomni.tool.support_tools import _persistent_namespace
                _persistent_namespace["_session_workspace"] = ws_dir
                _persistent_namespace["_output_dir"] = ws_dir
            except ImportError:
                pass

            try:
                agent_messages = []
                for msg in conversation_state["messages"]:
                    if msg["role"] == "user":
                        agent_messages.append(HumanMessage(content=msg["content"]))
                    elif msg["role"] == "assistant":
                        agent_messages.append(AIMessage(content=msg["content"]))
                agent_messages.append(HumanMessage(content=text_input))

                conversation_state["messages"].append({"role": "user", "content": text_input})

                inputs = {"messages": agent_messages, "next_step": None}
                config = {"recursion_limit": 500, "configurable": {"thread_id": 42}}

                plan_steps: list[dict] = []
                result_files: list[dict] = list(conversation_state["result_files"])
                tracked_dirs: list[str] = list(conversation_state["tracked_dirs"])
                allowed_roots: list[str] = list(conversation_state["allowed_result_roots"])
                active_plan_index: int | None = None
                excluded = conversation_state["uploaded_paths"]
                last_files_sig: tuple = ()
                # Accumulate executor log entries for persistence
                executor_log_entries: list[dict] = list(conversation_state.get("executor_log", []))

                def emit(payload: dict) -> None:
                    _put({"event": "message", "data": json.dumps(payload)})

                # Resolve the real data-lake root for sandbox checks
                _dl_real = os.path.realpath(_data_lake_root) if _data_lake_root else ""

                def _in_data_lake(p: str) -> bool:
                    """Return True if *p* is inside the read-only data-lake."""
                    if not _dl_real:
                        return False
                    try:
                        rp = os.path.realpath(p)
                        return os.path.commonpath([rp, _dl_real]) == _dl_real
                    except (ValueError, OSError):
                        return False

                def _in_workspace(p: str) -> bool:
                    """Return True if *p* is inside the session workspace."""
                    try:
                        rp = os.path.realpath(p)
                        rw = os.path.realpath(ws_dir)
                        return os.path.commonpath([rp, rw]) == rw
                    except (ValueError, OSError):
                        return False

                def collect_files(text: str, files: list[dict], a_path: str | None) -> None:
                    paths = _extract_output_paths(text)
                    for fp in paths:
                        abs_path = _resolve_abs_path(fp, a_path)
                        if not abs_path or abs_path in excluded:
                            continue
                        if any(f["path"] == abs_path for f in files):
                            continue
                        # Skip files inside the read-only data lake
                        if _in_data_lake(abs_path):
                            continue
                        parent = os.path.dirname(abs_path)
                        # Only expand allowed_roots within the session workspace
                        if parent and parent not in allowed_roots:
                            if _in_workspace(parent) and not parent.startswith(ul_dir):
                                allowed_roots.append(parent)
                        files.append(_make_file_entry(abs_path, ws_dir))

                def collect_dirs(text: str, a_path: str | None) -> None:
                    dirs = _extract_directory_paths(text)
                    for d in dirs:
                        abs_dir = _resolve_abs_path(d, a_path) if not os.path.isabs(d) else d
                        if not abs_dir:
                            candidate = os.path.join(os.getcwd(), d)
                            if os.path.isdir(candidate):
                                abs_dir = candidate
                        if not abs_dir or not os.path.isdir(abs_dir):
                            continue
                        if abs_dir in tracked_dirs:
                            continue
                        # Skip upload dir and anything inside data lake
                        if abs_dir.startswith(ul_dir):
                            continue
                        if _in_data_lake(abs_dir):
                            continue
                        # Only track directories inside the session workspace
                        if not _in_workspace(abs_dir):
                            continue
                        tracked_dirs.append(abs_dir)
                        if abs_dir not in allowed_roots:
                            allowed_roots.append(abs_dir)
                        _scan_directory(abs_dir, result_files, excluded,
                                        since_ts=run_start_ts, workspace_dir=ws_dir)

                def rescan_tracked_dirs() -> None:
                    for d in tracked_dirs:
                        if os.path.isdir(d):
                            _scan_directory(d, result_files, excluded,
                                            since_ts=run_start_ts, workspace_dir=ws_dir)

                # Internal files that should never appear in Results
                _internal_names = frozenset({"session.json", "uploads"})

                def scan_workspace() -> None:
                    """Scan the entire session workspace for new files.

                    This is the primary discovery mechanism — since CWD is the
                    session workspace, every file the agent creates will be here.
                    The regex-based collect_files/collect_dirs are kept as a
                    supplement for immediate detection, but this full scan
                    ensures nothing is missed.
                    """
                    if not os.path.isdir(ws_dir):
                        return
                    try:
                        for entry in sorted(os.scandir(ws_dir), key=lambda e: e.name):
                            if entry.name.startswith('.') or entry.name in _internal_names:
                                continue
                            if entry.is_dir():
                                _scan_directory(entry.path, result_files, excluded,
                                                since_ts=run_start_ts, workspace_dir=ws_dir)
                            elif entry.is_file():
                                ap = entry.path
                                if ap in excluded:
                                    continue
                                already = any(f["path"] == ap for f in result_files)
                                if not already:
                                    if run_start_ts is not None:
                                        try:
                                            if entry.stat().st_mtime < run_start_ts:
                                                continue
                                        except OSError:
                                            continue
                                    result_files.append(_make_file_entry(ap, ws_dir))
                    except PermissionError:
                        pass

                def emit_files_if_changed() -> None:
                    nonlocal last_files_sig
                    visible = _normalize_files(result_files, allowed_roots, excluded,
                                               workspace_dir=ws_dir,
                                               data_lake_root=_data_lake_root)
                    sig = _files_signature(visible)
                    if sig != last_files_sig:
                        last_files_sig = sig
                        result_files.clear()
                        result_files.extend(visible)
                        emit({"type": "files", "files": result_files})

                t = time()
                run_start_ts = t
                solution_found = False
                agent_path = getattr(agent, "path", None)
                # Track all reasoning text in case we need it as fallback for solution
                all_reasoning_texts: list[str] = []

                if result_files:
                    emit_files_if_changed()

                if agent.use_tool_retriever:
                    emit({"type": "status", "content": "Retrieving relevant tools..."})
                    executor_log_entries.append({"kind": "status", "content": "Retrieving relevant tools..."})
                    try:
                        selected = agent._prepare_resources_for_retrieval(text_input)
                        if selected:
                            agent.update_system_prompt_with_selected_resources(selected)
                    except Exception:
                        pass

                for s in agent.app.stream(inputs, stream_mode="values", config=config):
                    # Check cancellation between iterations
                    if cancel_event.is_set():
                        emit({"type": "status", "content": "Stopped by user."})
                        executor_log_entries.append({"kind": "status", "content": "Stopped by user."})
                        break

                    t_step = time() - t
                    message = s["messages"][-1]

                    if message.content == text_input:
                        t = time()
                        continue

                    if not isinstance(message.content, str):
                        if isinstance(message.content, list):
                            parts = []
                            for block in message.content:
                                if isinstance(block, dict):
                                    btype = block.get("type")
                                    if btype in ("text", "output_text", "redacted_text"):
                                        parts.append(block.get("text") or block.get("content") or "")
                            msg_content = "".join(parts)
                        else:
                            msg_content = str(message.content)
                    else:
                        msg_content = message.content

                    # ---- Reasoning ----
                    tag_positions = []
                    for tag in ["<execute>", "<solution>", "<observation>"]:
                        pos = msg_content.find(tag)
                        if pos != -1:
                            tag_positions.append(pos)

                    has_tags = bool(tag_positions)

                    if has_tags:
                        first_tag_pos = min(tag_positions)
                        thinking = msg_content[:first_tag_pos].strip()
                    else:
                        # No tags at all — the entire message is reasoning/text
                        thinking = msg_content.strip()

                    if thinking:
                        emit({"type": "reasoning", "content": thinking})
                        executor_log_entries.append({"kind": "reasoning", "content": thinking})
                        all_reasoning_texts.append(thinking)
                        extracted = _extract_plan_steps(thinking)
                        plan_already_active = plan_steps and any(
                            st.get("done") or st.get("details") for st in plan_steps
                        )
                        if extracted and not plan_already_active:
                            plan_steps = extracted
                            for step in plan_steps:
                                if "details" not in step:
                                    step["details"] = []
                            active_plan_index = None
                            emit({"type": "plan", "steps": plan_steps})
                        elif plan_steps and active_plan_index is not None and 0 <= active_plan_index < len(plan_steps):
                            summary = _summarize_thinking(thinking)
                            if summary:
                                plan_steps[active_plan_index].setdefault("details", []).append(
                                    {"source": "reasoning", "text": summary}
                                )
                                emit({"type": "plan", "steps": plan_steps, "activeIndex": active_plan_index})

                    # ---- Solution ----
                    solution_match = re.search(r"<solution>(.*?)</solution>", msg_content, re.DOTALL)
                    if solution_match and not solution_found:
                        solution = solution_match.group(1).strip()
                        # Check if solution is an internal directive / too short
                        # and reasoning has richer user-facing content
                        solution = _enrich_solution_from_reasoning(solution, all_reasoning_texts)
                        emit({"type": "solution", "content": solution})
                        conversation_state["messages"].append({"role": "assistant", "content": solution})
                        solution_found = True
                        if plan_steps:
                            for step in plan_steps:
                                step["done"] = True
                            emit({"type": "plan", "steps": plan_steps})

                    # ---- Execute ----
                    execute_match = re.search(r"<execute>(.*?)</execute>", msg_content, re.DOTALL)
                    if execute_match:
                        code = execute_match.group(1).strip()
                        language = "python"
                        if code.strip().startswith("#!R"):
                            language = "r"
                            code = re.sub(r"^#!R", "", code, count=1).strip()
                        elif code.strip().startswith("#!BASH") or code.strip().startswith("#!CLI"):
                            language = "bash"
                            code = re.sub(r"^#!BASH|^#!CLI", "", code, count=1).strip()

                        if plan_steps:
                            active_plan_index = next(
                                (i for i, step in enumerate(plan_steps) if not step.get("done")),
                                None,
                            )
                            emit({"type": "plan", "steps": plan_steps, "activeIndex": active_plan_index})

                        emit({"type": "execute", "language": language, "code": code})
                        executor_log_entries.append({"kind": "code", "language": language, "code": code})

                        collect_dirs(code, agent_path)
                        collect_files(code, result_files, agent_path)
                        emit_files_if_changed()

                    # ---- Observation ----
                    obs_match = re.search(r"<observation>(.*?)</observation>", msg_content, re.DOTALL)
                    if obs_match:
                        observation = obs_match.group(1).strip()
                        emit({"type": "observation", "content": observation, "duration": round(t_step, 2)})
                        executor_log_entries.append({"kind": "observation", "content": observation, "duration": round(t_step, 2)})

                        if plan_steps and active_plan_index is not None and 0 <= active_plan_index < len(plan_steps):
                            obs_summary = _summarize_observation(observation)
                            if obs_summary:
                                plan_steps[active_plan_index].setdefault("details", []).append(
                                    {"source": "observation", "text": obs_summary}
                                )
                            plan_steps[active_plan_index]["done"] = True
                            active_plan_index = None
                            emit({"type": "plan", "steps": plan_steps})

                        collect_files(observation, result_files, agent_path)
                        collect_files(msg_content, result_files, agent_path)
                        collect_dirs(observation, agent_path)
                        rescan_tracked_dirs()
                        scan_workspace()
                        emit_files_if_changed()

                    rescan_tracked_dirs()
                    scan_workspace()
                    emit_files_if_changed()
                    t = time()

                # Final rescan — full workspace scan catches everything
                rescan_tracked_dirs()
                scan_workspace()
                emit_files_if_changed()

                if not solution_found:
                    try:
                        final_msg = s["messages"][-1].content if s.get("messages") else ""
                    except Exception:
                        final_msg = ""
                    sol_match = re.search(r"<solution>(.*?)</solution>", str(final_msg), re.DOTALL)
                    if sol_match:
                        sol = sol_match.group(1).strip()
                        sol = _enrich_solution_from_reasoning(sol, all_reasoning_texts)
                        emit({"type": "solution", "content": sol})
                        conversation_state["messages"].append({"role": "assistant", "content": sol})
                    else:
                        cleaned = re.sub(r"<execute>.*?</execute>", "", str(final_msg), flags=re.DOTALL)
                        cleaned = re.sub(r"<observation>.*?</observation>", "", cleaned, flags=re.DOTALL)
                        cleaned = re.sub(r"\n\s*\n", "\n\n", cleaned).strip()
                        # If cleaned text looks like an internal directive, prefer reasoning
                        cleaned = _enrich_solution_from_reasoning(cleaned, all_reasoning_texts)
                        if cleaned:
                            emit({"type": "solution", "content": cleaned})
                            conversation_state["messages"].append({"role": "assistant", "content": cleaned})

                # Persist
                final_files = _normalize_files(result_files, allowed_roots, excluded,
                                               workspace_dir=ws_dir,
                                               data_lake_root=_data_lake_root)
                conversation_state["result_files"] = final_files
                conversation_state["tracked_dirs"] = tracked_dirs
                conversation_state["allowed_result_roots"] = allowed_roots
                conversation_state["executor_log"] = executor_log_entries
                conversation_state["plan_steps"] = plan_steps
                conversation_state["active_plan_index"] = active_plan_index

                # Save session to disk for restart recovery
                _save_session_to_disk(sess)

                emit({"type": "done"})

            except Exception as exc:
                _put({"event": "message", "data": json.dumps({"type": "error", "content": str(exc)})})
                _put({"event": "message", "data": json.dumps({"type": "done"})})
            finally:
                sess.pop("_cancel", None)
                try:
                    os.chdir(prev_cwd)
                except OSError:
                    pass
                _put(_DONE)

        async def event_generator():
            thread = threading.Thread(target=_producer, daemon=True)
            thread.start()
            while True:
                item = await queue.get()
                if item is _DONE:
                    break
                yield item

        return EventSourceResponse(event_generator())

    # ------------------------------------------------------------------
    # Stop streaming
    # ------------------------------------------------------------------
    @app.post("/api/stop")
    async def stop_stream():
        """Signal the running producer thread to stop as soon as possible."""
        sess = _active()
        cancel_ev = sess.get("_cancel")
        if cancel_ev and isinstance(cancel_ev, threading.Event):
            cancel_ev.set()
        return JSONResponse({"ok": True})

    # ------------------------------------------------------------------
    # File serving
    # ------------------------------------------------------------------
    @app.get("/api/files")
    async def get_file(path: str):
        if not os.path.isabs(path) or not os.path.exists(path):
            return JSONResponse({"error": "File not found"}, status_code=404)
        sess = _active()
        roots = sess["state"].get("allowed_result_roots", [])
        if roots and not _is_subpath(path, roots):
            return JSONResponse({"error": "Access denied"}, status_code=403)
        return FileResponse(path, filename=os.path.basename(path))

    # ------------------------------------------------------------------
    # Directory listing
    # ------------------------------------------------------------------
    @app.get("/api/dir")
    async def list_dir(path: str):
        if not os.path.isabs(path) or not os.path.isdir(path):
            return JSONResponse({"error": "Directory not found"}, status_code=404)
        sess = _active()
        roots = sess["state"].get("allowed_result_roots", [])
        if not roots or not _is_subpath(path, roots):
            return JSONResponse({"error": "Access denied"}, status_code=403)
        ws = sess["workspace"]
        entries = []
        try:
            for entry in sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name)):
                if entry.name.startswith('.'):
                    continue
                if entry.is_dir():
                    e_dict: dict = {"name": entry.name, "path": entry.path, "type": "directory", "ext": ""}
                    if ws:
                        e_dict["relativePath"] = _make_relative(entry.path, ws)
                    entries.append(e_dict)
                elif entry.is_file():
                    ext = os.path.splitext(entry.name)[1].lower()
                    e_dict = {
                        "name": entry.name, "path": entry.path,
                        "type": "image" if ext in _IMG_EXTS else "file",
                        "ext": ext, "timestamp": int(entry.stat().st_mtime * 1000),
                    }
                    if ws:
                        e_dict["relativePath"] = _make_relative(entry.path, ws)
                    entries.append(e_dict)
        except PermissionError:
            pass
        return JSONResponse({"entries": entries, "path": path})

    # ------------------------------------------------------------------
    # File upload
    # ------------------------------------------------------------------
    @app.post("/api/upload")
    async def upload_file(file: UploadFile = File(...)):
        sess = _active()
        original_name = file.filename or "file"
        dest = _safe_upload_name(sess["upload_dir"], original_name)
        with open(dest, "wb") as f:
            shutil.copyfileobj(file.file, f)
        sess["state"]["uploaded_paths"].add(dest)
        return JSONResponse({"path": dest, "name": original_name})

    # ------------------------------------------------------------------
    # Rescan workspace (manual refresh)
    # ------------------------------------------------------------------
    @app.post("/api/rescan")
    async def rescan_files():
        """Re-scan the active session workspace and return updated file list."""
        sess = _active()
        st = sess["state"]
        ws = sess["workspace"]
        ul = sess["upload_dir"]
        excluded = st["uploaded_paths"]
        dl_root = os.path.realpath(getattr(agent, "path", "") or "")
        _internal = {"session.json", "uploads"}
        result_files: list[dict] = []

        # Full scan of workspace
        if os.path.isdir(ws):
            try:
                for entry in sorted(os.scandir(ws), key=lambda e: e.name):
                    if entry.name.startswith('.') or entry.name in _internal:
                        continue
                    if entry.is_dir():
                        _scan_directory(entry.path, result_files, excluded,
                                        since_ts=None, workspace_dir=ws)
                    elif entry.is_file():
                        ap = entry.path
                        if ap not in excluded:
                            result_files.append(_make_file_entry(ap, ws))
            except PermissionError:
                pass

        visible = _normalize_files(result_files,
                                   st.get("allowed_result_roots", [ws]),
                                   excluded, workspace_dir=ws,
                                   data_lake_root=dl_root)
        st["result_files"] = visible
        _save_session_to_disk(sess)
        return JSONResponse({"files": visible})

    # ------------------------------------------------------------------
    # Legacy reset (creates new session)
    # ------------------------------------------------------------------
    @app.post("/api/reset")
    async def reset():
        sess = _get_or_create_session()
        if hasattr(agent, "main_history_copy"):
            agent.main_history_copy = []
        return JSONResponse({"ok": True, "sessionId": sess["id"]})

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------
    @app.get("/api/health")
    async def health():
        return JSONResponse({"status": "ok"})

    # ------------------------------------------------------------------
    # Mount frontend
    # ------------------------------------------------------------------
    frontend_dist = Path(__file__).parent / "frontend" / "dist"
    if frontend_dist.exists():
        app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")

    return app
