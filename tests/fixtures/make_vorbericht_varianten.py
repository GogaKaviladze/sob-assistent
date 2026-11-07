"""Synthetischer Fixture-Builder für DOCX-Vorbericht-Formatvarianten.

Erzeugt im RAM oder auf Festplatte reproduzierbare synthetische Testberichte
mit unterschiedlichen Layout- und Formatierungs-Merkmalen auf Basis des
synthetischen Falls "Hans Peter".

WICHTIG:
Alle enthaltenen Daten sind 100% synthetische Testdaten ohne echten Personenbezug.
"""
from __future__ import annotations

import argparse
import io
from pathlib import Path
import sys
from typing import Dict, List, Optional

import docx
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sob_generator.models import ClientData, ReportDraft, ReportMeta, ReportSection
from sob_generator.renderers.op_de_wisch_renderer import OpDeWischDocxRenderer
from sob_generator.llm.sections_op_de_wisch import OP_DE_WISCH_SECTION_DEFINITIONS
from sob_generator.llm.sections import SECTION_DEFINITIONS
from sob_generator import docx_fields as fields

FIXTURES_DIR = Path(__file__).resolve().parent / "vorbericht_varianten"
ODW_TEMPLATE_PATH = ROOT_DIR / "templates" / "hamburg" / "OpDerWisch" / "FO_SVB_Berichtsvorlage_2026.docx"
HAMBURG_TEMPLATE_PATH = ROOT_DIR / "templates" / "hamburg" / "Vorlage.docx"

SYNTH_HEADER_TEXT = "SYNTHETISCHE TESTDATEN - KEINE RECHTEN PERSONENBEZOGENEN DATEN"

# Synthetische Modelldaten Hans Peter (Op de Wisch Struktur: Bereiche 1–9)
ODW_HANS_PETER_SECTIONS = [
    {
        "id": "1",
        "title": "Psychische Verfassung / Selbstsorge",
        "applies": True,
        "leitziel": "Herr Peter geht achtsam mit Belastungen aus Alltag und Arbeit um.",
        "smarte_ziele": "• Herr Peter benennt belastende Situationen und sucht bei Bedarf das Gespräch mit der Assistenz.",
        "bericht": "Über den Berichtszeitraum hinweg hat Herr Peter wiederholt Belastungen benannt. Beim zweiten Arbeitskonflikt zeigte er sich bereits deutlich gelassener.",
        "vorschlag": "• Herr Peter wendet Strategien zur Einordnung von Konflikten eigenständig an.",
        "massnahmen": "Einzelgespräche zur Reflexion von Belastungsmomenten bei Bedarf.",
    },
    {
        "id": "2",
        "title": "Alltagsgestaltung /Tagesstruktur",
        "applies": True,
        "leitziel": "Herr Peter führt seinen Haushalt zunehmend selbständig.",
        "smarte_ziele": "• Herr Peter erledigt Reinigungs- und Wäscheaufgaben anhand einer eigenen Checkliste.\n• Herr Peter bereitet einfache warme Mahlzeiten selbständig zu.",
        "bericht": "Herr Peter nutzt die Aufgabenliste an der Pinnwand mittlerweile zuverlässig zur Strukturierung des Haushalts. Reinigungsaufgaben und Kochen gelingen weitgehend selbständig.",
        "vorschlag": "• Herr Peter übernimmt das Bügeln zunehmend selbständig.",
        "massnahmen": "Gemeinsame Überprüfung der Aufgabenliste und Übungseinheiten Haushaltsführung.",
    },
    {
        "id": "3",
        "title": "Gesundheit",
        "applies": True,
        "leitziel": "Herr Peter nimmt notwendige gesundheitsbezogene Termine wahr und organisiert sie zunehmend eigenständig.",
        "smarte_ziele": "• Herr Peter erhält und nutzt einen Physiotherapie-Termin trotz Hindernissen bei der Terminvergabe.",
        "bericht": "Am 22.04.2025 konnte der erste tatsächliche Termin wahrgenommen werden; laut Kontrolltermin hat die Physiotherapie spürbar geholfen.",
        "vorschlag": "• Herr Peter kümmert sich künftig frühzeitiger um Folgetermine, um Wartezeiten zu vermeiden.",
        "massnahmen": "Unterstützung bei der telefonischen Terminvereinbarung bei Bedarf.",
    },
    {
        "id": "4",
        "title": "Wohnen",
        "applies": False,
        "leitziel": "",
        "smarte_ziele": "",
        "bericht": "Die Wohnsituation ist stabil und unauffällig. Es wurden keine gesonderten Ziele vereinbart.",
        "vorschlag": "",
        "massnahmen": "",
    },
    {
        "id": "5",
        "title": "Finanzen / Institutionen",
        "applies": True,
        "leitziel": "Herr Peter tritt administrative Anliegen zunehmend selbständig an.",
        "smarte_ziele": "• Herr Peter bereitet Behördentermine mit eigenen Unterlagen bzw. Stichpunkten vor.",
        "bericht": "Bei der Begleitung zum Sozialamt zur Verlängerung der Eingliederungshilfe konnte Herr Peter sein Anliegen anhand vorbereiteter Unterlagen verständlich vortragen.",
        "vorschlag": "• Herr Peter bereitet künftige Behördentermine eigenständig vor.",
        "massnahmen": "Gemeinsame Durchsicht von Post und Behördenschreiben.",
    },
    {
        "id": "6",
        "title": "Sozialer Lebensraum",
        "applies": True,
        "leitziel": "Herr Peter baut tragfähige soziale Kontakte außerhalb der Familie auf und gestaltet seine Freizeit aktiv.",
        "smarte_ziele": "• Herr Peter nimmt Kontakt zu anderen Mitgliedern der Kegelgruppe auf.\n• Herr Peter nimmt regelmäßig am Training der Kegelgruppe teil.",
        "bericht": "Herr Peter hat einen festen Ansprechpartner in der Kegelgruppe gefunden, mit dem er sich auch außerhalb austauscht. Er nahm an der Weihnachtsfeier teil.",
        "vorschlag": "• Herr Peter übernimmt dauerhaft eine unterstützende Rolle für neue Mitglieder der Kegelgruppe.",
        "massnahmen": "Begleitung zu Gruppenaktivitäten bei Bedarf.",
    },
    {
        "id": "7",
        "title": "Arbeit / Beschäftigung / Ausbildung",
        "applies": True,
        "leitziel": "Herr Peter entwickelt seine berufliche Tätigkeit in der WfbM weiter.",
        "smarte_ziele": "• Herr Peter übernimmt eine erhöhte Arbeitszeit in der Verpackungsgruppe der WfbM.",
        "bericht": "Herr Peter fühlt sich in der Verpackungsgruppe zunehmend sicherer. Die erhöhte Arbeitszeit wurde dauerhaft übernommen.",
        "vorschlag": "• Herr Peter festigt seine Rolle in der Arbeitsgruppe und geht eigenständiger mit Konflikten um.",
        "massnahmen": "Regelmäßiger Austausch mit dem Gruppenleiter der Werkstatt.",
    },
    {
        "id": "8",
        "title": "Zusammenarbeit",
        "applies": False,
        "leitziel": "",
        "smarte_ziele": "",
        "bericht": "Die Zusammenarbeit mit der Assistenz und dem Umfeld verläuft partnerschaftlich und verlässlich.",
        "vorschlag": "",
        "massnahmen": "",
    },
    {
        "id": "9",
        "title": "Sonstiges",
        "applies": False,
        "leitziel": "",
        "smarte_ziele": "",
        "bericht": "",
        "vorschlag": "",
        "massnahmen": "",
    },
]

# Synthetische Modelldaten Hans Peter (Hamburg Struktur: 1.1–3)
HAMBURG_HANS_PETER_SECTIONS = [
    {
        "id": "1.1",
        "title": "Wohnsituation",
        "applies": False,
        "leitziel": "",
        "smarte_ziele": "",
        "bericht": "",
        "vorschlag": "",
    },
    {
        "id": "1.2",
        "title": "Alltägliche Lebensführung / Hauswirtschaft",
        "applies": True,
        "leitziel": "Herr Peter führt seinen Haushalt zunehmend selbständig.",
        "smarte_ziele": "Herr Peter erledigt Reinigungs- und Wäscheaufgaben anhand einer eigenen Checkliste.\nHerr Peter bereitet einfache warme Mahlzeiten selbständig zu.",
        "bericht": "Herr Peter nutzt eine an der Pinnwand angebrachte Aufgaben- bzw. Checkliste mittlerweile zuverlässig zur Strukturierung des Haushalts.",
        "vorschlag": "Herr Peter übernimmt das Bügeln zunehmend selbständig.\nHerr Peter kocht eine größere Mahlzeit pro Woche selbständig.",
    },
    {
        "id": "1.3",
        "title": "Basisversorgung / Grundpflege",
        "applies": False,
        "leitziel": "",
        "smarte_ziele": "",
        "bericht": "",
        "vorschlag": "",
    },
    {
        "id": "1.4",
        "title": "Emotionale und psychische Entwicklung",
        "applies": True,
        "leitziel": "Herr Peter geht achtsam mit Belastungen aus Alltag und Arbeit um.",
        "smarte_ziele": "Herr Peter benennt belastende Situationen und sucht bei Bedarf das Gespräch mit der Assistenz.",
        "bericht": "Über den Berichtszeitraum hinweg hat Herr Peter wiederholt Belastungen benannt. Beim zweiten Arbeitskonflikt zeigte er sich bereits deutlich gelassener.",
        "vorschlag": "Herr Peter wendet die im Berichtszeitraum erprobten Strategien zur Einordnung von Konfliktsituationen zunehmend eigenständig an.",
    },
    {
        "id": "1.5",
        "title": "Gesundheitsförderung und -erhaltung",
        "applies": True,
        "leitziel": "Herr Peter nimmt notwendige gesundheitsbezogene Termine wahr und organisiert sie zunehmend eigenständig.",
        "smarte_ziele": "Herr Peter erhält und nutzt einen Physiotherapie-Termin trotz Hindernissen bei der Terminvergabe.\nHerr Peter nimmt jährliche Vorsorgeuntersuchungen wahr.",
        "bericht": "Am 22.04.2025 konnte der erste tatsächliche Termin wahrgenommen werden; laut Kontrolltermin im November 2025 hat die Physiotherapie spürbar geholfen.",
        "vorschlag": "Herr Peter kümmert sich künftig frühzeitiger um Folgetermine, um erneute Wartezeiten zu vermeiden.",
    },
    {
        "id": "2.1",
        "title": "Gestaltung sozialer Beziehungen",
        "applies": True,
        "leitziel": "Herr Peter baut tragfähige soziale Kontakte außerhalb der Familie auf.",
        "smarte_ziele": "Herr Peter nimmt Kontakt zu anderen Mitgliedern der Kegelgruppe auf.",
        "bericht": "Anfangs blieb Herr Peter beim Kegeln nur Zuschauer; im Berichtszeitraum hat er nicht nur selbst mitgespielt, sondern auch einen festen Ansprechpartner gefunden.",
        "vorschlag": "Herr Peter übernimmt dauerhaft eine kleine unterstützende Rolle für neue Mitglieder der Kegelgruppe.",
    },
    {
        "id": "2.2",
        "title": "Teilnahme am kulturellen und gesellschaftlichen Leben",
        "applies": True,
        "leitziel": "Herr Peter gestaltet seine Freizeit aktiv und nimmt an einem festen Angebot teil.",
        "smarte_ziele": "Herr Peter nimmt regelmäßig am Training und an Wettkämpfen der Kegelgruppe teil.",
        "bericht": "Aus anfänglicher Zurückhaltung gegenüber dem Kegeln hat sich eine verlässliche Teilnahme entwickelt.",
        "vorschlag": "Herr Peter bleibt dauerhaft Mitglied der Kegelgruppe und nimmt weiter regelmäßig teil.",
    },
    {
        "id": "2.3",
        "title": "Kommunikation und Orientierung",
        "applies": True,
        "leitziel": "Herr Peter tritt administrative Anliegen zunehmend selbständig an.",
        "smarte_ziele": "Herr Peter bereitet Behördentermine mit eigenen Unterlagen bzw. Stichpunkten vor.",
        "bericht": "Bei der Begleitung zum Sozialamt zur Verlängerung der Eingliederungshilfe konnte Herr Peter sein Anliegen verständlich vortragen.",
        "vorschlag": "Herr Peter bereitet künftige Behördentermine eigenständig vor.",
    },
    {
        "id": "3",
        "title": "Beschäftigung, Arbeit, Ausbildung",
        "applies": True,
        "leitziel": "Herr Peter entwickelt seine berufliche Tätigkeit in der WfbM weiter.",
        "smarte_ziele": "Herr Peter übernimmt eine erhöhte Arbeitszeit in der Verpackungsgruppe der WfbM.",
        "bericht": "Herr Peter fühlt sich in der Verpackungsgruppe der WfbM zunehmend sicherer. Die erhöhte Arbeitszeit wurde dauerhaft übernommen.",
        "vorschlag": "Herr Peter festigt seine Rolle in der Verpackungsgruppe und geht eigenständiger mit Konflikten um.",
    },
]


def make_var_a_opdewisch_referenz() -> bytes:
    """Variante a: Original Op-de-Wisch-Aufbau (Referenz mit SDT-Controls)."""
    if not ODW_TEMPLATE_PATH.exists():
        raise FileNotFoundError(f"Vorlage nicht gefunden: {ODW_TEMPLATE_PATH}")

    sections = []
    for s_data in ODW_HANS_PETER_SECTIONS:
        sections.append(
            ReportSection(
                id=s_data["id"],
                title=s_data["title"],
                previous_goals_apply=s_data["applies"],
                leitziel=s_data["leitziel"],
                smarte_ziele=s_data["smarte_ziele"],
                bericht_zielerreichung=s_data["bericht"],
                vorschlag_ziele=s_data["vorschlag"],
                vorschlag_massnahmen=s_data.get("massnahmen", ""),
                realisierung_andere="",
            )
        )

    draft = ReportDraft(
        client=ClientData(
            name="Peter",
            vorname="Hans",
            geburtsdatum="12.05.1988",
            anschrift="Synthetischer Weg 12, 20095 Hamburg",
            gesetzliche_betreuung=False,
            gdb="50",
            pflegegrad="Kein Pflegegrad",
            leistungsart="ASP",
        ),
        meta=ReportMeta(
            leistungszeitraum_von="01.07.2024",
            leistungszeitraum_bis="30.06.2026",
            abwesenheitszeiten="Keine längeren Ausfallzeiten.",
            bezugsbetreuer="Synthetische Bezugsassistenz",
        ),
        developments="Wesentliche Entwicklungen im Berichtszeitraum: SYNTHETISCHE TESTDATEN.",
        sections=sections,
    )

    buf = io.BytesIO()
    renderer = OpDeWischDocxRenderer(ODW_TEMPLATE_PATH)
    renderer.render(draft, buf)
    buf.seek(0)

    # Prüf-Marker als Paragraphen einfügen
    doc = docx.Document(buf)
    p = doc.paragraphs[0].insert_paragraph_before(f"*** {SYNTHETISCHE_TESTDATEN_BANNER} ***")
    p.bold = True
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


SYNTHETISCHE_TESTDATEN_BANNER = "SYNTHETISCHE TESTDATEN - KEINE ECHTEN PERSONENDATEN"


def make_var_b_hamburg_referenz() -> bytes:
    """Variante b: Original Hamburg-Aufbau (Referenz mit WordprocessingML-Bookmarks)."""
    if not HAMBURG_TEMPLATE_PATH.exists():
        raise FileNotFoundError(f"Vorlage nicht gefunden: {HAMBURG_TEMPLATE_PATH}")

    root = fields.load_document_tree(HAMBURG_TEMPLATE_PATH)

    try:
        fields.set_formtext(root, "client_name_vorname", "Hans Peter")
    except KeyError:
        pass
    try:
        fields.set_formtext(root, "meta_leistungszeitraum_von", "01.07.2024")
    except KeyError:
        pass
    try:
        fields.set_formtext(root, "meta_leistungszeitraum_bis", "30.06.2026")
    except KeyError:
        pass
    try:
        fields.set_formtext(root, "developments", f"{SYNTHETISCHE_TESTDATEN_BANNER}\nWesentliche Entwicklungen.")
    except KeyError:
        pass

    for defn in SECTION_DEFINITIONS:
        s_data = next((s for s in HAMBURG_HANS_PETER_SECTIONS if s["id"] == defn.id), None)
        if not s_data:
            continue
        try:
            fields.set_formtext(root, f"sections_{defn.index}_leitziel", s_data["leitziel"])
        except KeyError:
            pass
        try:
            fields.set_formtext(root, f"sections_{defn.index}_smarte_ziele", s_data["smarte_ziele"])
        except KeyError:
            pass
        try:
            fields.set_formtext(root, f"sections_{defn.index}_bericht_zielerreichung", s_data["bericht"])
        except KeyError:
            pass
        try:
            fields.set_formtext(root, f"sections_{defn.index}_vorschlag_ziele", s_data["vorschlag"])
        except KeyError:
            pass

    from lxml import etree
    import zipfile

    new_xml = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(HAMBURG_TEMPLATE_PATH, "r") as zin, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = new_xml if item.filename == "word/document.xml" else zin.read(item.filename)
            zout.writestr(item, data)

    return buf.getvalue()


def make_var_c_andere_ueberschriften() -> bytes:
    """Variante c: Andere Überschriften (z.B. 'Lebensbereich 1' statt Bereichsname, gemischte Groß-/Kleinschreibung)."""
    doc = docx.Document()
    doc.add_heading(f"{SYNTHETISCHE_TESTDATEN_BANNER} - Vorbericht mit abweichenden Überschriften", level=1)
    doc.add_paragraph("Bericht für: Peter, Hans")
    doc.add_paragraph("Leistungszeitraum: 01.07.2024 bis 30.06.2026")
    doc.add_paragraph("Wesentliche Entwicklungen im Berichtszeitraum: Synthetische Testfalldaten.")

    # 9 Tabellen mit verschiedenen Überschriftsmustern
    patterns = [
        "Lebensbereich 1: Psychische Verfassung / Selbstsorge",
        "LEBENSBEREICH 2: ALLTAGSGESTALTUNG / TAGESSTRUKTUR",
        "Bereich 3 - Gesundheit",
        "Lebensbereich 4 (Wohnen)",
        "BEREICH 5: Finanzen / Institutionen",
        "Lebensbereich 6: Sozialer Lebensraum",
        "Abschnitt 7: Arbeit / Beschäftigung / Ausbildung",
        "Lebensbereich 8: Zusammenarbeit",
        "Bereich 9: Sonstiges",
    ]

    for s_data, header in zip(ODW_HANS_PETER_SECTIONS, patterns):
        table = doc.add_table(rows=5, cols=2)
        table.style = "Table Grid"
        # Kopfzeile
        table.cell(0, 0).text = header
        table.cell(0, 1).text = ""
        # Zeile 1: Leitziel
        table.cell(1, 0).text = "Leitziel:"
        table.cell(1, 1).text = s_data["leitziel"]
        # Zeile 2: Smarte Ziele
        table.cell(2, 0).text = "Smarte Ziele:"
        table.cell(2, 1).text = s_data["smarte_ziele"]
        # Zeile 3: Bericht
        table.cell(3, 0).text = "Bericht zur Zielerreichung:"
        table.cell(3, 1).text = s_data["bericht"]
        # Zeile 4: Vorschlag
        table.cell(4, 0).text = "Vorschlag für aktuelle (smarte) Ziele:"
        table.cell(4, 1).text = s_data["vorschlag"]
        doc.add_paragraph("")  # Abstand

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def make_var_c2_nur_bereichsnamen_ohne_nummern() -> bytes:
    """Variante c2: Überschriften ganz OHNE Nummerierung (nur Text, z. B. 'Psychische Verfassung / Selbstsorge')."""
    doc = docx.Document()
    doc.add_heading(f"{SYNTHETISCHE_TESTDATEN_BANNER} - Überschriften ohne Nummern", level=1)
    doc.add_paragraph("Bericht für: Peter, Hans")
    doc.add_paragraph("Leistungszeitraum: 01.07.2024 bis 30.06.2026")

    for s_data in ODW_HANS_PETER_SECTIONS:
        table = doc.add_table(rows=5, cols=2)
        table.style = "Table Grid"
        # Kopfzeile nur der Bereichsname
        table.cell(0, 0).text = s_data["title"]
        table.cell(0, 1).text = ""
        table.cell(1, 0).text = "Leitziel:"
        table.cell(1, 1).text = s_data["leitziel"]
        table.cell(2, 0).text = "Smarte Ziele:"
        table.cell(2, 1).text = s_data["smarte_ziele"]
        table.cell(3, 0).text = "Bericht zur Zielerreichung:"
        table.cell(3, 1).text = s_data["bericht"]
        table.cell(4, 0).text = "Vorschlag für aktuelle (smarte) Ziele:"
        table.cell(4, 1).text = s_data["vorschlag"]
        doc.add_paragraph("")

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def make_var_d_fliesstext_aufzaehlung() -> bytes:
    """Variante d: Ziele als Fließtext / Aufzählung statt Tabelle."""
    doc = docx.Document()
    doc.add_heading(f"{SYNTHETISCHE_TESTDATEN_BANNER} - Fließtext und Aufzählungen", level=1)
    doc.add_paragraph("Bericht für: Peter, Hans")
    doc.add_paragraph("Leistungszeitraum: 01.07.2024 bis 30.06.2026")
    doc.add_paragraph("Wesentliche Entwicklungen im Berichtszeitraum: Synthetischer Entwicklungsbericht im Fließtext.")

    for s_data in ODW_HANS_PETER_SECTIONS:
        doc.add_heading(f"{s_data['id']}. {s_data['title']}", level=2)
        if s_data["leitziel"]:
            doc.add_paragraph(f"Leitziel: {s_data['leitziel']}")
        else:
            doc.add_paragraph("Leitziel: (entfällt)")

        if s_data["smarte_ziele"]:
            doc.add_paragraph("Smarte Ziele:")
            for line in s_data["smarte_ziele"].splitlines():
                clean_line = line.lstrip("•-* ").strip()
                if clean_line:
                    doc.add_paragraph(clean_line, style="List Bullet")
        else:
            doc.add_paragraph("Smarte Ziele: (keine Ziele vereinbart)")

        if s_data["bericht"]:
            doc.add_paragraph(f"Bericht zur Zielerreichung: {s_data['bericht']}")

        if s_data["vorschlag"]:
            doc.add_paragraph(f"Vorschlag für aktuelle Ziele: {s_data['vorschlag']}")

        doc.add_paragraph("")

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def make_var_e_bereiche_andere_reihenfolge() -> bytes:
    """Variante e: Bereiche in anderer Reihenfolge (z.B. 7, 3, 1, 5, 2, 6, 4, 8, 9)."""
    doc = docx.Document()
    doc.add_heading(f"{SYNTHETISCHE_TESTDATEN_BANNER} - Bereiche in anderer Reihenfolge", level=1)
    doc.add_paragraph("Bericht für: Peter, Hans")
    doc.add_paragraph("Leistungszeitraum: 01.07.2024 bis 30.06.2026")

    # Geänderte Reihenfolge: 7, 3, 1, 5, 2, 6, 4, 8, 9
    shuffled_order = ["7", "3", "1", "5", "2", "6", "4", "8", "9"]
    sections_by_id = {s["id"]: s for s in ODW_HANS_PETER_SECTIONS}

    for sec_id in shuffled_order:
        s_data = sections_by_id[sec_id]
        table = doc.add_table(rows=5, cols=2)
        table.style = "Table Grid"
        table.cell(0, 0).text = f"{s_data['id']}. {s_data['title']}"
        table.cell(0, 1).text = ""
        table.cell(1, 0).text = "Leitziel:"
        table.cell(1, 1).text = s_data["leitziel"]
        table.cell(2, 0).text = "Smarte Ziele:"
        table.cell(2, 1).text = s_data["smarte_ziele"]
        table.cell(3, 0).text = "Bericht zur Zielerreichung:"
        table.cell(3, 1).text = s_data["bericht"]
        table.cell(4, 0).text = "Vorschlag für aktuelle (smarte) Ziele:"
        table.cell(4, 1).text = s_data["vorschlag"]
        doc.add_paragraph("")

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def make_var_f_fremde_abschnitte() -> bytes:
    """Variante f: Zusätzliche fremde Abschnitte (Deckblatt, Fußnoten, leere Tabellen)."""
    doc = docx.Document()

    # 1. Deckblatt
    doc.add_heading("Sozialpädagogischer Entwicklungsbericht", level=1)
    doc.add_paragraph(f"{SYNTHETISCHE_TESTDATEN_BANNER}")
    doc.add_paragraph("Erstellt von: Fachdienst Eingliederungshilfe")
    doc.add_paragraph("Aktenzeichen: SYN-2025-99881")

    # Leere Tabelle auf Deckblatt
    deck_table = doc.add_table(rows=2, cols=2)
    deck_table.style = "Table Grid"
    deck_table.cell(0, 0).text = "Klientenstatus"
    deck_table.cell(0, 1).text = "Aktiv"
    deck_table.cell(1, 0).text = "Erstgespräch"
    deck_table.cell(1, 1).text = "15.01.2024"

    doc.add_page_break()

    # Stammdaten
    doc.add_paragraph("Bericht für: Peter, Hans")
    doc.add_paragraph("Leistungszeitraum: 01.07.2024 bis 30.06.2026")

    # Leere Trenn-Tabelle
    doc.add_paragraph("Übersicht der Fachleistungsstunden:")
    doc.add_table(rows=1, cols=3)

    # Die regulären 9 Bereiche
    for s_data in ODW_HANS_PETER_SECTIONS:
        table = doc.add_table(rows=5, cols=2)
        table.style = "Table Grid"
        table.cell(0, 0).text = f"{s_data['id']}. {s_data['title']}"
        table.cell(0, 1).text = ""
        table.cell(1, 0).text = "Leitziel:"
        table.cell(1, 1).text = s_data["leitziel"]
        table.cell(2, 0).text = "Smarte Ziele:"
        table.cell(2, 1).text = s_data["smarte_ziele"]
        table.cell(3, 0).text = "Bericht zur Zielerreichung:"
        table.cell(3, 1).text = s_data["bericht"]
        table.cell(4, 0).text = "Vorschlag für aktuelle (smarte) Ziele:"
        table.cell(4, 1).text = s_data["vorschlag"]
        doc.add_paragraph("")

    # Anhang mit Unterschriftentabelle und Fußnoten
    doc.add_page_break()
    doc.add_heading("Anhang & Freigabe", level=2)
    doc.add_paragraph("Hinweis: Bericht wurde gemeinsam mit dem Klienten erörtert.")
    doc.add_paragraph("Fußnote 1: Zielvereinbarungen basieren auf dem HMB-T-Verfahren.")

    sign_table = doc.add_table(rows=2, cols=2)
    sign_table.style = "Table Grid"
    sign_table.cell(0, 0).text = "Ort, Datum:"
    sign_table.cell(0, 1).text = "Hamburg, den 30.06.2026"
    sign_table.cell(1, 0).text = "Unterschrift Klient"
    sign_table.cell(1, 1).text = "Unterschrift Bezugsassistenz"

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def make_var_g_verbundene_verschachtelte_tabellen() -> bytes:
    """Variante g: Verbundene Tabellenzellen und verschachtelte Tabellen."""
    doc = docx.Document()
    doc.add_heading(f"{SYNTHETISCHE_TESTDATEN_BANNER} - Verbundene & verschachtelte Tabellen", level=1)
    doc.add_paragraph("Bericht für: Peter, Hans")
    doc.add_paragraph("Leistungszeitraum: 01.07.2024 bis 30.06.2026")

    for s_data in ODW_HANS_PETER_SECTIONS:
        # Tabelle mit verbundener Kopfzeile (colspan=2)
        table = doc.add_table(rows=5, cols=2)
        table.style = "Table Grid"

        # Zelle (0,0) und (0,1) mergen
        table.cell(0, 0).merge(table.cell(0, 1))
        table.cell(0, 0).text = f"{s_data['id']}. {s_data['title']}"

        # Leitziel
        table.cell(1, 0).text = "Leitziel:"
        table.cell(1, 1).text = s_data["leitziel"]

        # Smarte Ziele: Verschachtelte Tabelle in Zelle (2, 1)
        table.cell(2, 0).text = "Smarte Ziele:"
        if s_data["smarte_ziele"]:
            lines = [l.strip() for l in s_data["smarte_ziele"].splitlines() if l.strip()]
            nested = table.cell(2, 1).add_table(rows=len(lines), cols=1)
            for idx, line in enumerate(lines):
                nested.cell(idx, 0).text = line
        else:
            table.cell(2, 1).text = ""

        # Bericht
        table.cell(3, 0).text = "Bericht zur Zielerreichung:"
        table.cell(3, 1).text = s_data["bericht"]

        # Vorschlag
        table.cell(4, 0).text = "Vorschlag für aktuelle (smarte) Ziele:"
        table.cell(4, 1).text = s_data["vorschlag"]

        doc.add_paragraph("")

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def make_var_h_fehlende_bereiche_3_von_9() -> bytes:
    """Variante h: Fehlende Bereiche (nur 3 von 9 Bereichen vorhanden)."""
    doc = docx.Document()
    doc.add_heading(f"{SYNTHETISCHE_TESTDATEN_BANNER} - Unvollständig (3 von 9)", level=1)
    doc.add_paragraph("Bericht für: Peter, Hans")
    doc.add_paragraph("Leistungszeitraum: 01.07.2024 bis 30.06.2026")

    # Nur Bereiche 1, 2 und 3
    for s_data in ODW_HANS_PETER_SECTIONS[:3]:
        table = doc.add_table(rows=5, cols=2)
        table.style = "Table Grid"
        table.cell(0, 0).text = f"{s_data['id']}. {s_data['title']}"
        table.cell(0, 1).text = ""
        table.cell(1, 0).text = "Leitziel:"
        table.cell(1, 1).text = s_data["leitziel"]
        table.cell(2, 0).text = "Smarte Ziele:"
        table.cell(2, 1).text = s_data["smarte_ziele"]
        table.cell(3, 0).text = "Bericht zur Zielerreichung:"
        table.cell(3, 1).text = s_data["bericht"]
        table.cell(4, 0).text = "Vorschlag für aktuelle (smarte) Ziele:"
        table.cell(4, 1).text = s_data["vorschlag"]
        doc.add_paragraph("")

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def make_var_i_leeres_fast_leeres_dokument() -> bytes:
    """Variante i: Fast leeres Dokument (nur Metadaten, keine Lebensbereiche/Ziele)."""
    doc = docx.Document()
    doc.add_heading(f"{SYNTHETISCHE_TESTDATEN_BANNER} - Leerer Vorbericht", level=1)
    doc.add_paragraph("Bericht für: Peter, Hans")
    doc.add_paragraph("Leistungszeitraum: 01.07.2024 bis 30.06.2026")
    doc.add_paragraph("Dieses Dokument enthält absichtlich keine Lebensbereiche oder Zielvereinbarungen.")

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


VARIANTS = {
    "a_opdewisch_referenz.docx": make_var_a_opdewisch_referenz,
    "b_hamburg_referenz.docx": make_var_b_hamburg_referenz,
    "c_andere_ueberschriften.docx": make_var_c_andere_ueberschriften,
    "c2_nur_bereichsnamen_ohne_nummern.docx": make_var_c2_nur_bereichsnamen_ohne_nummern,
    "d_fliesstext_aufzaehlung.docx": make_var_d_fliesstext_aufzaehlung,
    "e_bereiche_andere_reihenfolge.docx": make_var_e_bereiche_andere_reihenfolge,
    "f_fremde_abschnitte_deckblatt_fussnoten.docx": make_var_f_fremde_abschnitte,
    "g_verbundene_verschachtelte_tabellen.docx": make_var_g_verbundene_verschachtelte_tabellen,
    "h_fehlende_bereiche_3_von_9.docx": make_var_h_fehlende_bereiche_3_von_9,
    "i_leeres_fast_leeres_dokument.docx": make_var_i_leeres_fast_leeres_dokument,
}


def generate_all_fixtures(target_dir: Optional[Path] = None) -> Dict[str, Path]:
    """Generiert alle synthetischen Test-Fixtures auf Festplatte."""
    out_dir = target_dir or FIXTURES_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    generated = {}
    for filename, builder in VARIANTS.items():
        file_path = out_dir / filename
        file_bytes = builder()
        file_path.write_bytes(file_bytes)
        generated[filename] = file_path
    return generated


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generiert synthetische Vorbericht-Varianten.")
    parser.add_argument("--out-dir", type=Path, default=FIXTURES_DIR, help="Ausgabeverzeichnis")
    args = parser.parse_args()

    files = generate_all_fixtures(args.out_dir)
    print(f"Erfolgreich {len(files)} Fixture-Dateien generiert in: {args.out_dir}")
    for name, p in files.items():
        print(f" - {name} ({p.stat().st_size} Bytes)")
