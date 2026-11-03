"""Liest bisherige Pläne und Ziele aus einem früheren, ausgefüllten SOB oder Gesamtplan (ADR-4-Erweiterung, Issue #95).

Bislang blieben `leitziel`/`smarte_ziele` leer (ADR-4: kein alter SOB
eingelesen). Dieser Reader extrahiert sie pro Abschnitt aus einem ausgefüllten
Vorlage-docx, damit der Folgebericht an die bisherigen Ziele anknüpfen kann
(„Ziele aus dem vorherigen Bericht übernehmen").

Erweitert um den UniversalPlanImporter:
- XML-Bookmark-Fastpath für standardisierte Formulare (Hamburger SVB, Op de Wisch).
- Strukturierte Text- und Tabellen-Heuristik für freie Gesamtpläne.
- Pydantic-Datenmodelle (PlanSectionGoal, PreviousPlanData).
- Deterministisches Pydantic-Destillat via to_compact_context() (< 2.500 Zeichen / < 500 Tokens).
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Dict, List, Optional, Tuple, Union

import docx
from pydantic import BaseModel, Field

from sob_generator import docx_fields as fields
from sob_generator.llm.sections import SECTION_DEFINITIONS, SectionDefinition
from sob_generator.llm.sections_op_de_wisch import OP_DE_WISCH_SECTION_DEFINITIONS

logger = logging.getLogger(__name__)

# Whitespace inkl. der Unicode-Platzhalterspaces leerer Formularfelder
# (u00a0 NBSP, u2000-u200b div. Spaces/Zero-Width, u3000 ideographic).
_BLANK = re.compile(r"[\s\u00a0\u2000-\u200b\u3000]")

# Checkbox-Symbole
_CHECKBOX = re.compile(r"[☐☑☒□■\u2610\u2611\u2612]")

# Bekannte Boilerplate-Floskeln aus Formularen
_FORMULAR_BOILERPLATE = [
    re.compile(r"^Vereinbarte\s+Ziele\s+des\s+Gesamtplans[^\n:]*:\s*", re.IGNORECASE | re.MULTILINE),
    re.compile(r"entfällt\s*\(es\s+wurden\s+keine\s+Ziele\s+im\s+Gesamtplan\s+vereinbart\)", re.IGNORECASE),
    re.compile(r"^Bericht\s+zur\s+Zielerreichung[^\n:]*:\s*", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^Vorschlag\s+für\s+aktuelle[^\n:]*:\s*", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^Vorschlag\s+für\s+Maßnahmen[^\n:]*:\s*", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^Realisierung\s+durch\s+Andere[^\n:]*:\s*", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^Antrag\s+auf\s+Haushaltshilfen\s+wird\s+bei\s+der\s+zuständigen\s+Stelle\s+beantragt[^\n:]*:\s*", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\(z\.?\s*[bB]\.[^\)]*\)\s*", re.IGNORECASE),
    re.compile(r"^Auswahl\s*$", re.IGNORECASE | re.MULTILINE),
]


def _clean(value: Optional[str]) -> str:
    """Bereinigt Formularfelder um Unicode-Spaces, Checkboxen und führende/nachfolgende Leerzeichen.

    Gibt "" zurück, wenn der Wert leer ist oder nur aus Platzhalterzeichen besteht.
    """
    if not value:
        return ""
    val = _CHECKBOX.sub(" ", value)
    if not _BLANK.sub("", val):
        return ""
    lines = []
    for line in val.splitlines():
        line = _BLANK.sub(" ", line)
        line = re.sub(r" +", " ", line).strip()
        if line:
            lines.append(line)
    res = "\n".join(lines).strip()
    # Doppelte Bindestriche "- - " bereinigen
    res = re.sub(r"^(?:-\s*)+-\s+", "- ", res)
    if res in {".", "-", "–", "—", "•", "...", "…", ":", ";"}:
        return ""
    return res


def _strip_form_boilerplate(text: str) -> str:
    """Entfernt bekannte Formular-Floskeln und Boilerplate aus extrahierten Texten."""
    if not text:
        return ""
    val = text
    for bp in _FORMULAR_BOILERPLATE:
        val = bp.sub("", val)
    return _clean(val)


def _truncate_text(text: str, max_len: int) -> str:
    """Kürzt Text deterministisch an Satz- oder Wortgrenzen auf max_len Zeichen."""
    text = _clean(text)
    if len(text) <= max_len:
        return text
    cut = text[:max_len].strip()
    last_dot = cut.rfind(". ")
    if last_dot > max_len // 2:
        words = cut[:last_dot].split()
        prefix = words[-1] if words else ""
        if prefix.lower().rstrip(".-") not in {"bzw", "z.b", "u.a", "d.h", "ca", "inkl", "evtl"}:
            return cut[:last_dot + 1]
    last_space = cut.rfind(" ")
    if last_space > max_len // 2:
        words = cut[:last_space].split()
        if words and words[-1].lower().rstrip(".-") in {
            "bzw", "z.b", "u.a", "d.h", "und", "oder", "der", "die", "das", "des", "dem", "den"
        }:
            prev_space = cut[:last_space].rfind(" ")
            if prev_space > max_len // 3:
                last_space = prev_space
        return cut[:last_space].rstrip(".,;:-\u2014\u2013 ") + "…"
    return cut.rstrip(".,;:-\u2014\u2013 ") + "…"


def _extract_client_name_from_path(docx_path: Union[Path, str, None]) -> str:
    """Leitet den Klientennamen deterministisch aus dem Dateinamen ab, falls nicht im Formular hinterlegt."""
    if not docx_path:
        return ""
    if isinstance(docx_path, (str, Path)):
        stem = Path(docx_path).stem
    elif hasattr(docx_path, "stem"):
        stem = getattr(docx_path, "stem")
    else:
        return ""
    if any(k in stem.lower() for k in ["vorlage", "template", "blanko", "fo_svb", "muster_vorlage"]):
        return ""
    cleaned = re.split(r"_(?:SVB|SOB|Gesamtplan|Bericht|\d{4}-\d{2}-\d{2})", stem, flags=re.IGNORECASE)[0]
    parts = [p for p in re.split(r"[_\s]+", cleaned) if p]
    if len(parts) >= 2 and all(p[0].isupper() and p.isalpha() for p in parts[:2]):
        return " ".join(parts[:2])
    return ""


@dataclass(frozen=True)
class PreviousGoals:
    """Bisherige Ziele eines Abschnitts aus dem vorherigen SOB (Rückwärtskompatibel)."""

    leitziel: str
    smarte_ziele: str
    applies: bool  # False = im letzten Gesamtplan keine Ziele vereinbart


class IncompletePlanError(ValueError):
    """Wird geworfen, wenn ein Vorbericht unvollständig oder fehlerhaft (z. B. Duplikate) eingelesen wurde."""

    def __init__(self, found: int = 0, expected: int = 0, message: str = ""):
        self.found = found
        self.expected = expected
        if not message:
            message = f"❌ Vorbericht unvollständig gelesen: {found} von {expected} Bereichen erkannt. Bitte Datei prüfen."
        super().__init__(message)


class PlanSectionGoal(BaseModel):
    """Extrahierte Daten eines Abschnitts aus einem früheren Plan oder SOB."""

    section_id: str  # z. B. "1.1", "1.2", "Wohnen" etc.
    title: str = ""
    leitziel: str = ""
    smarte_ziele: str = ""
    stand_zielerreichung: str = ""  # extrahiert aus z. B. sections_{i}_bericht_zielerreichung
    vorschlag_ziele: str = ""
    vorschlag_massnahmen: str = ""
    realisierung_andere: str = ""
    applies: bool = True


class PreviousPlanData(BaseModel):
    """Gesamtdaten eines vorherigen Plans oder SOB-Berichts."""

    client_name: str = ""
    zeitraum: str = ""
    developments: str = ""
    sections: Dict[str, PlanSectionGoal] = Field(default_factory=dict)
    is_fallback: bool = False
    client_name_from_filename: bool = False

    def to_compact_context(self, max_tokens: int = 500) -> str:
        """Verdichtet Klientendaten, Entwicklungen und aktive Abschnitte auf < 2.500 Zeichen (< 500 Tokens).

        Befreit von bürokratischem Formular-Boilerplate, deterministisch formatiert.
        """
        target_max_chars = min(2400, max(400, int(max_tokens * 4.8)))

        header_parts = []
        if self.client_name:
            header_parts.append(f"Klient: {self.client_name}")
        if self.zeitraum:
            header_parts.append(f"Zeitraum: {self.zeitraum}")
        header_str = " | ".join(header_parts)

        active = [
            s for s in self.sections.values()
            if s.applies and (s.leitziel or s.smarte_ziele or s.stand_zielerreichung or s.vorschlag_ziele)
        ]

        def _render(
            dev_text: str,
            stand_limits: Optional[Dict[str, int]] = None,
            vorschlag_limits: Optional[Dict[str, int]] = None,
        ) -> str:
            lines = []
            if header_str:
                lines.append(header_str)
            if dev_text:
                lines.append(f"Entwicklungen: {dev_text}")
            for s in active:
                parts = [f"[{s.section_id} {s.title}]"]
                if s.leitziel:
                    parts.append(f"Leitziel: {s.leitziel}")
                if s.smarte_ziele:
                    parts.append(f"SMART: {s.smarte_ziele}")
                if s.stand_zielerreichung:
                    stand = s.stand_zielerreichung
                    if stand_limits is not None and s.section_id in stand_limits:
                        lim = stand_limits[s.section_id]
                        stand = _truncate_text(stand, lim) if lim > 0 else ""
                    if stand:
                        parts.append(f"Stand: {stand}")
                if s.vorschlag_ziele:
                    vorschlag = s.vorschlag_ziele
                    if vorschlag_limits is not None and s.section_id in vorschlag_limits:
                        lim = vorschlag_limits[s.section_id]
                        vorschlag = _truncate_text(vorschlag, lim) if lim > 0 else ""
                    if vorschlag:
                        parts.append(f"Vorschlag: {vorschlag}")
                lines.append(" | ".join(parts))
            return "\n".join(lines)

        full = _render(self.developments)
        if len(full) <= target_max_chars:
            return full

        # Dynamische Budgetierung für Stände, Vorschläge und Entwicklungen
        count_stands = sum(1 for s in active if s.stand_zielerreichung)
        count_vorschlag = sum(1 for s in active if s.vorschlag_ziele)
        total_var_slots = count_stands + count_vorschlag

        mock_limits = {s.section_id: 0 for s in active}
        fixed_len = len(_render("", mock_limits, mock_limits))
        available = max(0, target_max_chars - fixed_len)

        dev_budget = 0
        if self.developments:
            dev_budget = min(len(self.developments), max(80, available // max(1, total_var_slots + 2)))

        avail_for_vars = max(0, available - dev_budget)
        slot_budget = max(35, avail_for_vars // max(1, total_var_slots)) if total_var_slots else 0

        dev_truncated = _truncate_text(self.developments, dev_budget) if dev_budget else ""
        stand_limits = {s.section_id: slot_budget for s in active if s.stand_zielerreichung}
        vorschlag_limits = {s.section_id: slot_budget for s in active if s.vorschlag_ziele}

        result = _render(dev_truncated, stand_limits, vorschlag_limits)
        if len(result) > target_max_chars:
            result = _truncate_text(result, target_max_chars)
        return result


_VISIBLE_TEXT_XPATH = (
    ".//w:t[not(ancestor::w:sdtPr) and not(ancestor::w:sdt[w:sdtPr/w:showingPlcHdr]) "
    "and not(ancestor::w:del) and not(ancestor::w:moveFrom) "
    "and not(ancestor::w:r[w:rPr/w:vanish or w:rPr/w:specVanish or w:rPr/w:webHidden])]/text()"
)
_W_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


def _extract_element_text_with_sdt(element) -> str:
    """Extrahiert den sichtbaren Text eines WordprocessingML-Elements inkl. w:sdt (Content Controls), ohne w:sdtPr oder versteckte/gelöschte Stellen."""
    if hasattr(element, "xpath"):
        try:
            ts = element.xpath(_VISIBLE_TEXT_XPATH, namespaces=_W_NS)
        except TypeError:
            ts = element.xpath(_VISIBLE_TEXT_XPATH)
        return "".join(ts).strip()
    return getattr(element, "text", "") or ""


def _extract_cell_text_with_sdt(cell) -> str:
    """Extrahiert den mehrzeiligen Text einer Tabellenzelle inklusive Structured Document Tags (SDT) ohne versteckte Stellen."""
    if hasattr(cell, "_tc"):
        paragraphs = cell._tc.xpath(".//w:p")
        if paragraphs:
            lines = []
            for p in paragraphs:
                try:
                    ts = p.xpath(_VISIBLE_TEXT_XPATH, namespaces=_W_NS)
                except TypeError:
                    ts = p.xpath(_VISIBLE_TEXT_XPATH)
                lines.append("".join(ts))
            return "\n".join(lines)
    return getattr(cell, "text", "") or ""


class UniversalPlanImporter:
    """Universeller Importer für ausgefüllte Gesamtpläne und Sozial- und Verlaufsberichte (DOCX).

    Unterstützt:
    1. Primär: XML-Bookmark-Fastpath für standardisierte Formulare (Hamburger SVB).
    2. Fallback: Strukturierte Text- und Tabellen-Heuristik für freie Pläne und Op de Wisch Vorlagen.
    """

    def extract(
        self,
        docx_path: Union[str, Path, io.BytesIO, bytes, BinaryIO],
        filename: str = "",
    ) -> PreviousPlanData:
        """Extrahiert alle Plandaten (Klient, Zeitraum, Entwicklungen, Abschnitte) aus der DOCX-Datei oder dem In-Memory-Stream."""
        if isinstance(docx_path, (bytes, bytearray)):
            source = io.BytesIO(docx_path)
            fn = filename or "vorbericht.docx"
        elif hasattr(docx_path, "read"):
            source = docx_path
            fn = filename or getattr(docx_path, "name", "vorbericht.docx")
        else:
            path = Path(docx_path)
            if not path.is_file():
                raise FileNotFoundError(f"Dokument nicht gefunden: {path}")
            source = path
            fn = path.name

        # 1. Versuche Fastpath über XML-Bookmarks (Hamburger Vorlage)
        if hasattr(source, "seek"):
            source.seek(0)
        plan = self._extract_bookmarks(source, filename=fn)
        if plan is not None and plan.sections:
            return plan

        # 2. Versuche Fastpath für Op-de-Wisch-Vorlage (SDT + 9 Bereichstabellen)
        if hasattr(source, "seek"):
            source.seek(0)
        odw_plan = self._extract_op_de_wisch(source, filename=fn)
        if odw_plan is not None and odw_plan.sections:
            return odw_plan

        # 3. Fallback: Strukturierte Tabellen- und Text-Heuristik
        if hasattr(source, "seek"):
            source.seek(0)
        return self._extract_fallback(source, filename=fn)

    def _extract_bookmarks(
        self,
        path: Union[Path, io.BytesIO, BinaryIO],
        filename: str = "",
    ) -> Optional[PreviousPlanData]:
        """Extrahiert Plandaten über XML-Formulartext-Bookmarks."""
        try:
            if hasattr(path, "seek"):
                path.seek(0)
            root = fields.load_document_tree(path)
        except Exception:
            return None

        import xml.etree.ElementTree as ET

        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        bms = {el.attrib.get(f"{{{ns['w']}}}name") for el in root.findall(".//w:bookmarkStart", ns)}
        bms = {b for b in bms if b}

        has_section_bms = any(b.startswith("sections_") for b in bms)
        if not has_section_bms:
            return None

        # Klientenname
        client_name = ""
        client_name_from_filename = False
        for name_bm in ["client_name_vorname", "client_name", "client_vorname_nachname", "wrdTN1"]:
            if name_bm in bms:
                try:
                    val = _clean(fields.get_formtext(root, name_bm))
                    if val:
                        client_name = val
                        break
                except Exception:
                    pass
        if not client_name:
            fallback_source = path if isinstance(path, (str, Path)) else Path(filename or "vorbericht.docx")
            client_name = _extract_client_name_from_path(fallback_source)
            client_name_from_filename = bool(client_name)

        # Zeitraum
        zeitraum = ""
        von, bis = "", ""
        if "meta_leistungszeitraum_von" in bms:
            try:
                von = _clean(fields.get_formtext(root, "meta_leistungszeitraum_von"))
            except Exception:
                pass
        if "meta_leistungszeitraum_bis" in bms:
            try:
                bis = _clean(fields.get_formtext(root, "meta_leistungszeitraum_bis"))
            except Exception:
                pass
        if von and bis:
            zeitraum = f"{von} bis {bis}"
        elif von or bis:
            zeitraum = von or bis

        # Entwicklungen
        developments = ""
        if "developments" in bms:
            try:
                developments = _clean(fields.get_formtext(root, "developments"))
            except Exception:
                pass

        # Abschnitte
        sections: Dict[str, PlanSectionGoal] = {}
        for defn in SECTION_DEFINITIONS:
            lz, sz, bz, vz = "", "", "", ""
            vm, ra = "", ""
            if f"sections_{defn.index}_leitziel" in bms:
                try:
                    lz = _clean(fields.get_formtext(root, f"sections_{defn.index}_leitziel"))
                except Exception:
                    pass
            if f"sections_{defn.index}_smarte_ziele" in bms:
                try:
                    sz = _clean(fields.get_formtext(root, f"sections_{defn.index}_smarte_ziele"))
                except Exception:
                    pass
            if f"sections_{defn.index}_bericht_zielerreichung" in bms:
                try:
                    bz = _clean(fields.get_formtext(root, f"sections_{defn.index}_bericht_zielerreichung"))
                except Exception:
                    pass
            if f"sections_{defn.index}_vorschlag_ziele" in bms:
                try:
                    vz = _clean(fields.get_formtext(root, f"sections_{defn.index}_vorschlag_ziele"))
                except Exception:
                    pass
            if f"sections_{defn.index}_vorschlag_massnahmen" in bms:
                try:
                    vm = _clean(fields.get_formtext(root, f"sections_{defn.index}_vorschlag_massnahmen"))
                except Exception:
                    pass
            if f"sections_{defn.index}_realisierung_andere" in bms:
                try:
                    ra = _clean(fields.get_formtext(root, f"sections_{defn.index}_realisierung_andere"))
                except Exception:
                    pass

            is_na = False
            if f"sections_{defn.index}_previous_goals_apply" in bms:
                try:
                    is_na = fields.get_checkbox(root, f"sections_{defn.index}_previous_goals_apply")
                except Exception:
                    pass

            has_content = bool(lz or sz or bz or vz or vm)
            applies = has_content and not (is_na and not (lz or sz))

            sections[defn.id] = PlanSectionGoal(
                section_id=defn.id,
                title=defn.title,
                leitziel=lz,
                smarte_ziele=sz,
                stand_zielerreichung=bz,
                vorschlag_ziele=vz,
                vorschlag_massnahmen=vm,
                realisierung_andere=ra,
                applies=applies,
            )

        return PreviousPlanData(
            client_name=client_name,
            zeitraum=zeitraum,
            developments=developments,
            sections=sections,
            is_fallback=False,
            client_name_from_filename=client_name_from_filename,
        )

    @staticmethod
    def _parse_table_section(
        table,
        sec_id: str,
        sec_title: str,
        smart_pattern: str,
    ) -> PlanSectionGoal:
        """Extrahiert einen Lebensbereich aus einer strukturierten Bereichstabelle."""
        lz, sz, bz, vz, vm, ra = "", "", "", "", "", ""
        is_entfaellt = False
        for row in table.rows:
            seen_row_tcs = set()
            row_cells = []
            for c in row.cells:
                if c._tc not in seen_row_tcs:
                    seen_row_tcs.add(c._tc)
                    row_cells.append(c)

            row_cell_texts = [_extract_cell_text_with_sdt(c) for c in row_cells]
            row_text = "\n".join(row_cell_texts)

            if "entfällt" in row_text.lower():
                has_w14 = (
                    bool(
                        row._tr.xpath(
                            ".//*[local-name()='checkbox']/*[local-name()='checked'][@*[local-name()='val']='1']"
                        )
                    )
                    if hasattr(row, "_tr")
                    else False
                )
                has_box_char = bool(re.search(r"[☒☑■]\s*entfällt", row_text, re.IGNORECASE))
                if has_w14 or has_box_char:
                    is_entfaellt = True

            if "Leitziel:" in row_text:
                m_lz = re.search(
                    r"Leitziel\s*:\s*(.*?)(?=(?:" + smart_pattern + r"|$))",
                    row_text,
                    re.IGNORECASE | re.DOTALL,
                )
                if m_lz:
                    lz = _clean(_strip_form_boilerplate(m_lz.group(1)))
                m_sz = re.search(smart_pattern + r"\s*(.*)", row_text, re.IGNORECASE | re.DOTALL)
                if m_sz:
                    sz = _clean(_strip_form_boilerplate(m_sz.group(1)))
            elif "Bericht zur Zielerreichung" in row_text:
                parts = re.split(r"Bericht\s+zur\s+Zielerreichung[^\n:]*:\s*", row_text, flags=re.IGNORECASE)
                if len(parts) > 1:
                    bz = _clean(_strip_form_boilerplate(parts[1]))
            elif "Vorschlag für aktuelle" in row_text or "Vorschlag für Maßnahmen" in row_text:
                for c_txt in row_cell_texts:
                    if re.search(r"Vorschlag\s+für\s+aktuelle", c_txt, re.IGNORECASE):
                        m_vz = re.search(
                            r"Vorschlag\s+für\s+aktuelle[^\n:]*:\s*(.*?)(?=(?:Vorschlag\s+für\s+Maßnahmen|$))",
                            c_txt,
                            re.IGNORECASE | re.DOTALL,
                        )
                        if m_vz:
                            vz = _clean(_strip_form_boilerplate(m_vz.group(1)))
                        else:
                            parts_v = re.split(
                                r"Vorschlag\s+für\s+aktuelle[^\n:]*:\s*", c_txt, flags=re.IGNORECASE
                            )
                            if len(parts_v) > 1:
                                vz = _clean(_strip_form_boilerplate(parts_v[1]))
                    if re.search(r"Vorschlag\s+für\s+Maßnahmen", c_txt, re.IGNORECASE):
                        parts_m = re.split(
                            r"Vorschlag\s+für\s+Maßnahmen[^\n:]*:\s*", c_txt, flags=re.IGNORECASE
                        )
                        if len(parts_m) > 1:
                            vm = _clean(_strip_form_boilerplate(parts_m[1]))
            elif "Realisierung durch Andere" in row_text or "Realisierung durch andere" in row_text:
                m_ra = re.search(r"Realisierung\s+durch\s+Andere[^\n]*\n(.*)", row_text, re.IGNORECASE | re.DOTALL)
                if m_ra:
                    ra = _clean(_strip_form_boilerplate(m_ra.group(1)))
                else:
                    parts_ra = re.split(r"Realisierung\s+durch\s+Andere[^\n:]*:\s*", row_text, flags=re.IGNORECASE)
                    if len(parts_ra) > 1:
                        ra = _clean(_strip_form_boilerplate(parts_ra[1]))

        has_content = bool(lz or sz or bz or vz or vm)
        applies = has_content and not (is_entfaellt and not (lz or sz))
        return PlanSectionGoal(
            section_id=sec_id,
            title=sec_title,
            leitziel=lz,
            smarte_ziele=sz,
            stand_zielerreichung=bz,
            vorschlag_ziele=vz,
            vorschlag_massnahmen=vm,
            realisierung_andere=ra,
            applies=applies,
        )

    def _extract_op_de_wisch(
        self,
        path: Union[Path, io.BytesIO, BinaryIO],
        filename: str = "",
    ) -> Optional[PreviousPlanData]:
        """Primärer Fastpath für die Op-de-Wisch-Vorlage (SDT-Kopftabelle + 9 Bereichstabellen)."""
        fallback_source = path if isinstance(path, (str, Path)) else Path(filename or "vorbericht.docx")
        try:
            if hasattr(path, "seek"):
                path.seek(0)
            doc = docx.Document(path)
        except Exception:
            return None

        if len(doc.tables) < 10:
            return None

        t0 = doc.tables[0]
        if not t0._tbl.xpath(".//w:sdt"):
            return None

        t0_text = "\n".join(_extract_cell_text_with_sdt(c) for r in t0.rows for c in r.cells).lower()
        if "op de wisch" not in t0_text and "bericht für" not in t0_text:
            return None

        # 1. Klientenname aus Tabelle 0, Zeile 0, Zelle 2 (SDT[0])
        client_name = ""
        client_name_from_filename = False
        placeholders = ("name, vorname", "klient*in", "klientin", "geburtsdatum", "aktenzeichen")
        if len(t0.rows) > 0 and len(t0.rows[0].cells) > 2:
            c0_2 = t0.rows[0].cells[2]._tc
            sdts_c0_2 = c0_2.xpath(".//w:sdt")
            if sdts_c0_2:
                raw_sdt = _clean(_extract_element_text_with_sdt(sdts_c0_2[0]))
                if raw_sdt and not any(ph in raw_sdt.lower() for ph in placeholders):
                    client_name = raw_sdt
            if not client_name:
                cell_txt = _extract_cell_text_with_sdt(t0.rows[0].cells[2])
                m_c = re.search(
                    r"Bericht\s+für\s*(?:\(Name,\s*Vorname\))?\s*:[ \t\u00a0]*([^\n\t\r]+)",
                    cell_txt,
                    re.IGNORECASE,
                )
                if m_c:
                    raw_c = _clean(m_c.group(1))
                    if raw_c and not any(ph in raw_c.lower() for ph in placeholders):
                        client_name = raw_c

        if not client_name:
            client_name = _extract_client_name_from_path(fallback_source)
            client_name_from_filename = bool(client_name)

        # 2. Berichtszeitraum aus Tabelle 0, Zeile 3, Zelle 0 (SDT[1])
        zeitraum = ""
        if len(t0.rows) > 3 and len(t0.rows[3].cells) > 0:
            c3_0 = t0.rows[3].cells[0]._tc
            sdts_c3_0 = c3_0.xpath(".//w:sdt")
            raw_zr = ""
            if len(sdts_c3_0) > 1:
                raw_zr = _clean(_extract_element_text_with_sdt(sdts_c3_0[1]))
            if not raw_zr:
                raw_zr = _extract_cell_text_with_sdt(t0.rows[3].cells[0])
            m_zr = re.search(
                r"([0-9]{2}\.[0-9]{2}\.[0-9]{4})\s*(?:bis|-)\s*([0-9]{2}\.[0-9]{2}\.[0-9]{4})",
                raw_zr,
                re.IGNORECASE,
            )
            if m_zr:
                zeitraum = f"{m_zr.group(1)} bis {m_zr.group(2)}"

        # 3. Wesentliche Entwicklungen aus Tabelle 0, Zeile 4, Zelle 0
        developments = ""
        if len(t0.rows) > 4 and len(t0.rows[4].cells) > 0:
            c4_txt = _extract_cell_text_with_sdt(t0.rows[4].cells[0])
            c4_clean = re.sub(
                r"^\s*Wesentliche\s+(?:aktuelle\s+)?Entwicklungen\s+im\s+Berichtszeitraum\s*:\s*",
                "",
                c4_txt,
                flags=re.IGNORECASE,
            )
            developments = _clean(c4_clean)

        # 4. 9 Lebensbereiche aus Tabellen 1..N
        allowlist_map = {d.id: d for d in OP_DE_WISCH_SECTION_DEFINITIONS}
        main_id_re = re.compile(r"^(\d+(?:\.\d+)?)\.?\s*(.*)$")
        smart_pattern = r"(?:S\.?M\.?A\.?R\.?T\.?|Smarte?)\s*Ziele?\s*:"

        sections: Dict[str, PlanSectionGoal] = {}
        detected_headings: List[str] = []

        for table in doc.tables[1:]:
            if not table.rows or not table.rows[0].cells:
                continue
            head_txt = _extract_cell_text_with_sdt(table.cell(0, 0)).strip()
            m_main = main_id_re.match(head_txt)
            if not m_main:
                continue
            cand_id = m_main.group(1)
            if cand_id not in allowlist_map:
                continue
            detected_headings.append(cand_id)
            if cand_id in sections:
                raise IncompletePlanError(found=len(sections), expected=len(detected_headings))
            sec_title = m_main.group(2).strip() or allowlist_map[cand_id].title
            sections[cand_id] = self._parse_table_section(table, cand_id, sec_title, smart_pattern)

        if set(sections.keys()) != set(allowlist_map.keys()):
            return None

        return PreviousPlanData(
            client_name=client_name,
            zeitraum=zeitraum,
            developments=developments,
            sections=sections,
            is_fallback=False,
            client_name_from_filename=client_name_from_filename,
        )

    def _extract_fallback(
        self,
        path: Union[Path, io.BytesIO, BinaryIO],
        filename: str = "",
    ) -> PreviousPlanData:
        """Extrahiert Plandaten über Tabellen- und Textheuristik für freie oder Op de Wisch DOCX."""
        fallback_source = path if isinstance(path, (str, Path)) else Path(filename or "vorbericht.docx")
        try:
            if hasattr(path, "seek"):
                path.seek(0)
            doc = docx.Document(path)
        except Exception:
            fn_client = _extract_client_name_from_path(fallback_source)
            return PreviousPlanData(
                client_name=fn_client,
                is_fallback=True,
                client_name_from_filename=bool(fn_client),
            )

        sections: Dict[str, PlanSectionGoal] = {}
        client_name = ""
        client_name_from_filename = False
        zeitraum = ""
        developments = ""

        # 1. Metadaten aus Paragraphen & Tabellen (inkl. SDT/Content Controls)
        seen_tcs = set()
        all_texts = []
        for p in doc.paragraphs:
            txt = _extract_element_text_with_sdt(p._p) if hasattr(p, "_p") else p.text
            if txt:
                all_texts.append(txt)
        for t in doc.tables:
            for row in t.rows:
                for c in row.cells:
                    if c._tc not in seen_tcs:
                        seen_tcs.add(c._tc)
                        txt = _extract_cell_text_with_sdt(c)
                        if txt:
                            all_texts.append(txt)
        combined_text = "\n".join(all_texts)

        # Klientenname
        m_client = re.search(
            r"Bericht\s+für\s*(?:\(Name,\s*Vorname\))?\s*:[ \t\u00a0]*([^\n\t\r]+)",
            combined_text,
            re.IGNORECASE,
        )
        if m_client:
            raw_client = _clean(m_client.group(1))
            if raw_client and not any(
                ph in raw_client.lower()
                for ph in ["name, vorname", "klient*in", "klientin", "geburtsdatum", "aktenzeichen"]
            ):
                client_name = raw_client
        if not client_name:
            client_name = _extract_client_name_from_path(fallback_source)
            client_name_from_filename = bool(client_name)

        # Zeitraum
        m_zr = re.search(
            r"(?:Leistungszeitraum|Berichtszeitraum|Leistung\s+wurde\s+erbracht)\b[^\n\d]*([0-9]{2}\.[0-9]{2}\.[0-9]{4})\s*(?:bis|-)\s*([0-9]{2}\.[0-9]{2}\.[0-9]{4})",
            combined_text,
            re.IGNORECASE,
        )
        if m_zr:
            zeitraum = f"{m_zr.group(1)} bis {m_zr.group(2)}"

        # Entwicklungen
        m_dev = re.search(
            r"Wesentliche\s+(?:aktuelle\s+)?Entwicklungen\s+im\s+Berichtszeitraum\s*:\s*(.*?)(?=\n\s*(?:Name/|Leistungsanbieter|1\.)|$)",
            combined_text,
            re.IGNORECASE | re.DOTALL,
        )
        if m_dev:
            developments = _clean(m_dev.group(1))

        # Vorlagen-Erkennung (Hamburg vs. Op de Wisch)
        is_odw = bool(
            "op de wisch" in combined_text.lower()
            or "op-de-wisch" in fallback_source.name.lower()
            or "op_de_wisch" in fallback_source.name.lower()
            or "fo_svb" in fallback_source.name.lower()
        )
        if not is_odw:
            has_sub_id = False
            has_odw_high_id = False
            for t in doc.tables:
                for r_idx in range(min(2, len(t.rows))):
                    if len(t.rows[r_idx].cells) > 0:
                        cell_txt = _extract_cell_text_with_sdt(t.cell(r_idx, 0)).strip()
                        if re.match(r"^\d\.\d\b", cell_txt):
                            has_sub_id = True
                        elif re.match(r"^[4-9]\b", cell_txt):
                            has_odw_high_id = True
            if has_odw_high_id and not has_sub_id:
                is_odw = True

        allowlist_defs = OP_DE_WISCH_SECTION_DEFINITIONS if is_odw else SECTION_DEFINITIONS
        allowlist_map = {d.id: d for d in allowlist_defs}

        sub_id_re = re.compile(r"^(\d\.\d)\.?\s*(.*)$")
        main_id_re = re.compile(r"^(\d+(?:\.\d+)?)\.?\s*(.*)$")
        smart_pattern = r"(?:S\.?M\.?A\.?R\.?T\.?|Smarte?)\s*Ziele?\s*:"

        detected_headings: List[str] = []
        unknown_ids_count = 0

        # 2. Tabellen nach Abschnitten parsen
        for table in doc.tables:
            candidate_cells = []
            for row_idx in range(min(3, len(table.rows))):
                if len(table.rows[row_idx].cells) > 0:
                    candidate_cells.append(_extract_cell_text_with_sdt(table.cell(row_idx, 0)).strip())

            # Zuerst Sub-ID (\d\.\d) in candidate cells suchen
            candidate_id = ""
            candidate_title = ""
            for text in candidate_cells:
                m_sub = sub_id_re.match(text)
                if m_sub:
                    candidate_id = m_sub.group(1)
                    candidate_title = m_sub.group(2).strip()
                    break

            # Falls keine Sub-ID: Hauptnummer suchen
            if not candidate_id:
                for text in candidate_cells:
                    m_main = main_id_re.match(text)
                    if m_main:
                        candidate_id = m_main.group(1)
                        candidate_title = m_main.group(2).strip()
                        break

            if not candidate_id:
                continue

            # Allowlist-Prüfung
            if candidate_id not in allowlist_map:
                unknown_ids_count += 1
                continue

            detected_headings.append(candidate_id)

            # Duplikat-Prüfung: Duplikate überschreiben nie
            if candidate_id in sections:
                raise IncompletePlanError(found=len(sections), expected=len(detected_headings))

            sec_id = candidate_id
            sec_title = candidate_title or allowlist_map[candidate_id].title
            sections[sec_id] = self._parse_table_section(table, sec_id, sec_title, smart_pattern)

        # 3. Falls keine Tabellenabschnitte gefunden: Paragraph-Scan
        if not sections:
            current_sec_id = None
            current_title = ""
            current_lines = []
            for p in doc.paragraphs:
                txt = p.text.strip()
                m_sub = sub_id_re.match(txt)
                m_p = m_sub or main_id_re.match(txt)
                if m_p:
                    cand_id = m_p.group(1)
                    if cand_id not in allowlist_map:
                        unknown_ids_count += 1
                        continue
                    detected_headings.append(cand_id)
                    if cand_id in sections:
                        raise IncompletePlanError(found=len(sections), expected=len(detected_headings))

                    if current_sec_id:
                        sec_text = "\n".join(current_lines)
                        lz, sz, bz, vz = self._parse_section_text(sec_text, smart_pattern)
                        sections[current_sec_id] = PlanSectionGoal(
                            section_id=current_sec_id,
                            title=current_title,
                            leitziel=lz,
                            smarte_ziele=sz,
                            stand_zielerreichung=bz,
                            vorschlag_ziele=vz,
                            applies=bool(lz or sz or bz or vz),
                        )
                    current_sec_id = cand_id
                    current_title = m_p.group(2).strip() or allowlist_map[cand_id].title
                    current_lines = []
                else:
                    if current_sec_id:
                        current_lines.append(txt)

            if current_sec_id:
                sec_text = "\n".join(current_lines)
                lz, sz, bz, vz = self._parse_section_text(sec_text, smart_pattern)
                sections[current_sec_id] = PlanSectionGoal(
                    section_id=current_sec_id,
                    title=current_title,
                    leitziel=lz,
                    smarte_ziele=sz,
                    stand_zielerreichung=bz,
                    vorschlag_ziele=vz,
                    applies=bool(lz or sz or bz or vz),
                )

        if len(detected_headings) > len(sections):
            raise IncompletePlanError(found=len(sections), expected=len(detected_headings))

        logger.info(
            "UniversalPlanImporter: %d Abschnitte extrahiert (erwartet: %d, unbekannte IDs: %d)",
            len(sections),
            len(detected_headings),
            unknown_ids_count,
        )

        return PreviousPlanData(
            client_name=client_name,
            zeitraum=zeitraum,
            developments=developments,
            sections=sections,
            is_fallback=True,
            client_name_from_filename=client_name_from_filename,
        )

    def _parse_section_text(self, text: str, smart_pattern: str) -> Tuple[str, str, str, str]:
        """Hilfsfunktion zum Parsen eines Fließtext-Abschnitts."""
        lz, sz, bz, vz = "", "", "", ""
        if "Leitziel:" in text:
            m_lz = re.search(r"Leitziel\s*:\s*(.*?)(?=(?:" + smart_pattern + r"|$))", text, re.IGNORECASE | re.DOTALL)
            if m_lz:
                lz = _clean(_strip_form_boilerplate(m_lz.group(1)))
        m_sz = re.search(smart_pattern + r"\s*(.*?)(?=(?:Bericht\s+zur|Vorschlag|$))", text, re.IGNORECASE | re.DOTALL)
        if m_sz:
            sz = _clean(_strip_form_boilerplate(m_sz.group(1)))
        if "Bericht zur Zielerreichung" in text:
            m_bz = re.search(r"Bericht\s+zur\s+Zielerreichung[^\n:]*:\s*(.*?)(?=(?:Vorschlag|$))", text, re.IGNORECASE | re.DOTALL)
            if m_bz:
                bz = _clean(_strip_form_boilerplate(m_bz.group(1)))
        if "Vorschlag für aktuelle" in text:
            m_vz = re.search(r"Vorschlag\s+für\s+aktuelle[^\n:]*:\s*(.*)", text, re.IGNORECASE | re.DOTALL)
            if m_vz:
                vz = _clean(_strip_form_boilerplate(m_vz.group(1)))
        return lz, sz, bz, vz


def read_previous_plan(
    docx_path: Union[str, Path, io.BytesIO, bytes, BinaryIO],
    filename: str = "",
) -> PreviousPlanData:
    """Liest Gesamtdaten (Klient, Zeitraum, Entwicklungen, Abschnitte) aus einem DOCX-Vorbericht ein."""
    return UniversalPlanImporter().extract(docx_path, filename=filename)


def read_previous_goals(
    docx_path: Union[str, Path, io.BytesIO, bytes, BinaryIO],
    filename: str = "",
) -> Dict[str, PreviousGoals]:
    """Pro Abschnitts-ID (z.B. '1.1') die Ziele aus einem ausgefüllten SOB.

    Rückwärtskompatible Schnittstelle: liefert Dict[str, PreviousGoals].
    """
    plan = read_previous_plan(docx_path, filename=filename)
    goals: Dict[str, PreviousGoals] = {}

    # Für die Standard-Abschnitte der Hamburger Vorlage garantieren wir alle 9 IDs
    for defn in SECTION_DEFINITIONS:
        if defn.id in plan.sections:
            sec = plan.sections[defn.id]
            goals[defn.id] = PreviousGoals(
                leitziel=sec.leitziel,
                smarte_ziele=sec.smarte_ziele,
                applies=sec.applies and bool(sec.leitziel or sec.smarte_ziele),
            )
        else:
            goals[defn.id] = PreviousGoals(leitziel="", smarte_ziele="", applies=False)

    # Zusätzliche Abschnitte (z. B. aus freien Plänen) beifügen
    for sec_id, sec in plan.sections.items():
        if sec_id not in goals:
            goals[sec_id] = PreviousGoals(
                leitziel=sec.leitziel,
                smarte_ziele=sec.smarte_ziele,
                applies=sec.applies and bool(sec.leitziel or sec.smarte_ziele),
            )
    return goals


HAMBURG_TO_OP_DE_WISCH_GOALS = {
    "1.1": "4",  # Wohnsituation -> 4 Wohnen
    "1.2": "2",  # Alltägliche Lebensführung -> 2 Alltagsgestaltung / Tagesstruktur
    "1.3": "1",  # Basisversorgung -> 1 Psychische Verfassung / Selbstsorge
    "1.4": "1",  # Emotionale Entwicklung -> 1 Psychische Verfassung / Selbstsorge
    "1.5": "3",  # Gesundheitsförderung -> 3 Gesundheit
    "2.1": "6",  # Soziale Beziehungen -> 6 Sozialer Lebensraum
    "2.2": "6",  # Kulturelles Leben -> 6 Sozialer Lebensraum
    "2.3": "5",  # Kommunikation / Admin -> 5 Finanzen / Institutionen
    "3": "7",    # Arbeit Hamburg -> 7 Arbeit / Beschäftigung
}


def _merge_source_goals(sources: list[tuple[SectionDefinition, PreviousGoals]]) -> PreviousGoals:
    """Erhält bei zusammengeführten Abschnitten die Zuordnung zu den Quellen."""
    if not sources:
        return PreviousGoals(leitziel="", smarte_ziele="", applies=False)
    if len(sources) == 1:
        return sources[0][1]

    leitziele: list[str] = []
    smarte_ziele: list[str] = []
    for section, goals in sources:
        source_label = f"Hamburg {section.id} – {section.title}"
        if leitziel := _clean(goals.leitziel):
            leitziele.append(f"{source_label}:\n{leitziel}")
        if smart := _clean(goals.smarte_ziele):
            smarte_ziele.append(f"{source_label}:\n{smart}")
    return PreviousGoals(
        leitziel="\n\n".join(leitziele),
        smarte_ziele="\n\n".join(smarte_ziele),
        applies=True,
    )


def remap_previous_goals(
    previous_goals: Dict[str, PreviousGoals],
    target_sections: list[SectionDefinition],
) -> Dict[str, PreviousGoals]:
    """Passt bisherige Ziele an die Ziel-Abschnitts-IDs an.

    Wichtig, wenn ein Vorbericht aus der städtischen Vorlage (IDs '1.1'..'3')
    in die Op de Wisch-Vorlage (IDs '1'..'9') übernommen wird, damit Arbeitsziele
    nicht fälschlich unter '3 Gesundheit' landen.
    """
    if not previous_goals:
        return previous_goals

    target_ids = {s.id for s in target_sections}
    # Wenn alle Ziel-IDs 1..9 sind (Op de Wisch), aber Hamburg-IDs vorliegen
    if target_ids == {"1", "2", "3", "4", "5", "6", "7", "8", "9"}:
        has_dot_ids = any("." in k for k in previous_goals.keys())
        if has_dot_ids:
            sources_by_target: dict[str, list[tuple[SectionDefinition, PreviousGoals]]] = {
                section.id: [] for section in target_sections
            }
            for source_section in SECTION_DEFINITIONS:
                goals = previous_goals.get(source_section.id)
                if goals and goals.applies and (_clean(goals.leitziel) or _clean(goals.smarte_ziele)):
                    target_id = HAMBURG_TO_OP_DE_WISCH_GOALS[source_section.id]
                    sources_by_target[target_id].append((source_section, goals))
            return {
                target_id: _merge_source_goals(sources)
                for target_id, sources in sources_by_target.items()
            }

    return previous_goals
