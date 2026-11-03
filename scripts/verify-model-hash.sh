#!/usr/bin/env bash
# Verifiziert den SHA-256-Hash eines lokalen GGUF-Modells (Issue #116).
#
# Erwartungswert-Priorität:
#   1. Erstes CLI-Argument oder Umgebungsvariable SOB_MODEL_PATH für den Modellpfad
#   2. Erwarteter Hash aus SOB_MODEL_SHA256 oder Sidecar-Datei <modell>.sha256
#
# Nutzung:
#   ./scripts/verify-model-hash.sh [pfad/zum/modell.gguf]
#   SOB_MODEL_SHA256=<hash> ./scripts/verify-model-hash.sh pfad/zum/modell.gguf
set -euo pipefail

MODEL_PATH="${1:-${SOB_MODEL_PATH:-}}"

if [ -z "$MODEL_PATH" ]; then
  MODEL_PATH=$(find models -maxdepth 1 -name "*.gguf" 2>/dev/null | head -n 1 || true)
fi

if [ -z "$MODEL_PATH" ] || [ ! -f "$MODEL_PATH" ]; then
  echo "❌ Modelldatei nicht gefunden: '${MODEL_PATH:-<kein Pfad angegeben>}'" >&2
  exit 1
fi

EXPECTED_HASH="${SOB_MODEL_SHA256:-}"
SIDECAR_FILE="${MODEL_PATH}.sha256"

if [ -z "$EXPECTED_HASH" ] && [ -f "$SIDECAR_FILE" ]; then
  EXPECTED_HASH=$(awk '{print $1}' "$SIDECAR_FILE" | tr -d '\r\n')
fi

if [ -z "$EXPECTED_HASH" ]; then
  echo "❌ Kein SHA-256-Hash für Modell '$MODEL_PATH' konfiguriert." >&2
  echo "Start/Prüfung abgebrochen: Integrität kann ohne Prüfsumme nicht gewährleistet werden." >&2
  echo "Bitte SOB_MODEL_SHA256 setzen oder Sidecar-Datei '$SIDECAR_FILE' mit Hex-Digest bereitstellen." >&2
  echo "Hash erzeugen mit: shasum -a 256 \"$MODEL_PATH\" (bzw. sha256sum)" >&2
  exit 1
fi

if command -v sha256sum >/dev/null 2>&1; then
  ACTUAL_HASH=$(sha256sum "$MODEL_PATH" | awk '{print $1}')
elif command -v shasum >/dev/null 2>&1; then
  ACTUAL_HASH=$(shasum -a 256 "$MODEL_PATH" | awk '{print $1}')
else
  ACTUAL_HASH=$(python3 -c "import hashlib; print(hashlib.sha256(open('$MODEL_PATH', 'rb').read()).hexdigest())")
fi

EXPECTED_LOWER=$(echo "$EXPECTED_HASH" | tr '[:upper:]' '[:lower:]')
ACTUAL_LOWER=$(echo "$ACTUAL_HASH" | tr '[:upper:]' '[:lower:]')

if [ "$EXPECTED_LOWER" != "$ACTUAL_LOWER" ]; then
  echo "❌ Modell-Hash-Abweichung für '$MODEL_PATH'!" >&2
  echo "   Erwartet:  $EXPECTED_LOWER" >&2
  echo "   Berechnet: $ACTUAL_LOWER" >&2
  echo "Integrität der Modellgewichte nicht gewährleistet!" >&2
  exit 1
fi

echo "✓ Modell-Integrität bestätigt ($MODEL_PATH): $ACTUAL_LOWER"
exit 0
