> **Acerca de Biomni**
>
> Biomni es un agente de IA biomédica de propósito general. Para obtener la documentación completa (características, API, evaluación, etc.), consulte [README_EN.md](./README_EN.md).
>
> **Esta página se enfoca en el despliegue local.**

---

## Vista previa de la Web UI personalizada

<p align="center">
  <img src="./figs/customUI.png" alt="Biomni Custom Web UI" width="800px" />
</p>

---

## Despliegue Local

> Probado en macOS (Apple Silicon). También es compatible con Linux (Ubuntu).

### Requisitos previos

- [Anaconda](https://www.anaconda.com/download) o [Miniconda](https://docs.conda.io/en/latest/miniconda.html)
- Git
- Git LFS

---

### Paso 1: Clonar el repositorio

```bash
git clone https://github.com/1-CellBio/Biomni-Web.git
cd Biomni-Web
```

---

### Paso 2: Instalar Git LFS

<details>
<summary><b>macOS</b></summary>

```bash
brew install git-lfs
git lfs install
```
</details>

<details>
<summary><b>Ubuntu / Linux</b></summary>

```bash
sudo apt-get install git-lfs
git lfs install
```
</details>

---

### Paso 3: Crear el entorno conda

Esto creará un nuevo entorno de conda llamado `biomni_e1`.

```bash
cd biomni_env
conda env create -f environment.yml
```

> **Nota**: Esto puede tardar un tiempo dependiendo de su velocidad de red.

---

### Paso 4: Activar el entorno

```bash
conda activate biomni_e1
```

---

### Paso 5: Instalar paquetes adicionales

```bash
bash new_software_v008.sh
```

---

### Paso 6: Volver a la raíz del proyecto

```bash
cd ..
```

---

### Paso 7: Configurar claves de API

Biomni requiere una clave de API de LLM para funcionar. Copie el ejemplo de configuración y complete sus claves.

```bash
cp .env.example .env
```

Edite `.env` con sus claves de API reales.

<details>
<summary>Ejemplo de .env</summary>

```env
# Usando Zhipu GLM como ejemplo:
BIOMNI_CUSTOM_API_KEY=your_api_key_here
BIOMNI_CUSTOM_BASE_URL=https://open.bigmodel.cn/api/coding/paas/v4
LLM_SOURCE=Custom

# O usando Anthropic Claude:
# ANTHROPIC_API_KEY=your_anthropic_api_key_here

# Ruta de datos (opcional, por defecto ./data)
# BIOMNI_DATA_PATH=/path/to/your/data
```
</details>

Biomni requiere una suscripción al Coding Plan del modelo de lenguaje. Use los siguientes códigos de invitación para obtener un **10% de descuento**.

| Modelo | Proveedor | Enlace |
|-------|----------|------|
| GLM-5 | 智谱 AI | [Suscribirse](https://www.bigmodel.cn/glm-coding?ic=RCNVSLF7WF) |
| MiniMax | MiniMax | [Suscribirse](https://platform.minimaxi.com/subscribe/coding-plan?code=2HXOCIB1gJ&source=link) |


---

### Paso 8: Instalar Biomni con Web UI

```bash
pip install -e ".[web]"
```

---

### Paso 9: ¡Lanzar!

```bash
biomni-web
```

Abra su navegador y visite: **http://localhost:7860**

---

## Referencia Rápida

```bash
# Despliegue completo en un solo paso
git clone https://github.com/1-CellBio/Biomni-Web.git
cd Biomni-Web/biomni_env
conda env create -f environment.yml
conda activate biomni_e1
bash new_software_v008.sh
cd ..
cp .env.example .env        # Luego edite .env con sus claves de API
pip install -e ".[web]"
biomni-web
```

---

## Opciones de CLI

| Flag | Predeterminado | Descripción |
|------|---------|---------------------|
| `--port` | `7860` | Puerto a vincular |
| `--host` | `0.0.0.0` | Host/IP a vincular |
| `--path` | `None` | Ruta de datos (anula `BIOMNI_DATA_PATH`) |
| `--llm` | `None` | Nombre del modelo LLM |
| `--source` | `None` | Proveedor de origen del LLM |

**Ejemplos:**

```bash
# Usar un modelo específico
biomni-web --llm glm-4-plus --source Custom --path ./data

# Cambiar puerto
biomni-web --port 8080

# Solo localhost
biomni-web --host 127.0.0.1
```

---

## Solución de Problemas

<details>
<summary><b>Errores de SSL durante pip install</b></summary>

```bash
pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org -e ".[web]"
```
</details>

<details>
<summary><b>Puerto ya en uso</b></summary>

```bash
biomni-web --port 8888
```
</details>

<details>
<summary><b>No se puede acceder desde otros dispositivos en la LAN</b></summary>

Asegúrese de que `--host 0.0.0.0` (predeterminado) esté configurado y verifique los ajustes de su firewall.
</details>

---

## Suscripción de LLM

Biomni requiere una suscripción al Coding Plan de un LLM. Use los códigos de invitación a continuación para obtener un **10% de descuento**.

| Modelo | Proveedor | Enlace |
|-------|----------|------|
| GLM-5 | 智谱 AI | [Suscribirse](https://www.bigmodel.cn/glm-coding?ic=RCNVSLF7WF) |
| MiniMax | MiniMax | [Suscribirse](https://platform.minimaxi.com/subscribe/coding-plan?code=2HXOCIB1gJ&source=link) |

---

## Enlaces

- [Documentación Completa (README_EN.md)](./README_EN.md)
- [Repositorio de GitHub](https://github.com/1-CellBio/Biomni-Web)
- [Navegador de Transcriptómica de Célula Única / Espacial](https://1cellbio.com)
- [Biomni Oficial (Stanford)](https://biomni.stanford.edu)

---

## Citar

```bibtex
@article{huang2025biomni,
  title={Biomni: A General-Purpose Biomedical AI Agent},
  author={Huang, Kexin and Zhang, Serena and Wang, Hanchen and Qu, Yuanhao and Lu, Yingzhou and Roohani, Yusuf and Li, Ryan and Qiu, Lin and Zhang, Junze and Di, Yin and others},
  journal={bioRxiv},
  pages={2025--05},
  year={2025},
  publisher={Cold Spring Harbor Laboratory}
}
```

---

<p align="center">
  WebUI por <strong>CoEnLeL</strong> &middot; Impulsado por Biomni Agent &middot; Construido con React + FastAPI
</p>
