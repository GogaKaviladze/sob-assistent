"""Plan-Loader für frühere Entwicklungs- und Gesamtberichte (.docx) (ADR-4/ADR-10, SOLID).

Ermöglicht das Einlesen und Strukturieren von Vorberichten/Gesamtplänen, um
bisherige Ziele, den Stand der Zielerreichung und Klientendaten in den
Chat-Kontext und die Reflexion einzubinden.
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Dict, List, Optional, Union

from sob_generator import docx_fields as fields
from sob_generator.docx_safety import SafeDocx, sanitize_docx_bytes
from sob_generator.llm.sections import SECTION_DEFINITIONS
from sob_generator.llm.sections_op_de_wisch import OP_DE_WISCH_SECTION_DEFINITIONS
from sob_generator.previous_sob import IncompletePlanError

from .logger import logger

_BLANK = re.compile(r"[\s\u00a0\u2000-\u200b\u3000]")


def clean_form_value(value: Optional[str]) -> str:
    """Bereinigt Formularfeldwerte von Leerzeichen und Vorlagen-Platzhaltern."""
    if not value or not _BLANK.sub("", value):
        return ""
    return value.strip()


def extract_client_name_from_filename(filename: str) -> str:
    """Extrahiert heuristisch den Klientennamen aus Dateinamen wie 'Hans_Peter_SVB_ausgefuellt.docx' oder 'Erika_Musterfrau_Vivendi_PD.csv'."""
    stem = Path(filename).stem
    # Entferne typische Suffixe und Präfixe
    cleaned = re.sub(
        r"(?i)(_?SVB|_?SOB|_?Gesamtplan|_?Vorbericht|_?ausgefuellt|_?bericht|_?plan|_?vivendi|_?export|_?verlauf(?:sbericht)?|_?pd\b|_?csv|_?docx|_?20\d\d|_?synth(?:etisch)?)",
        "",
        stem,
    )
    cleaned = cleaned.replace("_", " ").replace("-", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if cleaned.lower() in {"", "plan", "vorbericht", "dokument", "bericht", "test", "dummy", "verlauf", "daten", "csv", "export", "synth", "synthetisch"}:
        return ""
    return cleaned if len(cleaned) > 2 else ""


@dataclass
class PlanGoalItem:
    """Bisherige Ziele und Stand der Zielerreichung für einen Lebensbereich."""

    section_id: str
    section_title: str
    leitziel: str = ""
    smarte_ziele: str = ""
    stand_zielerreichung: str = ""
    vorschlag_ziele: str = ""
    vorschlag_massnahmen: str = ""
    realisierung_andere: str = ""
    applies: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "section_id": self.section_id,
            "section_title": self.section_title,
            "leitziel": self.leitziel,
            "smarte_ziele": self.smarte_ziele,
            "stand_zielerreichung": self.stand_zielerreichung,
            "vorschlag_ziele": self.vorschlag_ziele,
            "vorschlag_massnahmen": self.vorschlag_massnahmen,
            "realisierung_andere": self.realisierung_andere,
            "applies": self.applies,
        }


@dataclass
class PlanData:
    """Strukturierte Daten eines eingelesenen Vorberichts/Gesamtplans."""

    filename: str
    client_name: str = ""
    zeitraum_von: str = ""
    zeitraum_bis: str = ""
    goals: List[PlanGoalItem] = field(default_factory=list)
    raw_summary: str = ""
    #: True, wenn client_name nur heuristisch aus dem Dateinamen stammt (nicht für Berichtsfelder nutzen)
    client_name_from_filename: bool = False
    hidden_removed: int = 0
    is_fallback: bool = False
    warning: str = ""
    parser_source: str = ""

    @property
    def goals_count(self) -> int:
        """Anzahl der Bereiche mit vereinbarten Zielen oder Zielerreichungsstand."""
        return len([
            g for g in self.goals
            if g.applies and (
                g.leitziel
                or g.smarte_ziele
                or g.stand_zielerreichung
                or g.vorschlag_ziele
                or g.vorschlag_massnahmen
            )
        ])

    @property
    def preview(self) -> str:
        """Kompakte, übersichtliche Textzusammenfassung für UI und Widgets."""
        lines = []
        if self.client_name:
            lines.append(f"Klient: {self.client_name}")
        if self.zeitraum_von or self.zeitraum_bis:
            zeit = f"{self.zeitraum_von} bis {self.zeitraum_bis}".strip()
            lines.append(f"Zeitraum: {zeit}")

        active_goals = [
            g for g in self.goals
            if g.applies and (
                g.leitziel
                or g.smarte_ziele
                or g.stand_zielerreichung
                or g.vorschlag_ziele
                or g.vorschlag_massnahmen
            )
        ]
        lines.append(f"Ziele: {len(active_goals)} Lebensbereiche erfasst")

        if active_goals:
            lines.append("Auszug:")
            for g in active_goals[:3]:
                title = g.section_title or f"Bereich {g.section_id}"
                desc = (
                    g.leitziel
                    or g.smarte_ziele
                    or g.stand_zielerreichung
                    or g.vorschlag_ziele
                    or g.vorschlag_massnahmen
                    or g.realisierung_andere
                )
                if len(desc) > 75:
                    desc = desc[:72] + "..."
                lines.append(f"• {title}: {desc}")
            if len(active_goals) > 3:
                lines.append(f"... (+{len(active_goals) - 3} weitere Bereiche)")
        elif self.raw_summary:
            lines.append(self.raw_summary[:200])

        return "\n".join(lines)

    def to_context_text(self) -> str:
        """Formatiert den Gesamtplan für den Kontext des LLM-Systemprompts."""
        lines = ["Bisheriger Gesamtplan / Vorbericht:"]
        if self.client_name:
            lines.append(f"Klient:in: {self.client_name}")
        if self.zeitraum_von or self.zeitraum_bis:
            lines.append(f"Bisheriger Zeitraum: {self.zeitraum_von} bis {self.zeitraum_bis}".strip())
        if self.raw_summary:
            lines.append(f"Entwicklungen / Hintergrund: {self.raw_summary}")

        active_goals = [
            g for g in self.goals
            if g.applies and (
                g.leitziel
                or g.smarte_ziele
                or g.stand_zielerreichung
                or g.vorschlag_ziele
                or g.vorschlag_massnahmen
                or g.realisierung_andere
            )
        ]
        if not active_goals:
            if not self.raw_summary:
                lines.append("Keine spezifischen Ziele extrahiert.")
            return "\n".join(lines)

        lines.append("Vereinbarte Ziele und bisherige Zielerreichung je Lebensbereich:")
        for g in active_goals:
            title = g.section_title or f"Bereich {g.section_id}"
            lines.append(f"\n[Bereich {g.section_id} – {title}]")
            if g.leitziel:
                lines.append(f"  • Leitziel: {g.leitziel}")
            if g.smarte_ziele:
                lines.append(f"  • Smartes Ziel: {g.smarte_ziele}")
            if g.stand_zielerreichung:
                lines.append(f"  • Bisherige Zielerreichung / Reflexion: {g.stand_zielerreichung}")
            if g.vorschlag_ziele:
                lines.append(f"  • Vorschlag für aktuelle Ziele: {g.vorschlag_ziele}")
            if g.vorschlag_massnahmen:
                lines.append(f"  • Vorschlag Maßnahmen: {g.vorschlag_massnahmen}")
            if g.realisierung_andere:
                lines.append(f"  • Realisierung durch andere: {g.realisierung_andere}")

        return "\n".join(lines)


def parse_plan_docx(
    docx_input: Union[str, Path, bytes, BinaryIO, SafeDocx],
    filename: str = "vorbericht.docx",
) -> PlanData:
    """Liest einen Vorbericht / Gesamtplan aus einer .docx-Datei oder einem In-Memory-Stream ein (Zero-Disk-Footprint)."""
    if isinstance(docx_input, SafeDocx):
        safe_docx = docx_input
    else:
        if isinstance(docx_input, (bytes, bytearray)):
            content_bytes = bytes(docx_input)
        elif hasattr(docx_input, "read"):
            if hasattr(docx_input, "seek"):
                docx_input.seek(0)
            c = docx_input.read()
            content_bytes = c if isinstance(c, bytes) else c.encode("utf-8")
        else:
            p = Path(docx_input)
            if not p.is_file():
                raise FileNotFoundError(f"Dokument nicht gefunden: {p}")
            content_bytes = p.read_bytes()

        # 0. Vorab-Sanitization gegen ZIP-Bomben, DOCTYPE-Angriffe und versteckte Inhalte
        safe_docx = sanitize_docx_bytes(content_bytes)

    target_source = io.BytesIO(safe_docx.data)
    hidden_removed = safe_docx.hidden_removed

    errors: List[Exception] = []
    doc_opened = False

    # 1. Prüfe, ob sob_generator.previous_sob eine read_previous_plan Funktion anbietet
    try:
        import sob_generator.previous_sob as prev_module
        if hasattr(prev_module, "read_previous_plan"):
            if hasattr(target_source, "seek"):
                target_source.seek(0)
            try:
                plan_result = getattr(prev_module, "read_previous_plan")(target_source, filename=filename)
            except TypeError:
                plan_result = getattr(prev_module, "read_previous_plan")(target_source)

            if isinstance(plan_result, PlanData):
                plan_result.hidden_removed = hidden_removed
                return plan_result
            if hasattr(plan_result, "sections"):
                mapped_goals: List[PlanGoalItem] = []
                sections_dict = getattr(plan_result, "sections", {})
                items = sections_dict.values() if isinstance(sections_dict, dict) else sections_dict
                section_title_map = {d.id: d.title for d in SECTION_DEFINITIONS}
                for g in items:
                    has_content = bool(
                        getattr(g, "leitziel", "")
                        or getattr(g, "smarte_ziele", "")
                        or getattr(g, "stand_zielerreichung", "")
                        or getattr(g, "vorschlag_ziele", "")
                        or getattr(g, "vorschlag_massnahmen", "")
                    )
                    applies = getattr(g, "applies", True)
                    if not has_content and not applies:
                        continue

                    sec_id = getattr(g, "section_id", "")
                    sec_title = (
                        getattr(g, "title", "")
                        or getattr(g, "section_title", "")
                        or section_title_map.get(sec_id, "")
                    )
                    mapped_goals.append(
                        PlanGoalItem(
                            section_id=sec_id,
                            section_title=sec_title,
                            leitziel=getattr(g, "leitziel", ""),
                            smarte_ziele=getattr(g, "smarte_ziele", ""),
                            stand_zielerreichung=getattr(g, "stand_zielerreichung", ""),
                            vorschlag_ziele=getattr(g, "vorschlag_ziele", ""),
                            vorschlag_massnahmen=getattr(g, "vorschlag_massnahmen", ""),
                            realisierung_andere=getattr(g, "realisierung_andere", ""),
                            applies=applies,
                        )
                    )

                zeitraum_von = getattr(plan_result, "zeitraum_von", "")
                zeitraum_bis = getattr(plan_result, "zeitraum_bis", "")
                zeitraum_str = getattr(plan_result, "zeitraum", "")
                if zeitraum_str and not (zeitraum_von or zeitraum_bis):
                    parts = re.split(r"\s+bis\s+|\s*-\s*", zeitraum_str, flags=re.IGNORECASE)
                    if len(parts) == 2:
                        zeitraum_von, zeitraum_bis = parts[0].strip(), parts[1].strip()
                    else:
                        zeitraum_von = zeitraum_str.strip()

                doc_client_name = getattr(plan_result, "client_name", "")
                client_name = doc_client_name or extract_client_name_from_filename(filename)
                raw_summary = getattr(plan_result, "developments", "") or getattr(plan_result, "raw_summary", "")

                is_fallback = getattr(plan_result, "is_fallback", False)
                warning = getattr(plan_result, "warning", "")
                parser_source = getattr(plan_result, "parser_source", "bookmarks")

                if mapped_goals:
                    return PlanData(
                        filename=filename,
                        client_name=client_name,
                        client_name_from_filename=bool(client_name) and not doc_client_name,
                        zeitraum_von=zeitraum_von,
                        zeitraum_bis=zeitraum_bis,
                        goals=mapped_goals,
                        raw_summary=raw_summary,
                        hidden_removed=hidden_removed,
                        is_fallback=is_fallback,
                        warning=warning,
                        parser_source=parser_source,
                    )
            elif hasattr(plan_result, "goals"):
                # Mapping falls anderes Dataclass-Objekt
                mapped_goals = []
                for g in getattr(plan_result, "goals", []):
                    mapped_goals.append(
                        PlanGoalItem(
                            section_id=getattr(g, "section_id", ""),
                            section_title=getattr(g, "section_title", "") or getattr(g, "title", ""),
                            leitziel=getattr(g, "leitziel", ""),
                            smarte_ziele=getattr(g, "smarte_ziele", ""),
                            stand_zielerreichung=getattr(g, "stand_zielerreichung", ""),
                            vorschlag_ziele=getattr(g, "vorschlag_ziele", ""),
                            vorschlag_massnahmen=getattr(g, "vorschlag_massnahmen", ""),
                            realisierung_andere=getattr(g, "realisierung_andere", ""),
                            applies=getattr(g, "applies", True),
                        )
                    )
                return PlanData(
                    filename=filename,
                    client_name=getattr(plan_result, "client_name", "") or extract_client_name_from_filename(filename),
                    client_name_from_filename=not getattr(plan_result, "client_name", "")
                    and bool(extract_client_name_from_filename(filename)),
                    zeitraum_von=getattr(plan_result, "zeitraum_von", ""),
                    zeitraum_bis=getattr(plan_result, "zeitraum_bis", ""),
                    goals=mapped_goals,
                    raw_summary=getattr(plan_result, "raw_summary", "") or getattr(plan_result, "developments", ""),
                    hidden_removed=hidden_removed,
                    is_fallback=getattr(plan_result, "is_fallback", False),
                    warning=getattr(plan_result, "warning", ""),
                    parser_source=getattr(plan_result, "parser_source", "fallback"),
                )
    except IncompletePlanError:
        raise
    except (ImportError, AttributeError) as exc:
        logger.warning("previous_sob Modul/Attribut nicht verfügbar: %s", type(exc).__name__)
    except Exception as exc:
        logger.warning("Fehler beim Einlesen via previous_sob: %s", type(exc).__name__)
        errors.append(exc)

    # 2. Formularfeld-basierte Extraktion via WordprocessingML-Bookmarks (ADR-10)
    client_name = ""
    zeitraum_von = ""
    zeitraum_bis = ""
    extracted_goals: List[PlanGoalItem] = []
    is_fallback = False
    warning = ""
    parser_source = "bookmarks"

    try:
        if hasattr(target_source, "seek"):
            target_source.seek(0)
        root = fields.load_document_tree(target_source)
        doc_opened = True

        try:
            client_name = clean_form_value(fields.get_formtext(root, "client_name_vorname"))
        except KeyError:
            pass

        try:
            zeitraum_von = clean_form_value(fields.get_formtext(root, "meta_leistungszeitraum_von"))
        except KeyError:
            pass

        try:
            zeitraum_bis = clean_form_value(fields.get_formtext(root, "meta_leistungszeitraum_bis"))
        except KeyError:
            pass

        for defn in SECTION_DEFINITIONS:
            try:
                leitziel = clean_form_value(fields.get_formtext(root, f"sections_{defn.index}_leitziel"))
            except KeyError:
                leitziel = ""

            try:
                smarte = clean_form_value(fields.get_formtext(root, f"sections_{defn.index}_smarte_ziele"))
            except KeyError:
                smarte = ""

            try:
                stand = clean_form_value(fields.get_formtext(root, f"sections_{defn.index}_bericht_zielerreichung"))
            except KeyError:
                stand = ""

            try:
                vorschlag = clean_form_value(fields.get_formtext(root, f"sections_{defn.index}_vorschlag_ziele"))
            except KeyError:
                vorschlag = ""

            if leitziel or smarte or stand or vorschlag:
                extracted_goals.append(
                    PlanGoalItem(
                        section_id=defn.id,
                        section_title=defn.title,
                        leitziel=leitziel,
                        smarte_ziele=smarte,
                        stand_zielerreichung=stand,
                        vorschlag_ziele=vorschlag,
                        applies=True,
                    )
                )
    except (KeyError, ValueError, AttributeError) as exc:
        logger.warning("Formularfeld-Extraktion unvollständig: %s", type(exc).__name__)
        errors.append(exc)
    except Exception as exc:
        logger.warning("Fehler bei Formularfeld-Extraktion: %s", type(exc).__name__)
        errors.append(exc)

    # 3. Fallback: python-docx Extraktion für Dokumente ohne Formularfeld-Bookmarks
    if not extracted_goals or not client_name:
        try:
            import docx
            if hasattr(target_source, "seek"):
                target_source.seek(0)
            doc = docx.Document(target_source)
            doc_opened = True

            all_paras = [p.text.strip() for p in doc.paragraphs if p.text.strip()]

            # Versuche Klientennamen aus Text zu extrahieren falls noch nicht gefunden
            if not client_name:
                for p in all_paras:
                    match = re.search(r"(?i)(?:Klient(?:in)?|Name|Betreute Person):\s*([^\n\r,]+)", p)
                    if match:
                        client_name = match.group(1).strip()
                        break

            # Falls immer noch keine Ziele via Bookmarks: Paragraphen & Tabellen analysieren
            if not extracted_goals:
                is_fallback = True
                parser_source = "text_fallback"
                warning = (
                    "⚠️ Vorbericht wurde über Text-Heuristik (Fallback) eingelesen. "
                    "Bitte die erkannten Lebensbereiche vor der weiteren Bearbeitung prüfen."
                )
                # Tabellen scannen
                for table in doc.tables:
                    for row in table.rows:
                        cells = [c.text.strip() for c in row.cells if c.text.strip()]
                        if len(cells) >= 2:
                            joined = " | ".join(cells)
                            if any(kw in joined.lower() for kw in ["ziel", "leit", "smart", "erreich"]):
                                extracted_goals.append(
                                    PlanGoalItem(
                                        section_id=str(len(extracted_goals) + 1),
                                        section_title=cells[0][:40],
                                        leitziel=cells[1],
                                        smarte_ziele=cells[2] if len(cells) > 2 else "",
                                        applies=True,
                                    )
                                )

                # Paragraphen scannen
                if not extracted_goals:
                    curr_section = ""
                    for p in all_paras:
                        if any(p.startswith(prefix) for prefix in ["1.", "2.", "3.", "4.", "Abschnitt", "Bereich"]):
                            curr_section = p
                        elif any(kw in p.lower() for kw in ["leitziel:", "smart:", "ziel:", "stand:"]):
                            extracted_goals.append(
                                PlanGoalItem(
                                    section_id=str(len(extracted_goals) + 1),
                                    section_title=curr_section or "Zielabschnitt",
                                    leitziel=p,
                                    applies=True,
                                )
                            )
        except Exception as exc:
            logger.warning("Fehler bei python-docx Fallback-Extraktion: %s", type(exc).__name__)
            errors.append(exc)

    if not doc_opened:
        if errors:
            raise errors[-1]
        raise ValueError("Dokument konnte nicht geöffnet werden.")

    client_name_from_filename = False
    if not client_name:
        client_name = extract_client_name_from_filename(filename)
        client_name_from_filename = bool(client_name)

    return PlanData(
        filename=filename,
        client_name=client_name,
        client_name_from_filename=client_name_from_filename,
        zeitraum_von=zeitraum_von,
        zeitraum_bis=zeitraum_bis,
        goals=extracted_goals,
        hidden_removed=hidden_removed,
        is_fallback=is_fallback,
        warning=warning,
        parser_source=parser_source,
    )


def parse_plan_text(text: str, filename: str = "vorbericht.txt") -> PlanData:
    """Liest einen Freitext- oder Markdown-Vorbericht (.txt, .md, POST /api/plan) ein und extrahiert Ziele/Lebensbereiche."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    client_name = ""
    zeitraum_von = ""
    zeitraum_bis = ""
    extracted_goals: List[PlanGoalItem] = []

    hamburg_title_map = {d.id: d.title for d in SECTION_DEFINITIONS}
    odw_title_map = {d.id: d.title for d in OP_DE_WISCH_SECTION_DEFINITIONS}
    section_title_map = {**odw_title_map, **hamburg_title_map}
    valid_section_ids = set(section_title_map.keys())

    heading_re = re.compile(
        r"^((?:#{1,4}\s*)?(?:(?:Lebens)?Bereich\s+|Abschnitt\s+)?|#{1,4}\s+)(\d+(?:\.\d+)?)\b([:\.\s–\-]*)(.*)$",
        re.IGNORECASE,
    )
    kw_re = re.compile(
        r"(?i)^(?:[#*\-•]+\s*)?(?:(?:alte|bisherige|aktuelle|neue|vereinbarte)\s+)?(leitziel(?:e)?|teilziel(?:e)?|smart(?:e\s+ziele)?|ziel(?:vereinbarung|erreichung|e)?(?:\s*\d+)?|stand(?:\s+der\s+zielerreichung)?|maßnahmen|massnahmen)\b\s*[:\-]\s*(.+)"
    )

    curr_sec_id = ""
    curr_sec_title = ""

    def _append_goal_field(sec_id: str, sec_title: str, kw_type: str, val: str) -> None:
        existing = next((g for g in extracted_goals if g.section_id == sec_id), None)
        if existing is None:
            existing = PlanGoalItem(section_id=sec_id, section_title=sec_title, applies=True)
            extracted_goals.append(existing)
        if "smart" in kw_type or "teilziel" in kw_type:
            existing.smarte_ziele = f"{existing.smarte_ziele}\n{val}".strip() if existing.smarte_ziele else val
        elif "stand" in kw_type or "erreichung" in kw_type:
            existing.stand_zielerreichung = f"{existing.stand_zielerreichung}\n{val}".strip() if existing.stand_zielerreichung else val
        elif "maßnahme" in kw_type or "massnahme" in kw_type:
            existing.vorschlag_massnahmen = f"{existing.vorschlag_massnahmen}\n{val}".strip() if existing.vorschlag_massnahmen else val
        else:
            existing.leitziel = f"{existing.leitziel}\n{val}".strip() if existing.leitziel else val

    for line in lines:
        if not client_name:
            m_cl = re.search(r"(?i)^(?:Klient(?:in|\*in)?|Name|Betreute Person)\s*:\s*(.+)$", line)
            if m_cl:
                client_name = m_cl.group(1).strip()
                continue

        m_zr = re.search(r"(\d{2}\.\d{2}\.\d{4})\s*(?:bis|-)\s*(\d{2}\.\d{2}\.\d{4})", line, re.IGNORECASE)
        if m_zr and not (zeitraum_von or zeitraum_bis):
            zeitraum_von, zeitraum_bis = m_zr.group(1), m_zr.group(2)

        m_h = heading_re.match(line)
        if m_h and not kw_re.match(line):
            prefix = (m_h.group(1) or "").strip()
            cand_id = m_h.group(2)
            sep = m_h.group(3) or ""
            rest = m_h.group(4).strip(" :–-")

            if cand_id in valid_section_ids:
                # Einstellige IDs (Op-de-Wisch 1..9) nur akzeptieren, wenn explizites Präfix (#, Bereich, Abschnitt)
                # oder kanonischer Bereichstitel vorliegt (verhindert '2 Termine wahrgenommen' als Bereich 2).
                is_valid_heading = False
                if "." in cand_id or prefix:
                    is_valid_heading = True
                elif cand_id in odw_title_map and rest:
                    canon_low = odw_title_map[cand_id].lower()
                    rest_low = rest.lower()
                    if (":" in sep or "." in sep) and (rest_low in canon_low or canon_low in rest_low):
                        is_valid_heading = True

                if is_valid_heading:
                    curr_sec_id = cand_id
                    inline_kw = kw_re.match(rest) if rest else None
                    if inline_kw:
                        curr_sec_title = section_title_map.get(cand_id, f"Bereich {cand_id}")
                        _append_goal_field(
                            curr_sec_id,
                            curr_sec_title,
                            inline_kw.group(1).lower(),
                            inline_kw.group(2).strip() or rest,
                        )
                    else:
                        curr_sec_title = rest or section_title_map.get(cand_id, f"Bereich {cand_id}")
                    continue
            else:
                # Ungültige Bereichs-ID (z. B. 9.9) setzt aktuellen Bereich zurück
                if prefix or "." in cand_id:
                    curr_sec_id = ""
                    curr_sec_title = ""
                    continue

        m_kw = kw_re.search(line)
        if m_kw:
            # Kein stiller Fallback auf '1.1': ohne erkannte Bereichsüberschrift nicht zuordnen
            if not curr_sec_id:
                continue
            kw_type = m_kw.group(1).lower()
            val = m_kw.group(2).strip() or line
            sec_title = curr_sec_title or section_title_map.get(curr_sec_id, f"Bereich {curr_sec_id}")
            _append_goal_field(curr_sec_id, sec_title, kw_type, val)
        elif curr_sec_id:
            existing = next((g for g in extracted_goals if g.section_id == curr_sec_id), None)
            if existing is None:
                existing = PlanGoalItem(section_id=curr_sec_id, section_title=curr_sec_title, leitziel=line, applies=True)
                extracted_goals.append(existing)
            else:
                existing.leitziel = f"{existing.leitziel}\n{line}".strip() if existing.leitziel else line

    client_name_from_filename = False
    if not client_name:
        client_name = extract_client_name_from_filename(filename)
        client_name_from_filename = bool(client_name)

    warning = (
        "⚠️ Vorbericht wurde über Text-Heuristik (Fallback) eingelesen. "
        "Bitte die erkannten Lebensbereiche vor der weiteren Bearbeitung prüfen."
    )

    return PlanData(
        filename=filename,
        client_name=client_name,
        client_name_from_filename=client_name_from_filename,
        zeitraum_von=zeitraum_von,
        zeitraum_bis=zeitraum_bis,
        goals=extracted_goals,
        raw_summary=(text or "").strip(),
        is_fallback=True,
        warning=warning,
        parser_source="text_plan",
    )

