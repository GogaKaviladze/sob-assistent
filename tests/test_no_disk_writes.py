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
from chat_app.chatbot import LLMProvider, ReflectionChatbot
from tests.fixtures.make_synthetic_vorbericht import build_hamburg_vorbericht


_AUDIT_HOOK_ACTIVE = False
_HOOK_REGISTERED = False


def _audit_hook(event: str, args: tuple) -> None:
    if not _AUDIT_HOOK_ACTIVE:
        return
    if event == "open":
        path, mode, flags = args[0], args[1], args[2]
        # Schreibmodi verbieten
        if mode and any(m in mode for m in ("w", "a", "x", "+")):
            raise AssertionError(f"Verbotener Schreibzugriff im Audit-Hook: {path} (mode={mode})")
        # Flags verbieten (O_WRONLY, O_RDWR, O_CREAT)
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT
        if flags and (flags & write_flags):
            raise AssertionError(f"Verbotener Schreibzugriff im Audit-Hook: {path} (flags={flags})")


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

    def __init__(self):
        self.reset_called = False

    def generate(self, messages: List[Dict[str, str]]) -> str:
        return "### Bereich 1.1 – Wohnsituation\nSynthetische Antwort ohne Festplattenzugriff."

    def generate_stream(self, messages: List[Dict[str, str]]) -> Generator[str, None, None]:
        yield "### Bereich 1.1 – Wohnsituation\n"
        yield "Synthetische Antwort "
        yield "ohne Festplattenzugriff."

    def reset(self) -> None:
        self.reset_called = True


def test_redesign_no_disk_writes(monkeypatch):
    """Prüft strikt: Upload CSV -> Upload DOCX -> Chat -> Export MD -> Export DOCX -> Reset erzeugt KEINE Festplatten-Schreiboperationen."""
    global _AUDIT_HOOK_ACTIVE

    # 1. Log-Handler auf Memory umstellen
    app_logger = logging.getLogger("chat_app")
    old_handlers = list(app_logger.handlers)
    app_logger.handlers.clear()
    memory_log_stream = io.StringIO()
    mem_handler = logging.StreamHandler(memory_log_stream)
    app_logger.addHandler(mem_handler)

    # 2. Blockiere Schreibmodi in builtins und os/pathlib
    def _forbidden_write(*args, **kwargs):
        raise AssertionError(f"Festplatten-Schreibzugriff blockiert: {args} {kwargs}")

    orig_open = io.open

    def _guarded_open(file, mode="r", *args, **kwargs):
        if any(m in mode for m in ("w", "a", "x", "+")):
            raise AssertionError(f"io.open Schreibzugriff blockiert: {file} (mode={mode})")
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

    # 3. App initialisieren mit Mock
    mock = MemoryMockLLM()
    bot = ReflectionChatbot(provider=mock)
    app = create_app(chatbot=bot)
    client = TestClient(app)

    # 4. Audit-Hook aktivieren
    _AUDIT_HOOK_ACTIVE = True

    try:
        # Ablauf:
        # A) CSV hochladen (aus hans_peter_synthetisch.csv)
        csv_path = pathlib.Path("tests/fixtures/hans_peter_synthetisch.csv")
        csv_bytes = csv_path.read_bytes()
        res_csv = client.post(
            "/api/upload",
            files={"file": ("hans_peter_synthetisch.csv", csv_bytes, "text/csv")},
        )
        assert res_csv.status_code == 200

        # B) DOCX Vorbericht hochladen (synthetisch im RAM erzeugt)
        docx_bytes = build_hamburg_vorbericht()
        res_plan = client.post(
            "/api/upload-plan",
            files={"file": ("vorbericht_synth.docx", docx_bytes, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        )
        assert res_plan.status_code == 200

        # C) Bereichswahl, Bereichswechsel und Bestätigung (Issue #136, RAM-only)
        res_area = client.post("/api/area", json={"bereich_id": "1.1"})
        assert res_area.status_code == 200

        res_area_switch = client.post("/api/area", json={"bereich_id": "1.4"})
        assert res_area_switch.status_code == 200
        assert bot.selected_bereich_id == "1.4"

        res_confirm = client.post("/api/area/confirm", json={"bereich_id": "1.4"})
        assert res_confirm.status_code == 200
        assert bot.case.is_area_confirmed("1.4") is True

        # D) Chat-Stream ausführen
        res_chat = client.post(
            "/api/chat-stream",
            json={"message": "Wie ist der aktuelle Stand im Wohnbereich?"},
        )
        assert res_chat.status_code == 200
        assert "Synthetische Antwort" in res_chat.text

        # E) Markdown-Export abrufen
        res_exp_md = client.get("/api/export")
        assert res_exp_md.status_code == 200
        assert "# Fachdialog" in res_exp_md.text

        # F) DOCX-Export abrufen
        res_exp_docx = client.get("/api/export/docx?template=hamburg")
        assert res_exp_docx.status_code == 200
        assert len(res_exp_docx.content) > 0

        # G) Reset ausführen
        res_reset = client.post("/api/reset")
        assert res_reset.status_code == 200

    finally:
        _AUDIT_HOOK_ACTIVE = False
        app_logger.handlers.clear()
        for h in old_handlers:
            app_logger.addHandler(h)
