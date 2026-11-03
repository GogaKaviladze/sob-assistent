from sob_generator.llm.prompts import build_gap_analysis_prompt, build_refine_section_prompt
from sob_generator.llm.schemas import DocumentationEntry, GapAnalysisOutput
from sob_generator.llm.sections import SectionDefinition
from sob_generator.stub_generator import StubDraftGenerator


def _dummy_section() -> SectionDefinition:
    return SectionDefinition(
        index=0,
        id="1.1",
        title="Wohnen",
        description="Wohnraumversorgung und Haushaltsführung",
    )


def test_gap_analysis_output_schema():
    out = GapAnalysisOutput(
        has_gaps=True,
        gaps=["Keine Notizen zur Haushaltsführung"],
        clarifying_question="Wie klappt die Führung des Haushalts aktuell?",
    )
    assert out.has_gaps is True
    assert len(out.gaps) == 1
    assert "Haushaltsführung" in out.gaps[0]
    assert out.clarifying_question.startswith("Wie")


def test_build_gap_analysis_prompt():
    section = _dummy_section()
    entries = [
        DocumentationEntry(datum="12.01.2026", kategorie="w_wohnen", text="Miete überwiesen"),
    ]
    prompt = build_gap_analysis_prompt(
        section=section,
        entries=entries,
        leitziel="Selbstbestimmtes Wohnen",
        smarte_ziele="Erhalt der Wohnung",
        previous_goals_apply=True,
        freitext="Freitext Notiz",
        user_notes=["Fachkraft bestätigt: Keine Mietrückstände."],
        anrede="Herr",
        nachname="Mustermann",
        vorname="Max",
    )

    assert "Wohnen" in prompt
    assert "Herr Mustermann" in prompt
    assert "Miete überwiesen" in prompt
    assert "Keine Mietrückstände" in prompt
    assert "has_gaps" in prompt


def test_build_refine_section_prompt():
    section = _dummy_section()
    entries = [
        DocumentationEntry(datum="12.01.2026", kategorie="w_wohnen", text="Miete überwiesen"),
    ]
    prompt = build_refine_section_prompt(
        section=section,
        entries=entries,
        leitziel="Selbstbestimmtes Wohnen",
        smarte_ziele="Erhalt der Wohnung",
        previous_goals_apply=True,
        freitext="",
        user_notes=["Fachkraft ergänzt: Nachbarschaftskonflikte wurden beigelegt."],
        anrede="Herr",
        nachname="Mustermann",
        vorname="Max",
    )

    assert "Nachbarschaftskonflikte wurden beigelegt" in prompt
    assert "Herr Mustermann" in prompt


def test_stub_generator_interactive_methods():
    stub = StubDraftGenerator()
    section = _dummy_section()

    # Gap analysis on empty entries
    res_gap = stub.analyze_section_gaps(
        section=section,
        entries=[],
        freitext="",
        user_notes=[],
    )
    assert res_gap.value.has_gaps is True
    assert len(res_gap.value.gaps) > 0
    assert "Wohnen" in res_gap.value.clarifying_question

    # Refine section with user notes
    res_refined = stub.refine_section(
        section=section,
        entries=[DocumentationEntry(datum="01.01.2026", kategorie="w_wohnen", text="Eintrag")],
        user_notes=["Zusatzinfo aus Chat"],
    )
    assert "Zusatzinfo aus Chat" in res_refined.value.bericht_zielerreichung


def test_build_field_goal_clarification_prompt():
    from sob_generator.llm.prompts import build_field_goal_clarification_prompt

    section = _dummy_section()
    entries = [
        DocumentationEntry(datum="12.01.2026", kategorie="w_wohnen", text="Miete überwiesen"),
    ]
    prompt = build_field_goal_clarification_prompt(
        section=section,
        entries=entries,
        leitziel="Selbstbestimmtes Wohnen",
        smarte_ziele="Erhalt der Wohnung",
        previous_goals_apply=True,
        freitext="",
        anrede="Herr",
        nachname="Mustermann",
        vorname="Max",
    )
    assert "Schritt 1 von 2" in prompt
    assert "genau 2 passgenaue, alltagsnahe SMART-Zielvorschläge" in prompt
    assert "Reflexionsfrage" in prompt
    assert "Duz die Fachkraft" in prompt


def test_build_field_questions_prompt():
    from sob_generator.llm.prompts import build_field_questions_prompt

    section = _dummy_section()
    entries = [
        DocumentationEntry(datum="12.01.2026", kategorie="w_wohnen", text="Miete überwiesen"),
    ]
    prompt = build_field_questions_prompt(
        section=section,
        entries=entries,
        user_notes=["Fachkraft Notiz"],
        anrede="Herr",
        nachname="Mustermann",
        vorname="Max",
    )
    assert "Schritt 2 von 3" in prompt
    assert "genau 2 bis 3 gezielte, fachlich tiefgehende Fragen" in prompt
    assert "Erstelle noch KEINEN Textentwurf" in prompt


def test_chat_turn_max_tokens_bound():
    from sob_generator.llm.generator import CHAT_TURN_MAX_TOKENS

    assert 250 <= CHAT_TURN_MAX_TOKENS <= 600


def test_build_revise_field_goals_prompt():
    from sob_generator.llm.prompts import build_revise_field_goals_prompt

    section = _dummy_section()
    entries = [
        DocumentationEntry(datum="12.01.2026", kategorie="w_wohnen", text="Miete überwiesen"),
    ]
    prompt = build_revise_field_goals_prompt(
        section=section,
        current_goals="Ziel 1: Erhalt der Wohnung.",
        critique="1. ziel ist nicht smart",
        entries=entries,
        anrede="Herr",
        nachname="Mustermann",
        vorname="Max",
    )
    assert "1. ziel ist nicht smart" in prompt
    assert "Ziel 1: Erhalt der Wohnung" in prompt
    assert "SMART" in prompt
    assert "Miete überwiesen" in prompt

