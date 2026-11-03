"""DOCX-Sicherheits- und Härtungsmodul (Zero-Disk, ReDoS-, Bomben- und XXE-Schutz).

Prüft DOCX-Dateien im Arbeitsspeicher auf:
1. ZIP-Bomben (Eintragsanzahl, Gesamtgröße, Dekompressionsrate, document.xml-Limit).
2. Verschachtelte Archive (.zip, .docx).
3. DOCTYPE-Deklarationen / XML-Entities (XXE-Schutz).
4. Versteckte Textstellen (w:vanish, w:specVanish, w:webHidden) sowie gelöschte Inhalte (w:del, w:delText, w:moveFrom).
"""
from __future__ import annotations

import io
import zipfile
import zlib
from dataclasses import dataclass
from typing import Dict, Optional

from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

# Grenzwerte (M8)
MAX_MEMBERS = 200
MAX_TOTAL_UNCOMPRESSED = 50 * 1024 * 1024  # 50 MB
MAX_DOCUMENT_XML = 20 * 1024 * 1024  # 20 MB
MAX_RATIO = 100  # Kompressionsrate <= 100:1 pro Eintrag

# Gehärteter XML-Parser ohne Netzwerkzugriff, DTD oder Entity-Auflösung
SAFE_PARSER = etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    load_dtd=False,
    huge_tree=False,
)

NESTED_EXTENSIONS = (
    ".zip",
    ".docx",
    ".docm",
    ".dotx",
    ".dotm",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
    ".7z",
    ".rar",
)


class UnsafeDocxError(ValueError, zipfile.BadZipFile):
    """Signalisiert eine sicherheitsrelevante Ablehnung oder Beschädigung einer DOCX-Datei."""

    def __init__(self, reason: str):
        super().__init__(f"Unsafe DOCX: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class SafeDocx:
    """Bereinigte DOCX-Bytes mit Metadaten über entfernte versteckte Textstellen."""

    data: bytes
    hidden_removed: int = 0


def _is_toggle_active(elem: Optional[etree._Element]) -> bool:
    """Prüft, ob ein OpenXML On/Off-Toggle-Element (wie w:vanish) aktiv ist."""
    if elem is None:
        return False
    val = elem.get(f"{{{W}}}val")
    if val is None:
        val = elem.get("val")
    if val is not None and val.strip().lower() in ("0", "false", "off"):
        return False
    return True


def _has_hidden_rpr(r: etree._Element) -> bool:
    """Ermittelt, ob ein Run als ausgeblendet/versteckt formatiert ist."""
    rpr = r.find(f"{{{W}}}rPr")
    if rpr is None:
        return False
    for tag in ("vanish", "specVanish", "webHidden"):
        elem = rpr.find(f"{{{W}}}{tag}")
        if _is_toggle_active(elem):
            return True
    return False


def sanitize_docx_bytes(data: bytes) -> SafeDocx:
    """Validiert und säubert DOCX-Bytes im Arbeitsspeicher.

    Raises:
        UnsafeDocxError: Wenn Grenzwerte überschritten werden oder schädliche Inhalte vorliegen.
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) < 4:
        raise UnsafeDocxError("bad_zip")

    try:
        zin = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, zlib.error, Exception):
        raise UnsafeDocxError("bad_zip")

    with zin:
        infolist = zin.infolist()

        # 1. Anzahl ZIP-Einträge
        if len(infolist) > MAX_MEMBERS:
            raise UnsafeDocxError("too_many_members")

        # 2. Vorab-Prüfung der deklarierten Header-Größen
        total_declared = 0
        has_document_xml = False
        for info in infolist:
            fn_lower = info.filename.lower()
            if fn_lower.endswith(NESTED_EXTENSIONS):
                raise UnsafeDocxError("nested_archive")

            if info.filename == "word/document.xml":
                has_document_xml = True
                if info.file_size > MAX_DOCUMENT_XML:
                    raise UnsafeDocxError("too_large")

            if info.file_size > MAX_TOTAL_UNCOMPRESSED:
                raise UnsafeDocxError("too_large")

            if info.compress_size > 0:
                if (info.file_size / info.compress_size) > MAX_RATIO:
                    raise UnsafeDocxError("ratio")
            elif info.file_size > 0:
                raise UnsafeDocxError("ratio")

            total_declared += info.file_size

        if not has_document_xml:
            raise UnsafeDocxError("bad_zip")

        if total_declared > MAX_TOTAL_UNCOMPRESSED:
            raise UnsafeDocxError("too_large")

        # 3. Begrenztes Streaming-Lesen jedes Eintrags (Header-Größen nicht blind vertrauen)
        chunk_size = 64 * 1024
        total_uncompressed = 0
        member_bytes_dict: Dict[str, bytes] = {}

        for info in infolist:
            member_buf = bytearray()
            try:
                with zin.open(info) as reader:
                    while True:
                        chunk = reader.read(chunk_size)
                        if not chunk:
                            break
                        member_buf.extend(chunk)
                        total_uncompressed += len(chunk)

                        if total_uncompressed > MAX_TOTAL_UNCOMPRESSED:
                            raise UnsafeDocxError("too_large")

                        if info.filename == "word/document.xml" and len(member_buf) > MAX_DOCUMENT_XML:
                            raise UnsafeDocxError("too_large")

                        comp_size = max(info.compress_size, 1)
                        if len(member_buf) > comp_size * MAX_RATIO:
                            raise UnsafeDocxError("ratio")
            except (zipfile.BadZipFile, zlib.error):
                raise UnsafeDocxError("bad_zip")

            content = bytes(member_buf)

            # 4. DOCTYPE / XXE-Prüfung bei XML- und Rels-Dateien
            fn_low = info.filename.lower()
            if fn_low.endswith((".xml", ".rels")) or content.startswith(b"<?xml") or content.lstrip().startswith(b"<"):
                if b"<!DOCTYPE" in content.upper():
                    raise UnsafeDocxError("doctype")

            member_bytes_dict[info.filename] = content

    # 5. Bereinigung von word/document.xml
    doc_xml = member_bytes_dict["word/document.xml"]
    try:
        tree = etree.fromstring(doc_xml, parser=SAFE_PARSER)
    except Exception:
        raise UnsafeDocxError("bad_zip")

    hidden_removed = 0

    # (a) w:del (Inhalte gelöschter Textstellen)
    for del_elem in tree.xpath(".//w:del", namespaces={"w": W}):
        parent = del_elem.getparent()
        if parent is not None:
            parent.remove(del_elem)
            hidden_removed += 1

    # (b) w:delText (außerhalb w:del, falls vorhanden)
    for dt_elem in tree.xpath(".//w:delText", namespaces={"w": W}):
        parent = dt_elem.getparent()
        if parent is not None:
            parent.remove(dt_elem)
            hidden_removed += 1

    # (c) w:moveFrom (verschobene/ausgeschnittene Textstellen)
    for move_elem in tree.xpath(".//w:moveFrom", namespaces={"w": W}):
        parent = move_elem.getparent()
        if parent is not None:
            parent.remove(move_elem)
            hidden_removed += 1

    # (d) Runs mit w:vanish, w:specVanish oder w:webHidden (w:ins bleibt)
    for r in tree.xpath(".//w:r", namespaces={"w": W}):
        if _has_hidden_rpr(r):
            parent = r.getparent()
            if parent is not None:
                parent.remove(r)
                hidden_removed += 1

    # 6. Re-zip in neuen In-Memory BytesIO
    new_doc_xml = etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=True)
    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in infolist:
            if item.filename == "word/document.xml":
                zout.writestr(item, new_doc_xml)
            else:
                zout.writestr(item, member_bytes_dict[item.filename])

    return SafeDocx(data=out_buf.getvalue(), hidden_removed=hidden_removed)
