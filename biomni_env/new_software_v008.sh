#!/bin/bash
# Biomni v0.0.8 - Incremental Software Installation
# This script installs all dependencies needed by biomni tool modules.
# Run this AFTER: conda env create -f environment.yml

set -e  # Exit on error

echo "=== Biomni v0.0.8: Installing core dependencies ==="
# Core dependencies missing from environment.yml but required by biomni
pip install anthropic beautifulsoup4 python-dotenv pyyaml scikit-learn
pip install nest-asyncio markdown accelerate
pip install gget gseapy

echo "=== Biomni v0.0.8: Installing bioinformatics packages ==="
# Bioinformatics packages (normally from bio_env.yml / setup.sh Step 3)
pip install biopython biopandas biotite
pip install scanpy anndata scvi-tools harmony-pytorch
pip install opencv-python scikit-image
pip install pybiomart pybedtools pyliftover
pip install rdkit pymed arxiv scholarly
pip install PyPDF2 reportlab
pip install cooler trackpy pykalman
pip install cellpose flowcytometrytools
pip install python-libsbml cobra
pip install python-louvain msprime
pip install SimpleITK

echo "=== Biomni v0.0.8: Installing new v008 packages ==="
pip install transformers sentencepiece langchain-google-genai langchain_ollama mcp
pip install lazyslide
pip install "git+https://github.com/YosefLab/popV.git@refs/pull/100/head"
pip install uv
#brew install git-lfs  # Run manually: brew install git-lfs && git lfs install
pip install fair-esm
uv pip install transcriptformer
pip install "zarr>=2.0,<3.0"  # resolves transcriptformer download issues
uv tool install arc-state
pip install nnunet nibabel nilearn
pip install mi-googlesearch-python
pip install git+https://github.com/pylabrobot/pylabrobot.git
conda install -y -c conda-forge weasyprint

echo "=== Biomni v0.0.8: Fixing torch ecosystem version compatibility ==="
# Upstream packages may pull in mismatched torch/torchvision versions
pip install torch==2.5.1 torchvision==0.20.1 torchaudio==2.5.1

echo "=== Biomni v0.0.8: Installation complete! ==="
