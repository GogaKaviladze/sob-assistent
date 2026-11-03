#!/usr/bin/env bash
# Einmal-Setup nach `git clone` auf Apple Silicon (siehe SETUP.md).
# Idempotent: mehrfaches Ausführen ist gefahrlos.
#
#   ./scripts/setup.sh
#
# Baut eine projektlokale .venv mit Python 3.12 (via mise) und installiert die
# Abhängigkeiten mit uv. llama-cpp-python kommt auf Apple Silicon als Wheel mit
# eingebautem Metal-Backend — kein manueller CMAKE-Build nötig.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v mise >/dev/null || { echo "✗ mise fehlt — siehe SETUP.md, Schritt 1."; exit 1; }
command -v uv   >/dev/null || { echo "✗ uv fehlt — siehe SETUP.md, Schritt 1.";   exit 1; }

echo "→ Python-Version laut mise.toml einrichten"
mise install

echo "→ projektlokale .venv anlegen (Python 3.12)"
uv venv --python "$(mise which python)" .venv

echo "→ Abhängigkeiten installieren"
uv pip install --python .venv/bin/python -r requirements.txt

echo "✓ Setup fertig. Modell laden: ./scripts/download-model.sh"
echo "  Smoke-Test (CPU/GPU, ohne Modell):"
echo "    .venv/bin/python -m sob_generator 'templates/Vivendi/Vivendi CSV Vorlage.csv' --stub"
