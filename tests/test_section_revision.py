"""Gezielte Abschnittsüberarbeitung erhält Kontext, Schema und Fehlerzustand."""
import json
from unittest.mock import MagicMock

import pytest

from sob_generator.draft_generator import DraftGenerator
from sob_generator.llm.generator import LLMDraftGenerator, SECTION_MAX_TOKENS
from sob_generator.llm.prompts import build_revise_section_prompt
from sob_generator.llm.schemas import DocumentationEntry, SectionLLMOutput
from sob_generator.llm.sections import SECTION_DEFINITIONS
from sob_generator.stub_generator import StubDraftGenerator


@pytest.fixture
def current_draft():
    return SectionLLMOutput(
        bericht_zielerreichung=(
            "Herr Winter bespricht die Wochenplanung mit der Assistenz. "
            "Die Unterstützung erfolgt anhand einer gemeinsam erstellten Übersicht."
        ),
        vorschlag_ziele="Herr Winter nutzt die vorhandene Wochenübersicht.",
        vorschlag_massnahmen="Die Assistenz unterstützt bei der Wochenplanung.",
        realisierung_andere="Die Ergotherapie wird fortgesetzt.",
    )


@pytest.fixture
def entries():
    return [DocumentationEntry(
        datum="01.09.2026",
        kategorie="alltag",
        text="Alex plant die Woche mit der Assistenz anhand einer Übersicht.",
    )]


def test_revision_prompt_separates_exact_instruction_from_public_facts(current_draft, entries):
    critique = 'Bitte formuliere den Bericht kürzer und ohne das Wort "Unterstützung".'
    prompt = build_revise_section_prompt(
        SECTION_DEFINITIONS[1], current_draft, critique, entries,
        leitziel="Selbstständig planen",
        smarte_ziele="Wochenübersicht anwenden",
        freitext="Alex benötigt die Übersicht.",
        user_notes=["Die Übersicht wird gemeinsam besprochen."],
        developments="Die Wohnsituation ist stabil.",
        anrede="Herr", nachname="Winter", vorname="Alex",
        forbidden_openers=("Im Berichtszeitraum",),
    )
    sources, instruction = prompt.split("## Änderungsauftrag (wörtlich)\n")
    assert critique not in sources
    assert critique in instruction
    assert current_draft.model_dump_json() in sources
    assert "Die Übersicht wird gemeinsam besprochen." in sources
    assert "Die Wohnsituation ist stabil." in sources
    assert "Selbstständig planen" in sources
    assert "Wochenübersicht anwenden" in sources
    assert "Alex" not in sources
    assert "Winter plant die Woche" in sources
    assert "Im Berichtszeitraum" in instruction
    assert "wortgetreu" in prompt
    assert "tatsächlich kürzer" in prompt
    assert "2 bis 5 Sätzen" not in prompt


def test_revision_prompt_respects_inapplicable_previous_goals(current_draft):
    prompt = build_revise_section_prompt(
        SECTION_DEFINITIONS[1], current_draft, "Kürzer bitte.", [],
        leitziel="Veraltetes Leitziel", smarte_ziele="Veraltetes Ziel",
        previous_goals_apply=False,
    )
    assert "Veraltetes" not in prompt
    assert "Bisherige Ziele: entfällt." in prompt


def test_revision_forces_schema_and_accepts_requested_single_sentence(current_draft, entries):
    revised = current_draft.model_copy(update={
        "bericht_zielerreichung": "Herr Winter plant mit der Assistenz anhand einer Wochenübersicht."
    })
    generator = LLMDraftGenerator("unused.gguf")
    generator._complete = MagicMock(side_effect=[revised.model_dump_json(), '{"unsupported_claims": [], "supported": true}'])

    result = generator.revise_section(
        SECTION_DEFINITIONS[1], current_draft, "Bitte auf einen Satz kürzen.", entries,
    )

    assert not result.needs_review
    assert result.value == revised
    assert len(result.value.bericht_zielerreichung) < len(current_draft.bericht_zielerreichung)
    assert result.value.vorschlag_ziele == current_draft.vorschlag_ziele
    assert result.value.vorschlag_massnahmen == current_draft.vorschlag_massnahmen
    assert result.value.realisierung_andere == current_draft.realisierung_andere
    assert generator._complete.call_count == 2
    assert generator._complete.call_args_list[0].args[1] is SectionLLMOutput
    assert generator._complete.call_args_list[0].args[2] == SECTION_MAX_TOKENS


def test_revision_grammar_reaches_real_completion_boundary(monkeypatch, current_draft):
    revised = current_draft.model_copy(update={"bericht_zielerreichung": "Die Wochenplanung gelingt mit Assistenz."})
    grammar = object()
    grammar_builder = MagicMock(return_value=grammar)
    monkeypatch.setattr("sob_generator.llm.generator.build_grammar", grammar_builder)
    generator = LLMDraftGenerator("Qwen2.5-7B-Instruct-Q4_K_M.gguf")
    model = MagicMock(side_effect=[{"choices": [{"text": revised.model_dump_json()}]}, {"choices": [{"text": '{"unsupported_claims": [], "supported": true}'}]}])
    model.tokenize.return_value = [1, 2, 3]
    model.n_ctx.return_value = 8192
    generator._llm = model

    result = generator.revise_section(SECTION_DEFINITIONS[1], current_draft, "Kürzer bitte.", [])

    assert not result.needs_review
    assert grammar_builder.call_args_list[0].args[0] is SectionLLMOutput
    assert grammar_builder.call_count == 2
    assert model.call_args.kwargs["grammar"] is grammar
    assert model.call_args.args[0].startswith("<|im_start|>user\n")


def test_revision_retries_invalid_json_before_success(current_draft):
    revised = current_draft.model_copy(update={"bericht_zielerreichung": "Die Wochenplanung gelingt mit Assistenz."})
    generator = LLMDraftGenerator("unused.gguf")
    generator._complete = MagicMock(side_effect=["not JSON", revised.model_dump_json(), '{"unsupported_claims": [], "supported": true}'])

    result = generator.revise_section(SECTION_DEFINITIONS[1], current_draft, "Kürzer bitte.", [])

    assert not result.needs_review
    assert result.value == revised
    assert generator._complete.call_count == 3
    assert all(call.args[1] is SectionLLMOutput for call in generator._complete.call_args_list[:2])


@pytest.mark.parametrize("invalid_output", ["not JSON", '{}', json.dumps({
    "bericht_zielerreichung": "Gekürzt.",
    "vorschlag_ziele": "",
    "vorschlag_massnahmen": "",
    "realisierung_andere": "",
})])
def test_invalid_revision_preserves_draft_and_reports_failure(current_draft, invalid_output):
    generator = LLMDraftGenerator("unused.gguf")
    generator._complete = MagicMock(return_value=invalid_output)

    result = generator.revise_section(SECTION_DEFINITIONS[1], current_draft, "Kürzer bitte.", [])

    assert result.needs_review
    assert result.value == current_draft
    assert result.value is not current_draft
    assert result.error
    assert result.raw_output == invalid_output
    assert generator._complete.call_count == 2


def test_runtime_revision_failure_is_explicit_and_keeps_existing_draft(current_draft):
    generator = LLMDraftGenerator("unused.gguf")
    generator._complete = MagicMock(side_effect=RuntimeError("context window exhausted"))

    result = generator.revise_section(SECTION_DEFINITIONS[1], current_draft, "Kürzer bitte.", [])

    assert result.needs_review
    assert result.value == current_draft
    assert result.value is not current_draft
    assert "context window exhausted" in result.error


def test_unchanged_revision_is_not_reported_as_success(current_draft):
    generator = LLMDraftGenerator("unused.gguf")
    generator._complete = MagicMock(return_value=current_draft.model_dump_json())

    result = generator.revise_section(SECTION_DEFINITIONS[1], current_draft, "Kürzer bitte.", [])

    assert result.needs_review
    assert "nicht verändert" in result.error
    assert result.value == current_draft


def test_blank_revision_instruction_skips_inference(current_draft):
    generator = LLMDraftGenerator("unused.gguf")
    generator._complete = MagicMock()

    result = generator.revise_section(SECTION_DEFINITIONS[1], current_draft, "  ", [])

    assert result.needs_review
    assert result.value == current_draft
    assert result.error
    generator._complete.assert_not_called()


def test_stub_revision_preserves_other_fields_and_does_not_leak_editorial_request(current_draft):
    stub = StubDraftGenerator()
    original = current_draft.model_dump()
    critique = "Bitte den Bericht verständlicher formulieren."

    result = stub.revise_section(SECTION_DEFINITIONS[1], current_draft, critique, [])

    assert isinstance(stub, DraftGenerator)
    assert not result.needs_review
    assert result.value.bericht_zielerreichung != current_draft.bericht_zielerreichung
    assert critique not in result.value.model_dump_json()
    assert result.value.vorschlag_ziele == current_draft.vorschlag_ziele
    assert result.value.vorschlag_massnahmen == current_draft.vorschlag_massnahmen
    assert result.value.realisierung_andere == current_draft.realisierung_andere
    assert current_draft.model_dump() == original


def test_stub_blank_revision_reports_failure(current_draft):
    result = StubDraftGenerator().revise_section(SECTION_DEFINITIONS[1], current_draft, "", [])

    assert result.needs_review
    assert result.error
    assert result.value == current_draft
