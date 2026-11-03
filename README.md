# sob-generator

**DSGVO-konformer Sozialberichtsgenerator**

Liest den Vivendi-PD-Export einer Eingliederungshilfe-Klientin und unterstützt bei der
dialogischen Reflexion und Formulierung des Hamburger Sozialberichts (SOB) — **lokal, ohne Cloud**.
Die Fachkraft prüft, steuert und übernimmt die Formulierungen.

> **Status:** Lauffähig — moderner partnerschaftlicher Fachdialog & Reflexionsassistent
> als lokale FastAPI-Webanwendung (`chat_app`): CSV- und Vorbericht-Upload, dialogische
> Reflexion auf Augenhöhe, tokenweises LLM-Streaming und strukturierter Export in die Zwischenablage.
>
> **Loslegen:** Installation und Start Schritt für Schritt in [`SETUP.md`](SETUP.md)
> oder direkt per Doppelklick auf `start.command`.

---

## Ziel

- **DSGVO Art. 25 — Privacy by Design:** Kein Byte verlässt die lokale Maschine.
  Das LLM läuft eingebettet und offline (ADR-2). Ausgabedateien werden standardmäßig
  pseudonym benannt (`Fall_{YYYY-MM-DD}_SOB.docx`, Issue #115). Datenfluss, Löschkonzept und
  kryptografischer Offline-Nachweis (Modell-Hash, deaktivierte Docs, DSB-Dokumentation):
  [`docs/DATENSCHUTZ.md`](docs/DATENSCHUTZ.md). Testbefehl: `pytest tests/test_offline_runtime.py -q`.
- **Keine Installation für die Fachkraft:** Einfacher Start via `start.command`
  oder `python -m chat_app` auf `http://127.0.0.1:8000`.
- **Fokussierter Fachdialog:** Zielgerichtete Erarbeitung von Zielerreichung, Entwicklungen
  und neuen Teilhabezielen auf Augenhöhe.
- **Mensch entscheidet:** Partnerschaftlicher Dialog — die Fachkraft
  steuert und validiert die Formulierungen vor dem Export (Human-in-the-Loop).

---

## Wie es funktioniert

```
[A] Vorbericht / Plan (.docx/.pdf) ──► UniversalPlanImporter ──► Bisherige Ziele & Kontext
[B] Vivendi PD CSV-Export          ──► VivendiPDImporter     ──► Klient + Verlaufsdaten
                                                                      │
                                                           ReflectionChatbot / LlamaService
                                                                (lokal, offline)
                                                                      │
                                                       ── Partnerschaftlicher Fachdialog ──
                                                                      │
                                                           Strukturierter Berichtsentwurf
                                                                      │
                                                        Export in Zwischenablage (Markdown)
```

Die Fachkraft bedient den Assistenten über die moderne **Web-Oberfläche (`chat_app`)**:

1. CSV-Verlauf und optionalen Vorbericht/Gesamtplan hochladen
2. Modell auswählen (lokale GGUF-Modelle unter `models/`, Standard: Qwen2.5-14B-Instruct)
3. **Reflexionsdialog führen** → Das Modell assistiert mit Impulsen, Fragen und Entwürfen
4. Entwurf prüfen, anpassen und freigeben
5. **Exportieren** → Strukturierter Berichtsentwurf per Klick in die Zwischenablage kopieren


---

## Projektstruktur

```
sob-generator/
├── chat_app/               # Moderne Webanwendung: FastAPI + Streaming-Chat UI
│   ├── app.py              #   FastAPI-Routen: Upload, Chat-Stream, Model-Select, Export
│   ├── chatbot.py          #   ReflectionChatbot, LLM-Provider & ReflectionEngine
│   ├── csv_loader.py       #   CSV-Parser & Vivendi-Kontextaufbereitung
│   ├── plan_loader.py      #   Plan-Parser für Vorberichte & Gesamtpläne
│   ├── logger.py           #   Diagnose- und Session-Logging
│   ├── __main__.py         #   CLI-Starter (python -m chat_app)
│   └── templates/          #   Moderne Single-Page-Chat-Oberfläche (HTML/JS/CSS)
├── sob_generator/          # Fachkern + Domain-Services
│   ├── chat_engine.py      #   ChatDialogueEngine (Domain Service & async Offloading)
│   ├── chat_session.py     #   ChatSession & WorkingMemory State Machine
│   ├── chat_store.py       #   SessionStore & ChatSessionStore mit TTL-Ablauf
│   ├── orchestrator.py     #   Import → Generieren → Render zusammengeführt
│   ├── importer.py         #   VivendiPDImporter (CSV)
│   ├── previous_sob.py     #   OldReportImporter (alter .docx-SOB → Ziele)
│   ├── models.py           #   Pydantic-Datenmodelle (ReportDraft)
│   ├── renderer.py         #   DocxRenderer (python-docx + docx_fields)
│   ├── docx_fields.py      #   Bookmark-Zugriff auf FORMTEXT/FORMCHECKBOX
│   ├── report_template.py  #   ReportTemplate-Registry
│   ├── draft_generator.py  #   DraftGenerator-Protokoll
│   ├── stub_generator.py   #   StubDraftGenerator (modellfrei, Tests/Smoke)
│   └── llm/                # GBNF-Grammar, Prompts, LlamaService
├── start.command           # Doppelklickbarer Starter für macOS
├── templates/
│   ├── hamburg/Vorlage.docx          # offizielles Fachamt-EGH-Formular
│   └── Vivendi/Vivendi CSV Vorlage.csv
├── scripts/                # setup.sh, download-model.sh
├── tests/                  # pytest: Domain, Chat-Engine, Importer, Chat-App, …
└── docs/
    ├── ARCHITECTURE.md     # Architektur-Entscheidungen (ADR)
    ├── DATENSCHUTZ.md      # Datenfluss & Löschkonzept (DSGVO-Prüfung)
    └── field-mapping.md    # docx-Feld ↔ Modellpfad
```

---

## Tech-Stack

| Schicht        | Wahl                                                 |
|----------------|------------------------------------------------------|
| Ausgabeformat  | Strukturierter Markdown-Bericht / Zwischenablage      |
| LLM            | eingebettetes llama.cpp, GGUF, Metal-GPU-Beschleunigung |
| Datenmodell    | Pydantic, typisiert                                  |
| Feld-Mapping   | Pydantic Schema-Mapping / Lebensbereiche              |
| LLM-Output     | Streaming SSE / Dialog-Reflexion                     |
| Oberfläche     | FastAPI Web-UI (`chat_app`, Port 8000)               |

Bewusst **nicht** gebaut: Cloud-LLM, PDF als Primärausgabe, Multi-Klient pro
Datei, automatische Einreichung (siehe ARCHITECTURE.md).

---

## Vorlagen

| Region  | Variante                 | Status        |
|---------|--------------------------|---------------|
| Hamburg | Fachamt EGH (SOB)        | ✅ vorhanden  |

---

## Lizenz

Intern — proprietär. Keine Weitergabe ohne schriftliche Genehmigung.
