"""Abstraktion über die LLM-Generierung (DIP).

Der Orchestrator hängt nur von diesem Protokoll ab, nicht vom konkreten
`llm.generator.LLMDraftGenerator`. Das echte `LLMDraftGenerator` erfüllt das
Protokoll strukturell bereits; in Tests und für Smoke-Läufe ohne Modell tritt
`StubDraftGenerator` an seine Stelle (ADR-2: kein echtes Modell in dieser
Session).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional, Protocol, runtime_checkable

from sob_generator.llm.generator import GenerationResult
from sob_generator.llm.schemas import (
    DevelopmentsLLMOutput,
    GoalProposalOutput,
    DialogueTurnOutput,
    DocumentationEntry,
    GapAnalysisOutput,
    ScreeningTurnOutput,
    SectionLLMOutput,
)
from sob_generator.llm.sections import SectionDefinition

if TYPE_CHECKING:
    from sob_generator.models import ClientData


@runtime_checkable
class DraftGenerator(Protocol):
    """Schnittstelle der LLM-Calls: Pass-0-Klassifikation + 9 Abschnitte + developments + Dialog."""

    def classify_entry(
        self,
        entry: DocumentationEntry,
        sections: List[SectionDefinition],
    ) -> str: ...

    def classify_entries_batch(
        self,
        entries: List[DocumentationEntry],
        sections: List[SectionDefinition],
    ) -> dict[str, list[DocumentationEntry]]: ...

    def generate_section(
        self,
        section: SectionDefinition,
        entries: List[DocumentationEntry],
        leitziel: str = "",
        smarte_ziele: str = "",
        previous_goals_apply: bool = True,
        freitext: str = "",
        anrede: str = "",
        nachname: str = "",
        vorname: str = "",
        forbidden_openers: tuple[str, ...] = (),
    ) -> "GenerationResult[SectionLLMOutput]": ...

    def refine_section(
        self,
        section: SectionDefinition,
        entries: List[DocumentationEntry],
        leitziel: str = "",
        smarte_ziele: str = "",
        previous_goals_apply: bool = True,
        freitext: str = "",
        user_notes: List[str] = (),
        anrede: str = "",
        nachname: str = "",
        vorname: str = "",
        forbidden_openers: tuple[str, ...] = (),
        developments: str = "",
    ) -> "GenerationResult[SectionLLMOutput]": ...

    def revise_section(
        self,
        section: SectionDefinition,
        current_draft: SectionLLMOutput,
        critique: str,
        entries: List[DocumentationEntry],
        leitziel: str = "",
        smarte_ziele: str = "",
        previous_goals_apply: bool = True,
        freitext: str = "",
        user_notes: List[str] = (),
        anrede: str = "",
        nachname: str = "",
        vorname: str = "",
        forbidden_openers: tuple[str, ...] = (),
        developments: str = "",
    ) -> "GenerationResult[SectionLLMOutput]": ...

    def analyze_section_gaps(
        self,
        section: SectionDefinition,
        entries: List[DocumentationEntry],
        leitziel: str = "",
        smarte_ziele: str = "",
        previous_goals_apply: bool = True,
        freitext: str = "",
        user_notes: List[str] = (),
        anrede: str = "",
        nachname: str = "",
        vorname: str = "",
    ) -> "GenerationResult[GapAnalysisOutput]": ...

    def generate_developments(
        self,
        entries: List[DocumentationEntry],
        previous_goals_summary: str = "",
        freitext: str = "",
        anrede: str = "",
        nachname: str = "",
        vorname: str = "",
    ) -> "GenerationResult[DevelopmentsLLMOutput]": ...

    def generate_field_goals(
        self,
        section: SectionDefinition,
        entries: List[DocumentationEntry],
        leitziel: str = "",
        smarte_ziele: str = "",
        client: Optional[ClientData] = None,
        developments: str = "",
    ) -> "GenerationResult[GoalProposalOutput]": ...

    def revise_field_goals(
        self,
        section: SectionDefinition,
        current_goals: str,
        critique: str,
        entries: List[DocumentationEntry],
        client: Optional[ClientData] = None,
    ) -> "GenerationResult[GoalProposalOutput]": ...

    def converse(self, context: dict) -> "GenerationResult[DialogueTurnOutput]": ...

    def generate_field_questions(
        self,
        section: SectionDefinition,
        entries: List[DocumentationEntry],
        confirmed_goals: str = "",
        client: Optional[ClientData] = None,
        user_notes: List[str] = (),
        is_living_draft: bool = False,
    ) -> str: ...

    def classify_intent(
        self,
        text: str,
        phase: "DialoguePhase",
    ) -> Optional["DialogueIntent"]: ...

    def process_screening_turn(
        self,
        message: str,
        sections: List[SectionDefinition],
        active_section_ids: List[str] | set[str],
        client: Optional[ClientData] = None,
    ) -> ScreeningTurnOutput: ...
