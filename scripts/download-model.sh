#!/usr/bin/env bash
# Lädt ein lokales GGUF-Sprachmodell nach models/ (gitignored).
#
# Profile (MODEL=…), Q4_K_M von bartowski:
#   qwen14b  Qwen2.5-14B-Instruct        (~9,0 GB) — Default, Pilot-Standardmodell
#   llama    Meta-Llama-3.1-8B-Instruct  (~4,9 GB) — Alternatives Modell
#   phi4     microsoft/phi-4 14B         (~9,1 GB) — Abnahme-Kandidat (#37)
#
#   ./scripts/download-model.sh               # qwen14b (Default)
#   MODEL=llama ./scripts/download-model.sh   # Llama 8B
#   MODEL=phi4 ./scripts/download-model.sh    # phi-4 für den Abnahmevergleich
#
# Feinsteuerung über MODEL_FILE / MODEL_URL überschreibt das Profil.
# Idempotent: existiert die Datei bereits, wird nichts neu geladen.
set -euo pipefail
cd "$(dirname "$0")/.."

case "${MODEL:-qwen14b}" in
  llama)
    DEFAULT_FILE="Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
    DEFAULT_REPO="bartowski/Meta-Llama-3.1-8B-Instruct-GGUF"
    ;;
  phi4)
    DEFAULT_FILE="phi-4-Q4_K_M.gguf"
    DEFAULT_REPO="bartowski/phi-4-GGUF"
    ;;
  qwen14b)
    DEFAULT_FILE="Qwen2.5-14B-Instruct-Q4_K_M.gguf"
    DEFAULT_REPO="bartowski/Qwen2.5-14B-Instruct-GGUF"
    ;;
  *)
    echo "✗ Unbekanntes MODEL='${MODEL}'. Erlaubt: llama, phi4, qwen14b." >&2
    exit 1
    ;;
esac

MODEL_FILE="${MODEL_FILE:-$DEFAULT_FILE}"
MODEL_URL="${MODEL_URL:-https://huggingface.co/${DEFAULT_REPO}/resolve/main/${MODEL_FILE}}"
TARGET="models/${MODEL_FILE}"

verify_if_configured() {
  if [ -n "${SOB_MODEL_SHA256:-}" ] || [ -f "${TARGET}.sha256" ]; then
    echo "→ Prüfe Modell-Integrität..."
    ./scripts/verify-model-hash.sh "$TARGET"
  else
    echo "ℹ Kein SHA-256-Hash für $TARGET hinterlegt (Sidecar ${TARGET}.sha256 oder SOB_MODEL_SHA256)."
    echo "  Tipp: Hash erzeugen mit 'shasum -a 256 $TARGET' und als Sidecar-Datei eintragen."
  fi
}

mkdir -p models
if [ -f "$TARGET" ]; then
  echo "✓ Modell schon vorhanden: $TARGET"
  verify_if_configured
  exit 0
fi

echo "→ Lade $MODEL_FILE nach $TARGET"
# -L folgt Redirects, -C - setzt abgebrochene Downloads fort.
curl -L -C - -o "$TARGET" "$MODEL_URL"
echo "✓ Fertig: $TARGET"
verify_if_configured
