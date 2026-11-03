# Architekturentscheidungen — sob-generator

Nur **Warum** und **Was**, kein Code, keine Implementierungsdetails.

---

## Kontext

sob-generator erstellt den offiziellen Hamburger Sozialbericht
(SOB) für ambulante Eingliederungshilfeleistungen. Die Fachkraft gibt:

- Den **alten SOB** (ausgefüllte `Vorlage.docx` aus dem letzten Bewilligungszeitraum)
- Den **Vivendi PD CSV-Export** (alle Dokumentationseinträge des aktuellen Zeitraums)

und erhält eine neue, vorausgefüllte `Vorlage.docx` zur Kontrolle und Unterschrift.

---

## Gesamtpipeline

```
[A] alter SOB (.docx)   ──► OldReportImporter  ──► PreviousGoals
[B] Vivendi CSV         ──► VivendiPDImporter   ──► Client + list[DocumentationEntry]
[C] Freitext-Notizen    ──► (direkt)            ──► str (Erfahrungswissen Fachkraft)
                                                           │
                                                 LLMDraftGenerator
                                                 (llama.cpp eingebettet, lokal)
                                                 ordnet selbst zu Abschnitten zu
                                                           │
                                                 ReportDraft (.json, editierbar)
                                                           │
                                            ── Human Review / Edit ──
                                                           │
                                                 DocxRenderer
                                                 (python-docx)
                                                           │
                                            output/Fall_Datum_SOB.docx (Issue #115)
```

---

## ADR-1: Ausgabeformat ist .docx, nicht PDF

**Entschieden:** Ausgabe ist die ausgefüllte `Vorlage.docx`, kein PDF.

**Warum:** Das Fachamt Hamburg akzeptiert nur das offizielle Word-Formular.
Ein selbst erzeugtes PDF würde ausgedruckt, unterschrieben und eingescannt —
unnötiger Medienbruch. Die .docx-Ausgabe kann direkt geöffnet, geprüft und
digital eingereicht werden.

**Konsequenz:** Jinja2-HTML-Templates und WeasyPrint werden nicht gebaut.
Der Generator befüllt die `FORMTEXT`-Felder der Original-`Vorlage.docx`
mit `python-docx`.

---

## ADR-2: Nur lokales LLM, eingebettet via llama.cpp

**Entschieden:** Kein API-Aufruf an externe Dienste (OpenAI, Anthropic, Google).
Das LLM läuft als **eingebettetes llama.cpp** (`llama-cpp-python`) *innerhalb*
der App — kein separater Ollama-Dienst.

**Warum:** Zwei Gründe greifen ineinander:

1. **DSGVO Art. 25 — Privacy by Design.** Die Daten betreffen
   leistungsberechtigte Personen mit Behinderung. Kein Byte davon verlässt die
   lokale Maschine. Das Modell läuft vollständig offline.
2. **Eine Installation, die alles kann.** Die Fachkraft soll nicht erst Ollama
   installieren und ein Modell ziehen. Mit llama.cpp als Python-Bibliothek wird
   die Inferenz-Engine Teil der App; das **GGUF-Modell wird mitgeliefert**.
   Doppelklick → App startet → Modell lädt → fertig. (Verteilung: ADR-8.)

**Modell:** Ein **3B-Modell als GGUF (Q4-quantisiert, ~2 GB)** mit
**bestmöglicher Deutschqualität** — das ist das Auswahlkriterium. Die
Zielmaschine ist ein Intel-Mac (CPU-only, kein Metal); 8B wäre auf dem
Dual-Core-i5 zu zäh. Kandidaten: Llama 3.2 3B Instruct, Qwen2.5-3B-Instruct,
oder ein deutsch-finegetuntes 3B — konkrete Wahl beim Code-Sprint empirisch
an echten Vivendi-Exporten. Die Textqualität wird zusätzlich über den
Human-in-the-Loop gesichert (ADR-3).

**Konsequenz:** Keine externe Laufzeitabhängigkeit. Das Modell ist Teil des
Auslieferungsumfangs (~2 GB). Läuft auf Intel-CPU (AVX2); auf Apple Silicon
zusätzlich GPU-beschleunigt (Metal), aber das ist kein Muss. Die Generierung ist
kein Chat, sondern ein einmaliger Export-Lauf — Wartezeit von 1–3 Minuten ist
akzeptabel. Kein Fallback auf Cloud-Dienste — gewollt.

**Nachtrag — Modell & Zielmaschine aktualisiert (siehe ADR-8):** Die Zielmaschine
ist auf **Apple Silicon** gewechselt; damit fällt die 3B-Decke des Intel-i5. Im
Einsatz ist nun **Llama 3.1 8B Instruct als GGUF (Q4_K_M, ~4,9 GB)** mit
GPU-Offload über Metal (`--n-gpu-layers -1`, Default). Die ursprüngliche
3B/~2-GB-Festlegung oben bleibt als damaliger Entscheidungsstand dokumentiert; die
Begründung für den Wechsel steht in ADR-8.

---

## ADR-3: Human-in-the-Loop über editierbares JSON-Draft

**Entschieden:** Zwischen LLM-Generierung und .docx-Ausgabe gibt es eine
explizite Pause mit editierbarem Zwischenstand.

**Warum:** LLM-Ausgaben sind nicht direkt vertrauenswürdig für ein offizielles
Behördendokument. Die Fachkraft muss jeden Fließtext prüfen und freigeben.

**Ablauf:** Der Review-Schritt liegt in der Weboberfläche (ADR-9). Die Fachkraft
sieht jeden vom LLM erzeugten Abschnitt als editierbares Formularfeld, liest,
korrigiert, gibt frei. Erst danach wird die `.docx` geschrieben.

**Konsequenz:** Das Draft-JSON ist das zentrale **interne** Datenmodell — nicht
mehr die Editier-Oberfläche. Die Fachkraft sieht Formularfelder, kein JSON.
Feldnamen auf Deutsch, selbsterklärend ohne Code-Kenntnisse.

---

## ADR-4: Zwei Importer, klar getrennt

**Entschieden:** `VivendiPDImporter` (CSV) und `OldReportImporter` (.docx)
sind separate Klassen, beide von `BaseImporter` abgeleitet.

**Warum:** Die Quellen haben verschiedene Formate und Zwecke:
- CSV → Klientenstammdaten + Was wurde im Zeitraum getan?
- Alter SOB → Was war das vereinbarte Ziel aus dem letzten Gesamtplan?

Beide zusammen ergeben den vollständigen Kontext.

**Vivendi CSV-Struktur** (aus `templates/Vivendi/Vivendi CSV Vorlage.csv`):

```
Zeile 1:  [leer × 16] ; Zeitraum (z.B. "30.01.2026 21:00 - 31.05.2026 09:13") ; …
Zeile 2:  Name der Einrichtung ; …
Zeile 3:  [leer]
Zeile 4:  "Nachname, Vorname (*TT.MM.JJJJ)" ; …
Zeile 5:  [leer × 5] ; Datum ;; Uhrzeit ;; Schicht ;; Kategorie ;; Kennzeichen ;; Details ;;; Handzeichen
Zeile 6+: [Metadatenzeile]  — Datum, Uhrzeit, Schicht, Kategorie, Handzeichen
Zeile 7+: [Textzeile]       — Fließtext des Mitarbeiters (Berichtseintrag)
           ↑ zwei Zeilen pro Eintrag, immer abwechselnd
```

- **Separator:** Semikolon (`;`), kein Komma
- **Encoding:** **UTF-8 mit BOM**, Zeilenenden **CRLF** (`file`-verifiziert). Beim Lesen `encoding="utf-8-sig"` verwenden, damit das BOM nicht in Zeile 1 landet.
- **Klient-Parsing:** `"Nachname, Vorname (*TT.MM.JJJJ)"` aus Zeile 4
- **Zeitraum-Parsing:** Zeile 1, Position 16, Format `DD.MM.JJJJ HH:MM - DD.MM.JJJJ HH:MM`
- **Eintragsstruktur:** Metadatenzeile + Folgezeile (Fließtext) — **immer paarweise**
- **Kategorie-Präfix:** Vivendi-Kategorien beginnen oft mit `w_` (z.B. `w_Kontakt Angehörige`)

**Konsequenz:** Alter SOB ist **optional**.

**Erstbericht-Sonderfall:** Es gibt keinen alten SOB, aber immer ein
**Gesamtkonferenz-Protokoll** (Erstgespräch beim Fachamt), in dem die
vereinbarten Ziele schriftlich festgehalten sind. Die Fachkraft liest die
Ziele aus dem Protokoll und tippt sie in das **Freitext-Feld (ADR-7)** —
das Protokoll wird nicht automatisch importiert, kein neuer Importer nötig.
Input B bleibt leer, die Zielfelder werden über den Freitext-Kontext vom
LLM befüllt.

**Feld-Mapping `OldReportImporter` → `PreviousGoals` (Zielfortschreibung):**
Im alten SOB steht unter „Vorschlag für aktuelle (smarte) Ziele im nächsten
Befürwortungszeitraum“ (`sections[N].vorschlag_ziele`, ADR-5/ADR-10) das,
was die Fachkraft sich für den **kommenden** Zeitraum vorgenommen hat — und
dieser kommende Zeitraum ist genau der, für den jetzt der neue SOB erstellt
wird. Diese Ziele sind also die **aktuell gültigen** Ziele und werden 1:1 in
`sections[N].smarte_ziele` des neuen `ReportDraft` übernommen:

```
alter_SOB.sections[N].vorschlag_ziele  ──► 1:1 ──►  neuer_SOB.sections[N].smarte_ziele
```

`OldReportImporter` liest dafür Bookmark `sections_N_vorschlag_ziele` aus
dem alten SOB (ADR-10) und schreibt den Wert unverändert in
`PreviousGoals.sections[N].smarte_ziele`.

**Feld-Mapping `leitziel`:** `sections[N].leitziel` wird ebenfalls 1:1 aus
`alter_SOB.sections[N].leitziel` übernommen (Leitziele sind langfristig und
ändern sich selten). **War das Feld im alten SOB leer, bleibt es im neuen
SOB ebenfalls leer** — `OldReportImporter` füllt `leitziel` nicht künstlich
auf, sondern übernimmt genau den (ggf. leeren) Wert aus Bookmark
`sections_N_leitziel`.

```
alter_SOB.sections[N].leitziel  ──► 1:1 (oder leer) ──►  neuer_SOB.sections[N].leitziel
```

---

## ADR-5: Typisierte Modelle für alle Felder der Vorlage

**Entschieden:** Kein `dict`, keine ungetypten Felder. Jedes `FORMTEXT`-Feld
der `Vorlage.docx` hat ein benanntes Pydantic-Feld im `ReportDraft`.

**Warum:** Schreibfehler in Feldnamen werden sofort erkannt. Das Draft-JSON
ist durch die Modellstruktur selbstdokumentierend.

**Geplante Modellstruktur:**

```
ReportDraft
├── client: ClientData
│     name, vorname, geburtsdatum, anschrift,
│     gesetzliche_betreuung, gdb, pflegegrad, leistungsart
│
├── meta: ReportMeta
│     leistungszeitraum_von, leistungszeitraum_bis,
│     abwesenheitszeiten, leistungsanbieter,
│     begruegnungsstaette, bezugsbetreuer
│
├── developments: str          # Wesentliche aktuelle Entwicklungen (LLM)
│
└── sections: list[ReportSection]   # 8 Abschnitte
      id, title,
      previous_goals_apply: bool,   # FORMCHECKBOX "entfällt"
      leitziel: str,                # aus altem SOB
      smarte_ziele: str,            # aus altem SOB
      bericht_zielerreichung: str,  # LLM → aus DocumentationEntry-Liste
      vorschlag_ziele: str,         # LLM
      vorschlag_massnahmen: str,    # LLM
      realisierung_andere: str      # LLM
```

**Abschnitte der Vorlage:**

| ID  | Titel |
|-----|-------|
| 1.1 | Wohnsituation |
| 1.2 | Alltägliche Lebensführung / Hauswirtschaft |
| 1.3 | Basisversorgung / Grundpflege |
| 1.4 | Emotionale und psychische Entwicklung |
| 1.5 | Gesundheitsförderung und -erhaltung |
| 2.1 | Gestaltung sozialer Beziehungen |
| 2.2 | Teilnahme am kulturellen und gesellschaftlichen Leben |
| 2.3 | Kommunikation und Orientierung |
| 3   | Beschäftigung, Arbeit, Ausbildung |

---

## ADR-6: LLM kategorisiert selbst — kein YAML-Mapping

**Entschieden:** Die Dokumentationseinträge werden **nicht** über eine YAML-Datei
auf SOB-Abschnitte vorgemappt. Das LLM bekommt pro Abschnitt Titel + Beschreibung
und entscheidet selbst, welche Einträge dort einfließen.

**Warum:** Ein YAML-Mapping wäre Konfigurationsaufwand pro Einrichtung und müsste
gepflegt werden, sobald neue Vivendi-Kategorien dazukommen. Llama 3 hat ein
großes Kontextfenster (128k) — alle Einträge eines Zeitraums passen problemlos
hinein. Die Zuordnung ist eine Klassifikationsaufgabe, die ein LLM zuverlässig
löst. Die Fachkraft prüft das Ergebnis ohnehin (ADR-3).

**Konsequenz:** Keine `category_mapping.yaml`, keine Erst-Konfiguration. Die App
funktioniert sofort mit jedem Vivendi-Export. Falls Llama 3B für die Klassifikation
zu schwach ist oder das Kontextfenster zu voll wird, wird **nachträglich**
optimiert (z.B. Vorfilterung per Kategorie) — nicht vorab.

---

## ADR-7: Freitext-Notizen der Fachkraft als dritte Eingabequelle

**Entschieden:** Die Fachkraft kann zusätzlich zu CSV und altem SOB einen
**freien Text** eingeben — alles, was ihr zum Klienten einfällt und nicht in
Vivendi dokumentiert ist. Das LLM übernimmt diese Infos und ergänzt damit den
Bericht.

**Warum:** Vivendi-Einträge sind oft knapp und ereignisbezogen. Die Fachkraft
trägt aber Erfahrungswissen, das nirgends strukturiert erfasst ist:
Einschätzungen zur Entwicklung, Stimmungen, Kontext zu Vorfällen, Ziele für
den nächsten Zeitraum. Dieses Wissen gehört in den Bericht — bisher müsste sie
es mühsam manuell formulieren.

**Ablauf:** In der Weboberfläche (ADR-9) gibt es vor dem Generieren ein
Freitext-Feld, in das die Fachkraft frei tippt — alles, was ihr zum Klienten
einfällt. Leer lassen ist erlaubt (Freitext ist optional).

Der Freitext geht ungefiltert mit in den LLM-Kontext. Das LLM ordnet die
Aussagen selbst den passenden Abschnitten zu (wie bei den CSV-Einträgen, ADR-6)
und verwebt sie zu Fließtext.

**Konsequenz:** Der Freitext ist **optional**. Er ist die einzige Stelle, an der
die Fachkraft *vor* der Generierung Wissen einspeist statt *danach* zu
korrigieren — das verbessert die Qualität des ersten Drafts spürbar. Auch dieser
Text bleibt lokal (ADR-2).

---

## ADR-8: Auslieferung als eine macOS-App (.app / DMG)

**Entschieden:** Die App wird für macOS als eine selbstständige `.app` gebaut
und als DMG verteilt — Python-Laufzeit, llama.cpp und das GGUF-Modell sind
enthalten. **Baseline-Zielmaschine ist Intel-Mac** (Gogas eigenes Gerät:
2015er i5, 16 GB); Apple Silicon läuft schneller, ist aber keine Voraussetzung.

**Warum:** Die Fachkraft ist keine Entwicklerin. Sie soll die App installieren
und nutzen, ohne Terminal, ohne Python, ohne Modell-Download. Goga selbst
arbeitet auf einem Mac und stellt die App der Einrichtung vor.

**Konsequenz:**
- Build via **PyInstaller** oder **py2app** bündelt alles in eine `.app`
  (Intel-Build; optional `universal2` für beide Architekturen).
- Das DMG wird groß (**~2,5 GB**), weil das Modell mitreist. Verteilung per
  USB/Download, nicht per Mail.
- Für die Vorstellung in der Einrichtung sollte die App **signiert + notarisiert**
  werden (Apple Developer Account), sonst blockt Gatekeeper. Für den ersten
  eigenen Lauf reicht Rechtsklick → Öffnen.
- Andere Plattformen (Windows/Linux) sind **später** möglich (llama.cpp läuft
  überall), aber nicht Teil des ersten Wurfs.

**Offene Überlegung — Zielhardware ggf. Apple Silicon statt Intel-Baseline
(Stand 2026-06-15, noch nicht entschieden):**
Der 2015er-i5 ist die Worst-Case-Baseline und zwingt zu einem 3B-Modell, dessen
Deutschqualität für Behördentexte grenzwertig ist (ADR-2). Alternative: Träger
schaffen ein **dediziertes Apple-Silicon-Gerät (MacBook Pro M4/M5,
≥24 GB Unified Memory)** an. Das bleibt **vollständig lokal** — der DSGVO-Moat
aus ADR-2 (kein Byte verlässt die Maschine, Art.-9-Daten) bleibt unangetastet —,
hebt aber die 3B-Decke auf.
- Entscheidend ist **RAM (Unified Memory)**, nicht der Chip-Name: ≥24 GB für
  7–8B, 36–48 GB für 14B–32B (Q4, Metal/llama.cpp).
- Öffnet die Modellwahl (#11) auf **7–14B**, z. B. Qwen2.5-7B/14B-Instruct
  (**Apache 2.0**, kommerziell sauber — anders als die 3B-Variante).
- **Vereinfacht** das Packaging: eine Architektur statt fragiler
  AVX2-auf-alt-Intel-Builds, Metal-Beschleunigung.
- Verschiebt das Messziel von #17: dann **7–8B auf Apple Silicon** messen, nicht
  3B auf Intel-i5.
- **Voraussetzung (Vertriebsannahme):** Bereitschaft der Träger zur
  Hardware-Anschaffung (~1.800–2.500 €). Gehört in die Phase-3-Validierung mit
  2–3 Einrichtungen. Bis bestätigt, bleibt die Intel-i5-Baseline der Fallback.

---

## ADR-9: Frontend-Architektur — Interaktive Chat-Webanwendung (`chat_app`)

**Entschieden:** Die Fachkraft bedient das Tool über eine moderne, interaktive
**Web-Oberfläche (`chat_app`)**, die als lokale **FastAPI**-Anwendung auf `127.0.0.1:8000`
läuft und bequem über `start.command` oder `python -m chat_app` gestartet wird.

**Pivot-Konsolidierung (Issue #97):**
Die ursprüngliche monolithische Batch-Generierung mit `pywebview` wurde vollständig
abgelöst. Anstelle eines starren Formulardurchlaufs tritt ein partnerschaftlicher,
dialogischer Reflexionsassistent:
- **Streaming-Chat:** LLM-Antworten werden via Server-Sent Events (SSE) tokenweise
  gestreamt — kein langes, intransparentes Warten auf einen Gesamt-Batch.
- **Wegfall von `pywebview`:** Beseitigung nativer Webview-Abhängigkeiten (`pywebview`,
  `WKWebView`-Eigenheiten, native FileDialog-Hooks). Die Web-UI läuft zuverlässig
  im Standardbrowser der Fachkraft.
- **DSGVO Art. 25:** Die Web-App bindet strikt an `127.0.0.1`. Alle Assets (CSS, JS)
  werden lokal ausgeliefert — kein Byte verlässt die Maschine, keine Cloud-Abhängigkeit.

**Ablauf in `chat_app`:**
```
start.command / python -m chat_app → FastAPI-Server bindet an 127.0.0.1:8000
                                  → Standardbrowser öffnet sich
  1. CSV-Verlauf und optionalen Vorbericht/Plan hochladen (In-Memory-Parsing)
  2. Lokales Modell auswählen (GGUF unter models/)
  3. Dialog führen: Reflexionsimpulse, Fragen, Entwürfe je Lebensbereich
  4. Entwurf freigeben & strukturierte Abschnitte per Klick in die Zwischenablage kopieren
```

**Konsequenz:** Reduktion von Komplexität und Abhängigkeiten. Der veraltete monolithische
Batch-Code (`app/`) ist entfernt; `chat_app` ist der alleinige Applikationsstandard.

---

## ADR-10: Feld-Mapping via benannte Bookmarks, nicht Index

**Entschieden:** Jedes `FORMTEXT`-/`FORMCHECKBOX`-Feld der `Vorlage.docx`, das
ein Pydantic-Feld aus ADR-5 befüllt, bekommt ein `w:bookmarkStart`/`w:bookmarkEnd`
um das Feld, benannt nach dem Modellpfad (z.B. `client_geburtsdatum`,
`sections_0_leitziel`, `sections_2_previous_goals_apply`). Der `DocxRenderer`
adressiert Felder ausschließlich über diese Bookmark-Namen, niemals über
Tabellen-/Zeilen-/Spaltenindex.

**Analyse:** `templates/hamburg/Vorlage.docx` enthält **100 Legacy-Formularfelder**
(86 × `FORMTEXT`, 13 × `FORMCHECKBOX`, 1 × `FORMDROPDOWN`) — `python-docx` hat
dafür keine API, Zugriff nur über `word/document.xml` (`w:fldChar` als
`w:r`-Kindelement). Die wenigen vorhandenen Namen (`wrdZielEmpf`,
`wrdcboRealGruppe1_2`, …) sind **Word-Autogenerierungs-Reste**, mehrfach
dupliziert (z.B. `wrdZielEmpf` 17×) und damit nicht eindeutig — keine
brauchbare Adressierung.

**Warum Bookmarks und nicht Index:**
- Mehrere Felder liegen **in derselben Tabellenzelle** (z.B. T0 R0 C0 enthält
  3 Felder: Name/Vorname, Geburtsdatum, Anschrift). Ein Index auf Zellebene
  wäre zu grob, ein Index auf Feld-Ebene zählt über das ganze Dokument durch
  und verschiebt sich bei **jeder** Strukturänderung (neue Zeile, neue Spalte,
  gelöschte Dopplung) komplett — siehe die bekannte doppelte
  „Bericht zur Zielerreichung“-Zeile in 8 von 9 Abschnitten, die in einer
  künftigen Vorlagenbereinigung verschwinden soll (ADR-10-Folgepunkt).
- Bookmarks sind **lokal stabil**: Wird eine Tabelle umgebaut, bleibt das
  Bookmark am Feld, solange das Feld selbst nicht gelöscht wird. Das deckt
  sich mit Issue #8.
- Bookmark-Namen verdoppeln die ADR-5-Modellpfade 1:1 — das Mapping ist
  **selbstdokumentierend** im XML lesbar, kein separates Lookup-Schema nötig.

**Naming Convention:**
- `client_<feld>` / `meta_<feld>` für `ReportDraft.client.*` / `.meta.*`
- `sections_<N>_<feld>` für `ReportDraft.sections[N].*` (N = 0..8, Punkte
  durch Unterstriche ersetzt, da Word-Bookmark-Namen keine Punkte erlauben)
- Checkbox-Paare (Ja/Nein) bekommen zwei Bookmarks mit Suffix
  `_ja` / `_nein` (z.B. `client_gesetzliche_betreuung_ja`); der Renderer setzt
  je nach `bool`-Wert genau eines der beiden Felder auf `checked`.

**Ergebnis:** 76 von 100 Feldern sind gemappt und in
`templates/hamburg/Vorlage.docx` mit Bookmarks versehen. Die übrigen 24
Felder (Tabellen 10–13, Dopplungen, Felder ohne ADR-5-Entsprechung) sind als
**offene Fragen** in `docs/field-mapping.md` dokumentiert — ADR-5 wird **nicht**
eigenmächtig erweitert, das ist Gegenstand einer Folge-Iteration nach dem
Code-Sprint.

**Mapping-Tabelle:** vollständige Feld-für-Feld-Zuordnung in
[`field-mapping.md`](field-mapping.md).

**Konsequenz:** `python-docx` bleibt für Strukturzugriff (Tabellen, Absätze)
im Einsatz; das **Befüllen** der Formularfelder erfolgt über
`document.xml` via `lxml`: Bookmark per Name finden, das umschlossene
`w:fldChar`-Feld (FORMTEXT-Ergebnis bzw. FORMCHECKBOX `w:checked`)
manipulieren. Ein Roundtrip-Test-Stub
(`tests/test_bookmark_roundtrip.py`) verifiziert das Schreiben/Lesen für
beide Feldtypen — Grundlage für die Acceptance-Tests aus #12.

---

## ADR-11: LLM-Output-Vertrag — JSON-Schema→GBNF pro Abschnitt, 10 Calls

**Entschieden:** Für die 4 LLM-Felder pro Abschnitt
(`bericht_zielerreichung`, `vorschlag_ziele`, `vorschlag_massnahmen`,
`realisierung_andere`) und für `developments` gibt es je ein flaches
Pydantic-Modell (`SectionLLMOutput`, `DevelopmentsLLMOutput`,
`sob_generator/llm/schemas.py`, nur `str`-Felder). Aus `model_json_schema()` erzeugt
`llama_cpp.LlamaGrammar.from_json_schema()` zur Laufzeit eine GBNF-Grammar
(`sob_generator/llm/grammar.py`), die per `grammar=`-Parameter an `Llama(...)` (bzw.
`Llama.__call__`) übergeben wird und den Output auf gültiges JSON in genau
dieser Struktur erzwingt.

Ein Lauf macht **10 LLM-Calls**: 9× (ein Call pro Abschnitt) + 1× für
`developments`. Jeder Call bekommt den vollständigen
`DocumentationEntry`-Kontext (ADR-6), Abschnittstitel/-beschreibung,
bisherige Ziele aus dem alten SOB (falls `previous_goals_apply`) und den
Freitext der Fachkraft (ADR-7) — siehe `sob_generator/llm/prompts.py`.

**Warum GBNF statt freiem JSON-Prompting:** llama-cpp-python (0.3.x)
unterstützt `LlamaGrammar.from_json_schema()` für genau den
Schema-Ausschnitt, den unsere Modelle nutzen (flaches Objekt, feste
String-Keys) — verifiziert anhand der erzeugten Grammar in
`build_grammar()`. Issue #9 beschreibt das Kernproblem: ein 3B-Modell liefert
freies JSON nicht zuverlässig (Fließtext vor/nach dem JSON, fehlende
Anführungszeichen, abweichende Keys). Grammar-Constrained Decoding
eliminiert diese Fehlerklasse, weil ungültige Tokens beim Sampling gar nicht
erst zugelassen werden — ein JSON-Schema-Prompt ohne Grammar-Zwang
("Antworte im folgenden JSON-Format: …") wäre nur eine Bitte, keine Garantie.

**Warum das ins ADR-2-Budget (1-3 Min. für alles) passt:** GCD läuft komplett
auf der CPU und parst inkrementell mit. llama.cpp sampelt zuerst ungemaskt
und berechnet die teure Token-Maske nur bei einem drohenden
Grammar-Verstoß ("lazy grammar") — bei einer einfachen Grammar wie unserer
(festes Objekt mit String-Werten) ist das selten der Fall. 10 kurze Calls
(max. 1024 bzw. 512 Output-Tokens) sind damit realistisch im Budget; falls
nicht, sind die Pro-Abschnitt-Calls unabhängig und könnten parallelisiert
werden (spätere Optimierung, kein Designproblem).

**Warum 1 Call pro Abschnitt statt 1 Mega-Call für alles:**
- Die Grammar für „9 Abschnitte als verschachteltes Array" ist komplexer und
  fehleranfälliger als 9× dieselbe flache Grammar.
- Granularer Retry: Schlägt ein Abschnitt fehl, betrifft das nur diesen
  einen Call — passt zum abschnittsweisen Human-in-the-Loop-Review (ADR-3).
- ADR-6 bleibt trotzdem gültig: Jeder Call bekommt weiterhin ALLE
  Dokumentationseinträge; die Zuordnung zu Abschnitten passiert im Prompt,
  nicht durch Vorfilterung.

**Few-Shot:** Je ein anonymisiertes Beispiel (1 Abschnitt + `developments`),
abgeleitet aus den Beispieltexten in
`test-scripts/ERSTE_SCRIPTE_SOB/prototyp_docx_renderer.py`
(`sob_generator/llm/prompts.py`). Das JSON-Schema wird **nicht** im Prompt-Text
beschrieben — die Struktur kommt über das Few-Shot-Beispiel, die Form wird
über die Grammar erzwungen ("Schema constrains, not informs").

**Validierung & Fehlerverhalten** (`sob_generator/llm/generator.py`):
1. Grammar-Output wird per `model_validate()` gegen das Pydantic-Modell
   geprüft.
2. Schlägt das fehl (z.B. durch `max_tokens` abgeschnittenes JSON) → 1 Retry
   mit verschärfter Erinnerung im Prompt.
3. Schlägt auch der Retry fehl → Felder bleiben leer, `needs_review=True`.
   Der Lauf bricht **nicht** ab; die Fachkraft sieht das leere Feld im
   Review (ADR-3) und füllt es manuell — der Vertrag lässt keinen
   stillschweigenden Müll durch, ohne den Gesamtlauf zu gefährden.

**Konsequenz:**
- Module: `sob_generator/llm/schemas.py` (Output-Modelle + `DocumentationEntry`),
  `sob_generator/llm/sections.py` (9 Abschnittsdefinitionen für Prompts),
  `sob_generator/llm/grammar.py` (JSON-Schema→GBNF, gecached), `sob_generator/llm/prompts.py`
  (Templates + Few-Shot), `sob_generator/llm/generator.py` (`LLMDraftGenerator` mit
  Retry/Fallback).
- `requirements.txt` neu: `llama-cpp-python`, `pydantic`, `python-docx`,
  `lxml`, `pytest`.
- `tests/test_llm_output_contract.py` deckt Grammar-Erzeugung,
  Prompt-Inhalte, Validierung sowie Retry/Fallback ab — ohne GGUF-Modell
  (`_complete` gemockt). Modellwahl (#11) und End-to-End-Acceptance-Test
  (#12) bauen darauf auf.
- Mit ADR-10 und ADR-11 ist das Design-Gate geschlossen — das Makro-Design
  (ADR 1–11) gilt als abgeschlossen. Offene Implementierungsaufgaben werden
  über GitHub Issues mit Prioritäts-Labels (`prio-1`/`prio-2`/`prio-3`)
  verfolgt, nicht über eine separate Roadmap-Datei.

---

## Was absichtlich nicht gebaut wird

| Idee | Warum nicht |
|------|-------------|
| Cloud-LLM | DSGVO — kein Client-Datum nach außen |
| PDF als Primärausgabe | Fachamt braucht .docx |
| Multi-Klient pro Datei | Vivendi exportiert immer 1 Klient pro CSV |
| Automatische Einreichung | Außerhalb des Scopes |

---

*Stand: 2026-06-14 — ADR-10 (Feld-Mapping via Bookmarks, #8) und ADR-11
(LLM-Output-Vertrag, #9) entschieden. Design-Gate geschlossen — das
Makro-Design (ADR 1–11) ist abgeschlossen. Priorisierung offener Aufgaben
läuft über GitHub-Issue-Labels (`prio-1`/`prio-2`/`prio-3`).*

---

## Nachträgliche Entscheidung — Weg C: Prompt-Rücknahme (#38, 2026-06-26)

**Problem:** Das 8B-Q4-Modell (ADR-2) war an seiner logischen Grenze.
Jeder neue Stil-Fix gegen einen Kollaps (#30/#36 Anti-Schablonen,
GROUNDING_INSTRUCTION) destabilisierte andere Felder (Whac-A-Mole-Effekt).

**Entschieden — Weg C:** Die Prompt-Eskalation wird chirurgisch
zurückgenommen — kein git-Rollback, alle Features behalten, nur die fragilen
Stil-Direktiven entfernt. Zurück auf Phase-1-Niveau: saubere, inhaltlich
schlankere Texte als stabiler Ausgangspunkt für objektive Abnahme.

**Bewusst BEHALTEN (Schutz-Code, keine fragilen Nudges):**
- **Opener-Guard** (orchestrator + `opener_diversity.py`) — CLAUDE.md-Invariante.
- **`repeat_penalty` 1.15** (#19) — technischer Loop-Schutz.
- **Vornamen-Scrubbing** (#32) — DSGVO-nah, kein Stil-Treiber.
- **Temperatur 0.7** (#30) — der einzige wirksame Kollaps-Hebel auf 8B.
- **Fester Seed 42** (#35/#38) — reproduzierbare A/B-Läufe.

**Bewusst ENTFERNT aus `build_system_prompt`:**
- `ANTI_TEMPLATE_INSTRUCTION` (#30/#36) — wirkte auf das 8B-Modell als Nudge,
  destabilisierte Maßnahmen-Felder.
- `GROUNDING_INSTRUCTION` (#30) — analog, fragiler Stil-Treiber.

**Few-Shot:** Von 3 rotierenden Distraktoren-Beispielen auf 1 fixes Beispiel
(`SECTION_FEW_SHOT_SITUATION`) reduziert. Temperatur 0.7 liefert die
nötige Varianz; das Beispiel dient als Stil-/Struktur-Referenz.

**Gate-Fundament:** `sob_generator/draft_diagnostics.py` enthält nun
zwei weitere Heuristiken (Platzhalter-Leak, Genitiv-Apostroph) und eine
reine `compare_to_baseline()`-Funktion. `scripts/model_acceptance.py`
läuft mit `--baseline scripts/acceptance_baseline.json` und gibt Exit-Code 1
bei Regression — CI/Gate-tauglich. Baseline-Werte: `max_shared_opener ≤ 2`,
`bullet_sections = 0`, `placeholder_leaks = 0`, `genitive_apostrophes = 0`;
`bare_surname_hits` nach dem ersten echten Weg-C-Lauf kalibrieren.
