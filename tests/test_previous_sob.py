"""Einlesen der Ziele aus einem vorherigen SOB (Feature A, ADR-4-Erweiterung).

Der Reader wird gegen eine zur Testzeit befüllte Kopie der committeten Vorlage
geprüft — keine Abhängigkeit von (gitignoriertem) dummy/-Material.
"""
import io
from pathlib import Path

import pytest

from sob_generator import docx_fields as fields
from sob_generator.llm.generator import GenerationResult
from sob_generator.llm.schemas import DevelopmentsLLMOutput, SectionLLMOutput
from sob_generator.llm.sections import SECTION_DEFINITIONS
from sob_generator.llm.sections_op_de_wisch import OP_DE_WISCH_SECTION_DEFINITIONS
from sob_generator.importer import VivendiImport
from sob_generator.models import ClientData, ReportDraft, ReportMeta, ReportSection
from sob_generator.orchestrator import build_draft
from sob_generator.previous_sob import (
    PlanSectionGoal,
    PreviousGoals,
    PreviousPlanData,
    UniversalPlanImporter,
    read_previous_goals,
    read_previous_plan,
    remap_previous_goals,
)
from sob_generator.renderers.op_de_wisch_renderer import OpDeWischDocxRenderer
from chat_app.plan_loader import parse_plan_docx

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DOCX = ROOT / "templates" / "hamburg" / "Vorlage.docx"
ODW_TEMPLATE_DOCX = ROOT / "templates" / "hamburg" / "OpDerWisch" / "FO_SVB_Berichtsvorlage_2026.docx"


def _filled_sob(tmp_path) -> Path:
    """Kopie der Vorlage mit Zielen in Abschnitt 1.2 (index 1); Rest leer."""
    root = fields.load_document_tree(TEMPLATE_DOCX)
    fields.set_formtext(root, "sections_1_leitziel", "Führt den Haushalt selbstständig.")
    fields.set_formtext(root, "sections_1_smarte_ziele", "Holt Infos über Aufgaben ein.")
    out = tmp_path / "alt_sob.docx"
    fields.save_document_tree(TEMPLATE_DOCX, root, out)
    return out


def test_read_previous_goals_extrahiert_gesetzte_ziele(tmp_path):
    goals = read_previous_goals(_filled_sob(tmp_path))

    assert goals["1.2"].applies
    assert "Haushalt" in goals["1.2"].leitziel
    assert "Aufgaben" in goals["1.2"].smarte_ziele


def test_read_previous_goals_leere_abschnitte_gelten_als_entfaellt(tmp_path):
    goals = read_previous_goals(_filled_sob(tmp_path))

    # Nie gesetzte Felder (nur Platzhalter-Spaces) → keine Ziele vereinbart.
    assert not goals["1.1"].applies
    assert goals["1.1"].leitziel == ""
    assert goals["1.1"].smarte_ziele == ""


class _SpyGenerator:
    """Zeichnet die an generate_section übergebenen Ziel-Argumente auf."""

    def __init__(self):
        self.section_calls = {}
        self.developments_summary = None

    def classify_entry(self, entry, sections):
        # Testeinträge nutzen die Abschnitts-ID direkt als Kategorie.
        return next((s.id for s in sections if s.id == entry.kategorie), sections[0].id)

    def generate_section(self, section, entries, leitziel="", smarte_ziele="",
                         previous_goals_apply=True, freitext="", anrede="", nachname="",
                         vorname="", forbidden_openers=()):
        self.section_calls[section.id] = (leitziel, smarte_ziele, previous_goals_apply)
        return GenerationResult(
            value=SectionLLMOutput(
                bericht_zielerreichung="x", vorschlag_ziele="z",
                vorschlag_massnahmen="m", realisierung_andere="",
            ),
            needs_review=False, raw_output="",
        )

    def generate_developments(self, entries, previous_goals_summary="", freitext="",
                              anrede="", nachname="", vorname=""):
        self.developments_summary = previous_goals_summary
        return GenerationResult(
            value=DevelopmentsLLMOutput(developments="y"),
            needs_review=False, raw_output="",
        )


def test_build_draft_reicht_vorherige_ziele_durch_und_uebernimmt_sie(tmp_path):
    previous = read_previous_goals(_filled_sob(tmp_path))
    spy = _SpyGenerator()
    # Eintrag für 1.2 damit der Guard nicht greift (kein Eintrag → kein LLM-Call).
    from sob_generator.llm.schemas import DocumentationEntry
    imported = VivendiImport(
        client=ClientData(),
        meta=ReportMeta(),
        entries=[
            DocumentationEntry(datum="01.01.2026", kategorie="1.2", text="Hausarbeit.")
        ],
    )

    draft = build_draft(imported, spy, SECTION_DEFINITIONS, previous_goals=previous)

    # 1.2 wird mit den bisherigen Zielen aufgerufen ...
    leitziel, smarte, applies = spy.section_calls["1.2"]
    assert applies and "Haushalt" in leitziel and "Aufgaben" in smarte
    # ... und im neuen Bericht übernommen.
    section_12 = next(s for s in draft.sections if s.id == "1.2")
    assert "Haushalt" in section_12.leitziel
    assert section_12.previous_goals_apply

    # 1.1 hat keine Einträge → Guard greift → kein LLM-Call, aber Ziele im Draft.
    assert "1.1" not in spy.section_calls
    section_11 = next(s for s in draft.sections if s.id == "1.1")
    assert not section_11.previous_goals_apply
    assert section_11.leitziel == ""
    # developments bekommt einen Leitziel-Auszug.
    assert "Haushalt" in spy.developments_summary


def test_remap_previous_goals_hamburg_to_op_de_wisch():
    hamburg_goals = {
        "1.1": PreviousGoals(leitziel="", smarte_ziele="", applies=False),
        "1.2": PreviousGoals(leitziel="Haushalt selbständig", smarte_ziele="Putzplan", applies=True),
        "1.5": PreviousGoals(leitziel="Arzttermine wahrnehmen", smarte_ziele="Physio nutzen", applies=True),
        "3": PreviousGoals(leitziel="WfbM Arbeit", smarte_ziele="Mehr Stunden", applies=True),
    }

    remapped = remap_previous_goals(hamburg_goals, OP_DE_WISCH_SECTION_DEFINITIONS)

    # 1.2 -> Op de Wisch 2 (Alltagsgestaltung)
    assert remapped["2"].applies
    assert remapped["2"].leitziel == "Haushalt selbständig"

    # 1.5 -> Op de Wisch 3 (Gesundheit)
    assert remapped["3"].applies
    assert remapped["3"].leitziel == "Arzttermine wahrnehmen"

    # 3 (Hamburg Arbeit) -> Op de Wisch 7 (Arbeit / Beschäftigung), NICHT Op de Wisch 3 (Gesundheit)!
    assert remapped["7"].applies
    assert remapped["7"].leitziel == "WfbM Arbeit"
    for source_id, target_id in (("1.2", "2"), ("1.5", "3"), ("3", "7")):
        assert remapped[target_id] == hamburg_goals[source_id]

    # Nicht belegte Abschnitte gelten als applies=False
    assert not remapped["1"].applies
    assert not remapped["8"].applies


@pytest.mark.parametrize("first_id,second_id,target_id", [("1.3", "1.4", "1"), ("2.1", "2.2", "6")])
def test_remap_previous_goals_preserves_both_sources_in_template_order(first_id, second_id, target_id):
    first = PreviousGoals("Erstes Leitziel", "Erste SMART-Maßnahme", True)
    second = PreviousGoals("Zweites Leitziel", "Zweite SMART-Maßnahme", True)
    source = {second_id: second, first_id: first}

    remapped = remap_previous_goals(source, OP_DE_WISCH_SECTION_DEFINITIONS)
    merged = remapped[target_id]

    assert merged.applies
    for text, first_value, second_value in (
        (merged.leitziel, first.leitziel, second.leitziel),
        (merged.smarte_ziele, first.smarte_ziele, second.smarte_ziele),
    ):
        assert first_value in text and second_value in text
        assert text.index(first_value) < text.index(second_value)
        assert first_id in text and second_id in text
        for section in SECTION_DEFINITIONS:
            if section.id in (first_id, second_id):
                assert section.title in text
    assert remapped == remap_previous_goals(dict(reversed(list(source.items()))), OP_DE_WISCH_SECTION_DEFINITIONS)
    assert list(remapped) == [section.id for section in OP_DE_WISCH_SECTION_DEFINITIONS]
    assert source == {second_id: second, first_id: first}


def test_remap_previous_goals_preserves_complementary_goal_and_action_with_sources():
    source = {
        "1.3": PreviousGoals("Regelmäßig essen", "", True),
        "1.4": PreviousGoals("", "Wöchentlich Entspannung üben", True),
    }

    merged = remap_previous_goals(source, OP_DE_WISCH_SECTION_DEFINITIONS)["1"]

    assert merged.applies
    assert "Regelmäßig essen" in merged.leitziel and "1.3" in merged.leitziel
    assert "1.4" not in merged.leitziel
    assert "Wöchentlich Entspannung üben" in merged.smarte_ziele and "1.4" in merged.smarte_ziele
    assert "1.3" not in merged.smarte_ziele


@pytest.mark.parametrize("ignored_id,kept_id", [("1.3", "1.4"), ("1.4", "1.3")])
@pytest.mark.parametrize("ignored", [
    PreviousGoals("Nicht vereinbart", "Nicht vereinbarte Maßnahme", False),
    PreviousGoals("", "", False),
    PreviousGoals("\u2002\u200b", " \n\u3000", True),
])
def test_remap_previous_goals_ignores_empty_and_non_applicable_sources(ignored_id, kept_id, ignored):
    kept = PreviousGoals("Vereinbartes Leitziel", "Vereinbarte Maßnahme", True)

    remapped = remap_previous_goals(
        {ignored_id: ignored, kept_id: kept}, OP_DE_WISCH_SECTION_DEFINITIONS,
    )

    assert remapped["1"] == kept


def test_remap_previous_goals_all_empty_sources_are_not_applicable():
    remapped = remap_previous_goals(
        {"1.3": PreviousGoals("\u2002", "\u200b", True), "1.4": PreviousGoals("", "", False)},
        OP_DE_WISCH_SECTION_DEFINITIONS,
    )

    assert all(goal == PreviousGoals("", "", False) for goal in remapped.values())


@pytest.mark.parametrize("source,target_sections", [
    ({}, OP_DE_WISCH_SECTION_DEFINITIONS),
    ({"1.3": PreviousGoals("Leitziel", "Maßnahme", True)}, SECTION_DEFINITIONS),
    ({"1": PreviousGoals("Leitziel", "Maßnahme", True)}, OP_DE_WISCH_SECTION_DEFINITIONS),
])
def test_remap_previous_goals_leaves_matching_template_and_empty_input_unchanged(source, target_sections):
    assert remap_previous_goals(source, target_sections) is source


def test_plan_section_goal_model():
    goal = PlanSectionGoal(
        section_id="1.2",
        title="Alltägliche Lebensführung",
        leitziel="Haushalt führen",
        smarte_ziele="Putzplan einhalten",
        stand_zielerreichung="Klappt gut",
        vorschlag_ziele="Selbständig kochen",
        applies=True,
    )
    assert goal.section_id == "1.2"
    assert goal.title == "Alltägliche Lebensführung"
    assert goal.leitziel == "Haushalt führen"
    assert goal.smarte_ziele == "Putzplan einhalten"
    assert goal.stand_zielerreichung == "Klappt gut"
    assert goal.vorschlag_ziele == "Selbständig kochen"
    assert goal.applies is True

    # Defaults
    minimal = PlanSectionGoal(section_id="4")
    assert minimal.title == ""
    assert minimal.vorschlag_ziele == ""
    assert minimal.applies is True


def test_previous_plan_data_to_compact_context_structure():
    plan = PreviousPlanData(
        client_name="Max Mustermann",
        zeitraum="01.01.2025 bis 31.12.2025",
        developments="Klient ist in eine eigene Wohnung umgezogen.",
        sections={
            "1.1": PlanSectionGoal(
                section_id="1.1",
                title="Wohnsituation",
                leitziel="Eigene Wohnung halten",
                smarte_ziele="Miete pünktlich überweisen",
                stand_zielerreichung="Miete wurde stets pünktlich gezahlt.",
                applies=True,
            ),
            "1.2": PlanSectionGoal(
                section_id="1.2",
                title="Hauswirtschaft",
                leitziel="",
                smarte_ziele="",
                stand_zielerreichung="",
                applies=False,
            ),
        },
    )

    compact = plan.to_compact_context(max_tokens=500)
    assert "Klient: Max Mustermann" in compact
    assert "Zeitraum: 01.01.2025 bis 31.12.2025" in compact
    assert "Entwicklungen: Klient ist in eine eigene Wohnung umgezogen." in compact
    assert "[1.1 Wohnsituation]" in compact
    assert "Leitziel: Eigene Wohnung halten" in compact
    assert "SMART: Miete pünktlich überweisen" in compact
    assert "Stand: Miete wurde stets pünktlich gezahlt." in compact
    # Inaktiver Abschnitt 1.2 darf nicht auftauchen
    assert "[1.2" not in compact
    assert len(compact) < 2500


def test_to_compact_context_respects_token_budget_on_large_texts():
    long_stand = "Herr Mustermann hat große Fortschritte erzielt. " * 40
    long_dev = "Es gab viele signifikante Meilensteine und Veränderungen. " * 20
    sections = {
        f"sec_{i}": PlanSectionGoal(
            section_id=f"sec_{i}",
            title=f"Abschnitt {i}",
            leitziel=f"Leitziel {i} aktiv verfolgen.",
            smarte_ziele=f"Smarte Maßnahme {i} im Alltag.",
            stand_zielerreichung=long_stand,
            applies=True,
        )
        for i in range(1, 10)
    }
    plan = PreviousPlanData(
        client_name="Test Person",
        zeitraum="01.01.2025 bis 31.12.2026",
        developments=long_dev,
        sections=sections,
    )

    compact = plan.to_compact_context(max_tokens=500)
    assert len(compact) < 2500
    for i in range(1, 10):
        assert f"[sec_{i}" in compact


def test_read_previous_plan_fastpath_bookmarks(tmp_path):
    root = fields.load_document_tree(TEMPLATE_DOCX)
    fields.set_formtext(root, "client_name_vorname", "Mustermann, Erika")
    fields.set_formtext(root, "meta_leistungszeitraum_von", "01.05.2025")
    fields.set_formtext(root, "meta_leistungszeitraum_bis", "30.04.2026")
    fields.set_formtext(root, "developments", "Teilnahme an Tagesstruktur stabil.")
    fields.set_formtext(root, "sections_1_leitziel", "Selbständige Haushaltsführung")
    fields.set_formtext(root, "sections_1_smarte_ziele", "Einkaufszettel schreiben")
    fields.set_formtext(root, "sections_1_bericht_zielerreichung", "Einkäufe gelingen zuverlässig.")
    fields.set_formtext(root, "sections_1_vorschlag_ziele", "Budgetplanung üben")

    test_docx = tmp_path / "fastpath_test.docx"
    fields.save_document_tree(TEMPLATE_DOCX, root, test_docx)

    plan = read_previous_plan(test_docx)
    assert plan.client_name == "Mustermann, Erika"
    assert plan.zeitraum == "01.05.2025 bis 30.04.2026"
    assert "Tagesstruktur" in plan.developments

    sec_12 = plan.sections["1.2"]
    assert sec_12.applies is True
    assert sec_12.leitziel == "Selbständige Haushaltsführung"
    assert sec_12.smarte_ziele == "Einkaufszettel schreiben"
    assert sec_12.stand_zielerreichung == "Einkäufe gelingen zuverlässig."
    assert sec_12.vorschlag_ziele == "Budgetplanung üben"

    # Leerer Abschnitt 1.1 gilt als inaktiv
    assert plan.sections["1.1"].applies is False
    assert plan.sections["1.1"].leitziel == ""


def test_read_previous_plan_fallback_table_heuristic(tmp_path):
    import docx

    doc = docx.Document()
    doc.add_paragraph("Bericht für (Name, Vorname): Anna Schmidt")
    doc.add_paragraph("Leistungszeitraum von 01.02.2025 bis 31.01.2026")
    doc.add_paragraph("Wesentliche Entwicklungen im Berichtszeitraum: Erfolgreicher Wechsel des Wohnumfelds.")

    t = doc.add_table(rows=4, cols=1)
    t.cell(0, 0).text = "4. Wohnen"
    t.cell(1, 0).text = "Leitziel: Eigene Wohnung erhalten. S.M.A.R.T. Ziele: Wöchentlich lüften und reinigen."
    t.cell(2, 0).text = "Bericht zur Zielerreichung / ausgeführte Maßnahme: Wohnung ist in gepflegtem Zustand."
    t.cell(3, 0).text = "Vorschlag für aktuelle (smarte) Ziele: Mietzahlungen kontrollieren."

    test_docx = tmp_path / "fallback_table_test.docx"
    doc.save(test_docx)

    plan = read_previous_plan(test_docx)
    assert plan.client_name == "Anna Schmidt"
    assert plan.zeitraum == "01.02.2025 bis 31.01.2026"
    assert "Wohnumfelds" in plan.developments

    assert "4" in plan.sections
    sec = plan.sections["4"]
    assert sec.applies is True
    assert sec.title == "Wohnen"
    assert sec.leitziel == "Eigene Wohnung erhalten."
    assert sec.smarte_ziele == "Wöchentlich lüften und reinigen."
    assert sec.stand_zielerreichung == "Wohnung ist in gepflegtem Zustand."
    assert sec.vorschlag_ziele == "Mietzahlungen kontrollieren."


def test_read_previous_plan_fallback_paragraphs(tmp_path):
    import docx

    doc = docx.Document()
    doc.add_paragraph("Bericht für: Klaus Meier")
    doc.add_paragraph("1.1 Wohnsituation")
    doc.add_paragraph("Leitziel: Ruhige Wohnumgebung sichern.")
    doc.add_paragraph("Smarte Ziele: Nachbarschaftskonflikte vermeiden.")
    doc.add_paragraph("Bericht zur Zielerreichung: Keine Beschwerden aufgetreten.")

    test_docx = tmp_path / "fallback_paragraphs_test.docx"
    doc.save(test_docx)

    plan = read_previous_plan(test_docx)
    assert plan.client_name == "Klaus Meier"
    assert "1.1" in plan.sections
    sec = plan.sections["1.1"]
    assert sec.applies is True
    assert sec.leitziel == "Ruhige Wohnumgebung sichern."
    assert sec.smarte_ziele == "Nachbarschaftskonflikte vermeiden."
    assert sec.stand_zielerreichung == "Keine Beschwerden aufgetreten."


def test_read_previous_plan_file_not_found():
    with pytest.raises(FileNotFoundError):
        read_previous_plan("nicht_existierende_datei_12345.docx")


def test_read_previous_plan_real_dummy():
    dummy_path = ROOT / "dummy" / "Hans_Peter_SVB_ausgefuellt.docx"
    if not dummy_path.is_file():
        pytest.skip("dummy/Hans_Peter_SVB_ausgefuellt.docx nicht vorhanden")

    plan = read_previous_plan(dummy_path)
    assert plan.client_name == "Hans Peter"
    assert "01.07.2024" in plan.zeitraum and "30.06.2026" in plan.zeitraum
    assert "Physiotherapie" in plan.developments

    active = {sid: s for sid, s in plan.sections.items() if s.applies}
    assert len(active) == 7
    expected_ids = {"1.2", "1.4", "1.5", "2.1", "2.2", "2.3", "3"}
    assert set(active.keys()) == expected_ids

    for sid in expected_ids:
        s = active[sid]
        assert s.leitziel, f"Leitziel in {sid} fehlt"
    assert "Herr Peter bereitet einfache warme Mahlzeiten selbständig zu." in plan.sections["1.2"].smarte_ziele
    assert "Herr Peter nimmt jährliche Vorsorgeuntersuchungen wahr." in plan.sections["1.5"].smarte_ziele

    compact = plan.to_compact_context(max_tokens=500)
    assert len(compact) < 2500
    assert "Klient: Hans Peter" in compact
    for sid in expected_ids:
        assert f"[{sid}" in compact


def test_to_compact_context_includes_vorschlag_ziele():
    """Prüft, dass vorgeschlagene Ziele im kompakten LLM-Kontext gerendert werden."""
    plan = PreviousPlanData(
        client_name="Max Mustermann",
        zeitraum="01.01.2025 bis 31.12.2025",
        developments="Gute Fortschritte im Alltag.",
        sections={
            "1.1": PlanSectionGoal(
                section_id="1.1",
                title="Wohnen",
                leitziel="Wohnung sichern",
                smarte_ziele="Miete zahlen",
                stand_zielerreichung="Wohnung ist sauber",
                vorschlag_ziele="Renovierung des Flurs angehen",
                applies=True,
            )
        },
    )
    compact = plan.to_compact_context(max_tokens=500)
    assert "Klient: Max Mustermann" in compact
    assert "[1.1 Wohnen]" in compact
    assert "Leitziel: Wohnung sichern" in compact
    assert "SMART: Miete zahlen" in compact
    assert "Stand: Wohnung ist sauber" in compact
    assert "Vorschlag: Renovierung des Flurs angehen" in compact


def test_to_compact_context_section_with_only_vorschlag_ziele():
    """Prüft, dass ein Abschnitt mit ausschließlich Vorschlagszielen als aktiv gilt und gerendert wird."""
    plan = PreviousPlanData(
        client_name="Erika Musterfrau",
        sections={
            "2.2": PlanSectionGoal(
                section_id="2.2",
                title="Arbeit",
                leitziel="",
                smarte_ziele="",
                stand_zielerreichung="",
                vorschlag_ziele="Praktikum in Gärtnerei beginnen",
                applies=True,
            )
        },
    )
    compact = plan.to_compact_context(max_tokens=500)
    assert "[2.2 Arbeit]" in compact
    assert "Vorschlag: Praktikum in Gärtnerei beginnen" in compact


def test_read_previous_plan_stream_fallback_extracts_client_name_from_filename():
    """Prüft, dass bei einem Stream-DOCX ohne Bookmarks und ohne 'Bericht für:' der Klientenname aus dem Dateinamen extrahiert wird."""
    import docx
    bio = io.BytesIO()
    doc = docx.Document()
    doc.add_paragraph("Dies ist ein formloser Bericht ohne XML-Bookmarks und ohne Klienten-Header.")
    doc.save(bio)
    bio.seek(0)

    plan = read_previous_plan(bio, filename="Anna_Schmidt_SOB.docx")
    assert plan.client_name == "Anna Schmidt"


def test_read_previous_plan_odw_template_full_roundtrip(tmp_path):
    """Prüft das Einlesen einer ausgefüllten Op de Wisch DOCX inklusive SDT-Kopfdaten und Checkboxen."""
    out_file = tmp_path / "odw_roundtrip.docx"
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
            bezugsbetreuer="Michael Begleiter",
        ),
        developments="Erika zeigt deutliche Stabilisierung im Alltag.",
        sections=[
            ReportSection(
                id="1",
                title="Psychische Verfassung / Selbstsorge",
                previous_goals_apply=True,
                leitziel="Erhalt der psychischen Stabilität",
                smarte_ziele="Erika nimmt 1x wöchentlich an der Reflexion teil.",
                bericht_zielerreichung="Die Teilnahme erfolgte verlässlich und stabil.",
                vorschlag_ziele="Eigenständiges Erkennen von Überlastungsgrenzen.",
            ),
            ReportSection(
                id="2",
                title="Alltagsgestaltung / Tagesstruktur",
                previous_goals_apply=False,
                leitziel="",
                smarte_ziele="",
                bericht_zielerreichung="Tagesstruktur ist gefestigt.",
                vorschlag_ziele="",
            ),
            ReportSection(
                id="3",
                title="Gesundheit",
                previous_goals_apply=True,
                leitziel="Arzttermine wahrnehmen",
                smarte_ziele="Regelmäßige Kontrolluntersuchungen.",
                bericht_zielerreichung="Termine wurden wahrgenommen.",
                vorschlag_ziele="Eigenständige Terminabsprache.",
            ),
        ],
    )

    renderer = OpDeWischDocxRenderer(ODW_TEMPLATE_DOCX)
    renderer.render(draft, out_file)

    plan = read_previous_plan(out_file)
    assert plan.client_name == "Mustermann, Erika"
    assert "01.01.2025" in plan.zeitraum and "31.12.2025" in plan.zeitraum
    assert "Stabilisierung" in plan.developments

    # Abschnitt 1 aktiv mit Zielen
    assert "1" in plan.sections
    sec1 = plan.sections["1"]
    assert sec1.applies is True
    assert sec1.leitziel == "Erhalt der psychischen Stabilität"
    assert "Reflexion" in sec1.smarte_ziele
    assert "verlässlich" in sec1.stand_zielerreichung
    assert "Überlastungsgrenzen" in sec1.vorschlag_ziele

    # Abschnitt 2 'entfällt' angekreuzt ohne eigene Ziele -> applies=False
    assert "2" in plan.sections
    sec2 = plan.sections["2"]
    assert sec2.applies is False

    # Abschnitt 3 aktiv
    assert "3" in plan.sections
    sec3 = plan.sections["3"]
    assert sec3.applies is True
    assert sec3.leitziel == "Arzttermine wahrnehmen"


def test_chat_app_parse_odw_docx(tmp_path):
    """Prüft, dass der Plan-Loader der Chat-App die ODW-DOCX korrekt in PlanData überführt."""
    out_file = tmp_path / "odw_chat_test.docx"
    draft = ReportDraft(
        client=ClientData(name="Peter", vorname="Hans"),
        meta=ReportMeta(leistungszeitraum_von="01.07.2024", leistungszeitraum_bis="30.06.2026"),
        developments="Erfolgreiche Schritte im Berichtszeitraum.",
        sections=[
            ReportSection(
                id="1",
                title="Psychische Verfassung",
                previous_goals_apply=True,
                leitziel="Achtsamkeit stärken",
                smarte_ziele="Regelmäßige Gespräche",
                bericht_zielerreichung="Gute Reflexion",
            ),
            ReportSection(
                id="5",
                title="Finanzen",
                previous_goals_apply=True,
                leitziel="Behördengänge vorbereiten",
                smarte_ziele="Unterlagen ordnen",
                bericht_zielerreichung="Selbständig vorgetragen",
            ),
        ],
    )
    renderer = OpDeWischDocxRenderer(ODW_TEMPLATE_DOCX)
    renderer.render(draft, out_file)

    plan_data = parse_plan_docx(out_file, filename="Hans_Peter_ODW.docx")
    assert plan_data.client_name == "Peter, Hans"
    assert plan_data.zeitraum_von == "01.07.2024"
    assert plan_data.zeitraum_bis == "30.06.2026"
    assert plan_data.goals_count == 2
    assert "Psychische Verfassung" in plan_data.preview
    assert "Finanzen" in plan_data.preview

    ctx = plan_data.to_context_text()
    assert "Peter, Hans" in ctx
    assert "Achtsamkeit stärken" in ctx
    assert "Behördengänge vorbereiten" in ctx


def test_dummy_odw_directory_files_load_successfully():
    """Verifiziert die Existenz und Einlesbarkeit der Testdaten in dummy/odw/."""
    odw_dir = ROOT / "dummy" / "odw"
    if not odw_dir.is_dir():
        pytest.skip("dummy/odw/ Verzeichnis nicht vorhanden")

    csv_file = odw_dir / "Hans_Peter_Vivendi_Export.csv"
    docx_file = odw_dir / "Hans_Peter_ODW_SVB_ausgefuellt.docx"

    assert csv_file.is_file(), "Hans_Peter_Vivendi_Export.csv fehlt in dummy/odw"
    assert docx_file.is_file(), "Hans_Peter_ODW_SVB_ausgefuellt.docx fehlt in dummy/odw"

    # CSV mit VivendiPDImporter einlesen
    from sob_generator.importer import VivendiPDImporter
    imported = VivendiPDImporter(csv_file).load()
    assert imported.client.name == "Peter"
    assert imported.client.vorname == "Hans"
    assert len(imported.entries) > 0

    # DOCX mit parse_plan_docx einlesen
    plan = parse_plan_docx(docx_file)
    assert plan.client_name == "Peter, Hans"
    assert plan.zeitraum_von == "01.07.2024"
    assert plan.zeitraum_bis == "30.06.2026"
    assert plan.goals_count >= 5



