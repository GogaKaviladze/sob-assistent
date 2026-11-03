"""OpDeWischDocxRenderer (Issue #63): ReportDraft → FO_SVB_Berichtsvorlage_2026.docx.

Befüllt die Word-Berichtsvorlage von Op de Wisch e.V. über Tabellenzellen,
Absätze und Content Controls (SDT) im Berichtskopf und den 9 Lebensbereichen.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import BinaryIO, List, Optional, Union

import docx
from docx.text.paragraph import Paragraph
from lxml import etree

from sob_generator.models import ClientData, ReportDraft, ReportMeta, ReportSection
from sob_generator.renderer import (
    NO_ENTRIES_HINT,
    REVIEW_MARKER,
    _bericht_text,
    _name_vorname,
    _split_items,
    _with_marker,
)


_INLINE_BULLET = re.compile(r"\s+•\s+")


def _bullet_items(value: str) -> List[str]:
    """Wie `_split_items`, trennt zusätzlich einzeilige Aufzählungen „a • b • c“."""
    items: List[str] = []
    for item in _split_items(value or ""):
        items.extend(part.strip() for part in _INLINE_BULLET.split(item) if part.strip())
    return items


class OpDeWischDocxRenderer:
    """Rendert einen ReportDraft in die Op de Wisch-Vorlage FO_SVB_Berichtsvorlage_2026.docx."""

    def __init__(self, template_path: Union[Path, str]) -> None:
        self._template_path = Path(template_path)

    def render(
        self, draft: ReportDraft, output_path: Union[Path, str, BinaryIO]
    ) -> Union[Path, BinaryIO]:
        doc = docx.Document(str(self._template_path))
        full_ns = dict(doc.element.nsmap)
        full_ns["w14"] = "http://schemas.microsoft.com/office/word/2010/wordml"
        w_ns = full_ns.get("w", "http://schemas.openxmlformats.org/wordprocessingml/2006/main")
        w14_ns = full_ns["w14"]

        self._render_header(doc, draft, full_ns, w_ns, w14_ns)
        self._render_sections(doc, draft, full_ns, w_ns, w14_ns)

        if hasattr(output_path, "write"):
            doc.save(output_path)
            return output_path

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(output_path))
        return output_path

    # -----------------------------------------------------------------------
    # Header / Kopfdaten (Tabelle 0)
    # -----------------------------------------------------------------------

    def _render_header(
        self,
        doc: docx.Document,
        draft: ReportDraft,
        full_ns: dict,
        w_ns: str,
        w14_ns: str,
    ) -> None:
        if not doc.tables:
            return
        t0 = doc.tables[0]

        # 1. Klientendaten (Row 0, Cell 2)
        if len(t0.rows) > 0 and len(t0.rows[0].cells) > 2:
            c0_2 = t0.rows[0].cells[2]._tc
            sdts_c0_2 = c0_2.xpath(".//w:sdt")
            if len(sdts_c0_2) > 0:
                self._set_sdt_text(sdts_c0_2[0], _name_vorname(draft.client), full_ns, w_ns)
            if len(sdts_c0_2) > 1:
                self._set_sdt_text(sdts_c0_2[1], draft.client.geburtsdatum, full_ns, w_ns)
            street, plz_city = self._split_address(draft.client.anschrift)
            if len(sdts_c0_2) > 2 and street:
                self._set_sdt_text(sdts_c0_2[2], street, full_ns, w_ns)
            if len(sdts_c0_2) > 3 and plz_city:
                self._set_sdt_text(sdts_c0_2[3], plz_city, full_ns, w_ns)

        # 2. Gesetzliche Betreuung (Row 1, Cell 0)
        if len(t0.rows) > 1:
            c1_0 = t0.rows[1].cells[0]._tc
            sdts_c1_0 = c1_0.xpath(".//w:sdt")
            if len(sdts_c1_0) > 1:
                # Checkbox Nein (sdt 0) und Ja (sdt 1)
                self._set_sdt_checkbox(sdts_c1_0[0], not draft.client.gesetzliche_betreuung, full_ns, w14_ns)
                self._set_sdt_checkbox(sdts_c1_0[1], bool(draft.client.gesetzliche_betreuung), full_ns, w14_ns)

        # 3. GdB und Pflegegrad (Row 2, Cell 0)
        if len(t0.rows) > 2:
            c2_0 = t0.rows[2].cells[0]._tc
            sdts_c2_0 = c2_0.xpath(".//w:sdt")
            if len(sdts_c2_0) > 1:
                has_gdb = bool(draft.client.gdb and draft.client.gdb.strip())
                self._set_sdt_text(sdts_c2_0[0], draft.client.gdb, full_ns, w_ns)
                self._set_sdt_checkbox(sdts_c2_0[1], not has_gdb, full_ns, w14_ns)
            if len(sdts_c2_0) > 3:
                has_pflegegrad = bool(draft.client.pflegegrad and draft.client.pflegegrad.strip())
                self._set_sdt_text(sdts_c2_0[2], draft.client.pflegegrad, full_ns, w_ns)
                self._set_sdt_checkbox(sdts_c2_0[3], not has_pflegegrad, full_ns, w14_ns)

        # 4. Leistungsart, Berichtszeitraum, Abwesenheitszeiten (Row 3, Cell 0)
        if len(t0.rows) > 3:
            c3_0 = t0.rows[3].cells[0]._tc
            sdts_c3_0 = c3_0.xpath(".//w:sdt")
            if len(sdts_c3_0) > 0:
                self._set_sdt_text(sdts_c3_0[0], draft.client.leistungsart, full_ns, w_ns)
            if len(sdts_c3_0) > 1:
                zeitraum = self._format_zeitraum(draft.meta)
                if zeitraum:
                    self._set_sdt_text(sdts_c3_0[1], zeitraum, full_ns, w_ns)
            if len(sdts_c3_0) > 2 and draft.meta.abwesenheitszeiten:
                self._set_sdt_text(sdts_c3_0[2], draft.meta.abwesenheitszeiten, full_ns, w_ns)

        # 5. Wesentliche Entwicklungen (Row 4, Cell 0)
        if len(t0.rows) > 4:
            c4_0 = t0.rows[4].cells[0]
            dev_text = _with_marker(draft.developments, draft.developments_needs_review)
            self._set_or_add_paragraph(c4_0, 1, dev_text)

        # 6. Bezugsbegleiter*in (Row 5, Cell 1)
        if len(t0.rows) > 5 and len(t0.rows[5].cells) > 1:
            c5_1 = t0.rows[5].cells[1]
            if draft.meta.bezugsbetreuer:
                self._set_or_add_paragraph(c5_1, 1, draft.meta.bezugsbetreuer)

    # -----------------------------------------------------------------------
    # Abschnitte / 9 Lebensbereiche (Tabellen 1–9)
    # -----------------------------------------------------------------------

    def _render_sections(
        self,
        doc: docx.Document,
        draft: ReportDraft,
        full_ns: dict,
        w_ns: str,
        w14_ns: str,
    ) -> None:
        # Erstelle Mapping von Abschnitts-ID/Index auf Tabelle
        sections_by_id = {s.id: s for s in draft.sections}

        for table_idx in range(1, min(10, len(doc.tables))):
            t = doc.tables[table_idx]
            # Primär nach 1-basierter ID ("1", "2", ...)
            sid = str(table_idx)
            section: Optional[ReportSection] = sections_by_id.get(sid)
            if section is None:
                continue

            # Row 1: Checkbox 'entfällt', Leitziel, S.M.A.R.T. Ziele
            if len(t.rows) > 1:
                r1_c0 = t.rows[1].cells[0]
                sdts = r1_c0._tc.xpath(".//w:sdt")
                if sdts:
                    # 'entfällt' ankreuzen wenn previous_goals_apply False ist
                    self._set_sdt_checkbox(sdts[0], not section.previous_goals_apply, full_ns, w14_ns)

                self._set_or_add_paragraph(r1_c0, 2, section.leitziel)

                self._set_bullet_paragraphs(r1_c0, 4, section.smarte_ziele)

            # Row 2: Bericht zur Zielerreichung
            if len(t.rows) > 2:
                r2_c0 = t.rows[2].cells[0]
                self._set_or_add_paragraph(r2_c0, 1, _bericht_text(section))

            # Row 3: Vorschlag Ziele (Col 0) & Vorschlag Maßnahmen (Col 1)
            if len(t.rows) > 3:
                r3 = t.rows[3]
                if len(r3.cells) > 0:
                    self._set_bullet_paragraphs(r3.cells[0], 1, section.vorschlag_ziele)

                if len(r3.cells) > 1:
                    self._set_bullet_paragraphs(r3.cells[1], 1, section.vorschlag_massnahmen)

            # Row 4: Realisierung durch Andere
            if len(t.rows) > 4:
                r4_c0 = t.rows[4].cells[0]
                if section.realisierung_andere:
                    if len(r4_c0.paragraphs) > 1 and not r4_c0.paragraphs[1].text.strip():
                        r4_c0.paragraphs[1].text = section.realisierung_andere
                    else:
                        r4_c0.add_paragraph(section.realisierung_andere)

    # -----------------------------------------------------------------------
    # Hilfsmethoden
    # -----------------------------------------------------------------------

    @staticmethod
    def _split_address(anschrift: str) -> tuple[str, str]:
        if not anschrift:
            return "", ""
        if "," in anschrift:
            parts = [p.strip() for p in anschrift.split(",", 1)]
            return parts[0], parts[1]
        return anschrift.strip(), ""

    @staticmethod
    def _format_zeitraum(meta: ReportMeta) -> str:
        von = meta.leistungszeitraum_von.strip()
        bis = meta.leistungszeitraum_bis.strip()
        if von and bis:
            return f"{von} - {bis}"
        if von:
            return f"ab {von}"
        if bis:
            return f"bis {bis}"
        return ""

    @classmethod
    def _set_bullet_paragraphs(cls, cell, index: int, value: str) -> None:
        """Ein Aufzählungspunkt = ein eigener Absatz (gleiche Absatzformatierung).

        Vorher landeten alle Punkte in einem Absatz (nur per Zeilenumbruch bzw. bei
        einzeiligen Eingaben als „• … • …“ hintereinander).
        """
        items = _bullet_items(value)
        if not items:
            cls._set_or_add_paragraph(cell, index, value)
            return
        cls._set_or_add_paragraph(cell, index, f"• {items[0]}")
        anchor = cell.paragraphs[index] if index < len(cell.paragraphs) else cell.paragraphs[-1]
        for item in items[1:]:
            new_p = copy.deepcopy(anchor._p)
            # w14:paraId/textId müssen dokumentweit eindeutig sein -> an Kopien entfernen
            for attr in list(new_p.attrib):
                if attr.endswith("}paraId") or attr.endswith("}textId"):
                    del new_p.attrib[attr]
            anchor._p.addnext(new_p)
            paragraph = Paragraph(new_p, anchor._parent)
            paragraph.text = f"• {item}"
            anchor = paragraph

    @staticmethod
    def _set_or_add_paragraph(cell, index: int, text: str) -> None:
        if not text:
            if index < len(cell.paragraphs):
                cell.paragraphs[index].text = ""
            return
        if index < len(cell.paragraphs):
            cell.paragraphs[index].text = text
        else:
            cell.add_paragraph(text)

    @staticmethod
    def _set_sdt_text(sdt: etree._Element, text: str, full_ns: dict, w_ns: str) -> None:
        for plchdr in sdt.xpath(".//w:sdtPr/w:showingPlcHdr", namespaces=full_ns):
            p = plchdr.getparent()
            if p is not None:
                p.remove(plchdr)
        content_list = sdt.xpath("./w:sdtContent", namespaces=full_ns)
        if not content_list:
            return
        content = content_list[0]
        for shd in content.xpath(".//w:shd", namespaces=full_ns):
            p = shd.getparent()
            if p is not None:
                p.remove(shd)
        for rStyle in content.xpath('.//w:rStyle[@w:val="Platzhaltertext"]', namespaces=full_ns):
            p = rStyle.getparent()
            if p is not None:
                p.remove(rStyle)
        ts = content.xpath(".//w:t", namespaces=full_ns)
        if ts:
            ts[0].text = text
            for extra in ts[1:]:
                p = extra.getparent()
                if p is not None:
                    p.remove(extra)
        else:
            r = etree.SubElement(content, f"{{{w_ns}}}r")
            t = etree.SubElement(r, f"{{{w_ns}}}t")
            t.text = text

    @staticmethod
    def _set_sdt_checkbox(sdt: etree._Element, checked: bool, full_ns: dict, w14_ns: str) -> None:
        for cb in sdt.xpath(".//w14:checkbox/w14:checked", namespaces=full_ns):
            cb.set(f"{{{w14_ns}}}val", "1" if checked else "0")
        for t in sdt.xpath(".//w:sdtContent//w:t", namespaces=full_ns):
            t.text = "☒" if checked else "☐"
