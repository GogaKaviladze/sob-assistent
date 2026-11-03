"""Regression- und Konsolidierungstests für den Pivot (Issue #97).

Stellt sicher, dass:
1. Der alte monolithische Ordner `app/` vollständig entfernt ist.
2. `pywebview` nicht mehr in `requirements.txt` enthalten ist.
3. `chat_app` als einziger Applikations-Einstiegspunkt fungiert und instanziierbar ist.
4. `start.command` einheitlich auf `chat_app` zeigt.
5. Verwaiste CLI-Batch-Module (`sob_generator.cli`) nicht mehr existieren.
"""
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_app_directory_is_removed():
    app_dir = REPO_ROOT / "app"
    assert not app_dir.exists(), "Der Ordner 'app/' darf nach der Konsolidierung nicht mehr existieren."


def test_pywebview_removed_from_requirements():
    req_file = REPO_ROOT / "requirements.txt"
    assert req_file.exists()
    content = req_file.read_text(encoding="utf-8")
    assert "pywebview" not in content, "'pywebview' darf nicht mehr in requirements.txt enthalten sein."


def test_cli_module_does_not_exist():
    cli_file = REPO_ROOT / "sob_generator" / "cli.py"
    assert not cli_file.exists(), "'sob_generator/cli.py' muss entfernt sein."


def test_start_command_points_to_chat_app():
    start_cmd = REPO_ROOT / "start.command"
    assert start_cmd.exists()
    content = start_cmd.read_text(encoding="utf-8")
    assert "python -m chat_app" in content, "start.command muss auf 'python -m chat_app' verweisen."


def test_chat_app_is_the_primary_application():
    from chat_app.app import create_app
    from chat_app.chatbot import ReflectionChatbot, ReflectionEngine
    app = create_app(chatbot=ReflectionChatbot(provider=ReflectionEngine()))
    client = TestClient(app)

    # UI Index Route
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Fachdialog & Reflexion" in response.text
    assert "Neuer Fall" in response.text
    assert "resetAll()" in response.text

    # API Status / Model Route
    models_res = client.get("/api/models")
    assert models_res.status_code == 200
    data = models_res.json()
    assert "models" in data
