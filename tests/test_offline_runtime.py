"""Tests für den Offline-Nachweis (Issue #116).

Sicherheits- und Datenschutz-Garantien:
1. Kein ausgehender Netzwerkverkehr zur Laufzeit (Socket-Blocker fängt Non-Loopback ab).
2. Lokale Webanwendung (FastAPI TestClient in-process) läuft komplett offline.
3. API-Dokumentation (/docs, /redoc, /openapi.json) ist deaktiviert (404).
4. Templates/Static enthalten keinerlei externe http(s):// Links zu Skripten, Styles oder Fonts.
5. GGUF-Modellintegrität: SHA-256-Prüfung schlägt bei Abweichung oder fehlendem Hash fehl (❌).
"""
import io
import os
import re
import socket
from pathlib import Path
from typing import Generator, List, Dict
import pytest
from fastapi.testclient import TestClient

from chat_app.app import create_app
from chat_app.chatbot import LLMProvider, ReflectionChatbot
from chat_app.model_integrity import (
    ModelIntegrityError,
    calculate_sha256,
    resolve_expected_sha256,
    verify_model_sha256,
)

ROOT_DIR = Path(__file__).resolve().parent.parent
SYNTHETIC_MODEL_PATH = ROOT_DIR / "tests" / "fixtures" / "synthetic_model.gguf"
SYNTHETIC_MODEL_HASH = "20b175879daae58aa54d6e62c127cf06207077e5abf2d12a5a85289ee0083339"


# ---------------------------------------------------------------------------
# Test-Doubles
# ---------------------------------------------------------------------------

class MockOfflineLLM:
    """Offline-Mock für LLMProvider ohne Modellgewichte oder Netzwerkanbindung."""
    model_name: str = "MockOfflineModel"
    is_real_llm: bool = False
    details: str = "In-Memory Test Double"

    def generate(self, messages: List[Dict[str, str]]) -> str:
        return "Mock-Offline-Reflexionsantwort."

    def generate_stream(self, messages: List[Dict[str, str]]) -> Generator[str, None, None]:
        yield "Mock-Offline-Token-1 "
        yield "Mock-Offline-Token-2."


# ---------------------------------------------------------------------------
# 1. Socket-Blocker (nur Loopback erlaubt)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def guard_outbound_network(monkeypatch):
    """Blockiert alle ausgehenden Socket-Verbindungen außerhalb von Loopback."""
    orig_connect = socket.socket.connect

    def restricted_connect(self, address):
        if isinstance(address, tuple) and len(address) >= 2:
            host = address[0]
            if host not in {"127.0.0.1", "::1", "localhost"}:
                raise RuntimeError(f"❌ Unerlaubte ausgehende Netzwerkverbindung blockiert: {address}")
        return orig_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", restricted_connect)


def test_socket_guard_blocks_external_network():
    """Verifiziert, dass der Socket-Guard ausgehende Verbindungen abfängt."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match="❌ Unerlaubte ausgehende Netzwerkverbindung blockiert"):
            s.connect(("93.184.216.34", 80))
    finally:
        s.close()


# ---------------------------------------------------------------------------
# 2. In-Process FastAPI TestClient & Offline-Lauf
# ---------------------------------------------------------------------------

@pytest.fixture
def offline_client():
    bot = ReflectionChatbot(provider=MockOfflineLLM())
    app = create_app(chatbot=bot)
    return TestClient(app)


def test_offline_runtime_endpoints_work_without_network(offline_client):
    """Startseite, Chat und Export funktionieren rein in-process ohne Netzwerk und ohne Modell."""
    # Startseite abrufen
    res_index = offline_client.get("/")
    assert res_index.status_code == 200
    assert "Fachdialog & Reflexion" in res_index.text

    # Chat-Turn ausführen
    res_chat = offline_client.post("/api/chat", json={"message": "Wie können wir vorgehen?"})
    assert res_chat.status_code == 200
    assert "Mock-Offline-Token" in res_chat.text

    # Markdown-Export abrufen
    res_export = offline_client.get("/api/export")
    assert res_export.status_code == 200
    data = res_export.json()
    assert "markdown" in data
    assert "Mock-Offline-Token" in data["markdown"]


# ---------------------------------------------------------------------------
# 3. Assert /docs, /redoc, /openapi.json sind deaktiviert (404)
# ---------------------------------------------------------------------------

def test_api_docs_endpoints_disabled(offline_client):
    """Stellt sicher, dass keine Swagger/ReDoc/OpenAPI-Dokumentationsseiten erreichbar sind."""
    for path in ["/docs", "/redoc", "/openapi.json"]:
        res = offline_client.get(path)
        assert res.status_code == 404, f"Endpunkt {path} sollte 404 sein, war {res.status_code}"


# ---------------------------------------------------------------------------
# 4. Scan Frontend Templates & Static auf externe URLs
# ---------------------------------------------------------------------------

def test_frontend_templates_contain_no_external_assets():
    """Prüft alle Templates und statischen Dateien auf externe http(s):// Verweise."""
    templates_dir = ROOT_DIR / "chat_app" / "templates"
    static_dir = ROOT_DIR / "chat_app" / "static"

    files_to_check: List[Path] = []
    if templates_dir.is_dir():
        files_to_check.extend([p for p in templates_dir.rglob("*") if p.is_file()])
    if static_dir.is_dir():
        files_to_check.extend([p for p in static_dir.rglob("*") if p.is_file()])

    assert files_to_check, "Keine Template-/Static-Dateien zur Prüfung gefunden!"

    # Regex für externe Skripte, Stylesheets, Fonts und URLs
    external_url_pattern = re.compile(
        r"""(?:src|href)=["']https?://|@import\s+(?:url\()?["']?https?://|url\(["']?https?://""",
        re.IGNORECASE,
    )

    violations = []
    for f in files_to_check:
        content = f.read_text(encoding="utf-8", errors="replace")
        matches = external_url_pattern.findall(content)
        if matches:
            violations.append(f"{f.name}: {matches}")

    assert not violations, f"❌ Externe Assets im Frontend gefunden: {violations}"


# ---------------------------------------------------------------------------
# 5. GGUF-Hash-Integritätstests gegen synthetische Fixture
# ---------------------------------------------------------------------------

def test_verify_model_sha256_match_via_argument():
    """Erfolgreiche Verifikation bei übereinstimmendem explizitem Hash-Argument."""
    assert SYNTHETIC_MODEL_PATH.is_file()
    verify_model_sha256(SYNTHETIC_MODEL_PATH, expected=SYNTHETIC_MODEL_HASH)


def test_verify_model_sha256_match_via_env(monkeypatch):
    """Erfolgreiche Verifikation bei Hash in Umgebungsvariable SOB_MODEL_SHA256."""
    monkeypatch.setenv("SOB_MODEL_SHA256", SYNTHETIC_MODEL_HASH)
    verify_model_sha256(SYNTHETIC_MODEL_PATH)


def test_verify_model_sha256_match_via_sidecar(tmp_path):
    """Erfolgreiche Verifikation über .sha256 Sidecar-Datei."""
    fake_model = tmp_path / "test_model.gguf"
    fake_model.write_bytes(b"TEST_MODEL_BYTES_XYZ")
    expected_hash = calculate_sha256(fake_model)

    sidecar = tmp_path / "test_model.gguf.sha256"
    sidecar.write_text(f"{expected_hash}  test_model.gguf\n", encoding="utf-8")

    # Ohne Argument und ohne Env greift Sidecar
    verify_model_sha256(fake_model)


def test_verify_model_sha256_mismatch_raises_error():
    """Hash-Abweichung führt zu Fehler mit großem ❌."""
    wrong_hash = "0000000000000000000000000000000000000000000000000000000000000000"
    with pytest.raises(ModelIntegrityError) as exc_info:
        verify_model_sha256(SYNTHETIC_MODEL_PATH, expected=wrong_hash)

    msg = str(exc_info.value)
    assert msg.startswith("❌"), f"Message sollte mit ❌ beginnen, war: {msg}"
    assert "Modell-Hash-Abweichung" in msg


def test_verify_model_sha256_missing_hash_raises_error(monkeypatch):
    """Fehlender konfigurierter Hash bricht mit großem ❌ ab."""
    monkeypatch.delenv("SOB_MODEL_SHA256", raising=False)
    # Sicherstellen, dass kein Sidecar neben dem Testmodell liegt
    sidecar = SYNTHETIC_MODEL_PATH.with_name(f"{SYNTHETIC_MODEL_PATH.name}.sha256")
    if sidecar.exists():
        sidecar.unlink()

    with pytest.raises(ModelIntegrityError) as exc_info:
        verify_model_sha256(SYNTHETIC_MODEL_PATH)

    msg = str(exc_info.value)
    assert msg.startswith("❌"), f"Message sollte mit ❌ beginnen, war: {msg}"
    assert "Kein SHA-256-Hash" in msg


def test_verify_model_sha256_nonexistent_file_raises_error():
    """Nicht existierende Datei führt zu FileNotFoundError mit ❌."""
    with pytest.raises(FileNotFoundError) as exc_info:
        verify_model_sha256("nicht_vorhanden.gguf", expected=SYNTHETIC_MODEL_HASH)

    msg = str(exc_info.value)
    assert msg.startswith("❌"), f"Message sollte mit ❌ beginnen, war: {msg}"
