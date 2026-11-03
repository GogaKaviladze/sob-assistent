"""Domain Service: ChatDialogueEngine (Issue #53, ADR-11).

Orchestrates multi-turn interactive SOB dialogue progression, intent-aware
phase transitions, section-scoped working memory, and non-blocking
asynchronous LLM offloading via anyio.to_thread.
"""
from __future__ import annotations

import json
import logging
import re
from functools import partial
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import anyio

from sob_generator.conversation import fallback_turn, is_uncertain
from sob_generator.chat_session import ChatSession, DialoguePhase, SectionStatus
from sob_generator.chat_store import ChatSessionStore, SessionStore
from sob_generator.dialogue_router import DialogueIntent, DialogueIntentResult, DialogueIntentRouter
from sob_generator.draft_generator import DraftGenerator
from sob_generator.llm.schemas import DocumentationEntry, ScreeningTurnOutput, SectionLLMOutput
from sob_generator.llm.sections import SectionDefinition
from sob_generator.previous_sob import read_previous_goals, remap_previous_goals
from sob_generator.report_template import TEMPLATE_REGISTRY, ReportTemplate

logger = logging.getLogger(__name__)

_DEFAULT_TEMPLATE = TEMPLATE_REGISTRY["hamburg_soziale_dienste"]


def format_goal_proposal(goals: str) -> str:
    """Bereitet versehentlich als JSON gelieferte Zielvorschläge lesbar auf."""
    stripped = goals.strip()
    if not stripped or stripped[0] not in "[{":
        return stripped
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return stripped

    items = parsed if isinstance(parsed, list) else [parsed]
    if not items or not all(isinstance(item, dict) for item in items):
        return stripped

    labels = {
        "zielinhalt": "Ziel",
        "messbar": "Prüfkriterium",
        "relevant": "Begründung",
        "tatsächlichkeitsgeprüft": "Realistisch",
        "zeitlich_begrenzt": "Zeitrahmen",
    }
    blocks = []
    for index, item in enumerate(items, start=1):
        details = [
            f"{labels.get(key, key.replace('_', ' ').title())}: {value.strip()}"
            for key, value in item.items()
            if isinstance(value, str) and value.strip()
        ]
        if details:
            heading = f"Ziel {index}" if len(items) > 1 else "Zielvorschlag"
            blocks.append(f"**{heading}**\n" + "\n".join(f"- {detail}" for detail in details))
    return "\n\n".join(blocks) or stripped


def professional_goal_reply(reply: str, client_name: str, has_goals: bool, context: str = "") -> str:
    """Hält die Begleitantwort zu Zielvorschlägen fachkraftbezogen und neutral."""
    cleaned = reply.strip()
    if has_goals:
        subject = f" für **{context}**" if context else ""
        return f"Bitte prüfe den folgenden Zielvorschlag{subject} fachlich und ergänze fehlende Angaben."
    if client_name and re.search(rf"(?i)(?:^|[.!?]\s){re.escape(client_name)}\s*[,!]", cleaned):
        return "Für einen belastbaren Zielvorschlag fehlt noch eine konkrete Beobachtung oder ein Wunsch der betreuten Person."
    return cleaned

_CRISIS_KEYWORDS_RE = re.compile(
    r"\b("
    r"suizid\w*|umbringen|freitod|lebensüberdruss|leben\s+nehmen|"
    r"psychos\w*|psychot\w*|dekompens\w*|wahn\w*|halluzinat\w*|"
    r"psychiatr\w*|stationär\s+aufgen\w*|krisenintervention|notaufnahme|krisendienst|"
    r"selbstgefährd\w*|fremdgefährd\w*|selbstverletz\w*"
    r")\b",
    re.IGNORECASE,
)


def is_crisis_or_high_risk_note(text: str) -> bool:
    """Erkennt Meldungen über akute suizidale, psychotische oder psychiatrische Krisen (Issue #71)."""
    if not text:
        return False
    return bool(_CRISIS_KEYWORDS_RE.search(text))


def extract_development_update(text: str) -> Optional[str]:
    """Extrahiert globale biografische Lebensereignisse (Klinik, Umzug, Todesfall) für session.developments (Issue #72)."""
    if not text:
        return None
    text_lower = text.lower()
    patterns = [
        (r"\b(?:verstorben|gestorben|todesfall|beerdigung|tod\s+von)\b", "Trauerfall im persönlichen Umfeld"),
        (r"\b(?:stationär|klinik|krankenhaus|notaufnahme|intensivstation|psychiatrie)\b", "Stationärer Klinikaufenthalt"),
        (r"\b(?:neue wohnung|umgezogen|umzug|wohnung bezogen)\b", "Wohnungswechsel / Bezug einer neuen Wohnung"),
        (r"\b(?:wohnung verloren|obdachlos|kündigung der wohnung)\b", "Wohnungsverlust"),
        (r"\b(?:neue arbeit|neuer job|arbeitsplatzwechsel|stelle angetreten)\b", "Berufliche Veränderung / Neuer Arbeitsplatz"),
    ]
    for pat, _ in patterns:
        if re.search(pat, text_lower):
            clean_text = text.strip().replace("\n", " ")
            if len(clean_text) > 140:
                clean_text = clean_text[:137] + "..."
            return clean_text
    return None


class ChatDialogueEngine:
    """Domain Service encapsulating conversational dialogue logic and async LLM execution."""

    def __init__(
        self,
        generator: DraftGenerator,
        template: Optional[ReportTemplate] = None,
        session_store: Optional[SessionStore] = None,
        router: Optional[DialogueIntentRouter] = None,
    ) -> None:
        self.generator = generator
        self.template = template or _DEFAULT_TEMPLATE
        self.session_store = session_store or ChatSessionStore()
        if router is not None:
            self.router = router
        elif hasattr(generator, "classify_intent") and callable(getattr(generator, "classify_intent")):
            self.router = DialogueIntentRouter(llm_classifier=generator.classify_intent)
        else:
            self.router = DialogueIntentRouter()

    def get_template(self, session: Optional[ChatSession] = None) -> ReportTemplate:
        if session and session.template_key in TEMPLATE_REGISTRY:
            return TEMPLATE_REGISTRY[session.template_key]
        return self.template

    def get_section_def(self, section_id: str, session: Optional[ChatSession] = None) -> Optional[SectionDefinition]:
        tmpl = self.get_template(session)
        for sec in tmpl.section_definitions:
            if sec.id == section_id:
                return sec
        return None

    async def start_session(
        self,
        csv_path: Path,
        alt_sob_path: Optional[Path] = None,
        anrede: str = "",
        template: Optional[ReportTemplate] = None,
        freitext: str = "",
    ) -> ChatSession:
        """Initializes a new interactive chat session from uploaded files."""
        tmpl = template or self.template
        imported = tmpl.importer_factory(csv_path).load()
        if anrede:
            imported.client.anrede = anrede
        previous_goals = (
            read_previous_goals(alt_sob_path) if alt_sob_path else {}
        )
        previous_goals = remap_previous_goals(previous_goals, tmpl.section_definitions)
        section_titles = {s.id: s.title for s in tmpl.section_definitions}

        public_notes = freitext.strip()
        initial_entries = [
            DocumentationEntry(datum="", kategorie="Freitext", text=paragraph.strip())
            for paragraph in re.split(r"\n\s*\n", public_notes)
            if paragraph.strip()
        ]
        entries_to_classify = [*imported.entries, *initial_entries]

        # Semantische Klassifikation der Einträge (Pass 0) via LLM Batching
        if hasattr(self.generator, "classify_entries_batch") and callable(getattr(self.generator, "classify_entries_batch")):
            classified_map = await anyio.to_thread.run_sync(
                partial(
                    self.generator.classify_entries_batch,
                    entries=entries_to_classify,
                    sections=tmpl.section_definitions,
                )
            )
            section_entries: Dict[str, List[DocumentationEntry]] = {
                s.id: classified_map.get(s.id, []) for s in tmpl.section_definitions
            }
            unclear: List[DocumentationEntry] = classified_map.get("unklar", [])
        else:
            section_entries = {s.id: [] for s in tmpl.section_definitions}
            unclear = []
            for entry in entries_to_classify:
                sid = await anyio.to_thread.run_sync(
                    partial(
                        self.generator.classify_entry,
                        entry=entry,
                        sections=tmpl.section_definitions,
                    )
                )
                if sid in section_entries:
                    section_entries[sid].append(entry)
                else:
                    unclear.append(entry)

        session = ChatSession.from_import(
            imported=imported,
            previous_goals=previous_goals,
            section_titles=section_titles,
            section_entries=section_entries,
            template_key=tmpl.key,
        )
        session.unclear_entries = unclear
        session.phase = DialoguePhase.SCREENING
        if freitext.strip():
            session.add_user_message(freitext)
            for entry in initial_entries:
                development = extract_development_update(entry.text)
                if development:
                    session.add_development_note(development)

        # Begrüßung nach Protokoll
        client_name = f"{imported.client.vorname} {imported.client.name}".strip() or "der Klientin/dem Klienten"
        session.add_assistant_message(
            f"Ich unterstütze dich als Fachkraft beim Sozialbericht für {client_name}. "
            "Zuerst prüfst du die vorgeschlagene Auswahl der Lebensfelder. Danach besprechen wir jedes ausgewählte Feld, "
            "formulieren Ziele und erstellen einen bearbeitbaren Entwurf. Du prüfst und bestätigst jeden Abschnitt vor der Freigabe. "
            "Fragen, fachliche Einschätzungen und fehlende Informationen kannst du jederzeit in normalen Sätzen eingeben."
        )

        # Screening-Vorschlag der Lebensfelder
        screening_lines = []
        first_active_sid = None
        for sec in tmpl.section_definitions:
            entries_count = len(section_entries.get(sec.id, []))
            has_prev_goals = bool(
                previous_goals.get(sec.id)
                and (previous_goals[sec.id].leitziel or previous_goals[sec.id].smarte_ziele)
            )
            if entries_count > 0 or has_prev_goals:
                session.sections[sec.id].is_active_field = True
                reasons = []
                if entries_count:
                    reasons.append(f"{entries_count} Dokumentationseinträge")
                if has_prev_goals:
                    reasons.append("Ziele aus dem vorherigen Bericht")
                reason = ", ".join(reasons)
                session.sections[sec.id].screening_rationale = reason
                screening_lines.append(f"• **{sec.id} {sec.title}** ➔ **zur Bearbeitung vorgeschlagen** ({reason})")
                if not first_active_sid:
                    first_active_sid = sec.id
            else:
                session.sections[sec.id].is_active_field = False
                session.sections[sec.id].status = SectionStatus.EMPTY
                session.sections[sec.id].screening_rationale = "Keine Angaben vorhanden – fachlich prüfen"
                screening_lines.append(f"• **{sec.id} {sec.title}** ➔ *nicht vorausgewählt* (keine Angaben vorhanden – bitte prüfen)")

        if first_active_sid:
            session.active_section_id = first_active_sid

        session.add_assistant_message(
            "Hier ist mein Vorschlag für die Einteilung der Lebensfelder:\n\n"
            + "\n".join(screening_lines) + "\n\n"
            "Prüfe bitte, ob die Auswahl fachlich vollständig ist. Du kannst zum Beispiel „Feld 7 aktivieren“ oder "
            "„Feld 3 abwählen“ schreiben. Wenn die Auswahl stimmt, wähle „Einteilung bestätigen“ oder schreibe „Einteilung passt“. "
            "Die Bestätigung betrifft nur die Auswahl der Lebensfelder, noch keine Berichtsinhalte."
        )

        self.session_store.store(session)
        return session

    async def process_message(self, session_id: str, message: str, action: str = "") -> ChatSession:
        """Processes an incoming user message and executes intent-aware transitions."""
        session = self.session_store.get(session_id)
        if session is None:
            raise KeyError(f"ChatSession with ID {session_id} not found.")

        if action:
            suggestion = next((item for item in session.get_reflection_suggestions() if item.action == action), None)
            if suggestion is None or message != suggestion.text:
                raise ValueError("Ungültige oder veraltete Dialogaktion.")
        session.add_user_message(message, capture_facts=session.phase == DialoguePhase.SCREENING)
        message = message.strip()
        if not message:
            session.add_assistant_message("Bitte gib eine Nachricht ein.")
            self.session_store.store(session)
            return session
        active_sid = session.active_section_id
        sec_def = self.get_section_def(active_sid, session) if active_sid else None
        sec_state = session.sections.get(active_sid) if active_sid else None
        active_entries = session.memory.section_entries.get(active_sid, []) if active_sid else []

        if session.phase != DialoguePhase.SCREENING:
            if not sec_def or not sec_state:
                return session
            return await self._process_field_turn(session, sec_def, message, action)

        if session.phase == DialoguePhase.SCREENING:
            tmpl = self.get_template(session)
            active_sids = [sid for sid, s in session.sections.items() if s.is_active_field]

            # LLM-gestützte Zustandsextraktion und Dialogführung (Issue #69)
            screening_output: ScreeningTurnOutput = await anyio.to_thread.run_sync(
                partial(
                    self.generator.process_screening_turn,
                    message=message,
                    sections=tmpl.section_definitions,
                    active_section_ids=active_sids,
                    client=session.client,
                )
            )

            # 1. Lebensfelder aktivieren
            for sid in screening_output.to_activate:
                if sid in session.sections:
                    session.sections[sid].is_active_field = True
                    session.sections[sid].status = SectionStatus.EMPTY
                    session.sections[sid].screening_rationale = "Manuell aktiviert"

            # 2. Lebensfelder deaktivieren
            for sid in screening_output.to_deactivate:
                if sid in session.sections:
                    session.sections[sid].is_active_field = False
                    session.sections[sid].status = SectionStatus.CONFIRMED
                    session.sections[sid].screening_rationale = "Manuell abgewählt"

            # 3. Priorisierung / Tausch anwenden
            if screening_output.first_active_id and screening_output.first_active_id in session.sections:
                session.active_section_id = screening_output.first_active_id
                session.sections[screening_output.first_active_id].is_active_field = True
            elif active_sid and (active_sid not in session.sections or not session.sections[active_sid].is_active_field):
                first_active = next((sid for sid, s in session.sections.items() if s.is_active_field), None)
                if first_active:
                    session.active_section_id = first_active

            # 4. Extrahierte Entwicklungsnotiz erfassen
            if screening_output.development_note:
                session.add_development_note(screening_output.development_note)

            # 5. Phasenübergang bei Bestätigung oder Fortführung
            if screening_output.is_confirmed:
                for section_state in session.sections.values():
                    if not section_state.is_active_field:
                        section_state.status = SectionStatus.CONFIRMED
                session.phase = DialoguePhase.FIELD_REFLECT
                active_sid = session.active_section_id
                sec_def = self.get_section_def(active_sid, session) if active_sid else None
                goal_msg = ""
                if sec_def:
                    sec_state = session.sections[active_sid]
                    active_entries = session.memory.section_entries.get(active_sid, [])
                    goal_msg = await self._goal_reply(session, sec_def)
                reply_text = screening_output.reply.strip()
                if goal_msg:
                    full_reply = f"{reply_text}\n\n{goal_msg}" if reply_text else f"Super, dann legen wir mit dem ersten aktiven Lebensfeld los.\n\n{goal_msg}"
                else:
                    full_reply = reply_text
                session.add_assistant_message(full_reply)
            else:
                session.add_assistant_message(screening_output.reply)


        self.session_store.store(session)
        return session

    async def _goal_reply(self, session, section, *, critique="") -> str:
        state = session.sections[section.id]
        entries = session.memory.section_entries.get(section.id, [])
        try:
            if critique:
                result = await anyio.to_thread.run_sync(partial(
                    self.generator.revise_field_goals, section=section,
                    current_goals=state.current_goals or state.smarte_ziele, critique=critique,
                    entries=[*entries, *[DocumentationEntry(datum="", kategorie="Fachkraft", text=n) for n in state.user_notes]],
                    client=session.client))
            else:
                result = await anyio.to_thread.run_sync(partial(
                    self.generator.generate_field_goals, section=section, entries=entries,
                    leitziel=state.leitziel, smarte_ziele=state.smarte_ziele,
                    client=session.client, developments=session.developments))
            if result.error or result.needs_review or (critique and not result.value.goals.strip()):
                raise ValueError("Invalid goal proposal")
        except Exception:
            logger.warning("Goal generation failed; previous goals retained.")
            return "Ich konnte die Ziele nicht zuverlässig formulieren. Der bisherige Stand bleibt erhalten. Bitte versuche es erneut oder bearbeite die Ziele direkt."
        state.proposed_goals = format_goal_proposal(result.value.goals)
        state.vorschlag_ziele = state.proposed_goals
        state.status = SectionStatus.NEEDS_CLARIFICATION
        reply = professional_goal_reply(
            result.value.reply,
            session.client.vorname,
            bool(state.proposed_goals),
            f"Lebensfeld {section.id} ({section.title})",
        )
        return reply + (f"\n\n{state.proposed_goals}" if state.proposed_goals else "")

    async def _write_draft(self, session, section, critique="") -> bool:
        state = session.sections[section.id]
        kwargs = dict(section=section, entries=session.memory.section_entries.get(section.id, []),
            leitziel=state.leitziel, smarte_ziele=state.smarte_ziele,
            previous_goals_apply=state.previous_goals_apply, user_notes=state.user_notes,
            anrede=session.client.anrede, nachname=session.client.name, vorname=session.client.vorname,
            developments=session.developments)
        # Current proposals and uncertainty are explicit context, never historical goals.
        kwargs["freitext"] = "\n".join(filter(None, [
            "Aktueller Zielinhalt (" + ("bestätigt" if state.current_goals == state.confirmed_goals else "Vorschlag zur Prüfung") + "): " + state.current_goals if state.current_goals else "",
            "Offene Angaben: " + "; ".join(state.uncertainties) if state.uncertainties else "",
            "Offene Frage: " + state.pending_question if state.pending_question else "",
        ]))
        try:
            if critique:
                current = SectionLLMOutput.model_construct(**{k: getattr(state, k) for k in SectionLLMOutput.model_fields})
                result = await anyio.to_thread.run_sync(partial(self.generator.revise_section,
                    current_draft=current, critique=critique, **kwargs))
            else:
                result = await anyio.to_thread.run_sync(partial(self.generator.refine_section, **kwargs))
            if result.error or result.needs_review:
                raise ValueError("Invalid draft")
            value = SectionLLMOutput.model_validate(result.value.model_dump())
            if critique and value == current:
                raise ValueError("Unchanged revision")
        except Exception:
            logger.warning("Draft generation failed; previous draft retained.")
            session.add_assistant_message("Ich konnte die Änderung nicht zuverlässig umsetzen. Der bisherige Entwurf bleibt erhalten. Bitte versuche es erneut oder bearbeite den Text direkt.")
            return False
        if state.current_goals:
            value.vorschlag_ziele = state.current_goals
        else:
            state.proposed_goals = value.vorschlag_ziele
        session.update_section_draft(section.id, **value.model_dump(), status=SectionStatus.DRAFTED)
        session.phase = DialoguePhase.FIELD_DRAFT
        session.add_assistant_message(f"Der Textentwurf für Lebensfeld {section.id} ist in der Vorschau aktualisiert. Bitte prüfe Inhalt und offene Angaben vor der Freigabe.")
        return True

    async def _process_field_turn(self, session, section, message, action):
        state = session.sections[section.id]
        lower = message.lower()
        # Explicit UI actions and unambiguous commands precede probabilistic routing.
        switch = re.fullmatch(r"(?:wechsel(?:e)?|weiter|zurück)(?:\s+zu(?:m)?)?\s+(?:lebensfeld|feld|abschnitt)\s+(\d+(?:\.\d+)?)\.?", lower.strip())
        if switch:
            target = switch.group(1)
            if target in session.sections:
                session.set_active_section(target)
                target_state = session.sections[target]
                target_state.is_active_field = True
                session.phase = DialoguePhase.FIELD_DRAFT if target_state.bericht_zielerreichung else DialoguePhase.FIELD_REFLECT
                session.add_assistant_message(f"Wir sind jetzt bei {target_state.title}. " + (target_state.pending_question or "Was möchtest du hier besprechen?"))
            else:
                session.add_assistant_message("Dieses Lebensfeld gibt es in der gewählten Vorlage nicht.")
        elif action == "revise_goals" or ("ziel" in lower and any(w in lower for w in ("anpassen", "ändern", "überarbeiten", "konkreter", "smart", "formulieren")) and not message.endswith("?")):
            session.add_assistant_message(await self._goal_reply(session, section, critique=message))
        elif action == "shorten_draft":
            await self._write_draft(session, section, critique=message)
        elif lower.strip(" .!") in ("ziele bestätigen", "ziele passen", "ziele passen so"):
            state.confirmed_goals = state.proposed_goals
            session.add_assistant_message("Die aktuellen Zielvorschläge sind bestätigt. " + (state.pending_question or "Welche Beobachtung beschreibt den bisherigen Verlauf?"))
        else:
            if action in ("explain_goal", "describe_observation") or message.endswith("?"):
                intent = DialogueIntentResult(intent=DialogueIntent.CLARIFICATION_QUESTION)
            elif is_uncertain(message) or lower.startswith("korrektur:"):
                intent = DialogueIntentResult(intent=DialogueIntent.ADDITIONAL_INFO, extracted_domain_fact=message)
            elif lower.strip(" .!") in ("abschnitt freigeben", "lebensfeld freigeben", "freigeben"):
                return await self.confirm_section(session.session_id, section.id)
            elif re.match(r"^(?:bitte|änder|kürz|straff|überarbeit|formulier|ziel|passt|ja|ok|weiter|entwurf|text|direkt|keine weiteren|überspringen|das passt|sieht gut)", lower):
                intent = await anyio.to_thread.run_sync(self.router.classify, message, session.phase)
            else:
                intent = DialogueIntentResult(intent=DialogueIntent.ADDITIONAL_INFO, extracted_domain_fact=message)
            draft_request = intent.intent == DialogueIntent.CONFIRMATION and not intent.extracted_domain_fact
            if intent.intent == DialogueIntent.REVISION_REQUEST:
                if state.bericht_zielerreichung:
                    await self._write_draft(session, section, critique=message)
                else:
                    session.add_assistant_message(await self._goal_reply(session, section, critique=message))
            elif draft_request:
                await self._write_draft(session, section)
            else:
                await self._converse(session, section, message, intent)
        self.session_store.store(session)
        return session

    async def _converse(self, session, section, message, intent):
        state = session.sections[section.id]
        recent = []
        for item in session.get_section_messages(section.id)[-7:-1]:
            public = item.content.strip()
            if public:
                recent.append({"role": item.role.value, "text": public})
        context = dict(field=section.title, field_description=section.description,
            entries=[e.model_dump() for e in session.memory.section_entries.get(section.id, [])],
            notes=list(state.user_notes), goals=state.current_goals, previous_goals=state.smarte_ziele,
            pending_question=state.pending_question, recent=recent, message=message,
            intent=intent.intent.value)
        try:
            result = await anyio.to_thread.run_sync(partial(self.generator.converse, context=context))
            if result.error or result.needs_review:
                raise ValueError("Invalid conversational turn")
            turn = result.value
        except Exception:
            logger.warning("Conversation generation failed; conservative fallback used.")
            turn = fallback_turn(context)
        if len(turn.reply.strip()) < 15:
            turn.reply = fallback_turn(context).reply
        uncertain = is_uncertain(message)
        if uncertain and message not in state.uncertainties:
            state.uncertainties.append(message)
        is_correction = message.lower().startswith("korrektur:")
        # An explicit correction is already a verbatim professional statement.
        # Its persistence must not depend on optional model extraction.
        fact = message.strip() if is_correction else turn.public_note.strip()
        # Extractive boundary: the model cannot introduce new wording as evidence.
        is_fact = intent.intent in (DialogueIntent.ADDITIONAL_INFO, DialogueIntent.CONFIRMATION, DialogueIntent.DEVELOPMENT_UPDATE)
        accepted_fact = bool(fact and fact in message and is_fact)
        if accepted_fact:
            if is_correction:
                from sob_generator.conversation import correction_target
                target = correction_target(message, state.user_notes)
                if target is None:
                    accepted_fact = False
                    turn.sufficient = False
                    turn.pending_question = "Welche frühere Angabe soll durch diese Korrektur ersetzt werden?"
                else:
                    removed = [state.user_notes[target]]
                    state.user_notes = [n for i, n in enumerate(state.user_notes) if i != target]
                    session.memory.learned_facts = [n for n in session.memory.learned_facts if n not in removed]
                    session.developments = "\n".join(n for n in session.developments.splitlines() if n not in removed)
            if accepted_fact:
                if fact not in state.user_notes:
                    state.user_notes.append(fact)
                if fact not in session.memory.learned_facts:
                    session.memory.learned_facts.append(fact)
                development = extract_development_update(fact)
                if development and development not in session.developments:
                    session.add_development_note(development)
        if is_correction:
            turn.sufficient = False
        if turn.pending_question.strip() == message.strip() and not is_fact:
            turn.pending_question = ""
            turn.reply = turn.reply.replace(message, "").strip()
        if not state.pending_question and is_fact:
            turn.sufficient = False
        if not turn.sufficient and not turn.pending_question and is_fact:
            turn.pending_question = state.pending_question or fallback_turn(context).pending_question
        if uncertain:
            turn.reply = fallback_turn(context).reply
            turn.pending_question = (fallback_turn(context).pending_question if is_crisis_or_high_risk_note(message) else state.pending_question or fallback_turn(context).pending_question)
            turn.sufficient = False
        if turn.pending_question and "?" not in turn.reply:
            turn.reply = f"{turn.reply.strip()} {turn.pending_question}"
        was_pending = bool(state.pending_question)
        if not uncertain and is_fact:
            state.pending_question = turn.pending_question
        elif not state.pending_question:
            state.pending_question = turn.pending_question
        session.add_assistant_message(turn.reply)
        sufficient = turn.sufficient and was_pending and not uncertain and accepted_fact and not turn.pending_question
        if is_correction:
            sufficient = False
        if sufficient or (state.bericht_zielerreichung and accepted_fact and not uncertain):
            await self._write_draft(session, section)
        elif is_fact:
            session.phase = DialoguePhase.FIELD_QUESTIONS

    async def confirm_section(self, session_id: str, section_id: str) -> ChatSession:
        """Confirms a section and advances to next active section or finalization."""
        session = self.session_store.get(session_id)
        if session is None:
            raise KeyError(f"ChatSession with ID {session_id} not found.")

        sec_state = session.sections.get(section_id)
        if sec_state and not sec_state.bericht_zielerreichung:
            sec_def = self.get_section_def(section_id, session)
            if sec_def and not await self._write_draft(session, sec_def):
                self.session_store.store(session)
                return session
        if sec_state:
            sec_state.confirmed_goals = sec_state.current_goals

        session.confirm_section(section_id)
        session.add_assistant_message(
            f"✓ Lebensfeld {section_id} ({session.sections[section_id].title}) wurde erfolgreich freigegeben."
        )

        next_sid = next(
            (sid for sid, s in session.sections.items() if s.is_active_field and s.status != SectionStatus.CONFIRMED),
            None,
        )
        if next_sid:
            session.set_active_section(next_sid)
            session.phase = DialoguePhase.FIELD_REFLECT
            next_sec_def = self.get_section_def(next_sid, session)
            if next_sec_def:
                next_entries = session.memory.section_entries.get(next_sid, [])
                goal_msg = await self._goal_reply(session, next_sec_def)
                session.add_assistant_message(
                    f"Kommen wir zum nächsten Lebensfeld:\n\n" + goal_msg
                )
        else:
            session.phase = DialoguePhase.FINALIZED
            session.add_assistant_message(
                "🎉 **Hervorragend! Alle aktiven Lebensfelder des Sozialberichts sind fertiggestellt und freigegeben.**\n\n"
                "Du kannst den fertigen Bericht nun über die Schaltfläche unten als offizielles Word-Dokument (.docx) exportieren."
            )

        self.session_store.store(session)
        return session

    def select_section(self, session_id: str, section_id: str) -> ChatSession:
        """Selects a section for viewing/editing."""
        session = self.session_store.get(session_id)
        if session is None:
            raise KeyError(f"ChatSession with ID {session_id} not found.")
        session.set_active_section(section_id)
        if session.phase != DialoguePhase.SCREENING:
            session.phase = DialoguePhase.FIELD_DRAFT if session.sections[section_id].bericht_zielerreichung else DialoguePhase.FIELD_REFLECT
        self.session_store.store(session)
        return session

    def edit_section(self, session_id: str, section_id: str, **fields) -> ChatSession:
        """Apply public, human-reviewed edits without altering historical goals."""
        session = self.session_store.get(session_id)
        if session is None or section_id not in session.sections:
            raise KeyError("Unknown session or section")
        if set(fields) - set(SectionLLMOutput.model_fields):
            raise ValueError("Unknown report field")
        cleaned = {key: value.strip() for key, value in fields.items()}
        session.update_section_draft(section_id, **cleaned, status=SectionStatus.DRAFTED)
        state = session.sections[section_id]
        state.is_active_field = True
        if "vorschlag_ziele" in cleaned:
            state.proposed_goals = cleaned["vorschlag_ziele"]
            state.confirmed_goals = ""
        session.set_active_section(section_id)
        session.phase = DialoguePhase.FIELD_DRAFT
        self.session_store.store(session)
        return session

    def export_docx(self, session_id: str, out_path: Path) -> Path:
        """Renders a snapshot; explicit completion owns session cleanup."""
        session = self.session_store.get(session_id)
        if session is None:
            raise KeyError(f"ChatSession with ID {session_id} not found.")

        draft = session.to_report_draft()
        tmpl = self.get_template(session)
        renderer = tmpl.renderer_factory(tmpl.docx_path)
        renderer.render(draft, out_path)
        return out_path

    def export_markdown(self, session_id: str) -> Tuple[str, str]:
        """Generates markdown export string and filename for the session."""
        session = self.session_store.get(session_id)
        if session is None:
            raise KeyError(f"ChatSession with ID {session_id} not found.")

        lines = [
            f"# SOB Chatverlauf — {session.client.vorname} {session.client.name}",
            f"- **Klient:in**: {session.client.anrede} {session.client.vorname} {session.client.name}".strip(),
            f"- **Berichtszeitraum**: {session.meta.leistungszeitraum_von} bis {session.meta.leistungszeitraum_bis}",
            f"- **Aktuelle Phase**: {session.phase.value}",
            "",
            "---",
            "",
        ]
        for msg in session.messages:
            role_label = "👤 Du (Fachkraft)" if msg.role.value == "user" else "🤖 SOB Assistentin"
            sec_label = f" (Abschnitt {msg.section_id})" if msg.section_id else ""
            lines.append(f"### {role_label}{sec_label}")
            lines.append("")
            lines.append(msg.content)
            lines.append("")
            lines.append("---")
            lines.append("")

        lines.append("## Aktueller Entwurfsstand")
        lines.append("")
        for sid, sec in session.sections.items():
            if sec.bericht_zielerreichung or sec.vorschlag_ziele or sec.vorschlag_massnahmen:
                lines.append(f"### Abschnitt {sid}: {sec.title} [{sec.status.value}]")
                if sec.bericht_zielerreichung:
                    lines.append(f"**Bericht zur Zielerreichung:**\n{sec.bericht_zielerreichung}\n")
                if sec.vorschlag_ziele:
                    lines.append(f"**Vorschlag smarte Ziele:**\n{sec.vorschlag_ziele}\n")
                if sec.vorschlag_massnahmen:
                    lines.append(f"**Vorschlag Maßnahmen:**\n{sec.vorschlag_massnahmen}\n")

        md_content = "\n".join(lines)
        filename = f"Chatverlauf_{session.client.vorname}_{session.client.name}.md".replace(" ", "_")
        return md_content, filename
