# Datenschutz — Datenfluss & Löschkonzept

**sob-generator** — Partnerschaftlicher Reflexionsassistent für Sozialberichte (Eingliederungshilfe).

Dieses Dokument beschreibt, **welche personenbezogenen Daten** verarbeitet werden,
**wo sie zu welchem Zeitpunkt liegen** und **wann und wie sie gelöscht werden**.
Es ist als zitierbare Grundlage für eine Datenschutz-Prüfung gedacht
(Speicherbegrenzung nach Art. 5 Abs. 1 lit. e DSGVO, Löschkonzept; ergänzend zum
Verarbeitungsverzeichnis nach Art. 30).

Die Architektur-Begründungen stehen in [`ARCHITECTURE.md`](ARCHITECTURE.md)
(ADR-2: nur lokales LLM; ADR-9: lokale Chat-Webanwendung). Dieses Dokument
konkretisiert daraus den **Datenlebenszyklus** im Rahmen der Pivot-Konsolidierung (Issue #97).

---

## 1. Geltungsbereich

Beschrieben wird das **ausgelieferte Produkt**: die Fachdialog & Reflexion Chat-App
(`chat_app`), die als lokale Webanwendung auf `http://127.0.0.1:8000` läuft und über
`start.command` oder `python -m chat_app` gestartet wird.

Der optionale reine Kommandozeilen-Pfad (`python -m sob_generator`) ist ein reines
Entwickler-/Testwerkzeug für synthetische Smoke-Tests und in Abschnitt 7 gesondert benannt.

---

## 2. Verarbeitete personenbezogene Daten

Die Berichte enthalten **besondere Kategorien personenbezogener Daten nach
Art. 9 DSGVO** (Sozial- und Gesundheitsdaten). Genau deshalb verarbeitet die App
ausschließlich lokal (Abschnitt 3).

| Kategorie | Beispiele | Quelle |
|---|---|---|
| Stammdaten der Klient:in | Vorname, Name, Geburtsdatum | Vivendi-CSV-Export |
| Leistungsmetadaten | Leistungszeitraum, Leistungsanbieter | Vivendi-CSV-Export |
| Verlaufsdokumentation (**Art. 9**) | Dokumentationseinträge, Verlaufsnotizen | Vivendi-CSV-Export |
| Vorbericht / Gesamtplan | Bisherige Ziele, Entwicklungen, Zielstände | hochgeladene `.docx`, `.txt` oder `.md` |
| Freitext-Eingaben | Reflexionsnotizen, Anweisungen der Fachkraft | Chat-Eingabezeile (UI) |
| Berichtsentwurf (**Art. 9**) | Dialogisch generierte Formulierungen je Lebensbereich | LLM-Inferenz (lokal) |

---

## 3. Grundprinzip: vollständig lokale Verarbeitung (Art. 25)

Privacy by Design / by Default — **kein Byte verlässt die Maschine**:

- Das LLM ist eingebettet (llama.cpp) und läuft offline (ADR-2).
- Der Server bindet ausschließlich an `127.0.0.1` (ADR-9).
- Alle Frontend-Assets (HTML, CSS, JS) werden lokal ausgeliefert —
  **kein CDN, kein externer Request** (ADR-2).
- Kein Cloud-Dienst, keine Telemetrie, keine Analyse-Aufrufe.

---

## 4. Datenfluss & Speicherorte

Das Speicherkonzept von `chat_app` folgt in der gesamten Verarbeitungs- und Generierungspipeline
dem Prinzip der **In-Memory-Verarbeitung** (flüchtiger Arbeitsspeicher des laufenden Prozesses).
Die Applikation legt zu keinem Zeitpunkt eigene temporäre Arbeitsdateien auf der Festplatte an
(DOCX- und CSV-Parsing, Prompt-Synthese und Export laufen rein speicherbasiert via `io.BytesIO`).
Beim vorgelagerten HTTP-Multipart-Empfang puffert das Web-Framework (FastAPI/Starlette) Uploads
vollständig im Speicher, bevor die Bytes direkt im RAM von der Anwendung eingelesen werden.
Um ein Überlaufen (Spill) auf die Festplatte garantiert auszuschließen, erzwingt eine vorgelagerte ASGI-Middleware
ein hartes Größenlimit (konfigurierbar über `SOB_MAX_UPLOAD_BYTES`, Standard: 10 MB, Maximalgrenze: 50 MB;
Überschreitungen werden sofort mit HTTP 413 abgewiesen) und synchronisiert die Spool-Schwelle (`spool_max_size >= Limit`),
sodass alle Upload-Daten nachweislich ausnahmslos im flüchtigen RAM verbleiben.

| Datum | Speicherort | Entsteht | Wird gelöscht | Codestelle |
|---|---|---|---|---|
| Uploads (CSV, Vorbericht) | In-Memory-Stream (`io.BytesIO`) | beim Upload (`/api/upload`, `/api/upload-plan`) | In-Memory-Parsing; keine anwendungsseitige Plattenablage | `chat_app/app.py`, `chat_app/csv_loader.py`, `chat_app/plan_loader.py` |
| Fachkontext & Verlaufsdaten | **nur RAM**, `ReflectionChatbot`-Instanz | nach Upload im Prozessspeicher | bei Reset (`/api/reset`) oder Prozessende | `chat_app/chatbot.py` |
| Dialogverlauf & Entwürfe | **nur RAM**, `ReflectionChatbot.messages` | während des Dialogs | bei Reset (`/api/reset`) oder Prozessende | `chat_app/chatbot.py` |
| Export-Bericht (Markdown) | **nur RAM**, JSON-Payload (`/api/export`) | beim Export-Klick | wird direkt in Zwischenablage kopiert; keine Dateiablage | `chat_app/app.py::export_chat` |
| Export-Bericht (DOCX) | **nur RAM**, `io.BytesIO`-Streaming (`/api/export/docx`) | beim Klick auf Word-Export | StreamingResponse im flüchtigen RAM; keine Temp-Dateien (Zero-Disk). Neutraler Dateiname `SOB_<Vorlage>_<Datum>.docx` ohne Klientennamen; der Browser legt die Datei im Downloads-Ordner ab (Cloud-Sync wie OneDrive beachten) | `chat_app/app.py::export_chat_docx`, `chat_app/docx_exporter.py` |
| Diagnose-Logs | `logs/chat_app.log` | bei Systemereignissen | **Strikt anonymisiert** — enthält keine PII oder Dateinamen | `chat_app/logger.py` |

---

## 5. Löschmechanismen & Datenminimierung im Detail

**a) In-Memory-Verarbeitung ohne anwendungsseitigen Platten-Zwischenspeicher (Zero-Disk-Pipeline)**
Im Gegensatz zu traditionellen Batch-Tools speichert `chat_app` weder hochgeladene Dateien noch
Export-Dokumente auf der Festplatte ab. Sowohl CSV- und DOCX-Eingaben als auch der finale
Word-Bericht-Export (`/api/export/docx`) werden direkt im flüchtigen Arbeitsspeicher (`io.BytesIO`)
verarbeitet, gerendert und per Streaming ausgeliefert — ohne Nutzung von `tempfile.mkdtemp` oder
temporären Zwischendateien. Durch die Synchronisation der Spool-Schwelle von Starlette mit dem über
`SOB_MAX_UPLOAD_BYTES` gesteuerten Upload-Limit ist zudem technisch sichergestellt, dass bei
Uploads kein Rollover auf die Festplatte erfolgt (kein Spill auf Disk).

**b) Neuer Fall & Session-Reset auf Knopfdruck**
Über die UI-Funktion **„Neuer Fall“** (`/api/reset`) kann die Fachkraft mit einem Klick alle geladenen
Dokumente (CSV und Vorbericht), Verläufe und Chatnachrichten sofort vollständig aus dem Arbeitsspeicher
löschen. Alternativ können Dokumente über die jeweiligen Entfernen-Buttons („CSV entfernen“, „Vorbericht entfernen“)
und der Chatverlauf über „Chat leeren“ (`/api/reset-chat`) selektiv zurückgesetzt werden.

**c) Beendigung der Anwendung & Prozesslebenszyklus**
Das bloße Schließen des Browser-Fensters oder -Tabs beendet den lokalen Webserver nicht.
Die vollständige Freigabe des Arbeitsspeichers durch das Betriebssystem erfolgt, sobald
der Server-Prozess im Terminal beendet wird (`Ctrl+C` im Terminal bzw. Schließen des
durch `start.command` geöffneten Terminal-Fensters). Für Fallwechsel innerhalb einer laufenden
Sitzung steht in der Kopfzeile der UI-Button **„Neuer Fall“** zur Verfügung (ruft `/api/reset` auf und
leert Chat, CSV und Vorbericht in einem Schritt). Es verbleiben keine Fragmente auf dem Datenträger.

**d) Strikt anonymisiertes Logging**
Die Datei `logs/chat_app.log` dient ausschließlich technischen Diagnosen (Inferenzzeit,
Tokens/s, Modellpfad). Die Logger-Funktionen ([`log_csv_loaded`](../chat_app/logger.py), [`log_plan_loaded`](../chat_app/logger.py))
sind so gehärtet, dass weder Klientennamen, noch benutzerkontrollierte Dateinamen
oder sensible Spalteninhalte in die Logdatei gelangen.

**e) Pseudonymisierte Dateinamen & konfigurierbare Speicherpfade (Issue #115)**
Ausgabedateien des CLI-Generators verwenden standardmäßig weder Vor- noch Nachnamen im Dateinamen (`Fall_{YYYY-MM-DD}_SOB.docx`). Über die Umgebungsvariable `SOB_EXPORT_PSEUDONYM` oder CLI `--pseudonym` kann ein Pseudonym bzw. eine Fallnummer vorgegeben werden (`<Pseudonym>_{YYYY-MM-DD}_SOB.docx`). Erlaubt sind nur `A–Z a–z 0–9 _ -` (max. 64 Zeichen); ungültige Werte (Leerzeichen, Umlaute, Sonderzeichen – typischerweise Klarnamen) werden mit einem ❌-Fehler abgelehnt, nicht still bereinigt, und der Wert erscheint nicht in der Fehlermeldung. Existiert die Zieldatei bereits, wird `_2`, `_3` … angehängt – nichts wird still überschrieben. Das Ausgabeverzeichnis lässt sich über `SOB_OUTPUT_DIR` bzw. CLI `--output` steuern.

---

## 6. Verhalten bei Abbruch & Absturz

Bricht die Anwendung unvorhergesehen ab (z. B. Prozessabbruch, Stromausfall),
entsteht **kein Datenschutzrisiko**, da:
- Keine temporären Falldatendateien der Anwendung auf der Festplatte liegen, die abgeräumt werden müssten.
- Der Arbeitsspeicher durch das Betriebssystem unmittelbar freigegeben wird.
- Keine persistenten Datenbanken oder Caches mit Klientenbezug existieren.

---

## 7. Außerhalb der App-Verantwortung

Bewusst klargestellt, was die App **nicht** kontrolliert:

- **Eingabedateien:** Vivendi-CSV-Dateien oder Vorberichte auf dem Dateisystem der Fachkraft
  unterliegen den Aufbewahrungs- und Schutzmaßnahmen der jeweiligen Einrichtung.
- **Exportierte Berichte:** Aus der Zwischenablage in das offizielle Word-Formular
  oder andere Zielsysteme eingefügte Texte liegen in der Verantwortung der Fachkraft.
- **Modelldateien (`models/`):** Reine GGUF-Gewichte ohne Personenbezug.

---

## 8. Technische & organisatorische Maßnahmen (Art. 32)

- **Netzwerkgrenze:** Bindung ausschließlich an `127.0.0.1`; kein externer Netzwerkverkehr.
- **Kein Tracking:** Weder Cookies noch Analytics oder externe Schriftarten/CDNs.
- **Strikte Log-Anonymisierung:** Keine Klientennamen oder Dateinamen in Logs.
- **In-Memory-Architektur:** Keine anwendungsseitige Speicherung von Falldaten auf der Festplatte; In-Memory-Verarbeitungspipeline.
- **Pseudonymisierte Dateinamen:** Ausgabedateien enthalten standardmäßig weder Vorname noch Nachname (`Fall_{YYYY-MM-DD}_SOB.docx` bzw. konfigurierbare Pseudonyme).

---

## 9. Offline-Nachweis (DSB-Dokumentation)

Zur Vorlage bei behördlichen oder betrieblichen Datenschutzbeauftragten (DSB)
gewährleistet die Anwendung den lückenlosen Nachweis der vollständigen Netzwerktrennung
und Modell-Integrität:

1. **Kein ausgehender Netzwerkverkehr zur Laufzeit:**
   - Alle Requests verbleiben streng prozessintern (Loopback `127.0.0.1` / `::1`).
   - Automatisierter Nachweis über Socket-Blockierung: Externe Verbindungsversuche schlagen
     unmittelbar mit `RuntimeError` fehl ([`tests/test_offline_runtime.py`](../tests/test_offline_runtime.py)).
   - In `start.command` sichern `HF_HUB_OFFLINE=1` und `TRANSFORMERS_OFFLINE=1` zusätzlich ab,
     dass HuggingFace- und Transformers-Bibliotheken keine Update- oder Hub-Prüfungen durchführen.

2. **Integritätsprüfung der Modellgewichte (GGUF-Hash):**
   - Beim Laden eines lokalen GGUF-Modells wird dessen SHA-256-Prüfsumme kryptografisch
     berechnet und gegen den konfigurierten Erwartungswert geprüft.
   - Der Erwartungswert wird über die Umgebungsvariable `SOB_MODEL_SHA256` oder eine Sidecar-Datei
     `<modell>.gguf.sha256` bereitgestellt.
   - Hash erzeugen (im Terminal):
     ```bash
     shasum -a 256 models/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf > models/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf.sha256
     ```
     bzw. unter Linux mit `sha256sum`:
     ```bash
     sha256sum models/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf > models/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf.sha256
     ```
   - **Kein stiller Fallback:** Fehlt der Hash oder weicht er ab, bricht die Initialisierung sofort
     mit einem sichtbaren Fehler (`❌`) ab. Ein Betrieb mit ungesicherten oder manipulierten Gewichten
     ist ausgeschlossen.
   - Prüfskript: `./scripts/verify-model-hash.sh [pfad/zum/modell.gguf]`

3. **Deaktivierung externer APIs & Dokumentations-Endpunkte:**
   - Swagger UI (`/docs`), ReDoc (`/redoc`) und das OpenAPI-Schema (`/openapi.json`) sind in der
     FastAPI-Anwendung explizit deaktiviert (`docs_url=None`, `redoc_url=None`, `openapi_url=None`)
     und liefern HTTP 404. Dadurch wird verhindert, dass Browser externe CDN-Ressourcen
     nachladen können.
   - Sämtliche Frontend-Assets (HTML, CSS, JS) werden rein lokal ausgeliefert —
     keine externen Webfonts, Tracker oder CDNs.

4. **Automatisierter Testbefehl:**
   ```bash
   pytest tests/test_offline_runtime.py -q
   ```

---

## 10. Start-Check (macOS-Sicherheitsprüfungen)

Die In-Memory-Verarbeitung schützt Falldaten im flüchtigen Arbeitsspeicher. Um sicherzustellen, dass keine Daten über das Host-Betriebssystem unverschlüsselt gesichert oder unbemerkt in Cloud-Speicher repliziert werden, führt `chat_app` vor dem Serverstart automatisierte Integritätsprüfungen auf macOS durch (`chat_app/start_check.py` bzw. `start.command`). Fehlt eine Sicherheitsvoraussetzung, bricht der Start mit einem roten `❌` und einer konkreten Handlungsanweisung ab:

1. **FileVault-Verschlüsselung:**
   - **Prüfung:** `fdesetup status` prüft, ob die Festplattenverschlüsselung aktiv ist („FileVault is On“).
   - **Behebung:** FileVault in *Systemeinstellungen → Datenschutz & Sicherheit → FileVault* aktivieren.

2. **Ausschluss von Cloud-Synchronisation:**
   - **Prüfung:** Stellt sicher, dass das Anwendungs- und Logverzeichnis nicht innerhalb von Cloud-Sync-Ordnern liegt (`~/Library/Mobile Documents/` [iCloud Drive], `~/Library/CloudStorage/` [OneDrive, Dropbox, Google Drive, Box], `~/Dropbox` sowie `~/Desktop` oder `~/Documents` bei aktiver iCloud-Schreibtisch- & Dokumenten-Synchronisation). Auch Symlinks auf Providerwurzeln unter `CloudStorage/` werden aufgelöst und erkannt. Ausgelagerte Log-Ziele via Symlink außerhalb des App-Verzeichnisses werden strikt abgewiesen.
   - **Schutz vor vorzeitiger Dateimodifikation:** Die Initialisierung des File-Loggers (`logs/chat_app.log`) wird strikt bis nach erfolgreicher Sicherheitsprüfung aufgeschoben.
   - **Behebung:** App-Ordner in ein ungesynceltes lokales Verzeichnis verschieben (z. B. `~/SOB`) und Symlinks für `logs/` entfernen.

3. **Time-Machine-Absicherung:**
   - **Prüfung:** `tmutil destinationinfo` prüft konfigurierte Backup-Ziele. Sofern Ziele definiert sind, muss der App-Ordner samt Log-Zielen entweder via `tmutil isexcluded` ausgeschlossen sein oder jedes Backup-Ziel muss verifizierbar und vollständig verschlüsselt sein (`diskutil info` meldet zeilenbasiert „FileVault: Yes“ bzw. „Encrypted: Yes“). Unbekannte oder unstrukturierte leere Ausgaben ohne explizite „No destinations configured“-Diagnose führen nach Fail-Closed-Prinzip zum Abbruch.
   - **Behebung:** App-Ordner über `tmutil addexclusion <pfad>` vom Backup ausschließen oder das Time-Machine-Medium verschlüsseln.

**Entwickler-Override:**
Für Test- und Entwicklungszwecke mit ausschließlich synthetischen Daten kann der Start-Check über die Umgebungsvariable `SOB_SKIP_START_CHECK=1` übersprungen werden. In diesem Modus erscheint sowohl im Terminal als auch dauerhaft in der Web-UI ein gut sichtbares Warnbanner (`⚠️ Start-Check übersprungen – nur synthetische Daten verwenden`).

---

## 11. Nachweis & Prüfung

Das Datenschutz-, Offline- und Logging-Verhalten ist über automatisierte Tests abgesichert:
- `tests/test_start_check.py`:
  Verifiziert die Start-Check-Prüfungen (FileVault, Cloud-Sync, Time Machine, Nicht-macOS-Bypass, Override-Modus und strikt anonymisiertes Logging).
- `tests/test_offline_runtime.py`:
  Verifiziert den Offline-Betrieb via Socket-Blocker, 404 auf Docs-Endpunkten,
  keine externen CDN-Assets sowie SHA-256-Prüfungen der Modellgewichte.
- `tests/test_chat_app.py::test_dsgvo_logging_no_client_name_or_filename`:
  Verifiziert, dass weder Klientennamen noch Dateinamen in `logs/chat_app.log` landen.
- `tests/test_chat_app.py::test_upload_endpoints_do_not_log_client_names_or_filenames`:
  Verifiziert, dass Endpunkt-Uploads mit klientenbezogenen Dateinamen strikt anonymisiert bleiben.
- `tests/test_orchestrator_e2e.py::test_output_filename_format`:
  Verifiziert, dass generierte Dateinamen standardmäßig keine Klarnamen enthalten; `test_invalid_pseudonym_raises_clear_error_without_echo` und `test_generate_sob_twice_same_day_does_not_overwrite` sichern Validierung und Kollisionsschutz ab.
- `tests/test_pivot_consolidation.py`:
  Verifiziert die dauerhafte Entfernung verwaister Batch-/App-Dateien.

---

## 12. Zuordnung zu DSGVO-Artikeln

| Artikel | Umsetzung |
|---|---|
| Art. 5 Abs. 1 lit. c — Datenminimierung | Keine Persistierung auf Platte; In-Memory-Verarbeitung; Session-Reset |
| Art. 5 Abs. 1 lit. e — Speicherbegrenzung | Sofortige Freigabe bei Reset oder Prozessende; flüchtiger Arbeitsspeicher |
| Art. 9 — besondere Kategorien | Gesundheits-/Sozialdaten verbleiben 100% lokal und offline |
| Art. 25 — Privacy by Design/Default | Lokales LLM, Offline-Inferenz, 127.0.0.1, anonymisiertes Logging |
| Art. 32 — Sicherheit der Verarbeitung | Lokale Netzgrenze, anwendungsseitiger Zero-Disk-Footprint für Falldaten |

---

*Stand: Pivot-Konsolidierung (Issue #97). Siehe auch [`ARCHITECTURE.md`](ARCHITECTURE.md), ADR-2/ADR-9.*
