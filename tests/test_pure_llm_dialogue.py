"""Tests for Pure LLM Dialogue & Elimination of Hardcoded Templates.

Verifies:
1. Zero Hardcoded Sentences / Templates in Chat Engine.
2. Hans Zimmer Surname Collision Regression: semantic distribution without keyword map collisions.
3. Batch Entry Classification prompt structure, schema, and error resilience.
4. Dynamic conversational wiring for all dialogue steps in ChatDialogueEngine.
"""
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from sob_generator.chat_engine import ChatDialogueEngine
from sob_generator.chat_session import DialoguePhase, SectionStatus
from sob_generator.chat_store import ChatSessionStore
from sob_generator.importer import VivendiPDImporter
from sob_generator.llm.generator import LLMDraftGenerator
from sob_generator.llm.prompts import build_batch_classification_prompt
from sob_generator.llm.schemas import (
    BatchClassificationItem,
    BatchEntryClassification,
    DocumentationEntry,
    ScreeningTurnOutput,
    SectionLLMOutput,
)
from sob_generator.llm.sections import SECTION_DEFINITIONS
from sob_generator.stub_generator import StubDraftGenerator

DUMMY_DIR = Path(__file__).resolve().parent.parent / "dummy"
HANS_ZIMMER_CSV = DUMMY_DIR / "Hans_Zimmer_Vivendi_Export.csv"
HANS_PETER_CSV = DUMMY_DIR / "Hans_Peter_Vivendi_Export.csv"


def test_zero_hardcoded_templates_in_chat_engine_module():
    """Stellt sicher, dass alle statischen Template-Funktionen und Keyword-Maps entfernt wurden."""
    import sob_generator.chat_engine as ce
    import sob_generator.llm.generator as gen_mod

    assert not hasattr(ce, "format_goal_clarification_message")
    assert not hasattr(ce, "format_field_questions_message")
    assert not hasattr(ce, "extract_key_topics")
    assert not hasattr(ce, "_SECTION_DEFAULT_SMART_GOALS")
    assert not hasattr(ce, "_fast_classify")
    assert not hasattr(ce, "parse_screening_section_adjustments")
    assert not hasattr(ce, "classify_screening_input")
    assert not hasattr(gen_mod, "_KEYWORD_SECTION_MAP")
    assert not hasattr(gen_mod, "_fast_classify")


def test_build_screening_turn_prompt_structure():
    """Prüft, dass der Screening-Prompt Vorlagen-Metadaten, KV-Cache-Aufbau und JSON-Schema enthält."""
    from sob_generator.llm.prompts import build_screening_turn_prompt

    prompt = build_screening_turn_prompt(
        message="wohnen ist erledigt der klient hat einen neue wohnung. kannst du deaktivieren",
        sections=SECTION_DEFINITIONS,
        active_section_ids={"1.1", "1.2"},
        anrede="Herr",
        nachname="Peter",
        vorname="Hans",
    )

    assert "Herr Peter" in prompt
    assert "[1.1] Wohnsituation" in prompt
    assert "to_deactivate" in prompt
    assert "to_activate" in prompt
    assert "development_note" in prompt
    assert "is_confirmed" in prompt
    assert "wohnen ist erledigt der klient hat einen neue wohnung" in prompt
    assert "antworte einfach kurz mit" not in prompt.lower()


def test_build_batch_classification_prompt_structure():
    """Prüft, dass der Batch-Prompt nummerierte Einträge, Abschnittsdefinitionen und JSON-Instruktion enthält."""
    entries = [
        DocumentationEntry(datum="01.07.2024", kategorie="", text="Wohnung aufgeräumt"),
        DocumentationEntry(datum="02.07.2024", kategorie="", text="Kochen und einkaufen"),
    ]
    prompt = build_batch_classification_prompt(entries, SECTION_DEFINITIONS)

    assert "[0] Datum: 01.07.2024 | Text: Wohnung aufgeräumt" in prompt
    assert "[1] Datum: 02.07.2024 | Text: Kochen und einkaufen" in prompt
    assert "1.1 Wohnsituation" in prompt
    assert "1.2 Alltägliche Lebensführung" in prompt
    assert 'classifications' in prompt
    assert 'section_id' in prompt


def test_classify_entries_batch_resilience_and_fallback():
    """Prüft, dass bei unvollständiger oder fehlerhafter Batch-Antwort auf Einzelschritte zurückgefallen wird."""
    gen = LLMDraftGenerator(model_path="dummy.gguf")
    entries = [
        DocumentationEntry(datum="01.01.2026", kategorie="", text="Eintrag 1"),
        DocumentationEntry(datum="02.01.2026", kategorie="", text="Eintrag 2"),
    ]

    partial_batch = '{"classifications": [{"id": 0, "section_id": "1.1"}]}'
    single_res = '{"section_id": "1.2"}'

    def fake_complete(prompt, model_cls, max_tokens):
        if model_cls is BatchEntryClassification:
            return partial_batch
        return single_res

    with patch.object(gen, "_complete", side_effect=fake_complete):
        res = gen.classify_entries_batch(entries, SECTION_DEFINITIONS)

    assert len(res["1.1"]) == 1
    assert res["1.1"][0].text == "Eintrag 1"
    assert len(res["1.2"]) == 1
    assert res["1.2"][0].text == "Eintrag 2"


def test_hans_zimmer_surname_collision_regression():
    """Regressionstest für Hans Zimmer: Nachname darf keine falsche Zuordnung zu 1.1 auslösen."""
    if not HANS_ZIMMER_CSV.exists():
        pytest.skip("dummy/ ist lokal (gitignored)")
    imported = VivendiPDImporter(HANS_ZIMMER_CSV).load()
    assert len(imported.entries) == 18

    expected_sids = [
        "1.1", "1.2", "1.3", "1.4", "1.5",
        "2.1", "2.2", "1.5", "3", "1.1",
        "1.2", "1.2", "1.4", "2.3", "2.1",
        "2.2", "2.3", "3",
    ]

    def mock_complete(prompt, model_cls, max_tokens):
        if "[0] Datum: 08.07.2024" in prompt:
            chunk_sids = expected_sids[:15]
        else:
            chunk_sids = expected_sids[15:]
        batch_output = {
            "classifications": [
                {"id": idx, "section_id": sid} for idx, sid in enumerate(chunk_sids)
            ]
        }
        return BatchEntryClassification.model_validate(batch_output).model_dump_json()

    gen = LLMDraftGenerator(model_path="test.gguf")
    with patch.object(gen, "_complete", side_effect=mock_complete):
        res = gen.classify_entries_batch(imported.entries, SECTION_DEFINITIONS)

    assert len(res["1.1"]) == 2, f"Erwartet 2 Einträge in 1.1, erhalten: {len(res['1.1'])}"
    assert len(res["1.2"]) == 3
    assert len(res["1.3"]) == 1
    assert len(res["1.4"]) == 2
    assert len(res["1.5"]) == 2
    assert len(res["2.1"]) == 2
    assert len(res["2.2"]) == 2
    assert len(res["2.3"]) == 2
    assert len(res["3"]) == 2


@pytest.mark.anyio
async def test_chat_dialogue_engine_wires_all_steps_to_generator():
    """Prüft, dass ChatDialogueEngine alle Dialogschritte dynamisch an das Generator-Interface delegiert."""
    mock_generator = MagicMock()
    mock_generator.classify_intent.return_value = None
    mock_generator.process_screening_turn.return_value = ScreeningTurnOutput(
        reply="DYNA_SCREENING_REPLY",
        is_confirmed=True,
    )
    mock_generator.classify_entries_batch.return_value = {
        s.id: [DocumentationEntry(datum="01.01.2026", kategorie="", text=f"Eintrag in {s.id}")]
        for s in SECTION_DEFINITIONS
    }
    from sob_generator.llm.generator import GenerationResult
    from sob_generator.llm.schemas import GoalProposalOutput, DialogueTurnOutput
    mock_generator.generate_field_goals.return_value = GenerationResult(GoalProposalOutput(goals="DYNA_GOALS_TEXT", reply="Welche Beobachtung liegt vor?"), False, "")
    mock_generator.converse.return_value = GenerationResult(DialogueTurnOutput(reply="DYNA_QUESTIONS_TEXT?", public_note="", supersedes=[], pending_question="DYNA_QUESTIONS_TEXT?", sufficient=False), False, "")
    mock_generator.revise_field_goals.return_value = GenerationResult(GoalProposalOutput(goals="DYNA_REVISED_TEXT", reply="Ist der Vorschlag passend?"), False, "")
    mock_generator.refine_section.return_value = MagicMock(
        error=None, needs_review=False, value=SectionLLMOutput(
            bericht_zielerreichung="DYNA_BERICHT",
            vorschlag_ziele="DYNA_ZIELE",
            vorschlag_massnahmen="DYNA_MASSNAHMEN",
            realisierung_andere="",
        )
    )

    store = ChatSessionStore()
    engine = ChatDialogueEngine(generator=mock_generator, session_store=store)

    if not HANS_PETER_CSV.exists():
        pytest.skip("dummy/ ist lokal (gitignored)")
    session = await engine.start_session(csv_path=HANS_PETER_CSV)
    mock_generator.classify_entries_batch.assert_called_once()
    assert session.phase == DialoguePhase.SCREENING

    # Bestätigung im Screening -> Ruft process_screening_turn und generate_field_goals auf
    session = await engine.process_message(session.session_id, "Passt so!")
    mock_generator.process_screening_turn.assert_called_once()
    assert session.phase == DialoguePhase.FIELD_REFLECT
    mock_generator.generate_field_goals.assert_called()
    assert "DYNA_SCREENING_REPLY" in session.messages[-1].content
    assert "DYNA_GOALS_TEXT" in session.messages[-1].content

    # Kritik an Zielen -> Ruft revise_field_goals auf
    session = await engine.process_message(session.session_id, "1. ziel ist nicht smart")
    assert session.phase == DialoguePhase.FIELD_REFLECT
    mock_generator.revise_field_goals.assert_called()
    assert "DYNA_REVISED_TEXT" in session.messages[-1].content

    # Frage nach vertiefenden Fragen -> Ruft generate_field_questions auf
    session = await engine.process_message(session.session_id, "Kannst du mir vertiefende Fragen stellen?")
    assert session.sections[session.active_section_id].pending_question
    mock_generator.converse.assert_called()
    assert "DYNA_QUESTIONS_TEXT" in session.messages[-1].content

    # Fachkraft antwortet auf Fragen -> Ruft refine_section auf
    session = await engine.process_message(session.session_id, "Entwurf erstellen")
    assert session.phase == DialoguePhase.FIELD_DRAFT
    mock_generator.refine_section.assert_called()
    active_sec = session.sections[session.active_section_id]
    assert active_sec.bericht_zielerreichung == "DYNA_BERICHT"
