"""Tests für den OpDeWischDocxRenderer (FO_SVB_Berichtsvorlage_2026.docx).

Verifiziert das korrekte Befüllen von Kopfdaten (Tabelle 0), Content Controls (SDT)
und den 9 Abschnittstabellen (Tabellen 1–9) inklusive Hinweistexten und Checkboxen.
"""
from pathlib import Path
import docx
from lxml import etree

from sob_generator.models import ClientData, ReportDraft, ReportMeta, ReportSection
from sob_generator.renderer import NO_ENTRIES_HINT, REVIEW_MARKER
from sob_generator.renderers.op_de_wisch_renderer import OpDeWischDocxRenderer

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DOCX = ROOT / "templates" / "hamburg" / "OpDerWisch" / "FO_SVB_Berichtsvorlage_2026.docx"


def test_op_de_wisch_renderer_full_roundtrip(tmp_path):
    draft = ReportDraft(
        client=ClientData(
            name="Mustermann",
            vorname="Erika",
            geburtsdatum="12.04.1982",
            anschrift="Mustergasse 7, 20144 Hamburg",
            gesetzliche_betreuung=True,
            gdb="40",
            pflegegrad="1",
            leistungsart="ASP",
        ),
        meta=ReportMeta(
            leistungszeitraum_von="01.01.2025",
            leistungszeitraum_bis="31.12.2025",
            abwesenheitszeiten="3 Wochen Reha",
            bezugsbetreuer="Michael Begleiter",
        ),
        developments="Erika zeigt deutliche Stabilisierung im Alltag.",
        developments_needs_review=False,
        sections=[
            ReportSection(
                id="1",
                title="Psychische Verfassung / Selbstsorge",
                previous_goals_apply=True,
                leitziel="Erhalt der psychischen Stabilität",
                smarte_ziele="Erika nimmt 1x wöchentlich an der Reflexion teil.\n- Regelmäßige Nachtruhe ab 22:00 Uhr.",
                bericht_zielerreichung="Die Teilnahme erfolgte verlässlich und stabil.",
                vorschlag_ziele="Eigenständiges Erkennen von Überlastungsgrenzen.",
                vorschlag_massnahmen="Wöchentliches Assistenzgespräch.\n- Begleitung bei Bedarf.",
                realisierung_andere="Psychiatrische Institutsambulanz (PIA)",
            ),
            ReportSection(
                id="2",
                title="Alltagsgestaltung / Tagesstruktur",
                previous_goals_apply=False,  # entfällt ist True!
                leitziel="",
                smarte_ziele="",
                bericht_zielerreichung="Tagesstruktur ist gefestigt.",
                vorschlag_ziele="Aufrechterhaltung der Haushaltsroutine.",
                vorschlag_massnahmen="Gemeinsames Kochen 1x monatlich.",
                realisierung_andere="",
            ),
            ReportSection(
                id="3",
                title="Gesundheit",
                previous_goals_apply=True,
                needs_review=True,
                no_entries=True,  # Guard-Fall: keine Einträge im Zeitraum
                bericht_zielerreichung="",
            ),
            ReportSection(
                id="4",
                title="Wohnen",
                previous_goals_apply=True,
                needs_review=True,
                no_entries=False,  # Technischer Review-Bedarf
                bericht_zielerreichung="Fragmentarische Dokumentation zur Wohnung.",
            ),
        ]
        + [
            ReportSection(
                id=str(i),
                title=f"Lebensbereich {i}",
                previous_goals_apply=True,
                bericht_zielerreichung=f"Dokumentation für Bereich {i}.",
            )
            for i in range(5, 10)
        ],
    )

    out_file = tmp_path / "odw_rendered.docx"
    renderer = OpDeWischDocxRenderer(TEMPLATE_DOCX)
    rendered_path = renderer.render(draft, out_file)

    assert rendered_path == out_file
    assert out_file.is_file()

    # Dokumentinhalt prüfen
    doc = docx.Document(str(out_file))
    assert len(doc.tables) >= 10

    full_ns = dict(doc.element.nsmap)
    full_ns["w14"] = "http://schemas.microsoft.com/office/word/2010/wordml"

    # --- Tabelle 0: Kopfdaten ---
    t0 = doc.tables[0]
    # R0 C2: Name, Geburtsdatum, Anschrift
    sdts_c0_2 = t0.rows[0].cells[2]._tc.xpath(".//w:sdt")
    assert "".join(sdts_c0_2[0].xpath(".//w:t/text()", namespaces=full_ns)) == "Mustermann, Erika"
    assert "".join(sdts_c0_2[1].xpath(".//w:t/text()", namespaces=full_ns)) == "12.04.1982"
    assert "".join(sdts_c0_2[2].xpath(".//w:t/text()", namespaces=full_ns)) == "Mustergasse 7"
    assert "".join(sdts_c0_2[3].xpath(".//w:t/text()", namespaces=full_ns)) == "20144 Hamburg"

    # R1 C0: Gesetzliche Betreuung (Ja ausgewählt)
    sdts_c1_0 = t0.rows[1].cells[0]._tc.xpath(".//w:sdt")
    cb_nein = sdts_c1_0[0].xpath(".//w14:checkbox/w14:checked/@w14:val", namespaces=full_ns)
    cb_ja = sdts_c1_0[1].xpath(".//w14:checkbox/w14:checked/@w14:val", namespaces=full_ns)
    assert cb_nein == ["0"]
    assert cb_ja == ["1"]

    # R2 C0: GdB & Pflegegrad
    sdts_c2_0 = t0.rows[2].cells[0]._tc.xpath(".//w:sdt")
    assert "".join(sdts_c2_0[0].xpath(".//w:t/text()", namespaces=full_ns)) == "40"
    assert sdts_c2_0[1].xpath(".//w14:checkbox/w14:checked/@w14:val", namespaces=full_ns) == ["0"]  # nicht entfällt
    assert "".join(sdts_c2_0[2].xpath(".//w:t/text()", namespaces=full_ns)) == "1"
    assert sdts_c2_0[3].xpath(".//w14:checkbox/w14:checked/@w14:val", namespaces=full_ns) == ["0"]

    # R3 C0: Leistungsart, Zeitraum, Abwesenheitszeiten
    sdts_c3_0 = t0.rows[3].cells[0]._tc.xpath(".//w:sdt")
    assert "".join(sdts_c3_0[0].xpath(".//w:t/text()", namespaces=full_ns)) == "ASP"
    assert "01.01.2025 - 31.12.2025" in "".join(sdts_c3_0[1].xpath(".//w:t/text()", namespaces=full_ns))
    assert "".join(sdts_c3_0[2].xpath(".//w:t/text()", namespaces=full_ns)) == "3 Wochen Reha"

    # R4 C0: Developments
    assert "Erika zeigt deutliche Stabilisierung" in t0.rows[4].cells[0].paragraphs[1].text

    # R5 C1: Bezugsbegleiter*in
    assert "Michael Begleiter" in t0.rows[5].cells[1].paragraphs[1].text

    # --- Tabelle 1: Psychische Verfassung ---
    t1 = doc.tables[1]
    # Checkbox 'entfällt' muss 0 sein
    cb_t1 = t1.rows[1].cells[0]._tc.xpath(".//w:sdt")[0].xpath(".//w14:checkbox/w14:checked/@w14:val", namespaces=full_ns)
    assert cb_t1 == ["0"]
    assert "Erhalt der psychischen Stabilität" in t1.rows[1].cells[0].paragraphs[2].text
    # S.M.A.R.T. Ziele mit Bullets: je Punkt ein eigener Absatz
    paras = t1.rows[1].cells[0].paragraphs
    assert paras[4].text == "• Erika nimmt 1x wöchentlich an der Reflexion teil."
    assert paras[5].text == "• Regelmäßige Nachtruhe ab 22:00 Uhr."
    # Bericht
    assert "Die Teilnahme erfolgte verlässlich und stabil." in t1.rows[2].cells[0].paragraphs[1].text
    # Vorschlag Ziele & Maßnahmen
    assert "Eigenständiges Erkennen" in t1.rows[3].cells[0].paragraphs[1].text
    assert "Wöchentliches Assistenzgespräch" in t1.rows[3].cells[1].paragraphs[1].text
    # Realisierung Andere
    assert "Psychiatrische Institutsambulanz" in t1.rows[4].cells[0].text

    # --- Tabelle 2: Alltagsgestaltung (entfällt = True) ---
    t2 = doc.tables[2]
    cb_t2 = t2.rows[1].cells[0]._tc.xpath(".//w:sdt")[0].xpath(".//w14:checkbox/w14:checked/@w14:val", namespaces=full_ns)
    assert cb_t2 == ["1"]  # entfällt ist angekreuzt!

    # --- Tabelle 3: Guard-Fall (keine Einträge) ---
    t3 = doc.tables[3]
    assert NO_ENTRIES_HINT in t3.rows[2].cells[0].paragraphs[1].text

    # --- Tabelle 4: Review-Marker ---
    t4 = doc.tables[4]
    assert REVIEW_MARKER in t4.rows[2].cells[0].paragraphs[1].text


def _bullet_draft(**section_kwargs) -> ReportDraft:
    return ReportDraft(
        client=ClientData(name="Musterfrau", vorname="Fiktiva"),
        meta=ReportMeta(),
        sections=[
            ReportSection(id=str(i), title=f"Bereich {i}", bericht_zielerreichung="Text.", **(section_kwargs if i == 1 else {}))
            for i in range(1, 10)
        ],
    )


def test_bullets_rendered_as_separate_paragraphs(tmp_path):
    """Jeder Aufzählungspunkt bekommt einen eigenen Absatz – auch bei einzeiliger Eingabe „a • b“."""
    draft = _bullet_draft(
        smarte_ziele="- Ziel eins\n- Ziel zwei",
        vorschlag_ziele="• Neues Ziel A • Neues Ziel B • Neues Ziel C",
        vorschlag_massnahmen="- Maßnahme 1\n- Maßnahme 2",
    )
    out = tmp_path / "odw_bullets.docx"
    OpDeWischDocxRenderer(TEMPLATE_DOCX).render(draft, out)
    doc = docx.Document(str(out))
    t1 = doc.tables[1]

    smart = [p.text for p in t1.rows[1].cells[0].paragraphs[4:6]]
    assert smart == ["• Ziel eins", "• Ziel zwei"]

    ziele = [p.text for p in t1.rows[3].cells[0].paragraphs if p.text.startswith("•")]
    assert ziele == ["• Neues Ziel A", "• Neues Ziel B", "• Neues Ziel C"]

    massn = [p.text for p in t1.rows[3].cells[1].paragraphs if p.text.startswith("•")]
    assert massn == ["• Maßnahme 1", "• Maßnahme 2"]

    # Kein Absatz enthält mehr als einen Aufzählungspunkt / keinen Zeilenumbruch-Trick
    for cell in (t1.rows[1].cells[0], t1.rows[3].cells[0], t1.rows[3].cells[1]):
        for p in cell.paragraphs:
            assert p.text.count("•") <= 1, p.text
            assert "\n" not in p.text

    # Kopierte Absätze übernehmen die Formatierung, aber keine doppelten w14:paraIds
    w14 = "{http://schemas.microsoft.com/office/word/2010/wordml}paraId"
    ids = [el.get(w14) for el in doc.element.iter() if el.get(w14)]
    assert len(ids) == len(set(ids))
    first, second = t1.rows[3].cells[0].paragraphs[1], t1.rows[3].cells[0].paragraphs[2]
    assert first.style.name == second.style.name


def test_bullet_without_list_keeps_plain_text(tmp_path):
    draft = _bullet_draft(vorschlag_ziele="Ein einzelnes Ziel ohne Aufzählung.")
    out = tmp_path / "odw_plain.docx"
    OpDeWischDocxRenderer(TEMPLATE_DOCX).render(draft, out)
    t1 = docx.Document(str(out)).tables[1]
    assert t1.rows[3].cells[0].paragraphs[1].text == "• Ein einzelnes Ziel ohne Aufzählung."
