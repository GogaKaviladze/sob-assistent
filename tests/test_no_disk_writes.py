"""M1-Sicherheits-Gerüst: Nachweis der vollständigen RAM-Exklusivität und Zero-Persistence (Issue #133)."""
from __future__ import annotations

import io
import logging
import os
import pathlib
import shutil
import sys
import tempfile
from typing import Any, Dict, Generator, List
import pytest
from starlette.testclient import TestClient

from chat_app.app import create_app
from chat_app.chatbot import LLMProvider, ReflectionChatbot, get_default_n_ctx
from chat_app.logger import truncate_log_file as real_truncate_log_file
from tests.fixtures.make_synthetic_vorbericht import build_hamburg_vorbericht


_AUDIT_HOOK_ACTIVE = False
_HOOK_REGISTERED = False
_VIOLATIONS: List[str] = []
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_ALLOWED_READ_PREFIXES = tuple(
    str((_REPO_ROOT / rel).resolve()) + os.sep
    for rel in ("templates", "models", "tests/fixtures", "chat_app/templates")
)


def _is_disallowed_repo_read(file_arg: Any) -> bool:
    if isinstance(file_arg, int):
        return False
    try:
        p_str = os.fspath(file_arg)
        if isinstance(p_str, bytes):
            p_str = p_str.decode("utf-8", errors="replace")
        resolved = str(pathlib.Path(p_str).resolve())
    except Exception:
        return False
    repo_prefix = str(_REPO_ROOT) + os.sep
    if not resolved.startswith(repo_prefix):
        return False
    if resolved.endswith(".py") or resolved.endswith(".pyc") or (os.sep + ".venv" + os.sep) in resolved:
        return False
    return not any(resolved.startswith(pref) for pref in _ALLOWED_READ_PREFIXES)


def _audit_hook(event: str, args: tuple) -> None:
    if not _AUDIT_HOOK_ACTIVE:
        return
    if event == "open":
        path, mode, flags = args[0], args[1], args[2]
        # Schreibmodi verbieten
        if mode and any(m in mode for m in ("w", "a", "x", "+")):
            msg = f"Verbotener Schreibzugriff im Audit-Hook: {path} (mode={mode})"
            _VIOLATIONS.append(msg)
            raise AssertionError(msg)
        # Flags verbieten (O_WRONLY, O_RDWR, O_CREAT)
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT
        if flags and (flags & write_flags):
            msg = f"Verbotener Schreibzugriff im Audit-Hook: {path} (flags={flags})"
            _VIOLATIONS.append(msg)
            raise AssertionError(msg)
        if _is_disallowed_repo_read(path):
            msg = f"Unerlaubter Lesezugriff außerhalb der Allowlist: {path}"
            _VIOLATIONS.append(msg)
            raise AssertionError(msg)


if not _HOOK_REGISTERED:
    try:
        sys.addaudithook(_audit_hook)
        _HOOK_REGISTERED = True
    except Exception:
        pass


class MemoryMockLLM(LLMProvider):
    """RAM-only Mock LLM Provider."""

    model_name: str = "MemoryMockLLM"
    is_real_llm: bool = True
    details: str = "In-Memory Test LLM"
    max_tokens: int = 128

    @property
    def n_ctx(self) -> int:
        return get_default_n_ctx()

    def count_tokens(self, text: str) -> int:
        if not text:
            return 0
        return len(text.split())

    def __init__(self):
        self.reset_called = False

    def generate(self, messages: List[Dict[str, str]]) -> str:
        return "### Bereich 1.4 – Emotionale und psychische Entwicklung\nSynthetische Antwort ohne Festplattenzugriff."

    def generate_stream(self, messages: List[Dict[str, str]]) -> Generator[str, None, None]:
        yield "### Bereich 1.4 – Emotionale und psychische Entwicklung\n"
        yield "Synthetische Antwort "
        yield "ohne Festplattenzugriff."

    def reset(self) -> None:
        self.reset_called = True


def _activate_write_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Installiert alle Schreib-Sperren und setzt die Violation-Liste zurück."""
    _VIOLATIONS.clear()

    def _forbidden_write(*args, **kwargs):
        msg = f"Festplatten-Schreibzugriff blockiert: {args} {kwargs}"
        _VIOLATIONS.append(msg)
        raise AssertionError(msg)

    orig_open = io.open

    def _guarded_open(file, mode="r", *args, **kwargs):
        if any(m in mode for m in ("w", "a", "x", "+")):
            msg = f"io.open Schreibzugriff blockiert: {file} (mode={mode})"
            _VIOLATIONS.append(msg)
            raise AssertionError(msg)
        if _is_disallowed_repo_read(file):
            msg = f"Unerlaubter Lesezugriff außerhalb der Allowlist: {file}"
            _VIOLATIONS.append(msg)
            raise AssertionError(msg)
        return orig_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _guarded_open)
    monkeypatch.setattr("io.open", _guarded_open)
    monkeypatch.setattr(pathlib.Path, "write_text", _forbidden_write)
    monkeypatch.setattr(pathlib.Path, "write_bytes", _forbidden_write)
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", _forbidden_write)
    monkeypatch.setattr(tempfile, "TemporaryFile", _forbidden_write)
    monkeypatch.setattr(tempfile, "mkstemp", _forbidden_write)
    monkeypatch.setattr(tempfile, "mkdtemp", _forbidden_write)
    monkeypatch.setattr(shutil, "copy", _forbidden_write)
    monkeypatch.setattr(shutil, "copy2", _forbidden_write)
    monkeypatch.setattr(shutil, "copyfile", _forbidden_write)
    monkeypatch.setattr(shutil, "copytree", _forbidden_write)


def test_guard_catches_swallowed_write(monkeypatch, tmp_path):
    """Selbsttest des Guards: Zeichnet Verstöße auch dann auf, wenn der Produktionscode (z.B. truncate_log_file) Exceptions schluckt."""
    global _AUDIT_HOOK_ACTIVE

    redirected_dir = tmp_path / "logs"
    redirected_file = redirected_dir / "chat_app.log"
    monkeypatch.setattr("chat_app.logger.LOG_DIR", redirected_dir)
    monkeypatch.setattr("chat_app.logger.LOG_FILE", redirected_file)

    app_logger = logging.getLogger("chat_app")
    old_handlers = list(app_logger.handlers)
    app_logger.handlers.clear()

    _activate_write_guard(monkeypatch)
    _AUDIT_HOOK_ACTIVE = True
    try:
        # truncate_log_file fängt intern Exception ab -> wirft nicht nach außen,
        # aber _VIOLATIONS muss den versuchten Schreibzugriff zwingend erfassen!
        real_truncate_log_file("reset")
        assert len(_VIOLATIONS) >= 1
        assert any("chat_app.log" in v for v in _VIOLATIONS)
        assert not redirected_file.exists()
    finally:
        _AUDIT_HOOK_ACTIVE = False
        app_logger.handlers.clear()
        for h in old_handlers:
            app_logger.addHandler(h)


def test_redesign_no_disk_writes(monkeypatch):
    """Prüft den vollständigen M1-Ablauf: CSV -> Plan -> Bereich 1.4 Chat -> Kürzung (2.2) -> Bereichswechsel (2.1) -> Preflight ❌ -> Export MD/DOCX -> Neuer Fall (409/200) -> Reset -> TTL erzeugt KEINE Festplatten-Schreiboperationen."""
    global _AUDIT_HOOK_ACTIVE
    import json

    # 1. Log-Handler auf Memory umstellen & Log-Truncation per Recorder abfangen
    app_logger = logging.getLogger("chat_app")
    old_handlers = list(app_logger.handlers)
    app_logger.handlers.clear()
    memory_log_stream = io.StringIO()
    mem_handler = logging.StreamHandler(memory_log_stream)
    app_logger.addHandler(mem_handler)

    truncate_reasons: List[str] = []

    def _record_truncate(reason: str = "start") -> int:
        truncate_reasons.append(reason)
        return 0

    monkeypatch.setattr("chat_app.chatbot.truncate_log_file", _record_truncate)
    monkeypatch.setenv("SOB_N_CTX", "1024")

    # 2. Blockiere Schreibmodi in builtins und os/pathlib
    _activate_write_guard(monkeypatch)

    # 3. App initialisieren mit Mock und Fake-Clock
    t = [10000.0]
    mock = MemoryMockLLM()
    bot = ReflectionChatbot(provider=mock, clock=lambda: t[0])
    app = create_app(chatbot=bot)
    client = TestClient(app)

    # Vorab CSV-Bytes lesen (erlaubter Fixture-Read)
    csv_path = pathlib.Path("tests/fixtures/hans_peter_synthetisch.csv")

    # 4. Audit-Hook aktivieren
    _AUDIT_HOOK_ACTIVE = True

    try:
        # Step 1: POST /api/upload tests/fixtures/hans_peter_synthetisch.csv -> entries_total == 57
        csv_bytes = csv_path.read_bytes()
        res_csv = client.post(
            "/api/upload",
            files={"file": ("hans_peter_synthetisch.csv", csv_bytes, "text/csv")},
        )
        assert res_csv.status_code == 200
        assert res_csv.json()["entries_total"] == 57

        # Step 2: POST /api/upload-plan build_hamburg_vorbericht() -> goals_count == 9, template "hamburg"
        docx_bytes = build_hamburg_vorbericht()
        res_plan = client.post(
            "/api/upload-plan",
            files={"file": ("vorbericht_synth.docx", docx_bytes, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        )
        assert res_plan.status_code == 200
        plan_json = res_plan.json()
        assert plan_json["goals_count"] == 9
        assert plan_json["template"] == "hamburg"

        # Step 3: POST /api/area 1.4, confirm 1.4, chat-stream -> SSE contains mock answer and done event
        res_area = client.post("/api/area", json={"bereich_id": "1.4"})
        assert res_area.status_code == 200
        assert bot.selected_bereich_id == "1.4"

        res_confirm = client.post("/api/area/confirm", json={"bereich_id": "1.4"})
        assert res_confirm.status_code == 200
        assert bot.case.is_area_confirmed("1.4") is True

        res_review = client.get("/api/area/1.4/review")
        assert res_review.status_code == 200
        assert res_review.json()["confirmed"] is True

        res_chat = client.post(
            "/api/chat-stream",
            json={"message": "Wie ist der aktuelle Stand im Bereich 1.4?"},
        )
        assert res_chat.status_code == 200
        assert "Synthetische Antwort" in res_chat.text
        sse_lines_1_4 = [
            json.loads(line.removeprefix("data: "))
            for line in res_chat.text.splitlines()
            if line.startswith("data: ")
        ]
        assert any(ev.get("done") is True for ev in sse_lines_1_4)

        # Step 4: Truncation in 2.2 (12+12 entries, largest area) with SOB_N_CTX=1024
        res_area_2_2 = client.post("/api/area", json={"bereich_id": "2.2"})
        assert res_area_2_2.status_code == 200
        res_conf_2_2 = client.post("/api/area/confirm", json={"bereich_id": "2.2"})
        assert res_conf_2_2.status_code == 200

        res_chat_2_2 = client.post(
            "/api/chat-stream",
            json={"message": "Bitte reflektiere die Einträge zur gesellschaftlichen Teilhabe."},
        )
        assert res_chat_2_2.status_code == 200
        sse_lines_2_2 = [
            json.loads(line.removeprefix("data: "))
            for line in res_chat_2_2.text.splitlines()
            if line.startswith("data: ")
        ]
        done_2_2 = next(ev for ev in sse_lines_2_2 if ev.get("done") is True)
        cs_2_2 = done_2_2["context_status"]
        assert cs_2_2["n_ctx"] == 1024
        assert cs_2_2["entries_kept"] < cs_2_2["entries_total"] or cs_2_2["dropped_messages"] > 0
        assert len(cs_2_2["warning_notices"]) > 0

        # Step 5: Area switch to 2.1 + confirm + chat; check 1.4 messages unchanged and selected_area.id == "2.1"
        msgs_1_4_before = list(bot.case.get_area_messages("1.4"))
        assert len(msgs_1_4_before) == 2

        res_area_2_1 = client.post("/api/area", json={"bereich_id": "2.1"})
        assert res_area_2_1.status_code == 200
        res_conf_2_1 = client.post("/api/area/confirm", json={"bereich_id": "2.1"})
        assert res_conf_2_1.status_code == 200

        res_chat_2_1 = client.post(
            "/api/chat-stream",
            json={"message": "Wie entwickeln sich die sozialen Beziehungen?"},
        )
        assert res_chat_2_1.status_code == 200
        assert bot.case.get_area_messages("1.4") == msgs_1_4_before

        res_sess_2_1 = client.get("/api/session")
        assert res_sess_2_1.status_code == 200
        assert res_sess_2_1.json()["selected_area"]["id"] == "2.1"

        # Step 6: Preflight ❌ with ~1500 words -> SSE error, message NOT in bot.messages
        msg_count_before_overflow = len(bot.messages)
        huge_msg = "ZZ_SYNTH_OVERFLOW " + ("Wort " * 1500)
        res_overflow = client.post("/api/chat-stream", json={"message": huge_msg})
        assert res_overflow.status_code == 200
        sse_overflow = [
            json.loads(line.removeprefix("data: "))
            for line in res_overflow.text.splitlines()
            if line.startswith("data: ")
        ]
        assert any(
            "error" in ev and "❌ Nachricht zu lang für das Kontextfenster" in ev["error"]
            for ev in sse_overflow
        )
        assert len(bot.messages) == msg_count_before_overflow
        assert all("ZZ_SYNTH_OVERFLOW" not in m.get("content", "") for m in bot.messages)

        # Step 7: GET /api/export and GET /api/export/docx?template=hamburg -> 200
        res_exp_md = client.get("/api/export")
        assert res_exp_md.status_code == 200
        assert "# Fachdialog" in res_exp_md.json()["markdown"]

        res_exp_docx = client.get("/api/export/docx?template=hamburg")
        assert res_exp_docx.status_code == 200
        assert len(res_exp_docx.content) > 0

        # Step 8: New case: POST /api/upload without new_case -> 409; with new_case=true -> 200, fall_id changed, history empty
        old_fall_id = bot.case.fall_id
        res_conflict = client.post(
            "/api/upload",
            files={"file": ("hans_peter_synthetisch.csv", csv_bytes, "text/csv")},
        )
        assert res_conflict.status_code == 409
        assert res_conflict.json()["needs_confirmation"] is True

        res_new_case = client.post(
            "/api/upload",
            data={"new_case": "true"},
            files={"file": ("hans_peter_synthetisch.csv", csv_bytes, "text/csv")},
        )
        assert res_new_case.status_code == 200
        assert bot.case.fall_id != old_fall_id
        assert len(bot.messages) == 0

        # Step 9: POST /api/reset -> 200, has_csv/has_plan false
        res_reset = client.post("/api/reset")
        assert res_reset.status_code == 200
        res_sess_after_reset = client.get("/api/session")
        assert res_sess_after_reset.status_code == 200
        sess_reset_json = res_sess_after_reset.json()
        assert sess_reset_json["has_csv"] is False
        assert sess_reset_json["has_plan"] is False

        # Step 10: TTL: load CSV again, t[0] += 61*60, GET /api/session -> has_csv false, case_closed_reason == "ttl"
        res_csv_ttl = client.post(
            "/api/upload",
            files={"file": ("hans_peter_synthetisch.csv", csv_bytes, "text/csv")},
        )
        assert res_csv_ttl.status_code == 200
        assert bot.has_csv is True

        t[0] += 61 * 60
        res_sess_ttl = client.get("/api/session")
        assert res_sess_ttl.status_code == 200
        sess_ttl_json = res_sess_ttl.json()
        assert sess_ttl_json["has_csv"] is False
        assert sess_ttl_json["case_closed_reason"] == "ttl"

        assert truncate_reasons == ["reset", "reset", "ttl"]
        assert _VIOLATIONS == [], f"Schreibzugriffe erkannt: {_VIOLATIONS}"

    finally:
        _AUDIT_HOOK_ACTIVE = False
        app_logger.handlers.clear()
        for h in old_handlers:
            app_logger.addHandler(h)
