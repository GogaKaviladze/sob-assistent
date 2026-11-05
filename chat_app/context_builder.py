"""Kontext-Builder für Lebensbereich-Fokussierung im Fachdialog (Issue #136).

Erzeugt deterministisch die markierten Datenblöcke (<fall_kopf>, <vorbericht_bereich>, <verlauf_eintraege>)
für einen ausgewählten Lebensbereich, ordnet CSV-Einträge über HAMBURG_SECTION_KEYWORDS zu
und berechnet Zähler für UI und Logging.
Zero Persistence, RAM-only, keine personenbezogenen Daten im Fallkopf.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from chat_app.budget import split_sentences

from chat_app.csv_loader import (
    _format_generic_row,
    _format_vivendi_entry,
    _parse_entry_datetime,
)
from chat_app.docx_exporter import (
    HAMBURG_SECTION_KEYWORDS,
    OP_DE_WISCH_TO_HAMBURG_MAP,
)
from chat_app.plan_loader import PlanData, PlanGoalItem
from sob_generator.llm.schemas import DocumentationEntry
from sob_generator.llm.sections import SECTION_DEFINITIONS
from sob_generator.llm.sections_op_de_wisch import OP_DE_WISCH_SECTION_DEFINITIONS

# Zeichenbereiche: Zero-Width (U+200B–U+200F, U+FEFF) und Bidi-Steuerzeichen (U+202A–U+202E, U+2066–U+2069)
_BIDI_AND_ZERO_WIDTH_RE = re.compile(
    r"[\u200B-\u200F\u202A-\u202E\u2066-\u2069\uFEFF]"
)

HAMBURG_TITLES: Dict[str, str] = {s.id: s.title for s in SECTION_DEFINITIONS}
ODW_TITLES: Dict[str, str] = {s.id: s.title for s in OP_DE_WISCH_SECTION_DEFINITIONS}

FREITEXT_FALLBACK_PREFIX = "⚠️ Freitext-Vorbericht ohne Bereichszuordnung – gesamter Text:"


def sanitize_for_prompt(text: str) -> str:
    """Bereinigt Eingaben vor dem Einfügen in Prompt-Datenblöcke.

    1. NFKC-Normalisierung
    2. Entfernung von Zero-Width- und Bidi-Steuerzeichen
    3. Escaping von < und > zu ‹ und › zur Verhinderung von XML/Tag-Injections
    """
    if not text:
        return ""
    # 1. NFKC-Normalisierung
    text = unicodedata.normalize("NFKC", text)
    # 2. Zero-Width und Bidi entfernen
    text = _BIDI_AND_ZERO_WIDTH_RE.sub("", text)
    # 3. < und > neutralisieren
    text = text.replace("<", "‹").replace(">", "›")
    return text


def get_canonical_title(bereich_id: str, is_odw: bool = False) -> str:
    """Gibt den offiziellen kanonischen Titel aus den Abschnittsdefinitionen zurück."""
    if is_odw and bereich_id in ODW_TITLES:
        return ODW_TITLES[bereich_id]
    if bereich_id in HAMBURG_TITLES:
        return HAMBURG_TITLES[bereich_id]
    if bereich_id in ODW_TITLES:
        return ODW_TITLES[bereich_id]
    return f"Bereich {bereich_id}"


def _match_keywords(search_text: str, keywords: List[str]) -> bool:
    """Prüft, ob ein Schlüsselwort im Text vorkommt (Wortgrenzen oder Teilstring bei Mehrwort)."""
    for kw in keywords:
        if " " in kw:
            if kw in search_text:
                return True
        else:
            if re.search(r"\b" + re.escape(kw) + r"\w*", search_text):
                return True
    return False


def classify_entry_sections(entry_text: str) -> Set[str]:
    """Ermittelt alle Hamburger Bereichs-IDs, zu denen der Eintrag schlüsselwortbasiert passt."""
    clean = entry_text.lower()
    matches: Set[str] = set()
    for sec_id, kws in HAMBURG_SECTION_KEYWORDS.items():
        if _match_keywords(clean, kws):
            matches.add(sec_id)
    return matches


@dataclass(frozen=True)
class DokuEntryItem:
    """Einzelner formatierter Doku-Eintrag für deterministische Budgetierung."""

    datum: str
    formatted_text: str
    is_assigned: bool
    sort_key: Any = None


@dataclass(frozen=True)
class BuiltContext:
    """Ergebnis des deterministischen Kontextaufbaus für einen Lebensbereich."""

    fall_kopf: str
    vorbericht_bereich: str
    verlauf_eintraege: str
    prompt_user_message: str
    assigned_count: int
    unassigned_count: int
    total_count: int
    bereich_id: str
    bereich_title: str
    vorbericht_mandatory: str = ""
    vorbericht_sentences: List[str] = field(default_factory=list)
    doku_entries: List[DokuEntryItem] = field(default_factory=list)


class ContextBuilder:
    """Baut deterministisch den isolierten Bereichs-Kontext aus Fall- und Verlaufsdaten."""

    @classmethod
    def is_odw_case(cls, case: Any, bereich_id: Optional[str] = None) -> bool:
        """Ermittelt, ob für den Fall die Op-de-Wisch-Vorlage aktiv ist."""
        if getattr(case, "template", "") == "op-de-wisch":
            return True
        if getattr(case, "template_selected", False):
            return False
        plan = getattr(case, "plan", None)
        if plan and (
            getattr(plan, "template", "") == "op-de-wisch"
            or any(
                getattr(g, "section_id", "") in ODW_TITLES
                and getattr(g, "section_id", "") not in HAMBURG_TITLES
                for g in getattr(plan, "goals", [])
            )
        ):
            return True
        if bereich_id and bereich_id in ODW_TITLES and bereich_id not in HAMBURG_TITLES:
            return True
        return False

    @classmethod
    def build(cls, case: Any, bereich_id: str) -> BuiltContext:
        """Erzeugt BuiltContext für den angegebenen Bereich."""
        is_odw = cls.is_odw_case(case, bereich_id)

        bereich_title = get_canonical_title(bereich_id, is_odw)

        # 1. Fallkopf zusammenstellen (Bereichstitel, Berichtszeitraum; keine Anschrift/Geburtsdatum/Dateiname)
        fall_kopf_lines = [f"Lebensbereich: {sanitize_for_prompt(bereich_title)} ({bereich_id})"]
        zeitraum = ""
        if case.plan and (case.plan.zeitraum_von or case.plan.zeitraum_bis):
            zeitraum = f"{case.plan.zeitraum_von} bis {case.plan.zeitraum_bis}".strip()
        elif case.plan_info:
            zv = case.plan_info.get("zeitraum_von", "")
            zb = case.plan_info.get("zeitraum_bis", "")
            if zv or zb:
                zeitraum = f"{zv} bis {zb}".strip()
            elif case.plan_info.get("zeitraum"):
                zeitraum = case.plan_info.get("zeitraum")

        if zeitraum and zeitraum != "bis":
            fall_kopf_lines.append(f"Berichtszeitraum: {sanitize_for_prompt(zeitraum)}")
        fall_kopf_text = "\n".join(fall_kopf_lines)

        # 2. Vorbericht nur dieses Bereichs
        vorbericht_mandatory_lines: List[str] = []
        vorbericht_freitext_sentences: List[str] = []
        vorbericht_lines: List[str] = []

        if case.plan and getattr(case.plan, "goals", None):
            matching_goals = [
                g for g in case.plan.goals
                if getattr(g, "section_id", "") == bereich_id
            ]
            if matching_goals:
                g = matching_goals[0]
                if getattr(g, "leitziel", ""):
                    lz_line = f"• Leitziel: {sanitize_for_prompt(g.leitziel)}"
                    vorbericht_mandatory_lines.append(lz_line)
                    vorbericht_lines.append(lz_line)
                if getattr(g, "smarte_ziele", ""):
                    smart_line = f"• Smartes Ziel: {sanitize_for_prompt(g.smarte_ziele)}"
                    vorbericht_mandatory_lines.append(smart_line)
                    vorbericht_lines.append(smart_line)

                # Freitext-Felder
                freitexte = [
                    ("Bisherige Zielerreichung / Reflexion", getattr(g, "stand_zielerreichung", "")),
                    ("Vorschlag für aktuelle Ziele", getattr(g, "vorschlag_ziele", "")),
                    ("Vorschlag Maßnahmen", getattr(g, "vorschlag_massnahmen", "")),
                    ("Realisierung durch andere", getattr(g, "realisierung_andere", "")),
                ]
                for label, val in freitexte:
                    if val and val.strip():
                        sents = split_sentences(val)
                        if sents:
                            vorbericht_freitext_sentences.append(f"• {label}: {sanitize_for_prompt(sents[0])}")
                            for rem_s in sents[1:]:
                                vorbericht_freitext_sentences.append(sanitize_for_prompt(rem_s))
                        vorbericht_lines.append(f"• {label}: {sanitize_for_prompt(val)}")

        elif getattr(case, "plan_context", None):
            # Unstrukturierter Vorbericht-Fallback
            sents = split_sentences(case.plan_context)
            vorbericht_freitext_sentences.extend(sents)
            vorbericht_lines.append(case.plan_context.strip())

        vorbericht_mandatory_text = "\n".join(vorbericht_mandatory_lines)
        if not vorbericht_lines:
            if not (case.plan and getattr(case.plan, "goals", None)) and getattr(case, "plan_context", None) and case.plan_context.strip():
                vorbericht_text = f"{FREITEXT_FALLBACK_PREFIX}\n{sanitize_for_prompt(case.plan_context.strip())}"
            elif case.plan and getattr(case.plan, "parser_source", "") == "text_plan" and getattr(case.plan, "raw_summary", None) and case.plan.raw_summary.strip():
                vorbericht_text = f"{FREITEXT_FALLBACK_PREFIX}\n{sanitize_for_prompt(case.plan.raw_summary.strip())}"
            else:
                vorbericht_text = "Keine Vorbericht-Ziele für diesen Lebensbereich erfasst."
        else:
            vorbericht_text = "\n".join(vorbericht_lines)

        # 3. Verlaufs-Einträge zuordnen & filtern
        assigned_count = 0
        unassigned_count = 0
        total_count = 0
        selected_lines: List[str] = []
        doku_items: List[DokuEntryItem] = []

        # Relevante Hamburg-IDs für die Zuordnung
        target_hamburg_ids: Set[str] = set()
        if is_odw:
            target_hamburg_ids = set(OP_DE_WISCH_TO_HAMBURG_MAP.get(bereich_id, []))
        else:
            target_hamburg_ids = {bereich_id}

        manual_assignments: Dict[str, str] = getattr(case, "manual_assignments", {}) or {}

        if case.csv and getattr(case.csv, "entries", None):
            entries = case.csv.entries
            total_count = len(entries)

            # Chronologisch sortieren (aufsteigend) mit stabilem CSV-Index
            indexed_entries = list(enumerate(entries))
            sorted_entries = sorted(
                indexed_entries,
                key=lambda item: _parse_entry_datetime(item[1].datum, getattr(item[1], "uhrzeit", "")),
            )

            for orig_idx, e in sorted_entries:
                search_text = f"{e.kategorie} {e.text}"
                matched_sec_ids = classify_entry_sections(search_text)
                is_auto = len(matched_sec_ids) > 0
                manual_target = manual_assignments.get(str(orig_idx))

                if manual_target is not None:
                    is_unassigned = False
                    is_assigned = (manual_target == bereich_id)
                else:
                    is_unassigned = not is_auto
                    is_assigned = bool(matched_sec_ids & target_hamburg_ids)

                if is_assigned:
                    assigned_count += 1
                    formatted = _format_vivendi_entry(e)
                    if formatted:
                        clean_fmt = sanitize_for_prompt(formatted)
                        selected_lines.append(clean_fmt)
                        doku_items.append(
                            DokuEntryItem(
                                datum=e.datum,
                                formatted_text=clean_fmt,
                                is_assigned=True,
                                sort_key=_parse_entry_datetime(e.datum, getattr(e, "uhrzeit", "")),
                            )
                        )
                elif is_unassigned:
                    unassigned_count += 1
                    formatted = _format_vivendi_entry(e)
                    if formatted:
                        clean_fmt = sanitize_for_prompt(formatted)
                        selected_lines.append(clean_fmt)
                        doku_items.append(
                            DokuEntryItem(
                                datum=e.datum,
                                formatted_text=clean_fmt,
                                is_assigned=False,
                                sort_key=_parse_entry_datetime(e.datum, getattr(e, "uhrzeit", "")),
                            )
                        )

        elif case.csv and getattr(case.csv, "rows", None) and case.csv.rows:
            rows = case.csv.rows
            headers = getattr(case.csv, "headers", [])
            total_count = len(rows)

            for orig_idx, r in enumerate(rows):
                idx = orig_idx + 1
                search_text = " ".join(r)
                matched_sec_ids = classify_entry_sections(search_text)
                is_auto = len(matched_sec_ids) > 0
                manual_target = manual_assignments.get(str(orig_idx))

                if manual_target is not None:
                    is_unassigned = False
                    is_assigned = (manual_target == bereich_id)
                else:
                    is_unassigned = not is_auto
                    is_assigned = bool(matched_sec_ids & target_hamburg_ids)

                date_val = r[0] if r else ""
                if is_assigned:
                    assigned_count += 1
                    formatted = _format_generic_row(idx, r, headers)
                    if formatted:
                        clean_fmt = sanitize_for_prompt(formatted)
                        selected_lines.append(clean_fmt)
                        doku_items.append(
                            DokuEntryItem(
                                datum=date_val,
                                formatted_text=clean_fmt,
                                is_assigned=True,
                                sort_key=idx,
                            )
                        )
                elif is_unassigned:
                    unassigned_count += 1
                    formatted = _format_generic_row(idx, r, headers)
                    if formatted:
                        clean_fmt = sanitize_for_prompt(formatted)
                        selected_lines.append(clean_fmt)
                        doku_items.append(
                            DokuEntryItem(
                                datum=date_val,
                                formatted_text=clean_fmt,
                                is_assigned=False,
                                sort_key=idx,
                            )
                        )

        if not selected_lines:
            verlauf_text = "Keine Verlaufsdokumentation für diesen Bereich vorhanden."
        else:
            verlauf_text = "\n".join(selected_lines)

        prompt_user_message = (
            f"<fall_kopf>\n{fall_kopf_text}\n</fall_kopf>\n\n"
            f'<vorbericht_bereich id="{bereich_id}">\n{vorbericht_text}\n</vorbericht_bereich>\n\n'
            f"<verlauf_eintraege>\n{verlauf_text}\n</verlauf_eintraege>"
        )

        return BuiltContext(
            fall_kopf=fall_kopf_text,
            vorbericht_bereich=vorbericht_text,
            verlauf_eintraege=verlauf_text,
            prompt_user_message=prompt_user_message,
            assigned_count=assigned_count,
            unassigned_count=unassigned_count,
            total_count=total_count,
            bereich_id=bereich_id,
            bereich_title=bereich_title,
            vorbericht_mandatory=vorbericht_mandatory_text,
            vorbericht_sentences=vorbericht_freitext_sentences,
            doku_entries=doku_items,
        )

    @classmethod
    def get_review_data(cls, case: Any, bereich_id: str) -> Dict[str, Any]:
        """Liefert strukturierte Daten für 'Bereich prüfen': Alt-SOB und zugeordnete CSV-Einträge."""
        is_odw = cls.is_odw_case(case, bereich_id)

        bereich_title = get_canonical_title(bereich_id, is_odw)
        confirmed = case.is_area_confirmed(bereich_id) if hasattr(case, "is_area_confirmed") else False

        # 1. Alt-SOB Teil für diesen Bereich
        alt_sob: Dict[str, Any] = {
            "has_goals": False,
            "section_id": bereich_id,
            "section_title": bereich_title,
            "leitziel": "",
            "smarte_ziele": "",
            "stand_zielerreichung": "",
            "vorschlag_ziele": "",
            "vorschlag_massnahmen": "",
            "realisierung_andere": "",
            "text": "",
        }
        if case.plan and getattr(case.plan, "goals", None):
            matching_goals = [
                g for g in case.plan.goals
                if getattr(g, "section_id", "") == bereich_id
            ]
            if matching_goals:
                g = matching_goals[0]
                has_content = bool(
                    getattr(g, "leitziel", "")
                    or getattr(g, "smarte_ziele", "")
                    or getattr(g, "stand_zielerreichung", "")
                    or getattr(g, "vorschlag_ziele", "")
                    or getattr(g, "vorschlag_massnahmen", "")
                    or getattr(g, "realisierung_andere", "")
                )
                alt_sob["has_goals"] = has_content
                alt_sob["leitziel"] = getattr(g, "leitziel", "")
                alt_sob["smarte_ziele"] = getattr(g, "smarte_ziele", "")
                alt_sob["stand_zielerreichung"] = getattr(g, "stand_zielerreichung", "")
                alt_sob["vorschlag_ziele"] = getattr(g, "vorschlag_ziele", "")
                alt_sob["vorschlag_massnahmen"] = getattr(g, "vorschlag_massnahmen", "")
                alt_sob["realisierung_andere"] = getattr(g, "realisierung_andere", "")

                text_lines = []
                if getattr(g, "leitziel", ""):
                    text_lines.append(f"Leitziel: {g.leitziel}")
                if getattr(g, "smarte_ziele", ""):
                    text_lines.append(f"Smartes Ziel: {g.smarte_ziele}")
                if getattr(g, "stand_zielerreichung", ""):
                    text_lines.append(f"Bisherige Zielerreichung: {g.stand_zielerreichung}")
                if getattr(g, "vorschlag_ziele", ""):
                    text_lines.append(f"Vorschlag Ziele: {g.vorschlag_ziele}")
                if getattr(g, "vorschlag_massnahmen", ""):
                    text_lines.append(f"Vorschlag Maßnahmen: {g.vorschlag_massnahmen}")
                if getattr(g, "realisierung_andere", ""):
                    text_lines.append(f"Realisierung durch andere: {g.realisierung_andere}")
                alt_sob["text"] = "\n".join(text_lines)

        if not alt_sob["has_goals"]:
            fallback_txt = ""
            if not (case.plan and getattr(case.plan, "goals", None)) and getattr(case, "plan_context", None) and case.plan_context.strip():
                fallback_txt = case.plan_context.strip()
            elif case.plan and getattr(case.plan, "parser_source", "") == "text_plan" and getattr(case.plan, "raw_summary", None) and case.plan.raw_summary.strip():
                fallback_txt = case.plan.raw_summary.strip()
            if fallback_txt:
                prefixed = f"{FREITEXT_FALLBACK_PREFIX}\n{fallback_txt}"
                alt_sob["has_goals"] = True
                alt_sob["is_freitext_fallback"] = True
                alt_sob["leitziel"] = prefixed
                alt_sob["text"] = prefixed

        # 2. CSV-Einträge
        assigned_entries: List[Dict[str, Any]] = []
        unassigned_entries: List[Dict[str, Any]] = []
        total_count = 0

        target_hamburg_ids: Set[str] = set()
        if is_odw:
            target_hamburg_ids = set(OP_DE_WISCH_TO_HAMBURG_MAP.get(bereich_id, []))
        else:
            target_hamburg_ids = {bereich_id}

        manual_assignments: Dict[str, str] = getattr(case, "manual_assignments", {}) or {}

        if case.csv and getattr(case.csv, "entries", None):
            entries = case.csv.entries
            total_count = len(entries)
            indexed_entries = list(enumerate(entries))
            sorted_entries = sorted(
                indexed_entries,
                key=lambda item: _parse_entry_datetime(item[1].datum, getattr(item[1], "uhrzeit", "")),
            )
            for orig_idx, e in sorted_entries:
                search_text = f"{e.kategorie} {e.text}"
                matched_sec_ids = classify_entry_sections(search_text)
                is_auto = len(matched_sec_ids) > 0
                manual_target = manual_assignments.get(str(orig_idx))

                if manual_target is not None:
                    is_unassigned = False
                    is_assigned = (manual_target == bereich_id)
                else:
                    is_unassigned = not is_auto
                    is_assigned = bool(matched_sec_ids & target_hamburg_ids)

                entry_dict = {
                    "id": str(orig_idx),
                    "entry_id": str(orig_idx),
                    "datum": getattr(e, "datum", ""),
                    "uhrzeit": getattr(e, "uhrzeit", ""),
                    "kategorie": getattr(e, "kategorie", ""),
                    "text": getattr(e, "text", ""),
                    "formatted": _format_vivendi_entry(e),
                    "manual_bereich_id": manual_target,
                    "is_manual": manual_target is not None,
                }
                if is_assigned:
                    assigned_entries.append(entry_dict)
                elif is_unassigned:
                    unassigned_entries.append(entry_dict)

        elif case.csv and getattr(case.csv, "rows", None) and case.csv.rows:
            rows = case.csv.rows
            headers = getattr(case.csv, "headers", [])
            total_count = len(rows)
            for orig_idx, r in enumerate(rows):
                idx = orig_idx + 1
                search_text = " ".join(r)
                matched_sec_ids = classify_entry_sections(search_text)
                is_auto = len(matched_sec_ids) > 0
                manual_target = manual_assignments.get(str(orig_idx))

                if manual_target is not None:
                    is_unassigned = False
                    is_assigned = (manual_target == bereich_id)
                else:
                    is_unassigned = not is_auto
                    is_assigned = bool(matched_sec_ids & target_hamburg_ids)

                entry_dict = {
                    "id": str(orig_idx),
                    "entry_id": str(orig_idx),
                    "datum": r[0] if len(r) > 0 else "",
                    "uhrzeit": "",
                    "kategorie": r[1] if len(r) > 1 else "",
                    "text": " | ".join(r),
                    "formatted": _format_generic_row(idx, r, headers),
                    "manual_bereich_id": manual_target,
                    "is_manual": manual_target is not None,
                }
                if is_assigned:
                    assigned_entries.append(entry_dict)
                elif is_unassigned:
                    unassigned_entries.append(entry_dict)

        # Bereichsoptionen für die manuelle Zuordnung: genau aus derselben Vorlagenquelle
        area_defs = OP_DE_WISCH_SECTION_DEFINITIONS if is_odw else SECTION_DEFINITIONS
        area_options = [{"id": s.id, "title": s.title} for s in area_defs]

        return {
            "bereich_id": bereich_id,
            "bereich_title": bereich_title,
            "confirmed": confirmed,
            "alt_sob": alt_sob,
            "assigned_entries": assigned_entries,
            "unassigned_entries": unassigned_entries,
            "assigned_count": len(assigned_entries),
            "unassigned_count": len(unassigned_entries),
            "total_count": total_count,
            "area_options": area_options,
            "available_areas": area_options,
        }
