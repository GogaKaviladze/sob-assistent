"""Kontext-Builder für Lebensbereich-Fokussierung im Fachdialog (Issue #136).

Erzeugt deterministisch die markierten Datenblöcke (<fall_kopf>, <vorbericht_bereich>, <verlauf_eintraege>)
für einen ausgewählten Lebensbereich, ordnet CSV-Einträge über HAMBURG_SECTION_KEYWORDS zu
und berechnet Zähler für UI und Logging.
Zero Persistence, RAM-only, keine personenbezogenen Daten im Fallkopf.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

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


class ContextBuilder:
    """Baut deterministisch den isolierten Bereichs-Kontext aus Fall- und Verlaufsdaten."""

    @classmethod
    def build(cls, case: Any, bereich_id: str) -> BuiltContext:
        """Erzeugt BuiltContext für den angegebenen Bereich."""
        is_odw = bool(
            case.plan
            and any(
                getattr(g, "section_id", "") in ODW_TITLES
                and getattr(g, "section_id", "") not in HAMBURG_TITLES
                for g in getattr(case.plan, "goals", [])
            )
        )
        if not is_odw and bereich_id in ODW_TITLES and bereich_id not in HAMBURG_TITLES:
            is_odw = True

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
        vorbericht_lines = []
        if case.plan and getattr(case.plan, "goals", None):
            matching_goals = [
                g for g in case.plan.goals
                if getattr(g, "section_id", "") == bereich_id
            ]
            if matching_goals:
                g = matching_goals[0]
                if getattr(g, "leitziel", ""):
                    vorbericht_lines.append(f"• Leitziel: {sanitize_for_prompt(g.leitziel)}")
                if getattr(g, "smarte_ziele", ""):
                    vorbericht_lines.append(f"• Smartes Ziel: {sanitize_for_prompt(g.smarte_ziele)}")
                if getattr(g, "stand_zielerreichung", ""):
                    vorbericht_lines.append(f"• Bisherige Zielerreichung / Reflexion: {sanitize_for_prompt(g.stand_zielerreichung)}")
                if getattr(g, "vorschlag_ziele", ""):
                    vorbericht_lines.append(f"• Vorschlag für aktuelle Ziele: {sanitize_for_prompt(g.vorschlag_ziele)}")
                if getattr(g, "vorschlag_massnahmen", ""):
                    vorbericht_lines.append(f"• Vorschlag Maßnahmen: {sanitize_for_prompt(g.vorschlag_massnahmen)}")
                if getattr(g, "realisierung_andere", ""):
                    vorbericht_lines.append(f"• Realisierung durch andere: {sanitize_for_prompt(g.realisierung_andere)}")

        if not vorbericht_lines:
            vorbericht_text = "Keine Vorbericht-Ziele für diesen Lebensbereich erfasst."
        else:
            vorbericht_text = "\n".join(vorbericht_lines)

        # 3. Verlaufs-Einträge zuordnen & filtern
        assigned_count = 0
        unassigned_count = 0
        total_count = 0
        selected_lines: List[str] = []

        # Relevante Hamburg-IDs für die Zuordnung
        target_hamburg_ids: Set[str] = set()
        if is_odw:
            target_hamburg_ids = set(OP_DE_WISCH_TO_HAMBURG_MAP.get(bereich_id, []))
        else:
            target_hamburg_ids = {bereich_id}

        if case.csv and getattr(case.csv, "entries", None):
            entries = case.csv.entries
            total_count = len(entries)

            # Chronologisch sortieren (aufsteigend)
            sorted_entries = sorted(
                entries,
                key=lambda e: _parse_entry_datetime(e.datum, getattr(e, "uhrzeit", "")),
            )

            for e in sorted_entries:
                search_text = f"{e.kategorie} {e.text}"
                matched_sec_ids = classify_entry_sections(search_text)
                is_unassigned = len(matched_sec_ids) == 0
                is_assigned = bool(matched_sec_ids & target_hamburg_ids)

                if is_assigned:
                    assigned_count += 1
                    formatted = _format_vivendi_entry(e)
                    if formatted:
                        selected_lines.append(sanitize_for_prompt(formatted))
                elif is_unassigned:
                    unassigned_count += 1
                    formatted = _format_vivendi_entry(e)
                    if formatted:
                        selected_lines.append(sanitize_for_prompt(formatted))

        elif case.csv and getattr(case.csv, "rows", None) and case.csv.rows:
            rows = case.csv.rows
            headers = getattr(case.csv, "headers", [])
            total_count = len(rows)

            for idx, r in enumerate(rows, start=1):
                search_text = " ".join(r)
                matched_sec_ids = classify_entry_sections(search_text)
                is_unassigned = len(matched_sec_ids) == 0
                is_assigned = bool(matched_sec_ids & target_hamburg_ids)

                if is_assigned:
                    assigned_count += 1
                    formatted = _format_generic_row(idx, r, headers)
                    if formatted:
                        selected_lines.append(sanitize_for_prompt(formatted))
                elif is_unassigned:
                    unassigned_count += 1
                    formatted = _format_generic_row(idx, r, headers)
                    if formatted:
                        selected_lines.append(sanitize_for_prompt(formatted))

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
        )
