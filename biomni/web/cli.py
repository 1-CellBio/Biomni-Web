"""CLI entry point for launching the Biomni Web UI.

Usage:
    biomni-web                        # default port 7860
    biomni-web --port 8080            # custom port
    biomni-web --host 127.0.0.1       # localhost only
    biomni-web --path /data/my_files  # set agent data path
"""

import argparse
import sys


def main():
    parser = argparse.ArgumentParser(
        prog="biomni-web",
        description="Launch the Biomni Web UI (React + FastAPI)",
    )
    parser.add_argument("--port", type=int, default=7860, help="Port to bind to (default: 7860)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host/IP to bind to (default: 0.0.0.0)")
    parser.add_argument("--path", type=str, default=None, help="Data path for the agent")
    parser.add_argument("--llm", type=str, default=None, help="LLM model name")
    parser.add_argument("--source", type=str, default=None, help="LLM source provider")

    args = parser.parse_args()

    # Lazy import so --help is fast
    from biomni.agent.a1 import A1

    kwargs = {}
    if args.path is not None:
        kwargs["path"] = args.path
    if args.llm is not None:
        kwargs["llm"] = args.llm
    if args.source is not None:
        kwargs["source"] = args.source

    print("Initializing Biomni agent...")
    agent = A1(**kwargs)
    agent.launch_web_demo(port=args.port, host=args.host)


if __name__ == "__main__":
    main()
