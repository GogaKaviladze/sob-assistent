"""Tests für Upload-Hardening, Datenschutz-Logging und Zero-Disk-Garantie.

Verifiziert:
1. Keine Dokumentinhalte / CANARY-Strings in Logs oder Fehlermeldungen bei Upload-Fehlern.
2. Globale Spool-Schwelle wird niemals gesenkt (Reihenfolge-Sicherheit bei mehreren Apps).
3. Striktes In-Memory-Verhalten: Uploads rollen niemals auf die Festplatte (SpooledTemporaryFile.rollover).
4. Gegenprobe im echten Upload-Pfad: Ohne die Härtung rollt Starlette bei > 1 MB tatsächlich über.
5. Exakte Grenzwerte: Body == Limit -> 200; Body == Limit + 1 -> 413.
6. Validierung fehlerhafter / negativer Content-Length-Header (400 statt stiller Fallback).
"""
from __future__ import annotations

import io
import logging
import os
import tempfile
from typing import Generator

import pytest
import starlette.formparsers
from fastapi.testclient import TestClient

from chat_app.app import create_app
from chat_app.chatbot import LlamaCppProvider, ReflectionChatbot, ReflectionEngine, create_llm_provider
from chat_app.upload_limits import (
    DEFAULT_MAX_UPLOAD_BYTES,
    HARD_CEILING_MAX_UPLOAD_BYTES,
    configure_spool_threshold,
    format_limit_mb,
    get_max_upload_bytes,
)

CANARY = "Fiktivgeheimnis Zebra-4711"


def make_test_app(**kwargs):
    if "chatbot" not in kwargs:
        kwargs["chatbot"] = ReflectionChatbot(provider=ReflectionEngine())
    return create_app(**kwargs)


@pytest.fixture(autouse=True)
def isolate_spool_max_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sichert und stellt Starlettes MultiPartParser.spool_max_size nach jedem Test wieder her."""
    original = getattr(starlette.formparsers.MultiPartParser, "spool_max_size", 1024 * 1024)
    monkeypatch.setattr(starlette.formparsers.MultiPartParser, "spool_max_size", original)


@pytest.fixture
def client() -> TestClient:
    app = make_test_app()
    return TestClient(app)


# ===========================================================================
# AUFGABE 1: Keine Inhalte in Logs und Fehlermeldungen
# ===========================================================================


def test_upload_plan_invalid_docx_bytes_no_leak(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    """Ungültige DOCX-Bytes (kein ZIP, enthält Kanarien-String) führen zu 400 ohne Inhaltsleck."""
    caplog.set_level(logging.DEBUG)

    corrupt_bytes = f"Not a zip archive: {CANARY}".encode("utf-8")
    files = {"file": ("vorbericht.docx", io.BytesIO(corrupt_bytes), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}

    res = client.post("/api/upload-plan", files=files)

    assert res.status_code == 400
    assert ("❌ Plan konnte nicht gelesen werden" in res.json()["detail"] or "❌ Datei abgelehnt" in res.json()["detail"])
    assert CANARY not in res.text
    assert CANARY not in caplog.text


def test_upload_plan_parser_exception_no_leak(client: TestClient, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """Wenn parse_plan_docx eine Exception mit Kanarien-String wirft, darf dieser weder im Response noch Log landen."""
    caplog.set_level(logging.DEBUG)

    def mock_parse_plan_docx(content_bytes: bytes, filename: str = ""):
        raise ValueError(f"Interner Parser-Fehler mit geheimem Inhalt: {CANARY}")

    monkeypatch.setattr("chat_app.app.parse_plan_docx", mock_parse_plan_docx)

    files = {"file": ("test_plan.docx", io.BytesIO(b"dummy bytes"), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}
    res = client.post("/api/upload-plan", files=files)

    assert res.status_code == 400
    data = res.json()
    assert data["detail"] == "❌ Plan konnte nicht gelesen werden (ValueError)."
    assert CANARY not in res.text
    assert CANARY not in caplog.text


def test_upload_csv_parser_exception_no_leak(client: TestClient, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """Wenn parse_csv_content eine Exception mit Kanarien-String wirft, darf dieser weder im Response noch Log landen."""
    caplog.set_level(logging.DEBUG)

    def mock_parse_csv(content: str, filename: str = "", client_name: str = ""):
        raise RuntimeError(f"CSV-Syntaxfehler mit Klientendaten: {CANARY}")

    monkeypatch.setattr("chat_app.app.parse_csv_content", mock_parse_csv)

    csv_bytes = b"Datum;Kategorie;Notiz\n01.01.2025;Arzt;Test"
    files = {"file": ("verlauf.csv", io.BytesIO(csv_bytes), "text/csv")}
    res = client.post("/api/upload", files=files)

    assert res.status_code == 400
    data = res.json()
    assert data["detail"] == "❌ CSV-Datei konnte nicht verarbeitet werden (RuntimeError)."
    assert CANARY not in res.text
    assert CANARY not in caplog.text


def test_select_model_exception_generic(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Modellwechsel-Fehler liefern generische Meldungen ohne sensitive interne Pfade oder Systemdetails."""
    caplog.set_level(logging.DEBUG)

    def mock_switch(model_name: str):
        raise FileNotFoundError(f"/geheimer/pfad/{CANARY}/model.gguf nicht gefunden")

    from chat_app.chatbot import ReflectionChatbot

    bot = ReflectionChatbot()
    monkeypatch.setattr(bot, "switch_model", mock_switch)
    test_client = TestClient(create_app(chatbot=bot))

    res = test_client.post("/api/select-model", json={"model_name": "test.gguf"})
    assert res.status_code == 400
    assert res.json()["detail"] == "❌ Modellwechsel fehlgeschlagen (FileNotFoundError)."
    assert CANARY not in res.text
    assert CANARY not in caplog.text


# ===========================================================================
# AUFGABE 2: Spool-Schwelle, Reihenfolge-Sicherheit & Invariante
# ===========================================================================


def test_spool_threshold_order_safety_and_never_decreases() -> None:
    """Punkt 1: App mit 50 MB, danach App mit 2 MB erzeugen -> spool_max_size >= 50 MB (wird nie gesenkt)."""
    # 1. App mit 50 MB erzeugen
    make_test_app(max_upload_bytes=50 * 1024 * 1024)
    assert starlette.formparsers.MultiPartParser.spool_max_size >= 50 * 1024 * 1024

    # 2. App mit 2 MB erzeugen -> Schwelle darf NICHT auf 2 MB absinken
    make_test_app(max_upload_bytes=2 * 1024 * 1024)
    assert starlette.formparsers.MultiPartParser.spool_max_size >= 50 * 1024 * 1024


def test_configure_spool_threshold_raises_runtime_error_on_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Punkt 1: RuntimeError statt assert wenn Invariante (Limit <= Spool-Schwelle) verletzt wird."""
    class BrokenMeta(type):
        @property
        def spool_max_size(cls):
            return 100

        @spool_max_size.setter
        def spool_max_size(cls, val):
            pass  # Ignoriert Zuweisung

    class BrokenParser(metaclass=BrokenMeta):
        pass

    monkeypatch.setattr(starlette.formparsers, "MultiPartParser", BrokenParser)

    with pytest.raises(RuntimeError) as exc_info:
        configure_spool_threshold(500)
    assert "Invariante verletzt" in str(exc_info.value)


def test_plan_loader_valid_document_without_goals_succeeds(caplog: pytest.LogCaptureFixture, tmp_path) -> None:
    """Gültiges DOCX-Dokument ohne Ziele wirft keine Exception und loggt keine sensiblen Daten."""
    caplog.set_level(logging.DEBUG)
    import docx
    from chat_app.plan_loader import parse_plan_docx

    valid_doc = tmp_path / "valid_empty.docx"
    d = docx.Document()
    d.add_paragraph(f"Hinweistext ohne Formularfelder {CANARY}")
    d.save(valid_doc)

    plan = parse_plan_docx(valid_doc, filename="valid_empty.docx")
    assert plan is not None
    assert plan.goals_count == 0
    assert CANARY not in caplog.text


def test_plan_loader_upfront_bad_zip_detection() -> None:
    """Punkt 6: Kaputte / Nicht-ZIP-Dateien werden vorab explizit als BadZipFile erkannt."""
    import zipfile
    from chat_app.plan_loader import parse_plan_docx

    with pytest.raises(zipfile.BadZipFile):
        parse_plan_docx(f"Kein Zip-Header: {CANARY}".encode("utf-8"))


def test_upload_limits_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prüft Default, Clamping auf Maximalgrenze und Fallback bei ungültigen Werten."""
    monkeypatch.delenv("SOB_MAX_UPLOAD_BYTES", raising=False)
    assert get_max_upload_bytes() == DEFAULT_MAX_UPLOAD_BYTES
    assert get_max_upload_bytes() == 10 * 1024 * 1024

    monkeypatch.setenv("SOB_MAX_UPLOAD_BYTES", str(5 * 1024 * 1024))
    assert get_max_upload_bytes() == 5 * 1024 * 1024

    monkeypatch.setenv("SOB_MAX_UPLOAD_BYTES", "0")
    assert get_max_upload_bytes() == DEFAULT_MAX_UPLOAD_BYTES
    monkeypatch.setenv("SOB_MAX_UPLOAD_BYTES", "-500")
    assert get_max_upload_bytes() == DEFAULT_MAX_UPLOAD_BYTES

    monkeypatch.setenv("SOB_MAX_UPLOAD_BYTES", "ungueltig")
    assert get_max_upload_bytes() == DEFAULT_MAX_UPLOAD_BYTES

    monkeypatch.setenv("SOB_MAX_UPLOAD_BYTES", str(2 * 1024 * 1024 * 1024))  # 2 GB
    assert get_max_upload_bytes() == HARD_CEILING_MAX_UPLOAD_BYTES
    assert get_max_upload_bytes() == 50 * 1024 * 1024


def test_format_limit_mb() -> None:
    """Prüft lesbare MB-Formatierung."""
    assert format_limit_mb(10 * 1024 * 1024) == "10 MB"
    assert format_limit_mb(2 * 1024 * 1024) == "2 MB"
    assert format_limit_mb(int(2.5 * 1024 * 1024)) == "2.5 MB"


# ===========================================================================
# AUFGABE 2: Beweise (Positivtest, Gegenprobe, Grenzwerte, Validierung)
# ===========================================================================


def test_proof_under_limit_no_disk_rollover(monkeypatch: pytest.MonkeyPatch) -> None:
    """Beweis (a): Upload knapp unter dem Limit (1.5 MB bei 2 MB Limit > 1 MB Starlette-Default) bleibt garantiert im RAM.

    Zeigt:
    - Status 200 (Erfolg)
    - Mindestens eine SpooledTemporaryFile-Instanz wird tatsächlich von Starlette angelegt
    - Alle angelegten SpooledTemporaryFile-Instanzen haben _rolled is False
    - tempfile.SpooledTemporaryFile.rollover() wird NICHT aufgerufen
    - Keine neue Datei entsteht im System-Tempverzeichnis
    """
    limit_2mb = 2 * 1024 * 1024
    monkeypatch.setenv("SOB_MAX_UPLOAD_BYTES", str(limit_2mb))

    app = make_test_app()
    test_client = TestClient(app)

    created_spools: list[tempfile.SpooledTemporaryFile] = []
    orig_init = tempfile.SpooledTemporaryFile.__init__

    def wrapped_init(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        created_spools.append(self)

    monkeypatch.setattr(tempfile.SpooledTemporaryFile, "__init__", wrapped_init)

    rollover_called = False
    orig_rollover = tempfile.SpooledTemporaryFile.rollover

    def mock_rollover(self):
        nonlocal rollover_called
        rollover_called = True
        return orig_rollover(self)

    monkeypatch.setattr(tempfile.SpooledTemporaryFile, "rollover", mock_rollover)

    temp_dir = tempfile.gettempdir()
    files_before = set(os.listdir(temp_dir))

    # 1.5 MB Testdaten (> 1 MB Starlette Default, < 2 MB konfiguriertes Limit)
    payload_lines = [f"01.01.2025;Eintrag;Testdaten {CANARY} {i}" for i in range(30000)]
    csv_text = "Datum;Kategorie;Notiz\n" + "\n".join(payload_lines)
    csv_bytes = csv_text.encode("utf-8")
    assert 1.2 * 1024 * 1024 < len(csv_bytes) < 1.9 * 1024 * 1024

    res = test_client.post(
        "/api/upload",
        files={"file": ("grosser_verlauf.csv", io.BytesIO(csv_bytes), "text/csv")},
    )

    assert res.status_code == 200
    assert res.json()["success"] is True

    # Punkt 3: SpooledTemporaryFile-Instanzen einfangen und _rolled prüfen
    assert len(created_spools) >= 1, "Starlette hat keine SpooledTemporaryFile-Instanz angelegt!"
    assert all(s._rolled is False for s in created_spools), "Mindestens eine SpooledTemporaryFile ist auf Disk gerollt!"
    assert not rollover_called, "SpooledTemporaryFile.rollover wurde aufgerufen!"

    files_after = set(os.listdir(temp_dir))
    new_files = files_after - files_before
    assert not new_files, f"Neue Dateien im Temp-Verzeichnis entdeckt: {new_files}"


def test_counterproof_rollover_occurs_without_spool_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Punkt 3 (Gegenprobe im echten Upload-Pfad):

    Zeigt, dass derselbe ~1.5 MB Upload OHNE die Spool-Anpassung (MultiPartParser.spool_max_size
    auf Starlette-Default 1 MB fixiert, Limit 2 MB) tatsächlich rollover() auslöst.
    Beweist, dass der Positiv-Test nicht trivial grün ist.
    """
    limit_2mb = 2 * 1024 * 1024
    app = make_test_app(max_upload_bytes=limit_2mb)
    test_client = TestClient(app)

    # MultiPartParser-Schwelle künstlich auf Starlettes ungesicherten Default (1 MB) zurücksetzen
    monkeypatch.setattr(starlette.formparsers.MultiPartParser, "spool_max_size", 1024 * 1024)

    created_spools: list[tempfile.SpooledTemporaryFile] = []
    orig_init = tempfile.SpooledTemporaryFile.__init__

    def wrapped_init(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        created_spools.append(self)

    monkeypatch.setattr(tempfile.SpooledTemporaryFile, "__init__", wrapped_init)

    rollover_called = False
    orig_rollover = tempfile.SpooledTemporaryFile.rollover

    def mock_rollover(self):
        nonlocal rollover_called
        rollover_called = True
        return orig_rollover(self)

    monkeypatch.setattr(tempfile.SpooledTemporaryFile, "rollover", mock_rollover)

    # 1.5 MB Testdaten
    payload_lines = [f"01.01.2025;Eintrag;Testdaten {CANARY} {i}" for i in range(30000)]
    csv_text = "Datum;Kategorie;Notiz\n" + "\n".join(payload_lines)
    csv_bytes = csv_text.encode("utf-8")
    assert 1.2 * 1024 * 1024 < len(csv_bytes) < 1.9 * 1024 * 1024

    res = test_client.post(
        "/api/upload",
        files={"file": ("grosser_verlauf.csv", io.BytesIO(csv_bytes), "text/csv")},
    )

    assert res.status_code == 200
    assert len(created_spools) >= 1
    # Ohne Spool-Anpassung muss rollover() ausgelöst worden sein:
    assert rollover_called is True, "Gegenprobe fehlgeschlagen: rollover() wurde trotz 1 MB Spool-Schwelle nicht aufgerufen!"
    assert any(s._rolled is True for s in created_spools), "Keine Spool-Instanz wurde als gerollt markiert!"


def test_proof_exact_boundary_body_equals_limit_and_limit_plus_one() -> None:
    """Punkt 4: Grenzfall exakt == Limit -> 200, kein Rollover; Body == Limit + 1 -> 413 ohne Leck."""
    limit = 60 * 1024  # 60 KB
    app = make_test_app(max_upload_bytes=limit)
    test_client = TestClient(app)

    boundary = "---------------------------boundaryExact4711"
    header_prefix = f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"grenze.csv\"\r\nContent-Type: text/csv\r\n\r\n".encode("utf-8")
    suffix = f"\r\n--{boundary}--\r\n".encode("utf-8")

    prefix_csv = f"Datum;Kategorie;Notiz\n01.01.2025;Test;{CANARY}_".encode("utf-8")
    fixed_len = len(header_prefix) + len(prefix_csv) + len(suffix)
    needed = limit - fixed_len
    assert needed > 0

    # 1. Exakt == Limit
    body_exact = header_prefix + prefix_csv + (b"A" * needed) + suffix
    assert len(body_exact) == limit

    res_exact = test_client.post(
        "/api/upload",
        content=body_exact,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert res_exact.status_code == 200, f"Fehler bei exaktem Limit: {res_exact.text}"
    assert res_exact.json()["success"] is True

    # 2. Exakt == Limit + 1
    body_plus_1 = header_prefix + prefix_csv + (b"A" * (needed + 1)) + suffix
    assert len(body_plus_1) == limit + 1

    res_plus_1 = test_client.post(
        "/api/upload",
        content=body_plus_1,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert res_plus_1.status_code == 413
    assert res_plus_1.json()["detail"] == f"❌ Datei zu groß (max. {format_limit_mb(limit)})."
    assert CANARY not in res_plus_1.text


def test_invalid_and_negative_content_length_rejected(caplog: pytest.LogCaptureFixture) -> None:
    """Punkt 5: Negativer oder ungültiger Content-Length liefert 400 '❌ Ungültige Anfrage.' statt stiller Fallback."""
    caplog.set_level(logging.DEBUG)
    app = make_test_app(max_upload_bytes=10 * 1024 * 1024)
    test_client = TestClient(app)

    # 1. Negativer Content-Length
    res_neg = test_client.post(
        "/api/upload",
        content=b"irgendein inhalt",
        headers={"Content-Length": "-10", "Content-Type": "multipart/form-data; boundary=b"},
    )
    assert res_neg.status_code == 400
    assert res_neg.json()["detail"] == "❌ Ungültige Anfrage."

    # 2. Nicht-numerischer Content-Length
    res_invalid = test_client.post(
        "/api/upload",
        content=b"irgendein inhalt",
        headers={"Content-Length": "ungueltig", "Content-Type": "multipart/form-data; boundary=b"},
    )
    assert res_invalid.status_code == 400
    assert res_invalid.json()["detail"] == "❌ Ungültige Anfrage."


def test_proof_over_limit_content_length_rejected(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Beweis (b1): Upload über dem Limit wird vorab mit 413 abgewiesen ohne Rollover oder Inhaltsleck."""
    caplog.set_level(logging.DEBUG)
    limit_2mb = 2 * 1024 * 1024
    monkeypatch.setenv("SOB_MAX_UPLOAD_BYTES", str(limit_2mb))

    app = make_test_app()
    test_client = TestClient(app)

    rollover_called = False
    orig_rollover = tempfile.SpooledTemporaryFile.rollover

    def mock_rollover(self):
        nonlocal rollover_called
        rollover_called = True
        return orig_rollover(self)

    monkeypatch.setattr(tempfile.SpooledTemporaryFile, "rollover", mock_rollover)

    payload_lines = [f"01.01.2025;Eintrag;Testdaten {CANARY} {i}" for i in range(55000)]
    csv_text = "Datum;Kategorie;Notiz\n" + "\n".join(payload_lines)
    csv_bytes = csv_text.encode("utf-8")
    assert len(csv_bytes) > 2.2 * 1024 * 1024

    res = test_client.post(
        "/api/upload",
        files={"file": ("zu_gross.csv", io.BytesIO(csv_bytes), "text/csv")},
    )

    assert res.status_code == 413
    assert res.json()["detail"] == "❌ Datei zu groß (max. 2 MB)."
    assert CANARY not in res.text
    assert CANARY not in caplog.text
    assert not rollover_called


def test_proof_over_limit_chunked_streaming_rejected(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Beweis (b2): Chunked-Streaming ohne Content-Length bricht während des Empfangs bei Limit-Überschreitung mit 413 ab."""
    caplog.set_level(logging.DEBUG)
    limit_2mb = 2 * 1024 * 1024
    monkeypatch.setenv("SOB_MAX_UPLOAD_BYTES", str(limit_2mb))

    app = make_test_app()
    test_client = TestClient(app)

    rollover_called = False
    orig_rollover = tempfile.SpooledTemporaryFile.rollover

    def mock_rollover(self):
        nonlocal rollover_called
        rollover_called = True
        return orig_rollover(self)

    monkeypatch.setattr(tempfile.SpooledTemporaryFile, "rollover", mock_rollover)

    def chunk_generator() -> Generator[bytes, None, None]:
        boundary = "---------------------------boundary98765"
        yield f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"stream.csv\"\r\nContent-Type: text/csv\r\n\r\nDatum;Notiz\n".encode("utf-8")
        chunk = (f"01.01.2025;{CANARY}\n" * 4000).encode("utf-8")
        for _ in range(25):
            yield chunk
        yield f"\r\n--{boundary}--\r\n".encode("utf-8")

    res = test_client.post(
        "/api/upload",
        content=chunk_generator(),
        headers={"Content-Type": "multipart/form-data; boundary=---------------------------boundary98765"},
    )

    assert res.status_code == 413
    assert res.json()["detail"] == "❌ Datei zu groß (max. 2 MB)."
    assert CANARY not in res.text
    assert CANARY not in caplog.text
    assert not rollover_called


def test_proof_rollover_detector_sanity() -> None:
    """Gegenprobe (c): Zeigt, dass der Rollover-Detektor empfindlich anschlägt, wenn SpooledTemporaryFile wirklich überläuft."""
    rollover_detected = False
    orig_rollover = tempfile.SpooledTemporaryFile.rollover

    def detector(self):
        nonlocal rollover_detected
        rollover_detected = True
        return orig_rollover(self)

    try:
        tempfile.SpooledTemporaryFile.rollover = detector

        spooled = tempfile.SpooledTemporaryFile(max_size=100)
        assert not rollover_detected
        assert not spooled._rolled

        spooled.write(f"Überlauf-Füllmaterial: {CANARY}".encode("utf-8") * 10)
        assert rollover_detected, "Gegenprobe fehlgeschlagen: Rollover-Detektor hat nicht ausgelöst!"
        assert spooled._rolled, "SpooledTemporaryFile wurde nicht auf Disk ausgelagert!"
    finally:
        tempfile.SpooledTemporaryFile.rollover = orig_rollover


def test_guard_blocks_real_gguf_loading_in_tests() -> None:
    """Issue #140: Stellt sicher, dass LlamaCppProvider in Tests durch den Guard deterministisch blockiert wird."""
    with pytest.raises(RuntimeError, match="Echtes GGUF-Modell darf in Unit-Tests nicht geladen werden"):
        LlamaCppProvider("models/fake.gguf")


def test_create_llm_provider_under_pytest_defaults_to_reflection_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #140: create_llm_provider weicht unter pytest mit SOB_ALLOW_HEURISTIC=1 auf ReflectionEngine aus."""
    monkeypatch.setenv("SOB_ALLOW_HEURISTIC", "1")
    provider = create_llm_provider()
    assert isinstance(provider, ReflectionEngine)


def test_upload_plan_doctype_rejected_400(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    """DOCX mit DOCTYPE wird mit 400 '❌ Datei abgelehnt: ungültiges oder zu großes Word-Dokument' abgewiesen."""
    caplog.set_level(logging.DEBUG)
    from tests.test_docx_safety import make_test_docx

    xml_with_doctype = f"""<?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE w:document [
      <!ENTITY xxe SYSTEM "file:///etc/passwd">
    ]>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body><w:p><w:r><w:t>{CANARY} &xxe;</w:t></w:r></w:p></w:body>
    </w:document>
    """
    docx_bytes = make_test_docx(xml_with_doctype)
    files = {"file": ("geheim_plan.docx", io.BytesIO(docx_bytes), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}

    res = client.post("/api/upload-plan", files=files)
    assert res.status_code == 400
    assert res.json()["detail"] == "❌ Datei abgelehnt: ungültiges oder zu großes Word-Dokument"
    assert CANARY not in res.text
    assert "geheim_plan" not in caplog.text
    assert CANARY not in caplog.text


def test_upload_plan_zip_bomb_rejected_400(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    """ZIP-Bombe im Plan-Upload wird mit 400 abgewiesen, ohne Server-Absturz oder Leck."""
    caplog.set_level(logging.DEBUG)
    from tests.test_docx_safety import CONTENT_TYPES_XML, RELS_XML
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES_XML)
        z.writestr("_rels/.rels", RELS_XML)
        with z.open("word/document.xml", "w") as f:
            chunk = b"\x00" * (10 * 1024 * 1024)
            for _ in range(100):
                f.write(chunk)
    bomb_bytes = buf.getvalue()

    files = {"file": ("bomb.docx", io.BytesIO(bomb_bytes), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}
    res = client.post("/api/upload-plan", files=files)
    assert res.status_code == 400
    assert res.json()["detail"] == "❌ Datei abgelehnt: ungültiges oder zu großes Word-Dokument"


def test_upload_plan_timeout_rejected_400(client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Timeout beim Parsen (SOB_DOCX_PARSE_TIMEOUT) liefert 400 mit derselben Fehlermeldung."""
    import time
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("SOB_DOCX_PARSE_TIMEOUT", "0.05")

    def slow_parse(*args, **kwargs):
        time.sleep(0.2)
        raise RuntimeError("should not reach here")

    monkeypatch.setattr("chat_app.app.parse_plan_docx", slow_parse)
    from tests.test_docx_safety import make_test_docx
    docx_bytes = make_test_docx("<w:document/>")

    files = {"file": ("slow.docx", io.BytesIO(docx_bytes), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}
    res = client.post("/api/upload-plan", files=files)
    assert res.status_code == 400
    assert res.json()["detail"] == "❌ Datei abgelehnt: ungültiges oder zu großes Word-Dokument"
    assert "timeout" in caplog.text.lower()

