"""DocxRenderer (ADR-10): ReportDraft → befüllte Vorlage.docx.

Felder werden ausschließlich über die Bookmark-Namen aus docs/field-mapping.md
adressiert (`docx_fields`). Nicht gemappte Felder (z.B. Abwesenheitszeiten
#16/#17) werden bewusst ausgelassen — sie sind in field-mapping.md als OFFEN
dokumentiert und nicht Teil dieses Durchstichs.

Listenfelder (`smarte_ziele`, `vorschlag_ziele`) werden als Aufzählung gerendert
(`set_formtext_list`, ADR-10/ADR-11): das LLM liefert sie zeilenweise.
"""
from __future__ import annotations

from pathlib import Path
from typing import BinaryIO, List, Union

from sob_generator import docx_fields as fields

from .models import ClientData, ReportDraft, ReportMeta, ReportSection

# Felder, die als Aufzählung (mehrere Bullets) gerendert werden. Maßnahmen
# liefert das LLM zeilenweise (z.B. "Die Assistenz ...\n- Die Assistenz ...");
# als Fließtext gingen die einzelnen Maßnahmen ineinander über (Bug-Fix).
LIST_FIELDS = ("smarte_ziele", "vorschlag_ziele", "vorschlag_massnahmen")

# Sichtbarer Hinweis, wenn die automatische Generierung scheiterte (#18) — wird
# dem Hauptfeld vorangestellt, statt den Abschnitt stumm leer zu lassen.
REVIEW_MARKER = "⚠ BITTE PRÜFEN — automatische Generierung fehlgeschlagen."

# Hinweis für den Guard-Fall (#50/K1): Pass 0 hat diesem Abschnitt keine
# Einträge zugeordnet — das ist Datenlage, kein Fehler. Anders formuliert als
# REVIEW_MARKER, damit die Fachkraft die Ursache sofort erkennt.
NO_ENTRIES_HINT = (
    "Keine Dokumentation für diesen Bereich im Berichtszeitraum — "
    "bitte manuell ergänzen."
)


def _with_marker(text: str, needs_review: bool) -> str:
    """Stellt dem Text den Review-Hinweis voran, wenn nötig (#18)."""
    if not needs_review:
        return text
    return f"{REVIEW_MARKER}\n{text}".rstrip()


def _name_vorname(client: ClientData) -> str:
    """„Name, Vorname" für Feld #1 — lässt fehlende Teile kommalos weg."""
    return ", ".join(part for part in (client.name, client.vorname) if part)


def _bericht_text(section: ReportSection) -> str:
    """Text des Bericht-Feldes inkl. Hinweis-Logik (#18/#50-K1).

    - Guard-Fall (keine Einträge, kein manuell ergänzter Text) → NO_ENTRIES_HINT.
      Hat die Fachkraft im Review bereits Text ergänzt, gewinnt dieser.
    - Echter Generierungs-Fehlschlag → REVIEW_MARKER vorangestellt.
    """
    if section.no_entries and not section.bericht_zielerreichung.strip():
        return NO_ENTRIES_HINT
    return _with_marker(section.bericht_zielerreichung, section.needs_review)


def _split_items(value: str) -> List[str]:
    """Zerlegt einen mehrzeiligen Feldwert in Aufzählungspunkte."""
    items = []
    for line in value.splitlines():
        line = line.strip().lstrip("-•").strip()
        if line:
            items.append(line)
    return items


def _remove_duplicate_bericht_rows(root) -> int:
    """Entfernt in-memory die doppelten 'Bericht zur Zielerreichung'-Zeilen in Tabellen 2–9.

    Bedingungen für die Entfernung (verschärft nach Review):
    - Die Zeile folgt unmittelbar auf die Original-Zeile mit Bookmark 'sections_N_bericht_zielerreichung'.
    - (a) Der GESAMTE Zeilentext (alle Zellen/Absätze, itertext der ganzen Zeile) muss identisch
      mit dem der vorangehenden Zeile sein.
    - Die Zeile enthält KEIN Bookmark.
    - Die Zeile enthält KEINE Checkbox, KEIN Dropdown, KEIN fldSimple und KEIN benanntes Formularfeld.
    - (b) Das unbenannte, leere FORMTEXT-Artefakt der Duplikat-Zeile darf keinen Default-Text
      (w:textInput/w:default) und keinen Ergebnistext zwischen fldChar separate/end haben.
    """
    removed_count = 0
    w_ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    ns = {"w": w_ns}

    for tbl in root.findall(".//w:tbl", ns):
        rows = tbl.findall("./w:tr", ns)
        for i in range(1, len(rows)):
            prev_row = rows[i - 1]
            curr_row = rows[i]

            prev_bms = [
                b.attrib.get(f"{{{w_ns}}}name", "")
                for b in prev_row.findall(".//w:bookmarkStart", ns)
            ]
            if not any(
                bm.startswith("sections_") and bm.endswith("_bericht_zielerreichung")
                for bm in prev_bms
            ):
                continue

            # (a) Gesamter Zeilentext aller Zellen/Absätze muss identisch sein
            prev_text = "".join(prev_row.itertext()).strip()
            curr_text = "".join(curr_row.itertext()).strip()
            if not prev_text or prev_text != curr_text:
                continue

            curr_bms = curr_row.findall(".//w:bookmarkStart", ns)
            if curr_bms:
                continue

            if (
                curr_row.findall(f".//{{{w_ns}}}checkBox")
                or curr_row.findall(f".//{{{w_ns}}}ddList")
                or curr_row.findall(f".//{{{w_ns}}}fldSimple")
            ):
                continue

            has_named_field = False
            for ff_data in curr_row.findall(f".//{{{w_ns}}}ffData"):
                name_el = ff_data.find(f"{{{w_ns}}}name")
                if name_el is not None and name_el.attrib.get(f"{{{w_ns}}}val", "").strip():
                    has_named_field = True
                    break
            if has_named_field:
                continue

            # (b) Kein Default-Text (w:textInput/w:default)
            has_default_text = False
            for default_el in curr_row.findall(f".//{{{w_ns}}}default"):
                if default_el.attrib.get(f"{{{w_ns}}}val", "").strip():
                    has_default_text = True
                    break
            if has_default_text:
                continue

            # (b) Kein Ergebnistext zwischen separate und end
            has_result_text = False
            for p in curr_row.findall(f".//{{{w_ns}}}p"):
                in_result = False
                for child in p.iter():
                    local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                    if local == "fldChar":
                        ftype = child.attrib.get(f"{{{w_ns}}}fldCharType")
                        if ftype == "separate":
                            in_result = True
                        elif ftype == "end":
                            in_result = False
                    elif in_result and local == "t":
                        txt = (child.text or "").replace("\u2002", " ").strip()
                        if txt:
                            has_result_text = True
                            break
                if has_result_text:
                    break
            if has_result_text:
                continue

            tbl.remove(curr_row)
            removed_count += 1

    return removed_count


class HamburgDocxRenderer:
    """Befüllt eine Kopie der Vorlage.docx aus einem ReportDraft."""

    def __init__(self, template_path):
        self._template = Path(template_path)

    def render(
        self, draft: ReportDraft, output_path: Union[Path, str, BinaryIO]
    ) -> Union[Path, BinaryIO]:
        root = fields.load_document_tree(self._template)
        _remove_duplicate_bericht_rows(root)
        self._render_client(root, draft.client)
        self._render_meta(root, draft.meta)
        fields.set_formtext(
            root,
            "developments",
            _with_marker(draft.developments, draft.developments_needs_review),
        )
        for section in draft.sections:
            self._render_section(root, section)
        if hasattr(output_path, "write"):
            fields.save_document_tree(self._template, root, output_path)
            return output_path
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fields.save_document_tree(self._template, root, output_path)
        return output_path

    def _render_client(self, root, client: ClientData) -> None:
        # „Bericht für (Name, Vorname)" (#1, #50/K3): ein Formularfeld, ADR-5
        # trennt name/vorname → Renderer fügt im Vorlagen-Format zusammen.
        fields.set_formtext(root, "client_name_vorname", _name_vorname(client))
        fields.set_formtext(root, "client_geburtsdatum", client.geburtsdatum)
        fields.set_formtext(root, "client_anschrift", client.anschrift)
        fields.set_formtext(root, "client_gdb", client.gdb)
        fields.set_formtext(root, "client_pflegegrad", client.pflegegrad)
        fields.set_formtext(root, "client_leistungsart", client.leistungsart)
        fields.set_checkbox(
            root, "client_gesetzliche_betreuung_ja", client.gesetzliche_betreuung
        )
        fields.set_checkbox(
            root, "client_gesetzliche_betreuung_nein", not client.gesetzliche_betreuung
        )

    def _render_meta(self, root, meta: ReportMeta) -> None:
        # Abwesenheitszeiten (#16/#17) sind un-bookmarked (OFFEN) → ausgelassen.
        fields.set_formtext(root, "meta_leistungszeitraum_von", meta.leistungszeitraum_von)
        fields.set_formtext(root, "meta_leistungszeitraum_bis", meta.leistungszeitraum_bis)
        fields.set_formtext(root, "meta_leistungsanbieter", meta.leistungsanbieter)
        fields.set_formtext(root, "meta_begruegnungsstaette", meta.begruegnungsstaette)
        fields.set_formtext(root, "meta_bezugsbetreuer", meta.bezugsbetreuer)

    def _render_section(self, root, section: ReportSection) -> None:
        n = _section_index(section)
        # „entfällt"-Checkbox: angekreuzt, wenn KEINE Ziele gelten (ADR-5).
        fields.set_checkbox(
            root, f"sections_{n}_previous_goals_apply", not section.previous_goals_apply
        )
        fields.set_formtext(root, f"sections_{n}_leitziel", section.leitziel)
        fields.set_formtext_list(
            root, f"sections_{n}_smarte_ziele", _split_items(section.smarte_ziele)
        )
        bericht_val = _bericht_text(section)
        bericht_lines = [line.strip() for line in bericht_val.splitlines() if line.strip()]
        if len(bericht_lines) > 1:
            fields.set_formtext_list(
                root,
                f"sections_{n}_bericht_zielerreichung",
                bericht_lines,
            )
        else:
            fields.set_formtext(
                root,
                f"sections_{n}_bericht_zielerreichung",
                bericht_val,
            )
        fields.set_formtext_list(
            root, f"sections_{n}_vorschlag_ziele", _split_items(section.vorschlag_ziele)
        )
        fields.set_formtext_list(
            root,
            f"sections_{n}_vorschlag_massnahmen",
            _split_items(section.vorschlag_massnahmen),
        )
        fields.set_formtext(
            root, f"sections_{n}_realisierung_andere", section.realisierung_andere
        )


def _section_index(section: ReportSection) -> int:
    """Bookmark-Index N (0..8) aus der Abschnitts-ID (z.B. '1.1' → 0, '3' → 8)."""
    from sob_generator.llm.sections import SECTION_DEFINITIONS

    for defn in SECTION_DEFINITIONS:
        if defn.id == section.id:
            return defn.index
    raise ValueError(f"Unbekannte Abschnitts-ID: {section.id!r}")


# Rückwärtskompatibilität (ADR-10 / Issue #63)
DocxRenderer = HamburgDocxRenderer

