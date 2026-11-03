# SETUP — sob-generator auf einem neuen Mac (Apple Silicon)

Von `git clone` zum gemessenen Lauf. Saubere, projektlokale Installation —
nichts wird global oder mit `sudo` installiert. Grundlagen und das Warum:
siehe das Repo [`mac-setup`](https://github.com/GogaKaviladze/mac-setup).

---

## 0. Voraussetzung

Apple Silicon (M-Serie). Der Performance-Hebel dieses Projekts ist der
**GPU-Offload über Apple Metal** — er ist seit diesem Stand der Default
(`--n-gpu-layers -1`). Auf dem alten Intel-iMac dauerte ein Lauf ~57 Min
(CPU-only); auf dem M5 ist das Ziel ~3 Min.

---

## 1. Werkzeuge (einmal pro Mac)

```bash
xcode-select --install        # Compiler-Toolchain (Pflicht für native Wheels)
brew install mise uv gh       # Versionsmanager + Python-Paketmanager + GitHub CLI
echo 'eval "$(mise activate zsh)"' >> ~/.zshrc && exec zsh
gh auth login                 # Repo ist privat — Autorisierung einmalig pro Mac
```

`mise` pinnt die Python-Version pro Projekt (`mise.toml`), `uv` baut die
projektlokale `.venv`. Keine globale Python-Installation, kein `sudo`.

`gh auth login` führt interaktiv durch den Browser-OAuth-Flow und hinterlegt
die Anmeldedaten lokal (Keyring) — danach funktionieren `gh repo clone` und
`git push`/`pull` ohne weitere Passwort- oder SSH-Key-Einrichtung.

---

## 2. Projekt holen und einrichten

```bash
mkdir -p ~/Dev && cd ~/Dev
gh repo clone GogaKaviladze/sob-generator
cd sob-generator

./scripts/setup.sh            # .venv (Python 3.12) + Abhängigkeiten
```

`setup.sh` ist idempotent. `llama-cpp-python` kommt auf Apple Silicon als
Wheel mit eingebautem Metal-Backend — kein manueller CMAKE-Build nötig.

---

## 3. Smoke-Test ohne Modell (Sekunden)

Prüft, dass Import → Rendering → docx durchläuft, bevor 4,9 GB Modell geladen
werden. Nutzt die mitgelieferte Beispiel-CSV und den Stub-Generator:

```bash
.venv/bin/python -m sob_generator \
  "templates/Vivendi/Vivendi CSV Vorlage.csv" --stub
```

Erwartung: `SOB erstellt: output/Fall_<Datum>_SOB.docx` (oder mit `--pseudonym`: `output/<Pseudonym>_<Datum>_SOB.docx`).

---

## 4. Modell laden (einmal, ~9,0 GB)

```bash
./scripts/download-model.sh   # → models/Qwen2.5-14B-Instruct-Q4_K_M.gguf (Pilot-Standardmodell)
```

Liegt unter `models/` (gitignored, nie committen — ADR-2/ADR-8). Bei sehr
großen Modellen kann das Ziel auch auf der externen SSD liegen; dann den Pfad
unten beim `--model` entsprechend angeben.

**Modell-Integrität & Offline-Sicherheit (Issue #116):**
Die Anwendung verifiziert vor dem Laden der Gewichte zwingend den SHA-256-Hash.
Hash erzeugen und als Sidecar-Datei ablegen (oder `SOB_MODEL_SHA256` setzen):
```bash
shasum -a 256 models/Qwen2.5-14B-Instruct-Q4_K_M.gguf > models/Qwen2.5-14B-Instruct-Q4_K_M.gguf.sha256
./scripts/verify-model-hash.sh models/Qwen2.5-14B-Instruct-Q4_K_M.gguf
```
Ohne konfigurierten Hash oder bei Abweichung bricht das Laden sofort mit ❌ ab. Details: [`docs/DATENSCHUTZ.md`](docs/DATENSCHUTZ.md#9-offline-nachweis-dsb-dokumentation).

---

## 5. Echter Lauf mit Zeitmessung

**Wichtig — Eingabedaten:** Ein vollständiger Bericht (wie
`output/Fall_2026-06-16_SOB.docx` bzw. `output/Fall-123_2026-06-16_SOB.docx`) entsteht nur aus einem **echten,
mehrzeiligen** Vivendi-Export. Diese Daten sind klientennah und werden **nie
committet** (`dummy/` ist gitignored). Beim Maschinenwechsel deshalb manuell
herüberkopieren — externe SSD/USB/Passwortmanager, **nicht** über git.

Die committete `templates/Vivendi/Vivendi CSV Vorlage.csv` hat nur **einen**
Eintrag und dient bloß dem Smoke-Test (Schritt 3) — daraus käme ein dünner
Bericht, kein voller.

```bash
.venv/bin/python -m sob_generator \
  "dummy/Max_Mustermann_Vivendi_Export.csv" \
  --model models/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf \
  --pseudonym "Fall-123"
```

Ergebnis: `output/Fall-123_<Datum>_SOB.docx` (Standard ohne Flag: `output/Fall_<Datum>_SOB.docx`) —
gleiche Vorlage, gleicher Renderer; mit echtem Modell auch inhaltlich gefüllt.
Ausgabedateien enthalten standardmäßig weder Vorname noch Nachname (Datenschutz/DSGVO, Issue #115).
Speicherpfad und Pseudonym lassen sich über Flags (`--output`, `--pseudonym`)
oder Umgebungsvariablen (`SOB_OUTPUT_DIR`, `SOB_EXPORT_PSEUDONYM`) steuern.
Am Ende steht `Laufzeit: …s (… min)` — das ist die Messung.

**Vergleichsmessung CPU vs. GPU** (zeigt den Metal-Effekt):

```bash
# GPU (Default, alle Layer auf Metal)
... "dummy/Max_Mustermann_Vivendi_Export.csv" --model models/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf

# CPU-only zum Vergleich
... "dummy/Max_Mustermann_Vivendi_Export.csv" --model models/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf --n-gpu-layers 0
```

---

## 6. Tests

```bash
.venv/bin/python -m pytest -q
# Gezielter Offline- und DSGVO-Nachweistest (Issue #116):
.venv/bin/python -m pytest tests/test_offline_runtime.py -q
```

Läuft ohne Modell (Stub-Generator) und ist Teil jedes Commits
(`mac-setup`-Disziplin: erst grün, dann committen).

### Modell-Abnahmevergleich: Llama-3.1-8B vs. phi-4 (#37)

Der quantisierte 8B zeigt im echten Lauf eine Grounding-/Grammatik-Schwäche
(#37). phi-4 (14B, MIT) ist der Abnahme-Kandidat. So wird der Wechsel an
denselben synthetischen Fixtures objektiv vergleichbar — der Prompt geht als
reiner Text in beide Modelle, der Wechsel ist ein purer `--model`-Pfadwechsel.

```bash
# 1. phi-4 Q4_K_M laden (~9,1 GB, einmal; liegt gitignored unter models/)
MODEL=phi4 ./scripts/download-model.sh

# 2. Beide Modelle über die Fixture laufen lassen — mit Baseline-Gate (#38)
.venv/bin/python scripts/model_acceptance.py dummy/Max_Mustermann_Vivendi_Export.csv \
  --model models/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf
.venv/bin/python scripts/model_acceptance.py dummy/Max_Mustermann_Vivendi_Export.csv \
  --model models/phi-4-Q4_K_M.gguf
```

Der Report wertet die **maschinell entscheidbaren** Kriterien automatisch und
vergleicht sie gegen `scripts/acceptance_baseline.json` (#38-Gate):

| Kriterium | Baseline-Limit |
|---|---|
| `max_shared_opener` (Opener-Kollaps) | ≤ 2 |
| `bullet_sections` (Listen im Prosa-Feld) | 0 |
| `placeholder_leaks` (Few-Shot-Platzhalter im Bericht) | 0 |
| `genitive_apostrophes` (falscher dt. Genitiv) | 0 |
| `bare_surname_hits` | nach erstem Weg-C-Lauf kalibrieren |

Exit-Code 1 bei Regression (CI-/Gate-tauglich). `--no-baseline` deaktiviert
den Vergleich (nur Report, kein Exit-Code 1). Nach dem ersten echten Weg-C-Lauf
den `bare_surname_hits`-Schwellwert in `scripts/acceptance_baseline.json`
kalibrieren (Kommentar entfernen, Wert setzen).

Die **semantischen** Kriterien — Themen-Verschmierung zwischen Abschnitten und
Grammatik (#37) — bleiben bewusst manuell; dafür druckt der Report je Abschnitt
Opener und ersten Satz. DSGVO: nur die synthetische Mustermann-Fixture verwenden,
nie echte Klientendaten.

### Datenschutz & In-Memory-Verarbeitung (DSGVO Art. 25)

In `chat_app` werden Sitzungsdaten und hochgeladene Dateien (CSV und DOCX) nach dem Prinzip der Datenminimierung flüchtig im Arbeitsspeicher des laufenden Python-Prozesses verarbeitet. Die Anwendung legt in ihrer Parsing-, Verarbeitungs- und Exportpipeline zu keinem Zeitpunkt Zwischendateien auf der Festplatte an (anwendungsseitiger Zero-Disk-Footprint via `io.BytesIO`). Beim vorgelagerten HTTP-Multipart-Empfang puffert FastAPI/Starlette Uploads standardmäßig im Speicher (`SpooledTemporaryFile`).

Die Daten verbleiben bis zum Zurücksetzen über den UI-Button **„Neuer Fall“** (`/api/reset`) bzw. die selektiven Löschaktionen („Chat leeren“, „Entfernen“) oder bis zum Beenden des Server-Prozesses im Terminal (`Ctrl+C` bzw. Schließen des Terminal-Fensters) im RAM. Das bloße Schließen des Browser-Tabs beendet den lokalen Server nicht.

Auch die Diagnose-Logs (`logs/chat_app.log`) protokollieren nach DSGVO Art. 25 strikt anonymisiert: Weder Klientennamen, noch benutzerkontrollierte Dateinamen oder sensitive Falldaten gelangen in die Protokolle (nur aggregierte Metadaten wie Zeilenzahlen oder Inferenzgeschwindigkeiten).


---

## 7. Web-App starten (`chat_app`)

Die Fachdialog & Reflexion Chat-App läuft als lokale FastAPI-Webanwendung auf `http://127.0.0.1:8000`.

**Schnellstart (macOS):**
Einfach die Datei `start.command` im Finder doppelklicken oder im Terminal ausführen:

```bash
./start.command
```

**Manueller Start im Terminal:**

```bash
.venv/bin/python -m chat_app
# → Öffnet automatisch http://127.0.0.1:8000 im Standardbrowser
```

Die Anwendung sucht automatisch nach vorhandenen GGUF-Modellen unter `models/` und bindet diese ein.
Alles läuft vollständig offline und lokal auf `127.0.0.1` (DSGVO Art. 25, ADR-2).


---

## Wichtige Flags

| Flag | Default | Zweck |
|------|---------|-------|
| `--model <pfad.gguf>` | – | echtes LLM statt Stub |
| `--n-gpu-layers` | `-1` | `-1` = alle Layer auf Metal-GPU, `0` = CPU-only |
| `--stub` | aus | Lauf ohne Modell (Smoke-Test/CI) |
| `--alt-sob <pfad.docx>` | – | vorherige Ziele als Kontext übernehmen |
| `--threads` | `4` | CPU-Threads |
| `--n-ctx` | `8192` | Kontextfenster |
