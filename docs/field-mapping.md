# Feld-Mapping: `Vorlage.docx` ↔ `ReportDraft` (ADR-5)

Begleitdokument zu **ADR-10** (siehe `ARCHITECTURE.md`). Listet alle **100**
Legacy-Formularfelder (86 × `FORMTEXT`, 13 × `FORMCHECKBOX`, 1 × `FORMDROPDOWN`)
der `templates/hamburg/Vorlage.docx` in Dokumentreihenfolge, mit Bookmark-Name
(falls gesetzt) und Pydantic-Modellpfad (ADR-5).

**Stand:** 77 von 100 Feldern sind gemappt und mit Bookmarks versehen. Die
übrigen 23 Felder sind als **OFFEN** markiert (siehe Abschnitt
[Offene Fragen](#offene-fragen) am Ende) — sie werden *nicht* blind auf
ADR-5-Felder gepresst, sondern als eigene Entscheidung für eine Folge-Iteration
festgehalten.

Spalten: `#` = Position in Dokumentreihenfolge (1–100), `Typ`, `Kontext` =
umgebendes Label/Fließtext, `Bookmark` = gesetzter `w:bookmarkStart`-Name,
`Modellpfad` = Pydantic-Feld nach ADR-5.

---

## Tabelle 0 — Klient & Meta (Kopfbereich)

| # | Typ | Kontext | Bookmark | Modellpfad |
|---|-----|---------|----------|------------|
| 1 | FORMTEXT | „Bericht für (Name, Vorname):“ | `client_name_vorname` | `client.name` + `client.vorname` (Renderer fügt zu „Name, Vorname“ zusammen, #50/K3) |
| 2 | FORMTEXT | „Geburtsdatum:“ | `client_geburtsdatum` | `client.geburtsdatum` |
| 3 | FORMTEXT | „Aktuelle Anschrift:“ | `client_anschrift` | `client.anschrift` |
| 4 | FORMCHECKBOX | „Gesetzliche Betreuung: ☐ Nein“ | `client_gesetzliche_betreuung_nein` | `client.gesetzliche_betreuung` (= `False`) |
| 5 | FORMCHECKBOX | „☐ Ja“ | `client_gesetzliche_betreuung_ja` | `client.gesetzliche_betreuung` (= `True`) |
| 6 | FORMTEXT | „Name der gesetzl. Betreuung:“ | – | **OFFEN** |
| 7 | FORMTEXT | „Anschrift der gesetzl. Betreuung:“ | – | **OFFEN** |
| 8 | FORMTEXT | „Telefon/ Fax/ E-Mailadresse:“ (gesetzl. Betreuung) | – | **OFFEN** |
| 9 | FORMTEXT | „Grad der Behinderung und Merkmale des Schwerbehinderten-Ausweises:“ | `client_gdb` | `client.gdb` |
| 10 | FORMCHECKBOX | „☐ entfällt“ (zu GdB) | – | **OFFEN** |
| 11 | FORMTEXT | „Pflegegrad:“ | `client_pflegegrad` | `client.pflegegrad` |
| 12 | FORMCHECKBOX | „☐ entfällt“ (zu Pflegegrad) | – | **OFFEN** |
| 13 | FORMTEXT | „Leistungsart (z.B. ASP, PBW, WA):“ | `client_leistungsart` | `client.leistungsart` |
| 14 | FORMTEXT | „Leistung wurde erbracht: von …“ | `meta_leistungszeitraum_von` | `meta.leistungszeitraum_von` |
| 15 | FORMTEXT | „… bis …“ | `meta_leistungszeitraum_bis` | `meta.leistungszeitraum_bis` |
| 16 | FORMTEXT | „Abwesenheitszeiten im Leistungszeitraum … von …“ | – | **OFFEN** |
| 17 | FORMTEXT | „… bis …“ | – | **OFFEN** |
| 18 | FORMTEXT | „Wesentliche aktuelle Entwicklungen im Berichtszeitraum:“ | `developments` | `developments` |
| 19 | FORMTEXT | „Name/ Adresse des Leistungsanbieters:“ | `meta_leistungsanbieter` | `meta.leistungsanbieter` |
| 20 | FORMTEXT | „Name/ Adresse der Begegnungsstätte:“ | `meta_begruegnungsstaette` | `meta.begruegnungsstaette` |
| 21 | FORMTEXT | „Name/ Telefon/ E-Mailadresse der/des Bezugsbetreuerin/-betreuers:“ | `meta_bezugsbetreuer` | `meta.bezugsbetreuer` |

---

## Tabellen 1–9 — Abschnitte (`sections[0..8]`)

Jeder Abschnitt folgt demselben Muster aus 7 ADR-5-Feldern. Tabelle 1
(Abschnitt 1.1) hat keine Dopplung; Tabellen 2–9 (Abschnitte 1.2–3) haben
zusätzlich eine **doppelte** „Bericht zur Zielerreichung“-Zeile (OFFEN,
siehe unten).

| Sections-Index | Tabelle | Titel |
|---|---|---|
| 0 | T1 | 1.1 Wohnsituation |
| 1 | T2 | 1.2 Alltägliche Lebensführung/Hauswirtschaft |
| 2 | T3 | 1.3 Basisversorgung/Grundpflege |
| 3 | T4 | 1.4 Emotionale und psychische Entwicklung |
| 4 | T5 | 1.5 Gesundheitsförderung und -erhaltung |
| 5 | T6 | 2.1 Gestaltung sozialer Beziehungen |
| 6 | T7 | 2.2 Teilnahme am kulturellen und gesellschaftlichen Leben |
| 7 | T8 | 2.3 Kommunikation und Orientierung |
| 8 | T9 | 3. Beschäftigung, Arbeit, Ausbildung |

### Pro Abschnitt `sections[N]` (N = 0..8)

| Typ | Kontext | Bookmark | Modellpfad |
|-----|---------|----------|------------|
| FORMCHECKBOX | „Vereinbarte Ziele … ☐ entfällt (es wurden keine Ziele im Gesamtplan vereinbart)“ | `sections_N_previous_goals_apply` | `sections[N].previous_goals_apply` |
| FORMTEXT | „Leitziel:“ | `sections_N_leitziel` | `sections[N].leitziel` |
| FORMTEXT | „Smarte Ziele:“ | `sections_N_smarte_ziele` | `sections[N].smarte_ziele` |
| FORMTEXT | „Bericht zur Zielerreichung / ausgeführte Maßnahme bzw. Methodik zur Zielerreichung:“ (1. Vorkommen) | `sections_N_bericht_zielerreichung` | `sections[N].bericht_zielerreichung` |
| FORMTEXT | „Bericht zur Zielerreichung …“ (2. Vorkommen, nur T2–T9) | – | **OFFEN** (Dopplung, siehe unten) |
| FORMTEXT | „Vorschlag für aktuelle (smarte) Ziele im nächsten Befürwortungszeitraum:“ | `sections_N_vorschlag_ziele` | `sections[N].vorschlag_ziele` |
| FORMTEXT | „Vorschlag für Maßnahmen (…):“ | `sections_N_vorschlag_massnahmen` | `sections[N].vorschlag_massnahmen` |
| FORMTEXT | „Realisierung durch Andere: (…)“ | `sections_N_realisierung_andere` | `sections[N].realisierung_andere` |

**Globale Feld-Indizes pro Abschnitt** (Position 1–100 im Dokument):

| sections-Index | previous_goals_apply | leitziel | smarte_ziele | bericht (1.) | bericht (2., OFFEN) | vorschlag_ziele | vorschlag_massnahmen | realisierung_andere | weitere OFFEN |
|---|---|---|---|---|---|---|---|---|---|
| 0 | #22 | #23 | #24 | #25 | – | #26 | #27 | #28 | – |
| 1 | #29 | #30 | #31 | #32 | #33 | #34 | #35 | #36 | #37 (FORMDROPDOWN „Folgeantrag Haushaltshilfen“) |
| 2 | #38 | #39 | #40 | #41 | #42 | #43 | #44 | #45 | – |
| 3 | #46 | #47 | #48 | #49 | #50 | #51 | #52 | #53 | – |
| 4 | #54 | #55 | #56 | #57 | #58 | #59 | #60 | #61 | – |
| 5 | #62 | #63 | #64 | #65 | #66 | #67 | #68 | #69 | – |
| 6 | #70 | #71 | #72 | #73 | #74 | #75 | #76 | #77 | – |
| 7 | #78 | #79 | #80 | #81 | #82 | #83 | #84 | #85 | – |
| 8 | #87 | #88 | #89 | #90 | #91 | #92 | #93 | #94 | #86 „Situation zur Zeit:“ (vor dem Abschnitt) |

---

## Tabellen 10–13 — nicht in ADR-5 (OFFEN)

| # | Tabelle | Typ | Kontext | Modellpfad |
|---|---------|-----|---------|------------|
| 95 | T10 | FORMTEXT | „Vorschlag für den kalkulierten Zeitaufwand des Trägers — Einzelangebote:“ | **OFFEN** |
| 96 | T10 | FORMTEXT | „… Gruppenangebote:“ | **OFFEN** |
| 97 | T11 | FORMTEXT | „Wie zufrieden sind Sie mit der Unterstützungsleistung?“ | **OFFEN** |
| 98 | T12 | FORMTEXT | „Welche Wünsche und Vorstellungen haben Sie? Was ist Ihnen wichtig?“ | **OFFEN** |
| 99 | T13 | FORMTEXT (Name `Text1`) | „Bericht wurde besprochen am:“ | **OFFEN** |
| 100 | T13 | FORMTEXT (Name `Text2`) | „Die Unterschrift wurde aus folgenden Gründen nicht erteilt:“ | **OFFEN** |

---

## Offene Fragen

ADR-5 deckt **78 Modellfelder** ab (8 `client` + 6 `meta` + 1 `developments`
+ 9 × 7 `sections`-Felder). Die `Vorlage.docx` hat **100 Formularfelder**.
77 Modellfelder lassen sich 77 Formularfeldern zuordnen (die
gesetzliche-Betreuung-Checkbox ist ein Paar; #1 fasst zwei Modellfelder
zusammen). Die folgenden **23 Felder** sind nicht gemappt — bewusst, statt
das Modell ungeprüft zu erweitern:

1. **#1 „Bericht für (Name, Vorname)“** — ~~Entscheidung nötig~~
   **entschieden (#50/K3):** Das Modell behält `client.name`/`client.vorname`
   getrennt (der Importer parst sie ohnehin getrennt aus der CSV); der
   Renderer fügt beide im Vorlagen-Format „Name, Vorname“ in das Bookmark
   `client_name_vorname` zusammen. Keine Layoutänderung an der Vorlage, kein
   redundantes Modellfeld.
2. **#6–#8 „Name/Anschrift/Telefon der gesetzl. Betreuung“** — in ADR-5 nicht
   vorgesehen. Vermutlich fehlten diese beim Entwurf von ADR-5; Klären, ob sie
   ins Modell aufgenommen werden (`client.betreuung_name`,
   `client.betreuung_anschrift`, `client.betreuung_kontakt`?).
3. **#10, #12 „entfällt“-Checkboxen (GdB / Pflegegrad)** — ADR-5 hat nur
   `client.gdb` / `client.pflegegrad` als `str`. Ob „entfällt“ als eigenes
   `bool`-Feld nötig ist oder über einen Sonderwert in `gdb`/`pflegegrad`
   (z.B. leerer String) abgebildet wird, ist offen.
4. **#16–#17 „Abwesenheitszeiten von/bis“** — ADR-5 hat ein einzelnes
   `meta.abwesenheitszeiten: str`-Feld, die Vorlage hat zwei getrennte
   Datumsfelder. Entscheidung: Modell auf zwei Felder aufteilen oder
   Renderer kombiniert beide Bookmarks aus einem String (Parsing-Aufwand).
5. **#33, #42, #50, #58, #66, #74, #82, #91 — doppelte „Bericht zur
   Zielerreichung“-Zeile** in 8 von 9 Abschnitten (Tabellen 2–9). Die Duplikat-Zeile
   enthält ein unbenanntes, leeres FORMTEXT-Artefakt ohne Bookmark. Sie wird vom
   `HamburgDocxRenderer` beim Rendern in-memory entfernt (Zero-Disk), wenn sie
   exakt auf die Original-Zeile mit Bookmark folgt, der gesamte Zeilentext identisch
   ist und kein Bookmark sowie kein benanntes/befülltes Formularfeld trägt.
   Die Vorlagendatei selbst bleibt unverändert.
6. **#37 FORMDROPDOWN „Folgeantrag auf Haushaltshilfen wird bei der
   zuständigen Stelle beantragt“** — einziges Dropdown-Feld im Dokument,
   kommt in ADR-5 nicht vor. Klären ob/wie relevant für den Bericht.
7. **#86 „Situation zur Zeit: (z.B. Ausbildung, Arbeit, WfbM,
   Arbeitsprojekte)“** — zusätzliches Feld nur in Abschnitt 3
   (Beschäftigung), in ADR-5 nicht vorgesehen. Klären, ob `sections[8]`
   ein zusätzliches Feld `situation_zur_zeit` braucht.
8. **#95–#100 (Tabellen 10–13)** — Zeitaufwand des Trägers, Zufriedenheit,
   Wünsche des Leistungsberechtigten, Unterschriftsdaten. Diese Felder
   liegen außerhalb des in ADR-5 modellierten Bereichs (Abschnitte 1–9) und
   wirken wie ein separater "Abschluss"-Teil des Formulars. Klären, ob/wie
   sie ins Datenmodell aufgenommen werden (vermutlich eigene
   `ReportDraft`-Top-Level-Felder, z.B. `abschluss: ReportClosing`).

Diese 24 Punkte sind **keine Blocker für den Code-Sprint** (#10–#16): Die
76 gemappten Bookmarks decken den Kern der LLM-generierten Inhalte
(`developments` + alle 9 Abschnitte) sowie die Klienten-Stammdaten ab. Die
offenen Felder können in einer Folge-Iteration per eigenem Issue geklärt
werden, sobald echte Testdaten (golden fixture, #12) zeigen, wie wichtig sie
in der Praxis sind.
