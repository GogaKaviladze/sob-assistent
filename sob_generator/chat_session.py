"""Chat session state machine for interactive SOB generation.

Manages conversational state, multi-turn messages, section clarification status,
and conversion to/from ReportDraft.
"""
from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from sob_generator.llm.schemas import DocumentationEntry
from sob_generator.importer import VivendiImport
from sob_generator.models import ClientData, ReportDraft, ReportMeta, ReportSection
from sob_generator.previous_sob import PreviousGoals


class ChatRole(str, Enum):
    """Role of a message participant."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class DialoguePhase(str, Enum):
    """Phases of the structured, collaborative SVB dialogue protocol."""

    INTAKE = "intake"
    SCREENING = "screening"
    FIELD_REFLECT = "field_reflect"
    FIELD_GOALS = "field_goals"
    FIELD_QUESTIONS = "field_questions"
    FIELD_DRAFT = "field_draft"
    FIELD_REVIEW = "field_review"
    FINALIZED = "finalized"


class SectionStatus(str, Enum):
    """Lifecycle status of a report section during interactive generation."""

    EMPTY = "empty"
    NEEDS_CLARIFICATION = "needs_clarification"
    DRAFTED = "drafted"
    CONFIRMED = "confirmed"


class ReflectionSuggestion(BaseModel):
    """A structured response suggestion that prompts pedagogical reflection."""

    label: str
    text: str
    action: str = ""


class ChatMessage(BaseModel):
    """A single turn in the interactive dialogue."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    role: ChatRole
    content: str
    timestamp: float = Field(default_factory=time.time)
    section_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SectionChatState(BaseModel):
    """Interactive state and draft for a specific report section."""

    section_id: str
    title: str
    is_active_field: bool = True
    screening_rationale: str = ""
    status: SectionStatus = SectionStatus.EMPTY
    previous_goals_apply: bool = True
    leitziel: str = ""
    smarte_ziele: str = ""
    bericht_zielerreichung: str = ""
    fachliche_einordnung: str = ""
    vorschlag_ziele: str = ""
    vorschlag_massnahmen: str = ""
    realisierung_andere: str = ""
    gaps: List[str] = Field(default_factory=list)
    clarifying_questions: List[str] = Field(default_factory=list)
    user_notes: List[str] = Field(default_factory=list)
    proposed_goals: str = ""
    confirmed_goals: str = ""
    pending_question: str = ""
    uncertainties: List[str] = Field(default_factory=list)
    turn_count: int = 0

    @property
    def current_goals(self) -> str:
        return self.proposed_goals or self.confirmed_goals


    def to_report_section(self) -> ReportSection:
        """Converts state to final ReportSection."""
        full_bericht = self.bericht_zielerreichung
        if self.fachliche_einordnung:
            full_bericht = f"{full_bericht}\n\nFachliche Einordnung:\n{self.fachliche_einordnung}".strip()

        return ReportSection(
            id=self.section_id,
            title=self.title,
            previous_goals_apply=self.previous_goals_apply,
            leitziel=self.leitziel,
            smarte_ziele=self.smarte_ziele,
            bericht_zielerreichung=full_bericht,
            vorschlag_ziele=self.vorschlag_ziele,
            vorschlag_massnahmen=self.vorschlag_massnahmen,
            realisierung_andere=self.realisierung_andere,
            needs_review=(self.status != SectionStatus.CONFIRMED and self.is_active_field),
        )


class ClientWorkingMemory(BaseModel):
    """Working memory for resource-efficient, portioned SOB generation."""

    core_summary: str = ""
    learned_facts: List[str] = Field(default_factory=list)
    section_entries: Dict[str, List[DocumentationEntry]] = Field(default_factory=dict)


SECTION_PHASES = {
    DialoguePhase.FIELD_REFLECT,
    DialoguePhase.FIELD_GOALS,
    DialoguePhase.FIELD_QUESTIONS,
    DialoguePhase.FIELD_DRAFT,
    DialoguePhase.FIELD_REVIEW,
}


class ChatSession(BaseModel):
    """Complete in-memory session for an interactive SOB generation process."""

    session_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    client: ClientData = Field(default_factory=ClientData)
    meta: ReportMeta = Field(default_factory=ReportMeta)
    report_type: str = "Folgebericht"
    phase: DialoguePhase = DialoguePhase.SCREENING
    developments: str = ""
    developments_confirmed: bool = False
    sections: Dict[str, SectionChatState] = Field(default_factory=dict)
    memory: ClientWorkingMemory = Field(default_factory=ClientWorkingMemory)
    active_section_id: Optional[str] = None
    messages: List[ChatMessage] = Field(default_factory=list)
    unclear_entries: List[DocumentationEntry] = Field(default_factory=list)
    template_key: str = "hamburg_soziale_dienste"

    @classmethod
    def from_import(
        cls,
        imported: VivendiImport,
        previous_goals: Optional[Dict[str, PreviousGoals]] = None,
        section_titles: Optional[Dict[str, str]] = None,
        section_entries: Optional[Dict[str, List[DocumentationEntry]]] = None,
        template_key: str = "hamburg_soziale_dienste",
    ) -> ChatSession:
        """Initializes a new session from imported Vivendi and previous SOB data."""
        previous_goals = previous_goals or {}
        section_titles = section_titles or {}
        section_entries = section_entries or {sid: [] for sid in section_titles}

        sections: Dict[str, SectionChatState] = {}
        for sid, title in section_titles.items():
            pg = previous_goals.get(sid, PreviousGoals(leitziel="", smarte_ziele="", applies=True))
            sections[sid] = SectionChatState(
                section_id=sid,
                title=title,
                status=SectionStatus.EMPTY,
                previous_goals_apply=pg.applies,
                leitziel=pg.leitziel,
                smarte_ziele=pg.smarte_ziele,
            )

        active_sid = next(iter(sections.keys())) if sections else None

        return cls(
            client=imported.client,
            meta=imported.meta,
            sections=sections,
            memory=ClientWorkingMemory(section_entries=section_entries),
            active_section_id=active_sid,
            phase=DialoguePhase.SCREENING,
            template_key=template_key,
        )

    def add_user_message(self, content: str, section_id: Optional[str] = None, *, capture_facts: bool = True) -> ChatMessage:
        """Appends a user message to the session and records it as a section note."""
        if section_id is not None:
            sid = section_id
        elif self.phase in SECTION_PHASES:
            sid = self.active_section_id
        else:
            sid = None

        public_note = content.strip()

        msg = ChatMessage(role=ChatRole.USER, content=public_note, section_id=sid)
        self.messages.append(msg)

        if sid and sid in self.sections:
            if capture_facts and public_note and public_note not in self.sections[sid].user_notes:
                self.sections[sid].user_notes.append(public_note)
            self.sections[sid].turn_count += 1

        if capture_facts and public_note and public_note not in self.memory.learned_facts:
            self.memory.learned_facts.append(public_note)
        return msg

    def get_section_messages(self, section_id: str) -> List[ChatMessage]:
        """Returns exclusively messages belonging to the given section (strict isolation)."""
        return [m for m in self.messages if m.section_id == section_id]

    def get_section_notes(self, section_id: str) -> List[str]:
        """Returns scoped user notes for the target section."""
        if section_id not in self.sections:
            return []
        return list(self.sections[section_id].user_notes)

    def add_assistant_message(
        self,
        content: str,
        section_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ChatMessage:
        """Appends an assistant message to the session."""
        if section_id is not None:
            sid = section_id
        elif self.phase in SECTION_PHASES:
            sid = self.active_section_id
        else:
            sid = None

        msg = ChatMessage(
            role=ChatRole.ASSISTANT,
            content=content,
            section_id=sid,
            metadata=metadata or {},
        )
        self.messages.append(msg)
        return msg

    def add_development_note(self, note: str) -> None:
        """Adds a general development / life event note to the session."""
        note = note.strip()
        if not note:
            return
        if self.developments:
            self.developments = f"{self.developments}\n{note}"
        else:
            self.developments = note

    def set_active_section(self, section_id: str) -> None:
        """Switches current focus to target section."""
        if section_id not in self.sections:
            raise KeyError(f"Unknown section id: {section_id}")
        self.active_section_id = section_id

    def update_section_draft(
        self,
        section_id: str,
        bericht_zielerreichung: Optional[str] = None,
        vorschlag_ziele: Optional[str] = None,
        vorschlag_massnahmen: Optional[str] = None,
        realisierung_andere: Optional[str] = None,
        status: Optional[SectionStatus] = None,
    ) -> None:
        """Updates draft text and status for a specific section."""
        if section_id not in self.sections:
            raise KeyError(f"Unknown section id: {section_id}")
        sec = self.sections[section_id]
        if bericht_zielerreichung is not None:
            sec.bericht_zielerreichung = bericht_zielerreichung
        if vorschlag_ziele is not None:
            sec.vorschlag_ziele = vorschlag_ziele
        if vorschlag_massnahmen is not None:
            sec.vorschlag_massnahmen = vorschlag_massnahmen
        if realisierung_andere is not None:
            sec.realisierung_andere = realisierung_andere
        if status is not None:
            sec.status = status
        elif sec.status == SectionStatus.EMPTY:
            sec.status = SectionStatus.DRAFTED

    def confirm_section(self, section_id: str) -> None:
        """Marks a section as verified and confirmed by the social worker."""
        if section_id not in self.sections:
            raise KeyError(f"Unknown section id: {section_id}")
        self.sections[section_id].status = SectionStatus.CONFIRMED

    def is_all_confirmed(self) -> bool:
        """Checks if all sections have been confirmed."""
        return all(s.status == SectionStatus.CONFIRMED for s in self.sections.values())

    def to_report_draft(self) -> ReportDraft:
        """Builds final ReportDraft from current session state."""
        return ReportDraft(
            client=self.client,
            meta=self.meta,
            developments=self.developments,
            developments_needs_review=not self.developments_confirmed,
            sections=[s.to_report_section() for s in self.sections.values()],
            unclear_entries=self.unclear_entries,
        )

    def get_reflection_suggestions(self) -> List[ReflectionSuggestion]:
        """Returns 2 structured response suggestions tailored to the current dialogue phase."""
        if self.phase == DialoguePhase.FINALIZED:
            return []

        section = self.sections.get(self.active_section_id)
        title = section.title if section else "dieses Lebensfeld"
        if self.phase == DialoguePhase.SCREENING:
            return [
                ReflectionSuggestion(label="Einteilung bestätigen", text="Einteilung passt."),
                ReflectionSuggestion(label="Einteilung besprechen", text="Wie kann ich die Einteilung ändern?"),
            ]
        if self.phase in (DialoguePhase.FIELD_DRAFT, DialoguePhase.FIELD_REVIEW):
            return [
                ReflectionSuggestion(label="Berichtstext straffen", text="Bitte den Bericht kompakter formulieren.", action="shorten_draft"),
                ReflectionSuggestion(label="Ziele konkretisieren", text="Bitte die Zielvorschläge konkretisieren; fehlende Angaben offen lassen.", action="revise_goals"),
            ]
        return [
            ReflectionSuggestion(label="Ziel erklären", text=f"Warum schlägst du dieses Ziel für {title} vor?", action="explain_goal"),
            ReflectionSuggestion(label="Beobachtung beschreiben", text=f"Hilf mir, eine Beobachtung zu {title} von einer Interpretation zu unterscheiden.", action="describe_observation"),
        ]
