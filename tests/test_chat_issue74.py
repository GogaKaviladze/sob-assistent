"""Regression coverage for issue 74 using exclusively synthetic session data."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from sob_generator.chat_engine import ChatDialogueEngine
from sob_generator.chat_session import ChatSession, DialoguePhase, SectionStatus
from sob_generator.chat_store import ChatSessionStore
from sob_generator.dialogue_router import DialogueIntentRouter
from sob_generator.importer import VivendiImport
from sob_generator.llm.generator import GenerationResult
from sob_generator.llm.schemas import SectionLLMOutput
from sob_generator.models import ClientData, ReportMeta
from sob_generator.report_template import TEMPLATE_REGISTRY
from sob_generator.stub_generator import StubDraftGenerator


@pytest.fixture
def anyio_backend():
    return "asyncio"


class RecordingGenerator(StubDraftGenerator):
    def __init__(self):
        self.calls = []
        self.revision_error = None

    def refine_section(self, **kwargs):
        self.calls.append(("refine", deepcopy(kwargs)))
        return super().refine_section(**kwargs)

    def generate_field_questions(self, **kwargs):
        self.calls.append(("questions", deepcopy(kwargs)))
        return super().generate_field_questions(**kwargs)

    def generate_field_goals(self, **kwargs):
        self.calls.append(("goals", deepcopy(kwargs)))
        return super().generate_field_goals(**kwargs)

    def process_screening_turn(self, **kwargs):
        self.calls.append(("screening", deepcopy(kwargs)))
        return super().process_screening_turn(**kwargs)

    def classify_entries_batch(self, entries, sections):
        self.calls.append(("classify", deepcopy(entries)))
        return {sections[0].id: list(entries)}

    def revise_section(self, **kwargs):
        self.calls.append(("revise", deepcopy(kwargs)))
        if isinstance(self.revision_error, Exception):
            raise self.revision_error
        value = kwargs["current_draft"].model_copy(
            update={"bericht_zielerreichung": "Controlled revised report."}
        )
        return GenerationResult(value, bool(self.revision_error), "", self.revision_error)


def make_engine(phase=DialoguePhase.FIELD_DRAFT, template_key="hamburg_soziale_dienste"):
    template = TEMPLATE_REGISTRY[template_key]
    session = ChatSession.from_import(
        VivendiImport(client=ClientData(name="Mustermann", vorname="Max"), meta=ReportMeta(), entries=[]),
        section_titles={section.id: section.title for section in template.section_definitions},
        template_key=template_key,
    )
    session.phase = phase
    store = ChatSessionStore()
    store.store(session)
    generator = RecordingGenerator()
    return ChatDialogueEngine(generator, session_store=store, router=DialogueIntentRouter()), session, generator


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [None, "model failed", RuntimeError("model unavailable")])
async def test_draft_revision_receives_critique_and_current_draft_without_creating_facts(failure):
    engine, session, generator = make_engine()
    section = session.sections[session.active_section_id]
    session.update_section_draft(section.section_id, bericht_zielerreichung="Original report.",
        vorschlag_ziele="Original goals.", vorschlag_massnahmen="Original actions.")
    before = section.to_report_section().model_dump()
    generator.revision_error = failure
    critique = "Bitte den Bericht kürzen."
    await engine.process_message(session.session_id, critique)
    call = next(kwargs for name, kwargs in generator.calls if name == "revise")
    assert call["critique"] == critique
    assert call["current_draft"].bericht_zielerreichung == "Original report."
    assert call["current_draft"].vorschlag_ziele == "Original goals."
    assert critique not in repr(call["user_notes"])
    assert critique not in session.memory.learned_facts
    assert critique not in session.to_report_draft().model_dump_json()
    if failure:
        assert section.to_report_section().model_dump() == before
        assert "umgesetzt" not in session.messages[-1].content
        assert "nicht" in session.messages[-1].content
    else:
        assert section.bericht_zielerreichung == "Controlled revised report."


@pytest.mark.anyio
@pytest.mark.parametrize("template_key", list(TEMPLATE_REGISTRY))
async def test_next_field_uses_session_template_and_last_field_finalizes(template_key):
    engine, session, generator = make_engine(template_key=template_key)
    first, second, *_ = session.sections
    for sid, section in session.sections.items():
        section.is_active_field = sid in (first, second)
        section.bericht_zielerreichung = "Existing draft."
        if not section.is_active_field:
            section.status = SectionStatus.CONFIRMED
    await engine.confirm_section(session.session_id, first)
    expected = TEMPLATE_REGISTRY[template_key].section_definitions[1]
    assert generator.calls[-1] == ("goals", {
        "section": expected, "entries": [], "leitziel": "", "smarte_ziele": "",
        "client": session.client, "developments": "",
    })
    assert session.active_section_id == second
    assert expected.title in session.messages[-1].content
    await engine.confirm_section(session.session_id, second)
    assert session.phase == DialoguePhase.FINALIZED
    assert len(generator.calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("notes", ["", "Besondere Unterstützung bei der Wäsche."])
async def test_initial_notes_are_scoped_and_survive_first_turn(notes):
    generator = RecordingGenerator()
    template = TEMPLATE_REGISTRY["hamburg_soziale_dienste"]
    imported = VivendiImport(client=ClientData(name="Mustermann"), meta=ReportMeta(), entries=[])
    class Importer:
        def load(self):
            return imported
    template = replace(template, importer_factory=lambda _: Importer())
    engine = ChatDialogueEngine(generator, template=template, router=DialogueIntentRouter())
    session = await engine.start_session(Path("synthetic.csv"), freitext=notes)
    await engine.process_message(session.session_id, "Passt.")
    await engine.process_message(session.session_id, "Entwurf erstellen")
    if "Besondere" in notes:
        first = next(iter(session.sections))
        entries = session.memory.section_entries[first]
        assert [entry.text for entry in entries] == ["Besondere Unterstützung bei der Wäsche."]
        refined = next(kwargs for name, kwargs in generator.calls if name == "refine")
        assert [entry.text for entry in refined["entries"]] == ["Besondere Unterstützung bei der Wäsche."]
        assert all(not entries for sid, entries in session.memory.section_entries.items() if sid != first)
