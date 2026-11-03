"""Tests für das persistente Protokoll des App-Walkthroughs."""
import json

from evals.walkthrough_log import write_walkthrough_log
from sob_generator.chat_engine import ChatDialogueEngine
from sob_generator.chat_session import ChatSession, DialoguePhase, SectionChatState
from sob_generator.chat_store import ChatSessionStore
from sob_generator.stub_generator import StubDraftGenerator


def test_walkthrough_log_contains_visible_dialogue_and_internal_state(tmp_path):
    session = ChatSession(
        phase=DialoguePhase.FIELD_QUESTIONS,
        active_section_id="1.2",
        sections={
            "1.2": SectionChatState(
                section_id="1.2",
                title="Alltägliche Lebensführung / Hauswirtschaft",
                user_notes=["Ein synthetischer Einkauf wurde beobachtet."],
                uncertainties=["Bezahlen ist noch offen."],
                pending_question="Welche Unterstützung war erforderlich?",
            )
        },
    )
    session.add_user_message("Synthetische Testnachricht.", capture_facts=False)
    session.add_assistant_message("Synthetische Rückfrage.")
    store = ChatSessionStore()
    store.store(session)
    engine = ChatDialogueEngine(StubDraftGenerator(), session_store=store)

    markdown_path, state_path = write_walkthrough_log(engine, session, tmp_path)

    assert markdown_path.name == "app_walkthrough_latest.md"
    assert "Synthetische Testnachricht." in markdown_path.read_text(encoding="utf-8")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["phase"] == "field_questions"
    assert state["active_section_id"] == "1.2"
    assert state["sections"]["1.2"]["notes"] == [
        "Ein synthetischer Einkauf wurde beobachtet."
    ]
    assert state["sections"]["1.2"]["uncertainties"] == [
        "Bezahlen ist noch offen."
    ]
