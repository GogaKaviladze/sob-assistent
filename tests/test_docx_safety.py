"""Tests für die DOCX-Härtung (ZIP-Bomben, DOCTYPE-Angriffe, versteckter Text)."""
from __future__ import annotations

import io
import tempfile
import time
import tracemalloc
import zipfile
from pathlib import Path
import pytest
from lxml import etree

from sob_generator.docx_safety import (
    MAX_MEMBERS,
    MAX_TOTAL_UNCOMPRESSED,
    MAX_DOCUMENT_XML,
    MAX_RATIO,
    SafeDocx,
    UnsafeDocxError,
    sanitize_docx_bytes,
)
from chat_app.plan_loader import parse_plan_docx

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _wtag(tag: str) -> str:
    return f"{{{W}}}{tag}"


CONTENT_TYPES_XML = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

RELS_XML = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""


def make_test_docx(
    document_xml: str | bytes,
    extra_members: dict[str, bytes] | None = None,
) -> bytes:
    """Erzeugt ein minimales valides DOCX-Paket im Arbeitsspeicher."""
    buf = io.BytesIO()
    doc_bytes = document_xml if isinstance(document_xml, bytes) else document_xml.encode("utf-8")
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES_XML)
        z.writestr("_rels/.rels", RELS_XML)
        z.writestr("word/document.xml", doc_bytes)
        if extra_members:
            for name, content in extra_members.items():
                z.writestr(name, content)
    return buf.getvalue()


def test_zip_bomb_rejection():
    """ZIP-Bombe (>= 1 GB dekomprimiert, ~1 MB komprimiert) wird in < 1 s und < 100 MB RAM abgelehnt."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES_XML)
        z.writestr("_rels/.rels", RELS_XML)
        # 1 GB Nullen in 10 MB Chunks streamen (bleibt in RAM als ~1 MB ZIP)
        with z.open("word/document.xml", "w") as f:
            chunk = b"\x00" * (10 * 1024 * 1024)
            for _ in range(100):
                f.write(chunk)
    bomb_bytes = buf.getvalue()
    assert len(bomb_bytes) < 2 * 1024 * 1024  # Komprimiert ca. 1 MB

    tracemalloc.start()
    try:
        t0 = time.perf_counter()
        with pytest.raises(UnsafeDocxError) as exc_info:
            sanitize_docx_bytes(bomb_bytes)
        t_elapsed = time.perf_counter() - t0
        peak_ram = tracemalloc.get_traced_memory()[1]

        assert exc_info.value.reason in ("too_large", "ratio")
        assert t_elapsed < 1.0, f"Abweisung dauerte zu lange: {t_elapsed:.3f} s"
        assert peak_ram < 100 * 1024 * 1024, f"Speicherzuwachs zu hoch: {peak_ram / (1024*1024):.1f} MB"
    finally:
        tracemalloc.stop()


def test_too_many_members():
    """Archive mit mehr als 200 Einträgen werden mit too_many_members abgelehnt."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", b"<w:document/>")
        for i in range(201):
            z.writestr(f"word/part_{i}.xml", b"<data/>")
    data = buf.getvalue()

    with pytest.raises(UnsafeDocxError) as exc_info:
        sanitize_docx_bytes(data)
    assert exc_info.value.reason == "too_many_members"


def test_nested_archive_rejection():
    """Verschachtelte .zip oder .docx Archive werden sofort abgewiesen."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES_XML)
        z.writestr("_rels/.rels", RELS_XML)
        z.writestr("word/document.xml", b"<w:document/>")
        z.writestr("word/embeddings/embedded.zip", b"PK\x03\x04dummy")
    data = buf.getvalue()

    with pytest.raises(UnsafeDocxError) as exc_info:
        sanitize_docx_bytes(data)
    assert exc_info.value.reason == "nested_archive"


def test_doctype_rejection():
    """XML-Teile mit <!DOCTYPE (z. B. XXE / Entity-Angriff) werden sofort abgelehnt."""
    xml_with_doctype = """<?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE w:document [
      <!ENTITY xxe SYSTEM "file:///etc/passwd">
    ]>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body><w:p><w:r><w:t>&xxe;</w:t></w:r></w:p></w:body>
    </w:document>
    """
    data = make_test_docx(xml_with_doctype)

    with pytest.raises(UnsafeDocxError) as exc_info:
        sanitize_docx_bytes(data)
    assert exc_info.value.reason == "doctype"


def test_hidden_text_removal_and_counter():
    """Versteckter Text (vanish), Löschungen (del/delText) und Verschiebungen (moveFrom) werden entfernt.
    Eingefügter Text (ins) bleibt erhalten. hidden_removed zählt die 3 entfernten Stellen exakt."""
    import docx

    # Erzeuge Dokument mit docx für valide Basisstruktur
    doc = docx.Document()
    doc.add_paragraph("Bericht für (Name, Vorname): Anna Schmidt")
    doc.add_paragraph("Leistungszeitraum von 01.02.2025 bis 31.01.2026")
    t = doc.add_table(rows=2, cols=1)
    t.cell(0, 0).text = "4. Wohnen"
    t.cell(1, 0).text = "Leitziel: Start "

    base_buf = io.BytesIO()
    doc.save(base_buf)
    base_buf.seek(0)

    # Injektion der 4 Test-Elemente in document.xml
    with zipfile.ZipFile(base_buf) as zin:
        tree = etree.fromstring(zin.read("word/document.xml"))

    tbl = tree.find(".//" + _wtag("tbl"))
    assert tbl is not None
    last_p = tbl.findall(".//" + _wtag("p"))[-1]

    # 1. vanish run
    r_vanish = etree.SubElement(last_p, _wtag("r"))
    rpr = etree.SubElement(r_vanish, _wtag("rPr"))
    etree.SubElement(rpr, _wtag("vanish"))
    t_vanish = etree.SubElement(r_vanish, _wtag("t"))
    t_vanish.text = "ZZ_SYNTH_HIDDEN "

    # 2. del
    del_elem = etree.SubElement(last_p, _wtag("del"))
    del_r = etree.SubElement(del_elem, _wtag("r"))
    del_t = etree.SubElement(del_r, _wtag("delText"))
    del_t.text = "ZZ_SYNTH_DEL "

    # 3. moveFrom
    move_elem = etree.SubElement(last_p, _wtag("moveFrom"))
    move_r = etree.SubElement(move_elem, _wtag("r"))
    move_t = etree.SubElement(move_r, _wtag("t"))
    move_t.text = "ZZ_SYNTH_MOVE "

    # 4. ins
    ins_elem = etree.SubElement(last_p, _wtag("ins"))
    ins_r = etree.SubElement(ins_elem, _wtag("r"))
    ins_t = etree.SubElement(ins_r, _wtag("t"))
    ins_t.text = "ZZ_SYNTH_INS"

    dirty_docx = io.BytesIO()
    with zipfile.ZipFile(base_buf) as zin, zipfile.ZipFile(dirty_docx, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            if item.filename == "word/document.xml":
                zout.writestr(item, etree.tostring(tree))
            else:
                zout.writestr(item, zin.read(item.filename))

    dirty_bytes = dirty_docx.getvalue()

    # 1. Sanitize-Funktion direkt prüfen
    safe = sanitize_docx_bytes(dirty_bytes)
    assert isinstance(safe, SafeDocx)
    assert safe.hidden_removed == 3

    # 2. Durch den PlanLoader parsen (unterstützt SafeDocx oder rohe Bytes)
    plan = parse_plan_docx(safe, filename="vorbericht_synth.docx")
    ctx = plan.to_context_text()

    assert "ZZ_SYNTH_INS" in ctx
    assert "ZZ_SYNTH_HIDDEN" not in ctx
    assert "ZZ_SYNTH_DEL" not in ctx
    assert "ZZ_SYNTH_MOVE" not in ctx
    assert plan.hidden_removed == 3

    # Direktes Parsen der unbereinigten Bytes
    plan_raw = parse_plan_docx(dirty_bytes, filename="vorbericht_synth.docx")
    assert plan_raw.hidden_removed == 3
    assert "ZZ_SYNTH_INS" in plan_raw.to_context_text()
    assert "ZZ_SYNTH_HIDDEN" not in plan_raw.to_context_text()


def test_templates_regression_unchanged():
    """Beide Vorlagen (Hamburg und Op de Wisch) parsen nach Härtung mit unveränderten Ergebnissen."""
    hamburg_path = Path("templates/hamburg/Vorlage.docx")
    odw_path = Path("templates/hamburg/OpDerWisch/FO_SVB_Berichtsvorlage_2026.docx")

    assert hamburg_path.is_file()
    assert odw_path.is_file()

    hamburg_bytes = hamburg_path.read_bytes()
    odw_bytes = odw_path.read_bytes()

    safe_h = sanitize_docx_bytes(hamburg_bytes)
    assert safe_h.hidden_removed == 0
    plan_h = parse_plan_docx(safe_h.data, filename="Vorlage.docx")
    assert plan_h.goals_count == 35
    assert plan_h.client_name == "Vorlage"

    safe_odw = sanitize_docx_bytes(odw_bytes)
    assert safe_odw.hidden_removed == 0
    plan_odw = parse_plan_docx(safe_odw.data, filename="FO_SVB_Berichtsvorlage_2026.docx")
    assert plan_odw.goals_count == 28
    assert plan_odw.client_name == "FOsvorlage"


def test_zero_disk_writes_guarantee(monkeypatch: pytest.MonkeyPatch):
    """Sicherheitsgarantie: DOCX-Sanitization und -Parsing schreiben niemals auf die Festplatte."""
    real_open = open

    def guarded_open(file, mode="r", *args, **kwargs):
        if any(m in mode for m in ("w", "a", "x", "+")):
            raise RuntimeError(f"Festplattenschreiben verboten: {file} mit Modus {mode}")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", guarded_open)

    for fn_name in ("NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile", "mkstemp", "mkdtemp"):
        if hasattr(tempfile, fn_name):
            monkeypatch.setattr(tempfile, fn_name, lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("tempfile verboten")))

    doc_data = make_test_docx("""<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
        <w:body><w:p><w:r><w:t>In-Memory DOCX</w:t></w:r></w:p></w:body>
    </w:document>""")

    safe = sanitize_docx_bytes(doc_data)
    plan = parse_plan_docx(safe.data)
    assert plan.filename == "vorbericht.docx"
