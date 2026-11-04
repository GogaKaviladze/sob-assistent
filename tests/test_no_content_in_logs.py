"""M2- und Legal-Abnahmetests für das Kontext-Redesign (Issue #18).

Verifiziert:
1. Keine Falldaten, Klientennamen, Dateinamen oder Exception-Messages in Logs, stdout/stderr oder Fehler-Responses (M2).
2. Statischer Guard: Kein `logger.exception(`, `exc_info=True`, `traceback.print_exc` oder `traceback.format_exc` im Produktionscode.
3. Legal-Prüfungen:
   - UI-Hinweise ("Nur berichtsrelevante Inhalte eingeben", "KI-Entwurf", kein "Unter uns")
   - Export-Freiheit von Warn-/Fehlermarkern (❌, ⚠️, ℹ️, [Fehler, gekürzt, nicht mehr im Modellkontext) in Markdown und DOCX
   - Log-Trunkierung bei `/api/reset` auf exakt eine Statuszeile (`Log gelöscht: anlass=reset zeilen=...`)
"""
from __future__ import annotations

import io
import json
import logging
import urllib.parse
from pathlib import Path
from typing import Dict, Generator, List

import docx
import pytest
from fastapi.testclient import TestClient

from chat_app.app import create_app
from chat_app.chatbot import LLMProvider, ReflectionChatbot, get_default_n_ctx
from tests.fixtures.make_synthetic_vorbericht import build_hamburg_vorbericht
from tests.test_docx_safety import make_test_docx

FORBIDDEN_MARKERS = (
    "ZZ_SYNTH_SECRET",
    "ZZ_SYNTH_",
    "Peter",
    "Hans",
    "Synthetischer Klient",
    "ZZ_SYNTH_geheim_fall.csv",
    "ZZ_SYNTH_geheim_plan.docx",
)


class ConfigurableMockLLM(LLMProvider):
    """In-Memory Mock-Provider mit konfigurierbaren Fehlern für M2- und Legal-Tests."""

    model_name: str = "ConfigurableMockLLM"
    is_real_llm: bool = True
    details: str = "Synthetischer Test-Provider"
    max_tokens: int = 128

    def __init__(
        self,
        fail_stream: bool = False,
        fail_count_tokens: bool = False,
        reply: str = (
            "### Bereich 1.4 – Emotionale und psychische Entwicklung\n"
            "**Bisherige Zielerreichung:**\n"
            "Die vereinbarten Schritte wurden im Alltag stabil umgesetzt.\n"
            "**Vorschlag neue Ziele:**\n"
            "Selbstständige Tagesstruktur weiter festigen.\n"
            "**Vorschlag Maßnahmen:**\n"
            "Wöchentliche Reflexionsgespräche fortführen."
        ),
    ):
        self.fail_stream = fail_stream
        self.fail_count_tokens = fail_count_tokens
        self.reply = reply
        self._call_count_tokens = 0

    @property
    def n_ctx(self) -> int:
        return get_default_n_ctx()

    def count_tokens(self, text: str) -> int:
        if not text:
            return 0
        # "test" ist der interne Probeaufruf von has_tokenizer(provider)
        if text != "test" and self.fail_count_tokens:
            raise RuntimeError("ZZ_SYNTH_SECRET Peter Hans Synthetischer Klient")
        return len(text.split())

    def generate(self, messages: List[Dict[str, str]]) -> str:
        if self.fail_stream:
            raise RuntimeError("ZZ_SYNTH_SECRET Peter Hans Synthetischer Klient")
        return self.reply

    def generate_stream(self, messages: List[Dict[str, str]]) -> Generator[str, None, None]:
        if self.fail_stream:
            raise RuntimeError("ZZ_SYNTH_SECRET Peter Hans Synthetischer Klient")
        yield self.reply

    def reset(self) -> None:
        pass


@pytest.fixture
def isolated_log_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
):
    """Leitet LOG_FILE/LOG_DIR nach tmp_path um und aktiviert volles DEBUG-Capturing auf Root- und chat_app-Logger."""
    log_dir = tmp_path / "logs"
    log_file = log_dir / "chat_app.log"
    monkeypatch.setattr("chat_app.logger.LOG_DIR", log_dir)
    monkeypatch.setattr("chat_app.logger.LOG_FILE", log_file)

    root_logger = logging.getLogger()
    old_root_level = root_logger.level
    app_logger = logging.getLogger("chat_app")
    old_app_level = app_logger.level
    old_app_handlers = list(app_logger.handlers)

    root_logger.setLevel(logging.DEBUG)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="chat_app")
    capsys.readouterr()

    try:
        yield log_file
    finally:
        for h in list(app_logger.handlers):
            if h not in old_app_handlers:
                try:
                    h.close()
                except Exception:
                    pass
        app_logger.handlers.clear()
        for h in old_app_handlers:
            app_logger.addHandler(h)
        app_logger.setLevel(old_app_level)
        root_logger.setLevel(old_root_level)


def _assert_no_markers_leaked(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    response_text: str = "",
) -> None:
    """Prüft für alle Log-Records, stdout/stderr und den HTTP-Response-Body auf verbotene Marker und Tracebacks."""
    for record in caplog.records:
        msg = record.getMessage()
        exc_txt = record.exc_text or ""
        args_str = str(record.args)
        assert record.exc_info is None, f"exc_info darf niemals gesetzt sein: {record}"
        for marker in FORBIDDEN_MARKERS:
            assert marker not in msg, f"Marker '{marker}' in record.getMessage(): {msg}"
            assert marker not in exc_txt, f"Marker '{marker}' in record.exc_text: {exc_txt}"
            assert marker not in args_str, f"Marker '{marker}' in str(record.args): {args_str}"

    captured = capsys.readouterr()
    for marker in FORBIDDEN_MARKERS:
        assert marker not in captured.out, f"Marker '{marker}' in stdout: {captured.out}"
        assert marker not in captured.err, f"Marker '{marker}' in stderr: {captured.err}"
        if response_text:
            assert marker not in response_text, f"Marker '{marker}' im HTTP-Response-Body: {response_text}"


# ===========================================================================
# 2. M2: Keine Inhalte in Logs, stdout/stderr oder Fehlermeldungen
# ===========================================================================


def test_m2_generate_stream_exception_no_leak(
    isolated_log_env: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """(a) provider.generate_stream wirft RuntimeError mit Marker -> SSE '❌ Fehler bei der Modell-Generierung (RuntimeError)', kein Leck."""
    provider = ConfigurableMockLLM(fail_stream=True)
    bot = ReflectionChatbot(provider=provider)
    app = create_app(chatbot=bot)
    client = TestClient(app)

    res = client.post(
        "/api/chat-stream",
        json={"message": "Frage zu ZZ_SYNTH_SECRET für Peter Hans (Synthetischer Klient)"},
    )
    assert res.status_code == 200
    assert "❌ Fehler bei der Modell-Generierung (RuntimeError)" in res.text
    _assert_no_markers_leaked(caplog, capsys, res.text)


def test_m2_count_tokens_exception_no_leak(
    isolated_log_env: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """(b) provider.count_tokens wirft RuntimeError mit Marker -> SSE '❌ Fehler bei der Kontext-Erstellung (RuntimeError)', kein Leck."""
    provider = ConfigurableMockLLM(fail_count_tokens=True)
    bot = ReflectionChatbot(provider=provider)
    app = create_app(chatbot=bot)
    client = TestClient(app)

    res = client.post(
        "/api/chat-stream",
        json={"message": "Frage zu ZZ_SYNTH_SECRET für Peter Hans"},
    )
    assert res.status_code == 200
    assert "❌ Fehler bei der Kontext-Erstellung (RuntimeError)" in res.text
    _assert_no_markers_leaked(caplog, capsys, res.text)


def test_m2_broken_docx_bytes_no_leak(
    isolated_log_env: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """(c) Kaputte DOCX-Bytes (b'PK\\x03\\x04ZZ_SYNTH_SECRET...') -> 400 ohne Inhalts- oder Dateinamensleck."""
    bot = ReflectionChatbot(provider=ConfigurableMockLLM())
    app = create_app(chatbot=bot)
    client = TestClient(app)

    corrupt_docx = b"PK\x03\x04ZZ_SYNTH_SECRET Peter Hans Synthetischer Klient corrupt archive"
    res = client.post(
        "/api/upload-plan",
        files={
            "file": (
                "ZZ_SYNTH_geheim_plan.docx",
                io.BytesIO(corrupt_docx),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert detail.startswith("❌")
    _assert_no_markers_leaked(caplog, capsys, res.text)


def test_m2_docx_with_doctype_rejected_400_no_leak(
    isolated_log_env: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """(d) DOCX mit DOCTYPE -> 400 '❌ Datei abgelehnt: ungültiges oder zu großes Word-Dokument', kein Leck."""
    bot = ReflectionChatbot(provider=ConfigurableMockLLM())
    app = create_app(chatbot=bot)
    client = TestClient(app)

    xml_with_doctype = """<?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE w:document [
      <!ENTITY xxe SYSTEM "file:///etc/passwd">
    ]>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body><w:p><w:r><w:t>ZZ_SYNTH_SECRET Peter Hans Synthetischer Klient &xxe;</w:t></w:r></w:p></w:body>
    </w:document>
    """
    docx_bytes = make_test_docx(xml_with_doctype)
    res = client.post(
        "/api/upload-plan",
        files={
            "file": (
                "ZZ_SYNTH_geheim_plan.docx",
                io.BytesIO(docx_bytes),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert res.status_code == 400
    assert res.json()["detail"] == "❌ Datei abgelehnt: ungültiges oder zu großes Word-Dokument"
    _assert_no_markers_leaked(caplog, capsys, res.text)


def test_m2_broken_or_unparsable_csv_rejected_400_no_leak(
    isolated_log_env: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(e) Unparsbare / kaputte CSV mit Marker -> 400 ohne Leck von Zellen, Klientennamen oder Dateinamen."""
    bot = ReflectionChatbot(provider=ConfigurableMockLLM())
    app = create_app(chatbot=bot)
    client = TestClient(app)

    # 1. Ungültige CSV ohne Dokumentationsspalten mit geheimem Marker (echter ValueError aus parse_csv_content)
    unparsable_csv = (
        "SpalteA;SpalteB\n"
        "ZZ_SYNTH_SECRET;Peter Hans Synthetischer Klient\n"
    ).encode("utf-8")
    res_unparsable = client.post(
        "/api/upload",
        files={"file": ("ZZ_SYNTH_geheim_fall.csv", io.BytesIO(unparsable_csv), "text/csv")},
    )
    assert res_unparsable.status_code == 400
    assert res_unparsable.json()["detail"].startswith("❌")
    _assert_no_markers_leaked(caplog, capsys, res_unparsable.text)

    # 2. CSV mit Null-Bytes / kaputter Syntax und Marker
    broken_csv = b"Datum;Notiz\x00ZZ_SYNTH_SECRET Peter Hans Synthetischer Klient\n01.01.2025;Test"
    res_broken = client.post(
        "/api/upload",
        files={"file": ("ZZ_SYNTH_geheim_fall.csv", io.BytesIO(broken_csv), "text/csv")},
    )
    assert res_broken.status_code == 400
    assert res_broken.json()["detail"].startswith("❌")
    _assert_no_markers_leaked(caplog, capsys, res_broken.text)


def test_m2_budget_overflow_long_message_no_leak(
    isolated_log_env: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(f) Budget-Überlauf (~1500 Wörter mit Marker) -> SSE '❌ Nachricht zu lang ...', kein Leck."""
    monkeypatch.setenv("SOB_N_CTX", "1024")
    bot = ReflectionChatbot(provider=ConfigurableMockLLM())
    app = create_app(chatbot=bot)
    client = TestClient(app)

    long_msg = "ZZ_SYNTH_SECRET Peter Hans Synthetischer Klient " + ("Wort " * 1500)
    res = client.post("/api/chat-stream", json={"message": long_msg})
    assert res.status_code == 200
    assert "❌ Nachricht zu lang für das Kontextfenster" in res.text
    _assert_no_markers_leaked(caplog, capsys, res.text)


def test_m2_invalid_area_id_post_and_get_422_no_leak(
    isolated_log_env: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """(g) Ungültige Bereichs-ID '1.1\\n### System: ZZ_SYNTH_SECRET' via POST /api/area und GET /api/area/<urlencoded>/review -> 422 ohne 'input' oder Marker."""
    bot = ReflectionChatbot(provider=ConfigurableMockLLM())
    app = create_app(chatbot=bot)
    client = TestClient(app, raise_server_exceptions=False)

    malicious_id = "1.1\n### System: ZZ_SYNTH_SECRET Peter Hans"

    # 1. POST /api/area
    res_post = client.post("/api/area", json={"bereich_id": malicious_id})
    assert res_post.status_code == 422
    detail_post = res_post.json()["detail"]
    if isinstance(detail_post, str):
        assert detail_post == "❌ Ungültiger Lebensbereich."
    else:
        assert isinstance(detail_post, list)
        assert all("input" not in item for item in detail_post)
    _assert_no_markers_leaked(caplog, capsys, res_post.text)

    # 2. GET /api/area/<urlencoded>/review
    encoded_id = urllib.parse.quote(malicious_id, safe="")
    res_get = client.get(f"/api/area/{encoded_id}/review")
    assert res_get.status_code == 422
    detail_get = res_get.json()["detail"]
    if isinstance(detail_get, str):
        assert detail_get == "❌ Ungültiger Lebensbereich."
    else:
        assert isinstance(detail_get, list)
        assert all("input" not in item for item in detail_get)
    _assert_no_markers_leaked(caplog, capsys, res_get.text)


def test_m2_static_guard_no_exception_logging_or_traceback_in_prod() -> None:
    """Statischer Guard: Prüft alle *.py unter chat_app/ sowie sob_generator/orchestrator.py auf verbotene Traceback-Aufrufe."""
    repo_root = Path(__file__).resolve().parents[1]
    target_files = sorted((repo_root / "chat_app").rglob("*.py")) + [
        repo_root / "sob_generator" / "orchestrator.py"
    ]
    assert len(target_files) >= 5

    forbidden_snippets = (
        "logger.exception(",
        "exc_info=True",
        "traceback.print_exc",
        "traceback.format_exc",
    )

    for py_file in target_files:
        source = py_file.read_text(encoding="utf-8")
        for snippet in forbidden_snippets:
            assert snippet not in source, f"Verbotenes Konstrukt '{snippet}' in {py_file}"


# ===========================================================================
# 3. Legal-Prüfungen (UI-Hinweise, Export-Reinheit, Log-Trunkierung bei Reset)
# ===========================================================================


def test_legal_index_html_hints_and_no_unter_uns() -> None:
    """GET / enthält 'Nur berichtsrelevante Inhalte eingeben' und 'KI-Entwurf', und enthält nicht 'Unter uns'."""
    bot = ReflectionChatbot(provider=ConfigurableMockLLM())
    app = create_app(chatbot=bot)
    client = TestClient(app)

    res = client.get("/")
    assert res.status_code == 200
    html = res.text
    assert "Nur berichtsrelevante Inhalte eingeben" in html
    assert "KI-Entwurf" in html
    assert "Unter uns" not in html


def test_legal_exports_contain_no_warnings_or_error_markers(
    isolated_log_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nach einem Ablauf mit Kürzung (⚠️) und Preflight-Fehler (❌) enthalten weder /api/export noch /api/export/docx Warn- oder Fehlermarker."""
    monkeypatch.setenv("SOB_N_CTX", "1024")
    reply_2_2 = (
        "### Bereich 2.2 – Teilnahme am kulturellen und gesellschaftlichen Leben\n"
        "**Bisherige Zielerreichung:**\n"
        "Regelmäßige Teilnahme an der Sportgruppe wurde erreicht.\n"
        "**Vorschlag neue Ziele:**\n"
        "Eigenständige Anfahrt zu Vereinsaktivitäten sichern.\n"
        "**Vorschlag Maßnahmen:**\n"
        "Begleitung bei neuen Freizeitangeboten fortsetzen."
    )
    bot = ReflectionChatbot(provider=ConfigurableMockLLM(reply=reply_2_2))
    app = create_app(chatbot=bot)
    client = TestClient(app)

    # 1. Synthetische CSV und Hamburger Vorbericht laden
    csv_bytes = Path("tests/fixtures/hans_peter_synthetisch.csv").read_bytes()
    res_csv = client.post(
        "/api/upload",
        files={"file": ("hans_peter_synthetisch.csv", csv_bytes, "text/csv")},
    )
    assert res_csv.status_code == 200

    docx_bytes = build_hamburg_vorbericht()
    res_plan = client.post(
        "/api/upload-plan",
        files={
            "file": (
                "vorbericht_synth.docx",
                docx_bytes,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert res_plan.status_code == 200

    # 2. Bereich 2.2 wählen, bestätigen und chatten (löst bei n_ctx=1024 Doku-Kürzung und ⚠️ warning_notices aus)
    assert client.post("/api/area", json={"bereich_id": "2.2"}).status_code == 200
    assert client.post("/api/area/confirm", json={"bereich_id": "2.2"}).status_code == 200

    res_chat = client.post(
        "/api/chat-stream",
        json={"message": "Wie hat sich die gesellschaftliche Teilhabe entwickelt?"},
    )
    assert res_chat.status_code == 200
    sse_events = [
        json.loads(line.removeprefix("data: "))
        for line in res_chat.text.splitlines()
        if line.startswith("data: ")
    ]
    done_ev = next(ev for ev in sse_events if ev.get("done") is True)
    assert len(done_ev["context_status"]["warning_notices"]) > 0

    # 3. Mehrere Runden anhängen, bis auch dropped_messages > 0 auftritt ("nicht mehr im Modellkontext")
    last_round_res = None
    for idx in range(6):
        long_turn = f"Beobachtung Runde {idx}: " + ("Alltagsreflexion " * 45)
        r = client.post("/api/chat-stream", json={"message": long_turn})
        assert r.status_code == 200
        last_round_res = r

    assert last_round_res is not None
    last_sse_events = [
        json.loads(line.removeprefix("data: "))
        for line in last_round_res.text.splitlines()
        if line.startswith("data: ")
    ]
    last_done_ev = next(ev for ev in last_sse_events if ev.get("done") is True)
    assert last_done_ev["context_status"]["dropped_messages"] > 0

    # 4. Preflight-Fehler (❌ Nachricht zu lang) auslösen
    res_err = client.post("/api/chat-stream", json={"message": "Überlang " * 1500})
    assert res_err.status_code == 200
    assert "❌ Nachricht zu lang" in res_err.text

    # 5. Exporte abrufen, auf tatsächlichen Chat-Inhalt prüfen und verbotene Marker ausschließen
    expected_chat_snippet = "Regelmäßige Teilnahme an der Sportgruppe"
    forbidden_export_strings = (
        "❌",
        "⚠️",
        "ℹ️",
        "[Fehler",
        "gekürzt",
        "nicht mehr im Modellkontext",
    )

    res_md = client.get("/api/export")
    assert res_md.status_code == 200
    md_text = res_md.json()["markdown"]
    assert expected_chat_snippet in md_text
    for marker in forbidden_export_strings:
        assert marker not in md_text, f"Verbotener Marker '{marker}' im Markdown-Export gefunden!"

    res_docx = client.get("/api/export/docx?template=hamburg")
    assert res_docx.status_code == 200
    doc = docx.Document(io.BytesIO(res_docx.content))
    docx_texts: List[str] = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                docx_texts.append(cell.text)
    full_docx_text = "\n".join(docx_texts)

    assert expected_chat_snippet in full_docx_text
    for marker in forbidden_export_strings:
        assert marker not in full_docx_text, f"Verbotener Marker '{marker}' im DOCX-Export gefunden!"


def test_legal_log_file_truncated_to_single_line_on_reset(
    isolated_log_env: Path,
) -> None:
    """Log-Datei unter tmp_path: Nach einigen Log-Zeilen kürzt POST /api/reset die Datei auf genau eine Zeile 'Log gelöscht: anlass=reset zeilen=...'."""
    log_file = isolated_log_env
    app_logger = logging.getLogger("chat_app")

    # Initiale Datei mit einem FileHandler verbinden und mehrere Zeilen schreiben
    log_file.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_file, encoding="utf-8")
    app_logger.addHandler(fh)
    app_logger.info("Technische Statuszeile 1")
    app_logger.info("Technische Statuszeile 2")
    app_logger.info("Technische Statuszeile 3")
    fh.flush()

    lines_before = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines_before) == 3

    bot = ReflectionChatbot(provider=ConfigurableMockLLM())
    app = create_app(chatbot=bot)
    client = TestClient(app)

    res = client.post("/api/reset")
    assert res.status_code == 200

    lines_after = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines_after) == 1
    assert lines_after[0].startswith("Log gelöscht: anlass=reset zeilen=")
    assert lines_after[0] == "Log gelöscht: anlass=reset zeilen=3"
