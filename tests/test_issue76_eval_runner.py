"""Tests für die reproduzierbare Dialogevaluation zu Issue 76."""
from pathlib import Path

import pytest

from evals.issue76_runner import evaluate_state, load_cases, run_case, run_suite, write_results
from sob_generator.chat_session import ChatSession, DialoguePhase, SectionChatState
from sob_generator.stub_generator import StubDraftGenerator


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_case_catalog_contains_unique_core_scenarios():
    cases = load_cases()
    assert {case["id"] for case in cases} == {
        "einzelne_beobachtung",
        "wunsch_ohne_zeitraum",
        "vollstaendiger_zielauftrag",
        "unsicherheit",
        "korrektur",
    }


def test_evaluator_reports_unfounded_goal_and_missing_question():
    session = ChatSession(
        phase=DialoguePhase.FIELD_REFLECT,
        active_section_id="1.1",
        sections={"1.1": SectionChatState(
            section_id="1.1",
            title="Wohnsituation",
            proposed_goals="Unbelegtes Ziel",
        )},
    )
    case = {
        "expected": {
            "goal_state": "empty",
            "last_reply_has_question": True,
        }
    }
    violations = evaluate_state(case, session, ["Die Beobachtung wurde übernommen."])
    assert "Es wurde ohne erwartete Grundlage ein Ziel erzeugt." in violations
    assert "Die erwartete fachliche Rückfrage fehlt." in violations


@pytest.mark.anyio
async def test_stub_runs_uncertainty_case_without_model(tmp_path: Path):
    case = next(item for item in load_cases() if item["id"] == "unsicherheit")
    result = await run_case(case, StubDraftGenerator())
    assert result.passed
    assert result.uncertainties == ["Das kann ich noch nicht beurteilen."]


@pytest.mark.anyio
async def test_suite_cache_avoids_repeating_case(tmp_path: Path):
    case = next(item for item in load_cases() if item["id"] == "unsicherheit")
    cache_path = tmp_path / "cache.json"
    first = await run_suite([case], StubDraftGenerator(), cache_path=cache_path)
    second = await run_suite([case], StubDraftGenerator(), cache_path=cache_path)
    assert not first[0].cached
    assert second[0].cached
    results_path = write_results(second, tmp_path / "results.json")
    assert results_path.exists()
    assert '"passed": 1' in results_path.read_text(encoding="utf-8")
