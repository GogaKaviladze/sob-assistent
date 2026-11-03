# CLAUDE.md — Agent-Einstieg

Lokaler, DSGVO-konformer Generator für den Hamburger Sozialbericht (SOB):
Vivendi-PD-CSV → lokales LLM → ausgefüllte `Vorlage.docx`. Offline, Human-in-the-Loop.

**Diese Datei dupliziert nichts** — sie verweist und ergänzt nur das, was sonst nirgends steht.

| Du suchst… | Lies |
|---|---|
| Struktur, Pipeline, Tech-Stack | `README.md` |
| Alle Befehle, Flags, Onboarding | `SETUP.md` |
| Architektur-*Begründungen* (ADR-1..11, das *Warum*) | `docs/ARCHITECTURE.md` |
| DSGVO-Datenfluss & Löschkonzept | `docs/DATENSCHUTZ.md` |
| docx-Feld ↔ Modellpfad | `docs/field-mapping.md` |

## Bevor du Code anfasst — harte Invarianten

- **Anti-Corruption-Layer.** Vivendi-CSV-Wissen lebt **nur** in `sob_generator/importer.py`.
  Der Kern (`models.py`, `orchestrator.py`, `renderer.py`, `sob_generator/llm/*`) arbeitet mit
  kanonischen Pydantic-Modellen. Eine neue Datenquelle = ein neuer Importer hinter dem
  Protokoll, **kein Refactor am Kern**.
- **DSGVO — keine echten Daten.** Niemals echte Klientendaten committen. `dummy/`, `models/`
  und `output/` sind gitignored. Tests/Fixtures sind **synthetisch-fachlich** (Mustermann),
  nie echt. Details: `docs/DATENSCHUTZ.md`.
- **LLM-Output ist grammatik-erzwungen.** GBNF wird aus dem Pydantic-Schema erzeugt.
  Änderungen am Output-Vertrag betreffen `sob_generator/llm/schemas.py` **und** `sob_generator/llm/grammar.py`
  gemeinsam — danach `tests/test_llm_output_contract.py` grün halten.
- **SOB-Stil-Hebel.** Bericht-Felder dürfen **nicht** schablonenhaft gleich anfangen
  (harter Opener-Guard, `sob_generator/opener_diversity.py`); Maßnahmen-Felder **dürfen**
  gleich anfangen, nur der Inhalt variiert. Der Kollaps-Hebel ist `temperature` (0.7).

## Issue lösen — Workflow

1. Issue-Text holen: `gh issue view <N>` (gh ist installiert + autorisiert; Labels `prio-1/2/3`).
2. Branch `feature_<kurz>` von `main`. Commit-Schema: `feat(#NN): …` bzw. `fix(#NN): …`.
3. Mehrere Issues integrieren: je Issue ein Branch von `main`, ein **kurzlebiger**
   Integrationsbranch nur zum gemeinsamen Testen — danach **beide löschen** (kein Dauer-Stage).
4. Vor jedem Commit: `python -m pytest -q` grün (läuft modellfrei über den Stub-Generator).

## Aufgabe → wo anfangen

| Aufgabe betrifft… | Start-Dateien |
|---|---|
| CSV-Import / neue Datenquelle | `sob_generator/importer.py`, `sob_generator/models.py` |
| LLM-Prompt / Few-Shots / Grounding | `sob_generator/llm/prompts.py`, `sob_generator/llm/sections.py` |
| LLM-Output-Schema / Grammatik | `sob_generator/llm/schemas.py`, `sob_generator/llm/grammar.py`, `sob_generator/llm/generator.py` |
| docx-Felder / Rendering | `sob_generator/renderer.py`, `sob_generator/docx_fields.py`, `docs/field-mapping.md`, `templates/hamburg/Vorlage.docx` |
| Pipeline / CLI | `sob_generator/orchestrator.py`, `sob_generator/__main__.py` |
| Chat-App / Web-UI / Streaming | `chat_app/app.py`, `chat_app/chatbot.py`, `chat_app/templates/` |
| Stil / Anti-Schablone | `sob_generator/opener_diversity.py`, `sob_generator/draft_diagnostics.py` |
| Modell-Abnahme / -Vergleich | `scripts/model_acceptance.py`, `sob_generator/draft_diagnostics.py` |

## Schnelle Verifikation

```bash
# Smoke-Test (Sekunden, ohne Modell)
python -m sob_generator "templates/Vivendi/Vivendi CSV Vorlage.csv" --stub
# Tests
python -m pytest -q
```

Vollständige Befehle, Flags und der echte LLM-Lauf: `SETUP.md`.
