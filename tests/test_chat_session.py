"""Tests for ChatSession state machine and ChatSessionStore."""
from datetime import date

from sob_generator.chat_store import ChatSessionStore
from sob_generator.chat_session import (
    ChatMessage,
    ChatRole,
    ChatSession,
    DialoguePhase,
    SectionChatState,
    SectionStatus,
)
from sob_generator.importer import VivendiImport
from sob_generator.models import ClientData, ReportMeta
from sob_generator.previous_sob import PreviousGoals


def _make_dummy_import() -> VivendiImport:
    return VivendiImport(
        client=ClientData(
            name="Mustermann",
            vorname="Max",
            anrede="Herr",
            geburtsdatum="01.01.1980",
            anschrift="Musterstr. 1",
        ),
        meta=ReportMeta(
            leistungszeitraum_von="01.01.2026",
            leistungszeitraum_bis="30.06.2026",
        ),
        entries=[],
    )


def test_chat_session_initialization():
    imported = _make_dummy_import()
    previous_goals = {
        "1.1": PreviousGoals(leitziel="Selbstbestimmt leben", smarte_ziele="Wohnung halten", applies=True),
        "1.2": PreviousGoals(leitziel="", smarte_ziele="", applies=False),
    }
    section_titles = {
        "1.1": "Wohnen",
        "1.2": "Arbeit",
    }

    session = ChatSession.from_import(
        imported=imported,
        previous_goals=previous_goals,
        section_titles=section_titles,
    )

    assert session.client.name == "Mustermann"
    assert session.active_section_id == "1.1"
    assert len(session.sections) == 2
    assert session.sections["1.1"].title == "Wohnen"
    assert session.sections["1.1"].leitziel == "Selbstbestimmt leben"
    assert session.sections["1.1"].status == SectionStatus.EMPTY
    assert not session.sections["1.2"].previous_goals_apply


def test_chat_session_messages_and_notes():
    imported = _make_dummy_import()
    session = ChatSession.from_import(
        imported=imported,
        section_titles={"1.1": "Wohnen", "1.2": "Arbeit"},
    )
    # in FIELD_GOALS phase, messages are attached to active section:
    session.phase = DialoguePhase.FIELD_GOALS
    user_msg = session.add_user_message("Herr Mustermann hat seine Miete immer pünktlich bezahlt.")
    assert user_msg.role == ChatRole.USER
    assert user_msg.section_id == "1.1"
    assert session.sections["1.1"].user_notes == ["Herr Mustermann hat seine Miete immer pünktlich bezahlt."]

    assistant_msg = session.add_assistant_message("Gibt es Rückstände oder Konflikte mit den Nachbarn?")
    assert assistant_msg.role == ChatRole.ASSISTANT
    assert assistant_msg.section_id == "1.1"
    assert len(session.messages) == 2


def test_screening_notes_do_not_contaminate_section_1_1():
    imported = _make_dummy_import()
    session = ChatSession.from_import(
        imported=imported,
        section_titles={"1.1": "Wohnen", "1.2": "Arbeit", "1.4": "Emotionale Entwicklung"},
    )
    assert session.phase == DialoguePhase.SCREENING

    # In SCREENING phase, user input must not contaminate section 1.1 user_notes
    msg = session.add_user_message("neugkeit bei hans peter ist es das seine vater verstorben ist")
    assert msg.section_id is None
    assert session.sections["1.1"].user_notes == []
    assert "neugkeit bei hans peter ist es das seine vater verstorben ist" in session.memory.learned_facts

    session.add_development_note("Vater von Hans Peter ist verstorben.")
    assert "Vater von Hans Peter ist verstorben." in session.developments


def test_chat_session_section_flow_and_conversion():
    imported = _make_dummy_import()
    session = ChatSession.from_import(
        imported=imported,
        section_titles={"1.1": "Wohnen"},
    )

    session.update_section_draft(
        section_id="1.1",
        bericht_zielerreichung="Wohnsituation ist stabil.",
        vorschlag_ziele="Weiterhin selbstständige Haushaltsführung.",
        vorschlag_massnahmen="Wöchentliche Assistenz beim Einkauf.",
    )

    assert session.sections["1.1"].status == SectionStatus.DRAFTED
    assert not session.is_all_confirmed()

    # Before confirmation -> needs_review is True
    draft = session.to_report_draft()
    assert draft.sections[0].needs_review is True

    # After confirmation -> needs_review is False
    session.confirm_section("1.1")
    assert session.sections["1.1"].status == SectionStatus.CONFIRMED
    assert session.is_all_confirmed()

    final_draft = session.to_report_draft()
    assert final_draft.sections[0].needs_review is False
    assert final_draft.sections[0].bericht_zielerreichung == "Wohnsituation ist stabil."


def test_chat_session_store_lifecycle():
    store = ChatSessionStore()
    imported = _make_dummy_import()
    session = ChatSession.from_import(imported=imported, section_titles={"1.1": "Wohnen"})

    stored = store.store(session)
    assert stored.session_id == session.session_id
    assert store.get(session.session_id) is not None

    # GDPR discard
    store.discard(session.session_id)
    assert store.get(session.session_id) is None


def test_chat_session_working_memory_and_portioning():
    imported = _make_dummy_import()
    session = ChatSession.from_import(
        imported=imported,
        section_titles={"1.1": "Wohnen"},
        section_entries={"1.1": []},
    )
    assert "1.1" in session.memory.section_entries

    session.add_user_message("Herr Mustermann hat neue Arbeitszeiten.")
    assert "Herr Mustermann hat neue Arbeitszeiten." in session.memory.learned_facts
