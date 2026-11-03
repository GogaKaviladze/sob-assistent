"""Tests for interactive reflection suggestion chips in chat session."""
from sob_generator.chat_session import (
    ChatRole,
    ChatSession,
    DialoguePhase,
    ReflectionSuggestion,
    SectionChatState,
    SectionStatus,
)
from sob_generator.models import ClientData, ReportMeta


def test_reflection_suggestions_structure_and_count():
    session = ChatSession(
        client=ClientData(name="Mustermann", vorname="Max", anrede="Herr"),
        meta=ReportMeta(),
    )
    session.sections["1.1"] = SectionChatState(
        section_id="1.1",
        title="Wohnen und alltägliche Lebensführung",
    )

    session.active_section_id = "1.1"
    for phase in (DialoguePhase.SCREENING, DialoguePhase.FIELD_REFLECT,
                  DialoguePhase.FIELD_QUESTIONS, DialoguePhase.FIELD_DRAFT):
        session.phase = phase
        suggestions = session.get_reflection_suggestions()
        assert len(suggestions) == 2
        assert all(isinstance(item, ReflectionSuggestion) for item in suggestions)
        assert not any(word in item.text for item in suggestions for word in ("Rückzug", "Wochenplan", "Unterstützungsbedarf bestand"))
        if phase != DialoguePhase.SCREENING:
            assert all(item.action for item in suggestions)
    session.phase = DialoguePhase.FIELD_REFLECT
    assert "Wohnen" in session.get_reflection_suggestions()[0].text
    session.phase = DialoguePhase.FINALIZED
    assert session.get_reflection_suggestions() == []

