"""StubDraftGenerator — deterministischer Ersatz für das echte LLM (ADR-2).

Erzeugt ohne GGUF-Modell gültige `SectionLLMOutput`/`DevelopmentsLLMOutput` und
erfüllt damit das `DraftGenerator`-Protokoll. Zweck: den mechanischen Durchstich
(Import → Render) end-to-end laufen und prüfen lassen, bevor ein echtes Modell
verdrahtet wird (separater Schritt, Runbook in Issue #17). Der Text referenziert
die Eintragszahl, damit das erzeugte docx sichtbar von der Eingabe abhängt.
"""
from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, List, Optional

from sob_generator.llm.generator import GenerationResult
from sob_generator.llm.schemas import (
    DevelopmentsLLMOutput,
    DocumentationEntry,
    GapAnalysisOutput,
    ScreeningTurnOutput,
    SectionLLMOutput,
)
from sob_generator.llm.sections import SectionDefinition

if TYPE_CHECKING:
    from sob_generator.models import ClientData


class StubDraftGenerator:
    """Fake-Generator für Tests und Smoke-Läufe ohne Modell."""

    def classify_entry(
        self,
        entry: DocumentationEntry,
        sections: List[SectionDefinition],
    ) -> str:
        """Verteilt Einträge per Hash-Modulo gleichmäßig auf die Abschnitte.

        hashlib statt eingebautem hash(): Pythons String-Hash ist pro Prozess
        randomisiert (PYTHONHASHSEED), das würde die Zuordnung zwischen
        Testläufen instabil machen.
        """
        digest = hashlib.sha256(entry.text.encode("utf-8")).digest()
        return sections[int.from_bytes(digest[:8], "big") % len(sections)].id

    def classify_entries_batch(
        self,
        entries: List[DocumentationEntry],
        sections: List[SectionDefinition],
    ) -> dict[str, list[DocumentationEntry]]:
        """Deterministische Batch-Klassifikation für Tests ohne LLM."""
        result: dict[str, list[DocumentationEntry]] = {s.id: [] for s in sections}
        result["unklar"] = []
        for entry in entries:
            sid = self.classify_entry(entry, sections)
            if sid in result:
                result[sid].append(entry)
            else:
                result["unklar"].append(entry)
        return result

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
    ) -> "GenerationResult[SectionLLMOutput]":
        n = len(entries)
        # Der Opener trägt id+Titel zuerst → je Abschnitt distinkter 3-Wort-
        # Anfang, damit der Opener-Guard (#36) am Stub nicht sinnlos anschlägt.
        value = SectionLLMOutput(
            bericht_zielerreichung=(
                f"[STUB] {section.id} {section.title}: Entwurf auf Basis "
                f"von {n} Dokumentationseintraegen."
            ),
            vorschlag_ziele=(
                f"[STUB] Ziel A fuer {section.title}.\n"
                "- [STUB] Ziel B im naechsten Bewilligungszeitraum."
            ),
            vorschlag_massnahmen=f"[STUB] Massnahme zur Zielerreichung in {section.id}.",
            realisierung_andere="",
        )
        return GenerationResult(value=value, needs_review=False, raw_output="<stub>")

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
    ) -> "GenerationResult[SectionLLMOutput]":
        base_res = self.generate_section(
            section=section,
            entries=entries,
            leitziel=leitziel,
            smarte_ziele=smarte_ziele,
            previous_goals_apply=previous_goals_apply,
            freitext=freitext,
            anrede=anrede,
            nachname=nachname,
            vorname=vorname,
            forbidden_openers=forbidden_openers,
        )
        if user_notes:
            notes_summary = " | ".join(user_notes)
            refined_val = SectionLLMOutput(
                bericht_zielerreichung=f"{base_res.value.bericht_zielerreichung} (Ergänzt durch Dialog: {notes_summary})",
                vorschlag_ziele=base_res.value.vorschlag_ziele,
                vorschlag_massnahmen=base_res.value.vorschlag_massnahmen,
                realisierung_andere=base_res.value.realisierung_andere,
            )
            return GenerationResult(value=refined_val, needs_review=False, raw_output="<stub-refined>")
        return base_res

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
    ) -> "GenerationResult[SectionLLMOutput]":
        """Markiert die simulierte Überarbeitung ohne Redaktionsauftrag als Berichtsfakt."""
        value = current_draft.model_copy(deep=True)
        if not critique.strip():
            return GenerationResult(
                value=value,
                needs_review=True,
                raw_output="<stub-revision>",
                error="Kein Änderungsauftrag für die Überarbeitung angegeben.",
            )
        value.bericht_zielerreichung = f"[STUB-REVISION] {value.bericht_zielerreichung}"
        return GenerationResult(value=value, needs_review=False, raw_output="<stub-revision>")

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
    ) -> "GenerationResult[GapAnalysisOutput]":
        if not entries and not freitext and not user_notes:
            value = GapAnalysisOutput(
                has_gaps=True,
                gaps=[f"Keine Dokumentationseinträge für {section.title} vorhanden."],
                clarifying_question=f"Gibt es aktuelle Entwicklungen im Bereich {section.title}?",
            )
        else:
            value = GapAnalysisOutput(has_gaps=False, gaps=[], clarifying_question="")
        return GenerationResult(value=value, needs_review=False, raw_output="<stub-gap>")

    def generate_developments(
        self,
        entries: List[DocumentationEntry],
        previous_goals_summary: str = "",
        freitext: str = "",
        anrede: str = "",
        nachname: str = "",
        vorname: str = "",
    ) -> "GenerationResult[DevelopmentsLLMOutput]":
        value = DevelopmentsLLMOutput(
            developments=(
                f"[STUB] Wesentliche aktuelle Entwicklungen aus {len(entries)} "
                "Dokumentationseintraegen im Berichtszeitraum."
            )
        )
        return GenerationResult(value=value, needs_review=False, raw_output="<stub>")

    def generate_field_goals(self, section, entries, leitziel="", smarte_ziele="", client=None, developments=""):
        from sob_generator.llm.schemas import GoalProposalOutput
        goals = smarte_ziele
        reply = f"Für Lebensfeld {section.id}: {section.title} liegen {len(entries)} Dokumentationseinträge vor. "
        reply += "Welchen Wunsch der Person möchtest du als Ziel aufgreifen?"
        return GenerationResult(GoalProposalOutput(goals=goals, reply=reply), False, "<stub>")

    def revise_field_goals(self, section, current_goals, critique, entries, client=None):
        from sob_generator.llm.schemas import GoalProposalOutput
        # The fallback cannot reliably interpret arbitrary editing instructions.
        return GenerationResult(GoalProposalOutput(goals=current_goals, reply="Bitte bearbeite die Ziele direkt oder versuche die Generierung erneut."), True, "<stub>", "No model for goal revision")

    def converse(self, context: dict):
        from sob_generator.conversation import fallback_turn
        return GenerationResult(fallback_turn(context), False, "<stub>")

    def generate_field_questions(
        self,
        section: SectionDefinition,
        entries: List[DocumentationEntry],
        confirmed_goals: str = "",
        client: Optional[ClientData] = None,
        user_notes: List[str] = (),
        is_living_draft: bool = False,
    ) -> str:
        client_name = (
            f"{client.anrede} {client.name}".strip()
            if (client and client.anrede and client.name)
            else (client.vorname if client else "die betreute Person")
        )
        n = len(entries)
        is_psych = section.id in ("1", "1.4") or any(
            kw in section.title.lower() for kw in ("psychisch", "emotional", "selbstsorge")
        )

        if is_living_draft:
            if is_psych:
                return (
                    f"Welche konkreten Deeskalations- oder Assistenzabsprachen greifen bei {client_name} im Alltag bei beginnender Überforderung am besten, "
                    f"und welche Hilfsmittel oder Notfallkontakte sollen verbindlich im Bericht festgehalten werden?"
                )
            return (
                f"Welche konkreten Assistenzleistungen, Hilfsmittel oder Routinen unterstützen {client_name} im Bereich {section.title} aktuell am wirksamsten, "
                f"und welche Hürden zeigen sich noch im Alltag?"
            )

        return (
            f"Gut — dann kommen jetzt meine gezielten Rückfragen zu **Lebensfeld {section.id} ({section.title})**, bevor ich den Textentwurf formuliere:\n\n"
            f"1. **Begleitungsdynamik & Hilfebedarf**: Mit Blick auf den Verlauf ({n} Einträge): Wie gestaltet sich die konkrete Zusammenarbeit mit {client_name} im Bereich {section.title} und wo besteht Unterstützungsbedarf?\n"
            f"2. **Entwicklung & Meilensteine**: Welche konkreten Fortschritte oder Hemmnisse zeigten sich im Berichtszeitraum bezüglich {confirmed_goals or section.title}?\n"
            f"3. **Krisen & Externe Hilfen**: Gibt es besondere Herausforderungen, Krisenmomente oder therapeutische Maßnahmen, die festgehalten werden sollten?\n\n"
            "Du kannst fachliche Fragen und Unsicherheiten normal formulieren; sie werden nicht automatisch als Berichtstatsachen übernommen."
        )

    def classify_intent(
        self,
        text: str,
        phase: "DialoguePhase",
    ) -> Optional["DialogueIntent"]:
        """Deterministic stub classifier - returns None to fall back to rule router."""
        return None

    def process_screening_turn(
        self,
        message: str,
        sections: List[SectionDefinition],
        active_section_ids: List[str] | set[str],
        client: Optional[ClientData] = None,
    ) -> ScreeningTurnOutput:
        text_stripped = message.strip()
        text_lower = text_stripped.lower()

        # Negation check
        is_negated = bool(
            re.search(
                r"\b(?:nicht|nein|kein|keine|keineswegs|überhaupt\s+nicht|stimmt\s+nicht|gar\s+nicht|bloß\s+nicht|keinesfalls|abgelehnt|falsch|unpassend)\b",
                text_lower,
            )
        )

        # 1. Swaps: e.g. "1.1 und 2.1 tauschen" or "4 und 2 tauschen"
        swap_match = re.search(
            r"(?:(\d(?:\.\d)?)\s*(?:und|mit)\s*(\d(?:\.\d)?)\s*tauschen|tausche\s*(\d(?:\.\d)?)\s*(?:und|mit)\s*(\d(?:\.\d)?)|(\d(?:\.\d)?)\s*vor\s*(\d(?:\.\d)?))",
            text_lower,
        )
        if swap_match:
            groups = [g for g in swap_match.groups() if g]
            if len(groups) == 2:
                sid1, sid2 = groups[0], groups[1]
                return ScreeningTurnOutput(
                    reply=(
                        f"Ich habe die Lebensfelder angepasst: Die Reihenfolge wurde geändert, "
                        f"{sid2} wird nun zuerst bearbeitet."
                    ),
                    to_activate=[sid1, sid2],
                    first_active_id=sid2,
                    is_confirmed=False,
                )

        # 2. General change requests without specific section details: "ändern", "einteilung ändern", etc.
        guidance_match = re.match(
            r"^\s*(?:ändern|einteilung\s+ändern|lebensfelder\s+ändern|aufteilung\s+ändern|anpassen|lebensfelder\s+anpassen|wie\s+kann\s+ich\s+ändern|möchte\s+ändern)\s*[.!]?\s*$",
            text_lower,
        )
        if guidance_match or any(
            phrase in text_lower
            for phrase in ("anders einteilen", "andere einteilung", "anders aufteilen", "einteilung anpassen")
        ):
            return ScreeningTurnOutput(
                reply=(
                    "Gerne! Du kannst die Einteilung der Lebensfelder flexibel anpassen. "
                    "Nenne mir einfach die Lebensbereiche, die du aktivieren oder abwählen möchtest."
                ),
                is_confirmed=False,
            )

        # 3. Section Adjustments (by ID or semantic topic)
        to_activate: List[str] = []
        to_deactivate: List[str] = []

        topic_section_candidates = {
            "wohnen": ("1.1", "4"),
            "wohnung": ("1.1", "4"),
            "alltag": ("1.2", "2"),
            "haushalt": ("1.2", "2"),
            "lebensführung": ("1.2", "2"),
            "arbeit": ("1.3", "7"),
            "job": ("1.3", "7"),
            "beschäftigung": ("1.3", "7"),
            "psychisch": ("1.4", "1"),
            "selbstsorge": ("1.4", "1"),
            "beziehung": ("2.1", "6"),
            "sozial": ("2.1", "6"),
            "finanzen": ("2.2", "5"),
            "geld": ("2.2", "5"),
            "gesundheit": ("2.3", "3"),
        }

        deact_keywords = (
            "deaktivieren", "deaktiviere", "weglassen", "entfällt", "entfaellt",
            "streichen", "abwählen", "erledigt", "stabil", "raus", "schließen",
            "kein bedarf", "nicht mehr"
        )
        act_keywords = (
            "aktivieren", "aktiviere", "dazu", "aufnehmen", "mitnehmen",
            "hinzufügen", "rein", "bearbeiten", "mit rein"
        )

        valid_ids = {s.id for s in sections}
        clauses = re.split(r"[,;\n]+|\bund\b|\baber\b|(?<=[^\d])\.\s+", text_lower)

        for cl in clauses:
            cl_cleaned = cl.strip()
            if not cl_cleaned:
                continue

            has_deact = any(bool(re.search(rf"\b{re.escape(kw)}", cl_cleaned)) for kw in deact_keywords)
            has_act = any(bool(re.search(rf"\b{re.escape(kw)}", cl_cleaned)) for kw in act_keywords)
            if not (has_deact or has_act):
                continue

            found_sids = [
                sid for sid in sorted(valid_ids, key=len, reverse=True)
                if re.search(rf"(?<![\d.]){re.escape(sid)}(?![\d.])", cl_cleaned)
            ]
            if not found_sids:
                for topic, cand_ids in topic_section_candidates.items():
                    if topic in cl_cleaned:
                        for cand in cand_ids:
                            if cand in valid_ids and cand not in found_sids:
                                found_sids.append(cand)

            for sid in found_sids:
                if has_deact and not has_act:
                    if sid not in to_deactivate:
                        to_deactivate.append(sid)
                elif has_act and not has_deact:
                    if sid not in to_activate:
                        to_activate.append(sid)

        # 4. Life Events / Developments
        development_note: Optional[str] = None
        is_sensitive = any(
            kw in text_lower
            for kw in ("verstorben", "tot", "tod", "todesfall", "krise", "unfall", "klinik", "suizid")
        )

        if is_sensitive:
            development_note = f"Biografisches Ereignis: {text_stripped}"
        elif any(kw in text_lower for kw in ("neue wohnung", "umgezogen", "umzug", "wohnung bezogen", "wohnungswechsel")):
            development_note = "Klient hat eine neue eigene Wohnung bezogen und die Wohnsituation stabilisiert."
        elif any(kw in text_lower for kw in ("arbeitsplatz", "job", "kündigung", "trennung", "scheidung", "diagnose")):
            development_note = f"Aktuelle Entwicklung: {text_stripped}"

        # 5. Confirmation Detection
        has_confirmation = False
        if not is_negated:
            confirm_match = re.search(
                r"\b(?:(?:los\s+geht'?s)|loslegen|starten|start|los|weiter|beginnen|alles\s+(?:klar|gut|in\s+ordnung|stimmig)|in\s+ordnung|einverstanden|ok|okay|top|prima|super|stimmt(?:\s+so)?|passt(?:\s+so|\s+alles|\s+für\s+mich)?|(?:einteilung|aufteilung)\s+(?:passt|spiegelt|stimmt|ist\s+gut)|spiegelt\s+.*wider|können\s+.*(?:beginnen|starten|loslegen))\b",
                text_lower,
            )
            if confirm_match:
                has_confirmation = True

        # 6. Compose natural reply
        if is_sensitive:
            reply = (
                "Mein Beileid zu diesem schweren Verlust bzw. dieser herausfordernden Situation. "
                "Ich habe die Information in die allgemeinen Entwicklungen aufgenommen. "
                "Passt die Einteilung der Lebensfelder ansonsten so für dich?"
            )
        elif to_deactivate or to_activate:
            adjust_parts = []
            for sid in to_activate:
                sec_obj = next((s for s in sections if s.id == sid), None)
                title = sec_obj.title if sec_obj else sid
                adjust_parts.append(f"• **{sid} {title}** ➔ **aktiv**")
            for sid in to_deactivate:
                sec_obj = next((s for s in sections if s.id == sid), None)
                title = sec_obj.title if sec_obj else sid
                adjust_parts.append(f"• **{sid} {title}** ➔ *nicht ausgewählt*")
            adjust_summary = "\n".join(adjust_parts)

            if "neue wohnung" in text_lower or "wohnung" in text_lower:
                prefix = "Klasse, dass eine neue Wohnung gefunden wurde und die Wohnsituation geklärt ist! "
            else:
                prefix = ""

            if has_confirmation:
                reply = (
                    f"{prefix}Ich habe die Lebensfelder angepasst:\n{adjust_summary}\n\n"
                    "Dann starten wir direkt mit dem ersten Lebensfeld."
                )
            else:
                reply = (
                    f"{prefix}Ich habe die Lebensfelder angepasst:\n{adjust_summary}\n\n"
                    "Passt diese Aufteilung so für dich oder möchtest du weitere Bereiche anpassen?"
                )
        elif has_confirmation:
            reply = "Super, danke — dann legen wir mit dem ersten aktiven Lebensfeld los."
        elif development_note:
            reply = (
                "Vielen Dank für diese wichtige Information — ich habe das in die allgemeinen Entwicklungen aufgenommen. "
                "Passt die Einteilung der Lebensfelder ansonsten so für dich?"
            )
        else:
            reply = (
                "Möchtest du an der Einteilung der Lebensfelder noch etwas anpassen, oder passt die Aufteilung so für dich?"
            )

        return ScreeningTurnOutput(
            reply=reply,
            to_activate=to_activate,
            to_deactivate=to_deactivate,
            is_confirmed=has_confirmation,
            development_note=development_note,
        )
