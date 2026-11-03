"""Typisierte ReportDraft-Modelle (ADR-5).

Jedes FORMTEXT-/FORMCHECKBOX-Feld der Vorlage.docx hat ein benanntes
Pydantic-Feld. Die Bookmark-Namen (ADR-10) verdoppeln die Modellpfade 1:1
(siehe docs/field-mapping.md). `DocumentationEntry` lebt im LLM-Output-Vertrag
(`llm.schemas`) und wird dort wiederverwendet, nicht hier dupliziert.
"""
from __future__ import annotations

from typing import Dict, List

from pydantic import BaseModel, Field

from sob_generator.llm.schemas import DocumentationEntry


class ClientData(BaseModel):
    """Klientenstammdaten (ADR-5, `ReportDraft.client`)."""

    name: str = ""
    vorname: str = ""
    # Anrede für den Fließtext (#26/#27): "Herr"/"Frau" oder "" (= "keine",
    # Default/Fallback für divers/unbekannt). Die Vivendi-CSV liefert kein
    # Geschlecht, daher manuell im Upload-Formular gesetzt. "" → Fließtext ohne
    # Anrede, nur Nachname.
    anrede: str = ""
    geburtsdatum: str = ""
    anschrift: str = ""
    gesetzliche_betreuung: bool = False
    gdb: str = ""
    pflegegrad: str = ""
    leistungsart: str = ""


class ReportMeta(BaseModel):
    """Berichtskopf-Metadaten (ADR-5, `ReportDraft.meta`)."""

    leistungszeitraum_von: str = ""
    leistungszeitraum_bis: str = ""
    abwesenheitszeiten: str = ""
    leistungsanbieter: str = ""
    begruegnungsstaette: str = ""
    bezugsbetreuer: str = ""


class ReportSection(BaseModel):
    """Ein SOB-Abschnitt (ADR-5, `ReportDraft.sections[N]`).

    `leitziel`/`smarte_ziele` stammen aus dem alten SOB (ADR-4), die übrigen
    vier Felder vom LLM (ADR-11). `previous_goals_apply=False` entspricht der
    angekreuzten „entfällt"-Checkbox (keine Ziele im Gesamtplan vereinbart).
    """

    id: str
    title: str
    previous_goals_apply: bool = True
    leitziel: str = ""
    smarte_ziele: str = ""
    bericht_zielerreichung: str = ""
    vorschlag_ziele: str = ""
    vorschlag_massnahmen: str = ""
    realisierung_andere: str = ""
    # True, wenn die automatische Generierung scheiterte (#18) — der Abschnitt
    # muss manuell geprüft/ergänzt werden, statt stumm leer im Bericht zu landen.
    needs_review: bool = False
    # True, wenn Pass 0 diesem Abschnitt KEINE Dokumentationseinträge zuordnete
    # und kein Freitext vorlag (#50/K1). Das ist Datenlage, kein Fehler — die
    # Anzeige (Renderer/Review-UI) unterscheidet damit „keine Doku vorhanden"
    # vom echten Generierungs-Fehlschlag (needs_review ohne no_entries).
    no_entries: bool = False


class ReportDraft(BaseModel):
    """Das zentrale interne Datenmodell (ADR-3/ADR-5)."""

    client: ClientData
    meta: ReportMeta
    developments: str = ""
    developments_needs_review: bool = False
    sections: List[ReportSection] = Field(default_factory=list)
    # Einträge, die Pass 0 keinem Abschnitt zuordnen konnte (#50/K1). Sie
    # erscheinen in keinem Abschnitts-Call — die Fachkraft muss sie im Review
    # sehen und ggf. manuell einarbeiten (Human-in-the-Loop, ADR-3).
    unclear_entries: List[DocumentationEntry] = Field(default_factory=list)
    # Diagnose-Mapping aus Pass 0 (#50/K2): Abschnitts-ID (inkl. "unklar") →
    # Kurzlabels der zugeordneten Einträge. Nur für Log/Quality-Report, wird
    # nicht gerendert.
    classification: Dict[str, List[str]] = Field(default_factory=dict)

    @property
    def pending_review(self) -> List[str]:
        """IDs der review-bedürftigen Bausteine (#18) — leer, wenn alles ok."""
        pending = [s.id for s in self.sections if s.needs_review]
        if self.developments_needs_review:
            pending.append("developments")
        if self.unclear_entries:
            pending.append(f"unklar ({len(self.unclear_entries)} Einträge)")
        return pending
