# Robustheitstest: Vorbericht-Import mit DOCX-Formatvarianten

> **Status:** Abgeschlossen  
> **Basis:** `main` (Branch: `test/vorbericht-formatvarianten`)  
> **Testdaten:** 100% synthetische Daten (Basis: Testfall "Hans Peter", keine echten Personen- oder Klientendaten)  
> **Sicherheits-Invariante:** Strikt RAM-only (Zero-Disk-Persistence), keine Speicherung von Inhalten auf Datenträgern, keine personenbezogenen Daten in Logs.

---

## 1. Übersicht & Zielsetzung

Ziel dieser Untersuchung ist die systematische Prüfung der Zuverlässigkeit des Vorbericht-Imports (`POST /api/upload-plan` sowie der zugrundeliegenden DOCX-Parsing-Pipeline) bei abweichenden Layouts, Strukturen und Formatierungen.

Dazu wurden reproduzierbare synthetische DOCX-Dateien generiert (abgelegt unter `tests/fixtures/vorbericht_varianten/`) und über automatisierte Regressionstests in `tests/test_vorbericht_formatvarianten.py` validiert.

---

## 2. Ergebnis-Tabelle

| Variante | Datei | erkannt | Zielanzahl (soll / ist) | Warnung sichtbar? | Bemerkung |
| :--- | :--- | :---: | :---: | :---: | :--- |
| **a) Original Op-de-Wisch (Referenz)** | `a_opdewisch_referenz.docx` | ja | 6 / 6 | nein | Fastpath via SDT-Kopftabelle & 9 Tabellen. 6 aktive Ziele (Bereiche 1, 2, 3, 5, 6, 7). Bereiche 4, 8, 9 ohne Ziele markiert (`entfällt`). Kein Fallback. |
| **b) Original Hamburg (Referenz)** | `b_hamburg_referenz.docx` | ja | 7 / 7 | nein | Fastpath via WordprocessingML-Bookmarks (`sections_*`). 7 aktive Ziele (Bereiche 1.2, 1.4, 1.5, 2.1, 2.2, 2.3, 3). Kein Fallback. |
| **c) Andere Überschriften** | `c_andere_ueberschriften.docx` | ja | 8 / 8 | ja (`⚠️`) | Heuristik erkennt Präfixe wie `"Lebensbereich 1"`, `"LEBENSBEREICH 2"`, `"Bereich 3"`, `"Abschnitt 7"` sauber. 6 aktive Ziele + 2 Status-Berichte. Fallback-Warnung aktiv. |
| **c2) Nur Bereichsnamen (ohne Nummern)** | `c2_nur_bereichsnamen_ohne_nummern.docx` | nein (xfail) | 8 / 26 | ja (`⚠️`) | **xfail**: Ohne Bereichsnummern (1..9 bzw. 1.1..3) fragmentiert der Tabellen-Row-Fallback in 26 Einzelteile. Erfordert semantisches NLP-Mapping (siehe Vorschläge). |
| **d) Fließtext / Aufzählungen** | `d_fliesstext_aufzaehlung.docx` | ja | 6..9 / 9 | ja (`⚠️`) | Reine Fließtext- und Bullet-Point-Abschnitte ohne Tabellen werden über Paragraph-Scan vollständig erkannt. Fallback-Warnung aktiv. |
| **e) Bereiche in anderer Reihenfolge** | `e_bereiche_andere_reihenfolge.docx` | ja | 8 / 8 | ja (`⚠️`) | Reihenfolge 7, 3, 1, 5, 2, 6, 4, 8, 9 wird korrekt den jeweiligen Standard-Bereichs-IDs zugeordnet (z. B. Arbeit landet zuverlässig bei ID 7). |
| **f) Fremde Abschnitte & Deckblatt** | `f_fremde_abschnitte_deckblatt_fussnoten.docx` | ja | 8 / 8 | ja (`⚠️`) | Deckblatt, Aktenzeichen, leere Trenntabellen und Anhang/Unterschriften stören das Parsing nicht. Klientenname & alle Bereiche korrekt extrahiert. |
| **g) Verbundene / verschachtelte Tabellen** | `g_verbundene_verschachtelte_tabellen.docx` | ja | 8 / 8 | ja (`⚠️`) | `gridSpan`-Kopfzeilen und verschachtelte SMART-Ziele-Tabellen (`cell.add_table()`) werden vollständig ohne Textverlust eingelesen. |
| **h) Fehlende Bereiche (3 von 9)** | `h_fehlende_bereiche_3_von_9.docx` | ja (teilweise) | 3 / 3 | ja (`⚠️`) | Exakt die 3 vorhandenen Bereiche (1, 2, 3) werden ohne Absturz extrahiert. Template Op-de-Wisch korrekt identifiziert. Fallback-Warnung aktiv. |
| **i) Fast leeres Dokument** | `i_leeres_fast_leeres_dokument.docx` | nein (erwartet) | 0 / 0 | ja (`❌` 422) | Saubere Ablehnung mit HTTP 422 (*Kein gültiger Vorbericht erkannt*), kein Absturz, kein stiller Datenverlust. |
| **0-Byte / Leere Datei** | `empty.docx` | nein (erwartet) | 0 / 0 | ja (`❌` 400) | Saubere Vorab-Ablehnung durch `sanitize_docx_bytes` mit HTTP 400 (*Datei abgelehnt*). |

---

## 3. Umgesetzte Verbesserungen (klein, eindeutig, abwärtskompatibel)

Im Rahmen des Tests wurden folgende minimale, hochwirksame Fixes in `sob_generator/previous_sob.py` implementiert:

1. **Tolerante Überschriften-Erkennung (`_extract_fallback`):**
   - Die Erkennungs-Regex `sub_id_re` und `main_id_re` unterstützen nun optionale textuelle Präfixe wie `Lebensbereich`, `Bereich` oder `Abschnitt` (case-insensitiv) sowie Trennzeichen (`:`, `-`, `–`, `—`).
   - *Effekt:* Dokumente mit Überschriften wie `"Lebensbereich 1: Psychische Verfassung"` oder `"LEBENSBEREICH 2"` werden zuverlässig erkannt und den kanonischen IDs zugeordnet, anstatt in den zeilenweisen Notfall-Fallback zu fallen.

2. **Robuste Erkennung von SMART-Zielen in separaten Zeilen (`_parse_table_section`):**
   - Wenn ein Anwender in einer eigenen Tabelle das Leitziel und die SMART-Ziele in zwei getrennte Zeilen aufgeteilt hat, wurden die SMART-Ziele zuvor ignoriert. Durch einen ergänzenden `elif re.search(smart_pattern, ...)`-Zweig werden SMART-Ziele nun auch auf eigenständigen Zeilen zuverlässig erfasst.

3. **Präzise Vorlagenerkennung bei unvollständigen & tabellenlosen Berichten:**
   - Wenn keine Tabellen vorhanden sind (Fließtext), prüft die Vorlagen-Heuristik nun auch Paragraphen auf Bereichsnummern.
   - Da im Hamburger SVB alle Bereiche außer `3` Sub-IDs mit Punkten besitzen (`1.1` bis `2.3`), indiziert das Auftreten von alleinstehenden Bereichsnummern wie `1` oder `2` ohne Sub-IDs eindeutig die Op-de-Wisch-Struktur. Dies verhindert Fehlzuordnungen bei Berichten, die nur die ersten 3 Lebensbereiche enthalten.

4. **Tolerante Extraktion des Klientennamens:**
   - `_extract_fallback` erkennt nun neben `"Bericht für:"` auch gängige Beschriftungen wie `"Klient:"`, `"Klientin:"` oder `"Betreute Person:"`.

---

## 4. Vorschläge für größere Umbauten (nicht umgesetzt)

Folgende Aspekte erfordern größere architektonische Änderungen und werden zur Prüfung für zukünftige Releases empfohlen:

1. **Semantisches / NLP-gestütztes Lebensbereichs-Mapping (Variante c2):**
   - *Problem:* Wenn Berichte weder Bereichsnummern (`1..9` bzw. `1.1..3`) noch Formular-Bookmarks enthalten, sondern ausschließlich Freitext-Titel (z. B. *"Gesundheit"* oder *"Alltag und Haushalt"*), scheitert die Regex-Erkennung.
   - *Lösungsvorschlag:* Einführung eines Normalisierungs-Wörterbuchs mit Levenshtein-Distanz oder Embedding-Similarity zur Zuordnung beliebiger Überschriften zu den 9 Standard-Lebensbereichen.

2. **Spezifische Warnmeldung bei unvollständiger Bereichsanzahl (< 9):**
   - *Problem:* Enthält ein Vorbericht absichtlich oder versehentlich nur 3 Bereiche (wie Variante h), importiert das System diese 3 Bereiche erfolgreich mit einer allgemeinen Fallback-Warnung (`⚠️ Vorbericht wurde über Heuristik/Fallback eingelesen...`).
   - *Lösungsvorschlag:* Ergänzung der Warnung um die konkrete Anzahl: `"⚠️ 3 von 9 Lebensbereichen erkannt. Bitte prüfen, ob der Vorbericht vollständig ist."`

3. **Konsolidierung der Fallback-Parser:**
   - *Problem:* `sob_generator/previous_sob.py` (`UniversalPlanImporter`) und `chat_app/plan_loader.py` besitzen teilweise redundante Heuristiken.
   - *Lösungsvorschlag:* Vollständige Bündelung aller Heuristiken in `UniversalPlanImporter`, sodass `plan_loader.py` ausschließlich als schlanker Adapter agiert.

---

## 5. Reproduzierbarkeit & Manuelle Prüfung

### Testausführung (im Worktree)
```bash
# Alle Formatvarianten-Tests ausführen:
.venv/bin/python -m pytest tests/test_vorbericht_formatvarianten.py -v

# Gesamte Testsuite (740+ Tests):
.venv/bin/python -m pytest -q
```

### Pfad zu den generierten Fixture-DOCX für manuelle UI-Tests
Die Testdateien liegen im Worktree unter:
```
tests/fixtures/vorbericht_varianten/
├── a_opdewisch_referenz.docx
├── b_hamburg_referenz.docx
├── c_andere_ueberschriften.docx
├── c2_nur_bereichsnamen_ohne_nummern.docx
├── d_fliesstext_aufzaehlung.docx
├── e_bereiche_andere_reihenfolge.docx
├── f_fremde_abschnitte_deckblatt_fussnoten.docx
├── g_verbundene_verschachtelte_tabellen.docx
├── h_fehlende_bereiche_3_von_9.docx
└── i_leeres_fast_leeres_dokument.docx
```

Fixture-Generator erneut ausführen:
```bash
.venv/bin/python tests/fixtures/make_vorbericht_varianten.py
```
