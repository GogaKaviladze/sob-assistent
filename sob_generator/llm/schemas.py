"""LLM-Output-Vertrag (ADR-11): Pydantic-Modelle für die vom LLM erzeugten Felder.

Diese Modelle sind die Quelle für die GBNF-Grammar (siehe grammar.py) und für
die Validierung der grammar-erzwungenen JSON-Antwort. Sie sind bewusst flach
(nur `str`-Felder) — jede Wurzel-Eigenschaft entspricht 1:1 einem
ADR-5-Feld bzw. ADR-10-Bookmark.
"""
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

# Alle gültigen Abschnitts-IDs der Hamburger SOB-Vorlage + Fallback "unklar".
SectionId = Literal["1.1", "1.2", "1.3", "1.4", "1.5", "2.1", "2.2", "2.3", "3", "unklar"]


class ScreeningTurnOutput(BaseModel):
    """Strukturierte Ausgabe des LLMs für einen Dialogschritt in der Screening-Phase (Issue #69)."""

    reply: str = Field(
        description="Natürliche, fachliche und empathische Antwort des Assistenten an die Fachkraft. "
        "Keine starren Schablonen, keine Floskeln.",
    )
    to_activate: list[str] = Field(
        default_factory=list,
        description="Liste der Lebensfeld-IDs (z. B. ['1.1'] oder ['4']), die aktiviert werden sollen.",
    )
    to_deactivate: list[str] = Field(
        default_factory=list,
        description="Liste der Lebensfeld-IDs (z. B. ['1.1'] oder ['4']), die deaktiviert oder auf erledigt/stabil gesetzt werden sollen.",
    )
    is_confirmed: bool = Field(
        default=False,
        description="True, wenn das Screening insgesamt bestätigt wurde und in die Detailreflexion übergegangen werden soll.",
    )
    development_note: Optional[str] = Field(
        default=None,
        description="Extrahierte Meilensteine, biografische Entwicklungen oder wichtige Lebensereignisse für den Gesamtbericht, falls in der Eingabe enthalten.",
    )
    first_active_id: Optional[str] = Field(
        default=None,
        description="Optional: ID des Lebensfeldes, das zuerst bearbeitet werden soll (z. B. bei Tausch oder Priorisierung).",
    )


class EntryClassification(BaseModel):
    """Klassifikationsergebnis für einen einzelnen Dokumentationseintrag (Pass 0)."""

    section_id: SectionId


class BatchClassificationItem(BaseModel):
    """Einzelne Klassifikation innerhalb eines Batches."""

    id: int = Field(description="0-basierter Index des Eintrags in der Batch-Liste")
    section_id: SectionId = Field(
        description="Zugeordnete Abschnitts-ID (z. B. '1.1', '1.2', ..., '3' oder 'unklar')"
    )


class BatchEntryClassification(BaseModel):
    """Batch-Klassifikationsergebnis für mehrere Dokumentationseinträge (Pass 0 Batch)."""

    classifications: list[BatchClassificationItem] = Field(
        default_factory=list,
        description="Liste der Zuordnungen für jeden Eintrag",
    )


class GapAnalysisOutput(BaseModel):
    """Ergebnis der Lücken- und Unklarheitenanalyse eines Abschnitts."""

    has_gaps: bool = Field(
        default=False,
        description="True, wenn wesentliche Informationen oder Fortschritte fehlen.",
    )
    gaps: list[str] = Field(
        default_factory=list,
        description="Liste erkannter Lücken, Unklarheiten oder fehlender Zieldokumentation.",
    )
    clarifying_question: str = Field(
        default="",
        description="Gezielte Frage an die Fachkraft zur Schließung der Lücke.",
    )


class DocumentationEntry(BaseModel):
    """Ein Eintrag aus dem Vivendi-CSV-Export (ADR-4) bzw. dem Freitext (ADR-7)."""

    datum: str
    kategorie: str
    text: str
    schicht: str = ""
    uhrzeit: str = ""


class SectionLLMOutput(BaseModel):
    """Die 4 vom LLM generierten Felder eines Abschnitts (ADR-5, sections[N])."""

    bericht_zielerreichung: str = Field(
        description="Bericht zur Zielerreichung / ausgeführte Maßnahme bzw. "
        "Methodik zur Zielerreichung im Berichtszeitraum."
    )

    @field_validator("bericht_zielerreichung")
    @classmethod
    def bericht_nicht_leer(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("bericht_zielerreichung darf nicht leer sein")
        return v

    vorschlag_ziele: str = Field(
        description="Vorschlag für aktuelle (smarte) Ziele im nächsten "
        "Bewilligungszeitraum."
    )
    vorschlag_massnahmen: str = Field(
        description="Vorschlag für Maßnahmen zur Erreichung der "
        "vorgeschlagenen Ziele."
    )

    @field_validator("vorschlag_ziele", "vorschlag_massnahmen")
    @classmethod
    def vorschlag_nicht_leer(cls, v: str) -> str:
        # #48: ohne Validator war "" valide — der Retry in
        # LLMDraftGenerator._generate wurde nie ausgelöst, leere Vorschläge
        # landeten ohne needs_review-Alarm im Draft. Der leere Fallback
        # (_empty_instance) nutzt model_construct und bleibt unberührt.
        if not v.strip():
            raise ValueError("Pflichtfeld darf nicht leer sein")
        return v
    realisierung_andere: str = Field(
        description="Realisierung durch andere Leistungserbringer "
        "(z.B. Therapie, Schule) — leerer String, falls nicht zutreffend."
    )


class DevelopmentsLLMOutput(BaseModel):
    """Das Top-Level-Feld `developments` (ADR-5) — wesentliche aktuelle
    Entwicklungen im Berichtszeitraum, abschnittsübergreifend."""

    developments: str = Field(
        description="Wesentliche aktuelle Entwicklungen im Berichtszeitraum, "
        "über alle Lebensbereiche hinweg."
    )


class GoalProposalOutput(BaseModel):
    """Report-ready proposal separate from conversational explanation."""

    goals: str = Field(description="Nur Zielinhalt; leer bei fehlender Grundlage.")
    reply: str = Field(min_length=1, description="Kurze Erläuterung und eine Rückfrage.")


class DialogueTurnOutput(BaseModel):
    """Scoped conversation and evidence-backed note extraction."""

    public_note: str = Field(description="Wörtlicher relevanter Auszug der aktuellen Eingabe oder leer.")
    supersedes: list[int] = Field(description="0-basierte Indizes ausdrücklich korrigierter früherer Notizen.")
    pending_question: str
    sufficient: bool = Field(description="Konkrete Antwort auf die offene Frage, keine bloße Unsicherheit.")
    reply: str = Field(min_length=1)


def dialogue_output_contract(message: str):
    """Constrain extraction to verbatim sentence candidates, including qualifiers."""
    import re
    from pydantic import create_model
    candidates = tuple(dict.fromkeys(["", message, *re.split(r"(?<=[.!?])\s+", message)]))
    return create_model("ExtractiveDialogueTurn", __base__=DialogueTurnOutput,
        public_note=(Literal[candidates], ...))


class GroundingCheckOutput(BaseModel):
    """Independent source check; any uncertainty prevents draft acceptance."""
    unsupported_claims: list[str]
    supported: bool
