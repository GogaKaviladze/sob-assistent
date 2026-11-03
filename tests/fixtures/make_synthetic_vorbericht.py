"""Synthetischer Fixture-Builder für Hamburger Vorberichte (Zero-Disk-Footprint in Tests).

Erstellt im RAM einen vollständigen synthetischen Vorbericht auf Basis von
templates/hamburg/Vorlage.docx mit eindeutigen Test-Markern je Lebensbereich.
Unterstützt wahlweise den XML-Bookmark-Fastpath oder (durch Umbenennung der Textmarken)
den Heuristik-Fallback-Parser.
"""
from __future__ import annotations

import argparse
import io
from pathlib import Path
import sys
import zipfile

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from lxml import etree

from sob_generator import docx_fields as fields
from sob_generator.llm.sections import SECTION_DEFINITIONS

VORLAGE_PATH = ROOT_DIR / "templates" / "hamburg" / "Vorlage.docx"


def build_hamburg_vorbericht(fallback: bool = False) -> bytes:
    """Erzeugt einen synthetischen Vorbericht im Speicher als DOCX-Bytes.

    Args:
        fallback: Wenn True, werden alle 'sections_*' Bookmarks in 'TextNNN' umbenannt,
                  sodass der Fallback-Parser greift.

    Returns:
        bytes der erzeugten .docx-Datei.
    """
    if not VORLAGE_PATH.exists():
        raise FileNotFoundError(f"Vorlage nicht gefunden: {VORLAGE_PATH}")

    # 1. XML-Tree aus Vorlage im Speicher laden
    root = fields.load_document_tree(VORLAGE_PATH)

    # 2. Metadaten setzen
    try:
        fields.set_formtext(root, "client_name_vorname", "Synthetischer Klient")
    except KeyError:
        pass
    try:
        fields.set_formtext(root, "meta_leistungszeitraum_von", "01.01.2025")
    except KeyError:
        pass
    try:
        fields.set_formtext(root, "meta_leistungszeitraum_bis", "31.12.2025")
    except KeyError:
        pass
    try:
        fields.set_formtext(root, "developments", "ZZ_SYNTH_DEVELOPMENTS")
    except KeyError:
        pass

    # 3. Jeden der 9 Bereiche mit eindeutigen ZZ_SYNTH-Markern füllen
    for defn in SECTION_DEFINITIONS:
        id_slug = defn.id.replace(".", "_")
        marker_map = {
            f"sections_{defn.index}_leitziel": f"ZZ_SYNTH_{id_slug}_LEITZIEL",
            f"sections_{defn.index}_smarte_ziele": f"ZZ_SYNTH_{id_slug}_SMARTE_ZIELE",
            f"sections_{defn.index}_bericht_zielerreichung": f"ZZ_SYNTH_{id_slug}_BERICHT",
            f"sections_{defn.index}_vorschlag_ziele": f"ZZ_SYNTH_{id_slug}_VORSCHLAG",
            f"sections_{defn.index}_vorschlag_massnahmen": f"ZZ_SYNTH_{id_slug}_MASSNAHMEN",
            f"sections_{defn.index}_realisierung_andere": f"ZZ_SYNTH_{id_slug}_REALISIERUNG",
        }
        for bm_name, marker_val in marker_map.items():
            try:
                fields.set_formtext(root, bm_name, marker_val)
            except KeyError:
                pass

    # 4. Falls Fallback: alle sections_* Bookmarks in TextNNN umbenennen
    if fallback:
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        for i, el in enumerate(root.findall(".//w:bookmarkStart", ns)):
            name = el.attrib.get(f"{{{ns['w']}}}name", "")
            if name.startswith("sections_"):
                el.attrib[f"{{{ns['w']}}}name"] = f"Text{i}"

    # 5. In-Memory ZIP zusammenbauen
    new_xml = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(VORLAGE_PATH, "r") as zin, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = new_xml if item.filename == "word/document.xml" else zin.read(item.filename)
            zout.writestr(item, data)

    return buf.getvalue()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Erstellt synthetischen Hamburger Vorbericht im DOCX-Format.")
    parser.add_argument("--out", required=True, help="Zielpfad für die generierte .docx-Datei")
    parser.add_argument("--fallback", action="store_true", help="Benennt Textmarken um, um den Fallback-Parser zu triggern")
    args = parser.parse_args()

    data_bytes = build_hamburg_vorbericht(fallback=args.fallback)
    out_path = Path(args.out)
    out_path.write_bytes(data_bytes)
    print(f"Synthetischer Vorbericht erfolgreich erstellt: {out_path} ({len(data_bytes)} Bytes)")
