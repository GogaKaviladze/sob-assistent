#!/bin/bash
# Doppelklickbarer Starter für die Fachdialog & Reflexion Chat-App (macOS).
# Im Finder doppelklicken oder im Terminal via ./start.command ausführen.

cd "$(dirname "$0")" || exit 1

if [ ! -f ".venv/bin/python" ]; then
    echo "❌ Virtuelle Umgebung (.venv) nicht gefunden!"
    echo "Bitte stelle sicher, dass .venv eingerichtet ist."
    read -p "Drücke Enter zum Beenden..."
    exit 1
fi

echo "======================================================="
echo "🚀 Fachdialog & Reflexion – Chatbot"
echo "======================================================="
echo "Starte Web-Server..."

# Defense in Depth (Issue #116): HF-/Transformers-Hub zur Laufzeit offline halten
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

.venv/bin/python -m chat_app
if [ $? -ne 0 ]; then
    read -p "Drücke Enter zum Beenden..."
    exit 1
fi
