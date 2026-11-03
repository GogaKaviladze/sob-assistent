"""Dialogue Intent Router for interactive SOB generation (ADR-11, Issue #58).

Classifies user messages in each dialogue phase into explicit intents,
preventing rigid state transitions and enabling real conversational interactions.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, List, Optional, Set, Tuple

from sob_generator.chat_session import DialoguePhase

logger = logging.getLogger(__name__)


class DialogueIntent(str, Enum):
    CONFIRMATION = "confirmation"
    REVISION_REQUEST = "revision_request"
    ADDITIONAL_INFO = "additional_info"
    CLARIFICATION_QUESTION = "clarification_question"
    SCREENING_ADJUST = "screening_adjust"
    SCREENING_SWAP = "screening_swap"
    SCREENING_GUIDANCE = "screening_guidance"
    DEVELOPMENT_UPDATE = "development_update"


@dataclass
class DialogueIntentResult:
    intent: DialogueIntent
    is_sensitive: bool = False
    has_confirmation: bool = False
    is_explicit_release: bool = False
    to_activate: List[str] = field(default_factory=list)
    to_deactivate: List[str] = field(default_factory=list)
    swap_pair: Optional[Tuple[str, str]] = None
    extracted_domain_fact: str = ""


_EXPLICIT_RELEASE_PATTERN = re.compile(
    r"\b(?:abschnitt\s+freigeben|lebensfeld\s+freigeben|freigeben|jetzt\s+freigeben|bitte\s+freigeben|abnahme|abschließen)\b",
    re.IGNORECASE,
)

_CONFIRM_PATTERN = re.compile(
    r"^\s*(?:(?:jetzt\s+|so\s+|das\s+|die\s+)?(?:ja|ja[,. ]+(?:passt|gerne|bitte|genau)|passt|passt\s+so|"
    r"passt[,. ]+weiter|passt\s+so[,. ]+weiter|"
    r"passt\s+alles|das\s+passt|passt\s+für\s+mich|start|starten|los|los\s+gehts|"
    r"los\s+geht's|alles\s+(?:klar|gut|in\s+ordnung|stimmt|korrekt)|einverstanden|"
    r"in\s+ordnung|ok|okay|top|prima|super|abgemacht|stimmt|stimmt\s+so|weiter|"
    r"weitergehen|alles\s+stimmig|stimmig|können\s+wir\s+so\s+machen|sieht\s+gut\s+aus|sieht\s+super\s+aus|klingt\s+gut|ziele\s+passen|"
    r"ziele\s+passen\s+so|ziele\s+sind\s+super|ziele\s+sind\s+stimmig|ziele\s+sind\s+gut|"
    r"einteilung\s+passt(?:\s+so)?|die\s+einteilung\s+passt(?:\s+so)?|"
    r"(?:die\s+)?einteilung\s+spiegelt.*wider.*(?:beginnen|starten).*|"
    r"fertig|entwurf\s+erstellen|entwurf|weiter\s+zum\s+entwurf|zum\s+entwurf|"
    r"passt\s+so[,. ]+weiter\s+zum\s+entwurf)(?:[,. ]+(?:danke|vielen\s+dank|bitte))?)\s*[.!]?\s*$",
    re.IGNORECASE,
)

_CONFIRM_PREFIX_PATTERN = re.compile(
    r"^\s*(?:ja|ja[,. ]+(?:passt|gerne|bitte|genau)|passt(?:\s+so|\s+alles|\s+für\s+mich)?|"
    r"alles\s+(?:klar|gut|in\s+ordnung|stimmig)|in\s+ordnung|ok|okay|top|prima|super|"
    r"einverstanden|stimmt(?:\s+so)?|ziele\s+passen(?:\s+so)?|ziele\s+sind\s+(?:gut|super|stimmig))\b\s*[,;.]\s*",
    re.IGNORECASE,
)

_NEGATION_PATTERN = re.compile(
    r"\b(?:nicht|nein|kein|keine|keineswegs|überhaupt\s+nicht|stimmt\s+nicht|gar\s+nicht|"
    r"bloß\s+nicht|keinesfalls|abgelehnt|falsch|unpassend|doch\s+nicht|noch\s+nicht)\b",
    re.IGNORECASE,
)

def _has_embedded_confirmation(text_lower: str) -> bool:
    """Prüft, ob in einem Teilsatz eine explizite Bestätigung vorliegt (strikt ohne Negation)."""
    if _NEGATION_PATTERN.search(text_lower):
        return False
    return bool(
        re.search(
            r"\b(?:passt(?:\s+so|\s+alles)?|alles\s+(?:klar|gut|in\s+ordnung|stimmig)|in\s+ordnung|einverstanden|ok|okay)\b",
            text_lower,
        )
    )

_SENSITIVE_LIFE_EVENTS = (
    "verstorben", "tot", "tod", "gestorben", "todesfall", "trauer",
    "beerdigung", "unfall", "klinik", "krankenhaus", "krise",
    "notaufnahme", "suizid", "verlust", "übergriff", "gewalt", "opfer",
    "schwer erkrankt", "krebs", "intensivstation", "reanimation",
)

_GENERAL_LIFE_EVENTS = (
    "neuigkeit", "neugkeit", "neues", "neue wohnung", "neuer job", "umzug", "umgezogen", "wohnung verloren",
    "arbeitsplatz", "job", "kündigung", "betreuung", "betreuer",
    "gesetzlich", "diagnose", "erkrankung", "operation", "rückfall",
    "sucht", "trennung", "scheidung", "heirat", "vater", "mutter",
    "eltern", "geschwister", "bruder", "schwester", "partner",
    "partnerin", "kind", "sohn", "tochter", "arzt", "therapie",
)

_REVISION_KEYWORDS = (
    "nicht smart", "smarter", "schreib es mir smart", "mach es smart",
    "smart formulieren", "zeitliche grenze", "zeitlimit", "zeitrahmen",
    "frist", "genauer formuliert", "präziser", "nicht so allgemein",
    "zu allgemein", "konkreter", "überarbeiten", "anpassen", "umformulieren",
    "neu formulieren", "korrigieren", "ändere ziel", "ändere", "ziel 1",
    "ziel 2", "erstes ziel", "zweites ziel", "nicht ziel 1", "nicht ziel 2",
    "anderes ziel", "andere ziele", "ziele ändern", "ziel ändern",
    "gefällt mir nicht", "passt nicht", "stimmt nicht ganz", "besser formulieren",
)

_SCREENING_GUIDANCE_PATTERNS = re.compile(
    r"^\s*(?:ändern|einteilung\s+ändern|lebensfelder\s+ändern|aufteilung\s+ändern|"
    r"anpassen|lebensfelder\s+anpassen|wie\s+kann\s+ich\s+ändern|möchte\s+ändern|"
    r"ändern\s+bitte|bitte\s+ändern)\s*[.!]?\s*$",
    re.IGNORECASE,
)

_SWAP_PATTERN = re.compile(
    r"(?:(\d\.\d|\d)\s*(?:und|mit)\s*(\d\.\d|\d)\s*tauschen|"
    r"tausche\s*(\d\.\d|\d)\s*(?:und|mit)\s*(\d\.\d|\d)|"
    r"(\d\.\d|\d)\s*vor\s*(\d\.\d|\d)|"
    r"reihenfolge\s*(?:von\s*)?(\d\.\d|\d)\s*(?:und|mit)\s*(\d\.\d|\d))",
    re.IGNORECASE,
)


class DialogueIntentRouter:
    """Classifies incoming user messages during chat sessions according to current phase."""

    def __init__(
        self,
        llm_classifier: Optional[Callable[[str, DialoguePhase], Optional[DialogueIntent]]] = None,
    ) -> None:
        self.llm_classifier = llm_classifier

    def classify(
        self,
        text: str,
        phase: DialoguePhase,
        valid_section_ids: Optional[Set[str]] = None,
    ) -> DialogueIntentResult:
        text_stripped = text.strip()
        text_lower = text_stripped.lower()
        valid_ids = valid_section_ids or set()
        is_negated = bool(_NEGATION_PATTERN.search(text_lower))

        # Optional LLM-assisted classification for complex/conversational turns
        if self.llm_classifier is not None and not _CONFIRM_PATTERN.match(text_stripped):
            try:
                llm_intent = self.llm_classifier(text_stripped, phase)
                if llm_intent:
                    # Enforce strict safety guard: negation NEVER permits CONFIRMATION
                    if llm_intent == DialogueIntent.CONFIRMATION and is_negated:
                        llm_intent = (
                            DialogueIntent.SCREENING_GUIDANCE
                            if phase == DialoguePhase.SCREENING
                            else DialogueIntent.REVISION_REQUEST
                        )
                    return DialogueIntentResult(
                        intent=llm_intent,
                        has_confirmation=(llm_intent == DialogueIntent.CONFIRMATION),
                        extracted_domain_fact=text_stripped if llm_intent == DialogueIntent.ADDITIONAL_INFO else "",
                    )
            except Exception as e:
                logger.warning("LLM intent classification fallback to rules: %s", e)

        if phase == DialoguePhase.SCREENING:
            return self._classify_screening(text_stripped, text_lower, valid_ids)

        if phase in (DialoguePhase.FIELD_REFLECT, DialoguePhase.FIELD_GOALS):
            return self._classify_goals(text_stripped, text_lower)

        if phase == DialoguePhase.FIELD_QUESTIONS:
            return self._classify_questions(text_stripped, text_lower)

        if phase in (DialoguePhase.FIELD_DRAFT, DialoguePhase.FIELD_REVIEW):
            return self._classify_draft(text_stripped, text_lower)

        return DialogueIntentResult(intent=DialogueIntent.ADDITIONAL_INFO)

    def _classify_screening(
        self, text: str, text_lower: str, valid_section_ids: Set[str]
    ) -> DialogueIntentResult:
        is_negated = bool(_NEGATION_PATTERN.search(text_lower))

        # 1. Pure confirmation (only if strictly not negated)
        if not is_negated and _CONFIRM_PATTERN.match(text):
            return DialogueIntentResult(intent=DialogueIntent.CONFIRMATION, has_confirmation=True)

        # 2. Swap detection (e.g. "1.1 und 2.1 tauschen")
        swap_match = _SWAP_PATTERN.search(text_lower)
        if swap_match:
            groups = [g for g in swap_match.groups() if g]
            if len(groups) == 2:
                return DialogueIntentResult(
                    intent=DialogueIntent.SCREENING_SWAP,
                    swap_pair=(groups[0], groups[1]),
                )

        # Also detect two section IDs with "tauschen" or "wechseln"
        if "tausch" in text_lower or "wechsel" in text_lower:
            found_sids = [sid for sid in sorted(valid_section_ids, key=len, reverse=True) if re.search(rf"(?<![\d.]){re.escape(sid)}(?![\d.])", text_lower)]
            if len(found_sids) >= 2:
                return DialogueIntentResult(
                    intent=DialogueIntent.SCREENING_SWAP,
                    swap_pair=(found_sids[0], found_sids[1]),
                )

        # 3. Screening guidance requests (e.g. "ändern", "einteilung ändern", "anders einteilen", "anpassen", "wie kann ich ändern")
        if _SCREENING_GUIDANCE_PATTERNS.match(text) or any(phrase in text_lower for phrase in ("anders einteilen", "andere einteilung", "anders aufteilen", "einteilung anpassen")):
            return DialogueIntentResult(intent=DialogueIntent.SCREENING_GUIDANCE, has_confirmation=False)

        # 4. Questions / Clarifications
        if text.endswith("?") or any(text_lower.startswith(qw) for qw in ("warum", "wie ", "wieso", "weshalb", "was ", "welche", "können wir", "kann man", "gibt es")):
            return DialogueIntentResult(intent=DialogueIntent.CLARIFICATION_QUESTION, has_confirmation=False)

        # 5. Section activation / deactivation adjustments
        to_activate, to_deactivate = self._parse_screening_adjustments(text_lower, valid_section_ids)
        has_confirm = (not is_negated) and _has_embedded_confirmation(text_lower)
        if to_activate or to_deactivate:
            return DialogueIntentResult(
                intent=DialogueIntent.SCREENING_ADJUST,
                has_confirmation=has_confirm,
                to_activate=to_activate,
                to_deactivate=to_deactivate,
            )

        # 6. Rejections / Negations without section adjustments (e.g. "passt nicht", "nein", "stimmt nicht", "überhaupt nicht", "keineswegs")
        if is_negated:
            return DialogueIntentResult(intent=DialogueIntent.SCREENING_GUIDANCE, has_confirmation=False)

        # 7. Life events / developments
        is_sensitive = any(kw in text_lower for kw in _SENSITIVE_LIFE_EVENTS)
        is_development = is_sensitive or any(kw in text_lower for kw in _GENERAL_LIFE_EVENTS)
        if is_development:
            return DialogueIntentResult(
                intent=DialogueIntent.DEVELOPMENT_UPDATE,
                is_sensitive=is_sensitive,
                has_confirmation=has_confirm,
            )

        # 8. Vague change intentions
        if any(w in text_lower for w in ("änder", "anpass", "wechsel", "anders")):
            return DialogueIntentResult(intent=DialogueIntent.SCREENING_GUIDANCE, has_confirmation=False)

        # 9. Fallback for longer biographical text
        if len(text.split()) > 3:
            return DialogueIntentResult(
                intent=DialogueIntent.DEVELOPMENT_UPDATE,
                is_sensitive=False,
                has_confirmation=False,
            )

        return DialogueIntentResult(intent=DialogueIntent.SCREENING_GUIDANCE, has_confirmation=False)

    def _parse_screening_adjustments(
        self, text_lower: str, valid_section_ids: Set[str]
    ) -> Tuple[List[str], List[str]]:
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

        clauses = re.split(r"[,;\n]+|\bund\b|\baber\b|(?<=[^\d])\.\s+", text_lower)
        activate_keywords = {
            "aktivieren", "aktiviere", "aktiv", "dazu", "aufnehmen", "mitnehmen", "hinzufügen",
            "rein", "öffnen", "brauchen", "bearbeiten", "mit rein", "ja"
        }
        deactivate_keywords = {
            "deaktivieren", "deaktiviere", "entfällt", "entfaellt", "weglassen", "nicht", "kein",
            "keine", "stabil", "raus", "schließen", "entfernen", "streichen", "abwählen", "erledigt"
        }

        for clause in clauses:
            clause_cleaned = clause.strip()
            if not clause_cleaned:
                continue

            has_deact = any(kw in clause_cleaned for kw in deactivate_keywords)
            has_act = any(kw in clause_cleaned for kw in activate_keywords)

            found_sids = [sid for sid in sorted(valid_section_ids, key=len, reverse=True) if re.search(rf"(?<![\d.]){re.escape(sid)}(?![\d.])", clause_cleaned)]
            if not found_sids and (has_deact or has_act):
                for topic, candidates in topic_section_candidates.items():
                    if topic in clause_cleaned:
                        for cand in candidates:
                            if cand in valid_section_ids and cand not in found_sids:
                                found_sids.append(cand)

            if not found_sids:
                continue

            for sid in found_sids:
                if has_deact and not has_act:
                    if sid not in to_deactivate:
                        to_deactivate.append(sid)
                elif has_act:
                    if sid not in to_activate:
                        to_activate.append(sid)
                else:
                    if sid not in to_activate:
                        to_activate.append(sid)

        return to_activate, to_deactivate

    def _classify_goals(self, text: str, text_lower: str) -> DialogueIntentResult:
        is_negated = bool(_NEGATION_PATTERN.search(text_lower))

        # Check revision request FIRST (e.g. "ziel ist nicht smart", "passt nicht", "stimmt nicht", "überarbeiten")
        if is_negated and not any(kw in text_lower for kw in ("erledigt", "gelungen", "erreicht", "fortschritte")):
            if any(kw in text_lower for kw in _REVISION_KEYWORDS) or any(w in text_lower for w in ("nicht", "nein", "stimmt nicht", "kein", "keineswegs")):
                return DialogueIntentResult(intent=DialogueIntent.REVISION_REQUEST)

        if any(kw in text_lower for kw in _REVISION_KEYWORDS):
            return DialogueIntentResult(intent=DialogueIntent.REVISION_REQUEST)

        # Check question
        if text.endswith("?") and not _CONFIRM_PATTERN.match(text):
            return DialogueIntentResult(intent=DialogueIntent.CLARIFICATION_QUESTION)

        # Check pure confirmation (ONLY if not negated)
        if not is_negated and _CONFIRM_PATTERN.match(text):
            return DialogueIntentResult(intent=DialogueIntent.CONFIRMATION, has_confirmation=True)

        # Check confirmation with domain focus (e.g. "Ziele passen so, Schwerpunkt ist Supermarkt-Training und Einkauf.")
        if not is_negated:
            prefix_match = _CONFIRM_PREFIX_PATTERN.match(text)
            if prefix_match:
                extra = text[prefix_match.end():].strip()
                return DialogueIntentResult(
                    intent=DialogueIntent.CONFIRMATION,
                    has_confirmation=True,
                    extracted_domain_fact=extra,
                )

        # User provides domain context / facts during goals
        return DialogueIntentResult(
            intent=DialogueIntent.ADDITIONAL_INFO,
            extracted_domain_fact=text,
        )

    def _classify_questions(self, text: str, text_lower: str) -> DialogueIntentResult:
        is_negated = bool(_NEGATION_PATTERN.search(text_lower))

        # Check question back to assistant
        if text.endswith("?") and len(text.split()) < 15 and not any(kw in text_lower for kw in ("erledigt", "begleitet", "unterstützt")):
            return DialogueIntentResult(intent=DialogueIntent.CLARIFICATION_QUESTION)

        # Check request to revise goals
        if any(kw in text_lower for kw in ("ziele ändern", "ziel ändern", "zurück zu zielen", "ziele anpassen", "passt nicht")):
            return DialogueIntentResult(intent=DialogueIntent.REVISION_REQUEST)

        # Check confirmation (ONLY if not negated)
        if not is_negated and (
            _CONFIRM_PATTERN.match(text)
            or any(kw in text_lower for kw in ("weiter zum entwurf", "zum entwurf", "entwurf erstellen", "entwurf bitte"))
        ):
            return DialogueIntentResult(intent=DialogueIntent.CONFIRMATION, has_confirmation=True)

        # Default in questions phase: user provides answers / domain details
        return DialogueIntentResult(
            intent=DialogueIntent.ADDITIONAL_INFO,
            extracted_domain_fact=text,
        )

    def _classify_draft(self, text: str, text_lower: str) -> DialogueIntentResult:
        is_negated = bool(_NEGATION_PATTERN.search(text_lower))

        # 1. Explicit section release signal (e.g. "abschnitt freigeben", "freigeben", "lebensfeld freigeben")
        if not is_negated and _EXPLICIT_RELEASE_PATTERN.search(text_lower):
            return DialogueIntentResult(
                intent=DialogueIntent.CONFIRMATION,
                has_confirmation=True,
                is_explicit_release=True,
            )

        # 2. General confirmation (e.g. "passt", "passt so", "sieht gut aus", "können wir übernehmen")
        if not is_negated and _CONFIRM_PATTERN.match(text):
            return DialogueIntentResult(
                intent=DialogueIntent.CONFIRMATION,
                has_confirmation=True,
                is_explicit_release=False,
            )

        # 3. Confirmation prefix with additional detail (e.g. "Passt so, aber noch ergänzen...")
        if not is_negated:
            prefix_match = _CONFIRM_PREFIX_PATTERN.match(text)
            if prefix_match:
                extra = text[prefix_match.end():].strip()
                return DialogueIntentResult(
                    intent=DialogueIntent.CONFIRMATION,
                    has_confirmation=True,
                    is_explicit_release=False,
                    extracted_domain_fact=extra,
                )

        # 4. Questions
        if text.endswith("?") or any(text_lower.startswith(qw) for qw in ("warum", "wie ", "wieso", "weshalb", "was ", "welche", "können wir", "kannst du", "gibt es")):
            return DialogueIntentResult(intent=DialogueIntent.CLARIFICATION_QUESTION, has_confirmation=False)

        # 5. Critique / Revision request
        if is_negated and not any(kw in text_lower for kw in ("erledigt", "gelungen", "erreicht", "fortschritte")):
            return DialogueIntentResult(intent=DialogueIntent.REVISION_REQUEST, has_confirmation=False)

        if any(kw in text_lower for kw in _REVISION_KEYWORDS) or any(w in text_lower for w in ("kürzen", "streichen", "löschen", "falsch", "korrigieren", "anpassen", "umformulieren", "zu lang", "zu kurz")):
            return DialogueIntentResult(intent=DialogueIntent.REVISION_REQUEST, has_confirmation=False)

        # 6. Additional domain info / Living Draft enrichment
        return DialogueIntentResult(
            intent=DialogueIntent.ADDITIONAL_INFO,
            extracted_domain_fact=text,
        )
