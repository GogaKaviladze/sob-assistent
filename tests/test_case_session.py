"""Tests für Fall-Objekt (CaseSession), harten Reset, TTL, Fehler-Entfernung und Heuristik-Sperre (Issue #133)."""
from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional
import pytest
from starlette.testclient import TestClient

from chat_app.app import create_app
from chat_app.chatbot import (
    LLMProvider,
    ReflectionChatbot,
    ReflectionEngine,
    ModelUnavailableError,
    create_llm_provider,
)
from chat_app.logger import truncate_log_file
from chat_app.csv_loader import CsvData
from chat_app.plan_loader import PlanData, PlanGoalItem


class MockLLM(LLMProvider):
    """Mock-Provider für deterministische Tests."""

    model_name: str = "MockLLM"
    is_real_llm: bool = True
    details: str = "Test Mock"

    def __init__(self, reply: str = "Test-Antwort"):
        self.reply = reply
        self.reset_called = False
        self.last_payload: List[Dict[str, str]] = []

    def generate(self, messages: List[Dict[str, str]]) -> str:
        self.last_payload = messages
        return self.reply

    def generate_stream(self, messages: List[Dict[str, str]]) -> Generator[str, None, None]:
        self.last_payload = messages
        yield self.reply

    def reset(self) -> None:
        self.reset_called = True


class ErrorMockLLM(LLMProvider):
    """Mock-Provider, der beim Streaming eine Exception mit vertraulichem Marker wirft."""

    model_name: str = "ErrorMockLLM"
    is_real_llm: bool = True
    details: str = "Error Mock"

    def __init__(self, secret: str = "ZZ_SYNTH_SECRET"):
        self.secret = secret
        self.reset_called = False

    def generate(self, messages: List[Dict[str, str]]) -> str:
        raise RuntimeError(self.secret)

    def generate_stream(self, messages: List[Dict[str, str]]) -> Generator[str, None, None]:
        raise RuntimeError(self.secret)

    def reset(self) -> None:
        self.reset_called = True


def test_case_session_lifecycle_and_reset():
    """Prüft, dass CaseSession alle Falldaten isoliert und hard_reset() alles leert und den Provider zurücksetzt."""
    mock = MockLLM()
    bot = ReflectionChatbot(provider=mock)

    initial_fall_id = bot.case.fall_id
    assert initial_fall_id
    assert len(initial_fall_id) >= 16

    # Falldaten setzen
    bot.set_csv(CsvData(filename="fall_a.csv", headers=["A", "B"], rows=[["1", "ZZ_SYNTH_FALL_A"]], total_rows=1))
    bot.set_plan_context("Vorbericht Text ZZ_SYNTH_FALL_A")
    bot.case.messages.append({"role": "user", "content": "Hallo Fall A"})

    assert bot.has_csv
    assert bot.has_plan
    assert len(bot.messages) == 1

    # Harter Reset
    bot.hard_reset(reason="reset")

    assert bot.case.fall_id != initial_fall_id
    assert bot.csv_data is None
    assert bot.plan_data is None
    assert bot.plan_context is None
    assert len(bot.messages) == 0
    assert bot.case.area_state == {}
    assert mock.reset_called is True


def test_upload_confirmation_409_and_new_case_override():
    """Upload Fall A -> Chat -> Upload Fall B ohne new_case gibt 409. Mit new_case=true erfolgt Reset."""
    mock = MockLLM()
    bot = ReflectionChatbot(provider=mock)
    app = create_app(chatbot=bot)
    client = TestClient(app)

    # 1. Fall A hochladen
    csv_a = "Datum;Kategorie;Text\n01.01.2025;Wohnen;ZZ_SYNTH_FALL_A_DATA"
    res_a = client.post(
        "/api/upload",
        files={"file": ("fall_a.csv", csv_a.encode("utf-8"), "text/csv")},
    )
    assert res_a.status_code == 200

    # Chattanfrage stellen
    client.post("/api/area", json={"bereich_id": "1.1"})
    res_chat = client.post("/api/chat", json={"message": "Frage zu ZZ_SYNTH_FALL_A"})
    assert res_chat.status_code == 200
    assert any("ZZ_SYNTH_FALL_A" in m["content"] for m in bot._build_payload())

    # 2. Fall B hochladen ohne Bestätigung -> 409
    csv_b = "Datum;Kategorie;Text\n02.02.2025;Wohnen;ZZ_SYNTH_FALL_B_DATA"
    res_b_conflict = client.post(
        "/api/upload",
        files={"file": ("fall_b.csv", csv_b.encode("utf-8"), "text/csv")},
    )
    assert res_b_conflict.status_code == 409
    data_conflict = res_b_conflict.json()
    assert data_conflict.get("needs_confirmation") is True
    assert "Neuer Fall?" in data_conflict.get("detail", "")

    # Fall A muss weiterhin aktiv sein
    assert "fall_a.csv" in bot.csv_data.filename
    assert len(bot.messages) > 0

    # 3. Fall B mit new_case=true hochladen -> 200
    res_b_ok = client.post(
        "/api/upload",
        data={"new_case": "true"},
        files={"file": ("fall_b.csv", csv_b.encode("utf-8"), "text/csv")},
    )
    assert res_b_ok.status_code == 200

    # Marker A darf nirgendwo mehr vorhanden sein
    assert not any("ZZ_SYNTH_FALL_A" in m["content"] for m in bot._build_payload())
    assert len(bot.messages) == 0
    export_text = bot.export_markdown()
    assert "ZZ_SYNTH_FALL_A" not in export_text
    assert mock.reset_called is True


def test_complementary_upload_no_confirmation_needed():
    """Plan nach CSV ohne bestehenden Verlauf und ohne Klientenkonflikt braucht keine Rückfrage."""
    mock = MockLLM()
    bot = ReflectionChatbot(provider=mock)
    app = create_app(chatbot=bot)
    client = TestClient(app)

    csv_content = "Datum;Kategorie;Text\n01.01.2025;Wohnen;Erste Notiz"
    res_csv = client.post(
        "/api/upload",
        files={"file": ("klient.csv", csv_content.encode("utf-8"), "text/csv")},
    )
    assert res_csv.status_code == 200

    # Ergänzender Plan-Upload (.txt)
    plan_content = "Gesamtplan für denselben Fall"
    res_plan = client.post(
        "/api/upload-plan",
        files={"file": ("vorbericht.txt", plan_content.encode("utf-8"), "text/plain")},
    )
    assert res_plan.status_code == 200
    assert bot.has_csv
    assert bot.has_plan


def test_client_name_not_silently_transferred():
    """Klientennamen werden nicht mehr stillschweigend zwischen CSV und Plan übertragen."""
    mock = MockLLM()
    bot = ReflectionChatbot(provider=mock)
    app = create_app(chatbot=bot)
    client = TestClient(app)

    # 1. Plan zuerst mit eigenem Klientennamen
    plan_data = PlanData(
        filename="vorbericht.docx",
        client_name="Max Mustermann",
        goals=[PlanGoalItem(section_id="1.1", section_title="Wohnen", leitziel="Test")],
    )
    bot.set_plan(plan_data)

    # 2. CSV ohne Klientenname hochladen
    csv_bytes = "Datum;Kategorie;Text\n01.01.2025;Wohnen;Notiz".encode("utf-8")
    res = client.post(
        "/api/upload",
        files={"file": ("daten.csv", csv_bytes, "text/csv")},
    )
    assert res.status_code == 200
    # Darf nicht von bot.plan_data.client_name überschrieben werden
    assert res.json()["client_name"] != "Max Mustermann"


def test_ttl_inactivity_fake_clock():
    """60 Min Inaktivität führt beim nächsten Zugriff zum harten Reset; /api/session meldet case_closed_reason einmalig."""
    mock = MockLLM()
    bot = ReflectionChatbot(provider=mock)
    app = create_app(chatbot=bot)
    client = TestClient(app)

    simulated_time = [10000.0]
    bot.clock = lambda: simulated_time[0]

    # Daten laden
    bot.set_csv(CsvData(filename="fall_ttl.csv", headers=["A"], rows=[["1"]], total_rows=1))
    assert bot.has_csv

    # 59 Minuten vergehen (3540 Sekunden) -> noch nicht abgelaufen
    simulated_time[0] += 59 * 60
    res_active = client.get("/api/session")
    assert res_active.status_code == 200
    assert res_active.json()["has_csv"] is True
    assert res_active.json().get("case_closed_reason") is None

    # Weitere 2 Minuten vergehen (insgesamt > 60 Minuten seit letzter Aktivität)
    simulated_time[0] += 2 * 60 + 1
    res_expired = client.get("/api/session")
    assert res_expired.status_code == 200
    body = res_expired.json()
    assert body["has_csv"] is False
    assert body.get("case_closed_reason") == "ttl"

    # Zweiter Aufruf meldet ttl nicht mehr (einmalige Meldung)
    res_second = client.get("/api/session")
    assert res_second.json().get("case_closed_reason") is None


def test_stream_error_leaves_history_clean_and_no_secret_in_logs(caplog):
    """Exception im Stream nimmt User-Nachricht zurück, leert Assistant-Nachricht und loggt keine Details."""
    secret_marker = "ZZ_SYNTH_SUPER_SECRET_TOKEN"
    error_mock = ErrorMockLLM(secret=secret_marker)
    bot = ReflectionChatbot(provider=error_mock)
    app = create_app(chatbot=bot)
    client = TestClient(app)

    with caplog.at_level(logging.INFO):
        res = client.post("/api/chat-stream", json={"message": "Sag das Geheimnis"})
        assert res.status_code == 200
        body = res.text

    # SSE-Event muss Fehler enthalten
    assert "error" in body
    assert "❌ Fehler bei der Modell-Generierung (RuntimeError)" in body

    # History muss komplett leer sein
    assert len(bot.messages) == 0
    export_text = bot.export_markdown()
    assert "Fehler" not in export_text
    assert secret_marker not in export_text

    # In caplog darf weder secret_marker noch traceback auftauchen
    assert secret_marker not in caplog.text
    assert "Traceback" not in caplog.text
    # Loggt nur type(err).__name__
    assert "RuntimeError" in caplog.text


def test_no_model_and_no_heuristic_returns_503(monkeypatch):
    """Ohne Modell und ohne SOB_ALLOW_HEURISTIC=1 antwortet /api/chat-stream mit 503."""
    monkeypatch.delenv("SOB_ALLOW_HEURISTIC", raising=False)
    monkeypatch.setenv("SOB_ALLOW_HEURISTIC", "0")
    monkeypatch.setattr("chat_app.chatbot.find_available_model", lambda: None)

    bot = ReflectionChatbot()
    assert bot.provider is None
    assert bot.model_error is not None

    app = create_app(chatbot=bot)
    client = TestClient(app)

    res_session = client.get("/api/session")
    assert res_session.status_code == 200
    assert res_session.json().get("model_error") is not None

    res_chat = client.post("/api/chat-stream", json={"message": "Hallo?"})
    assert res_chat.status_code == 503
    assert "❌ Kein Modell geladen" in res_chat.json()["detail"]


def test_heuristic_mode_with_explicit_env(monkeypatch):
    """Mit SOB_ALLOW_HEURISTIC=1 wird die ReflectionEngine genutzt und in session gekennzeichnet."""
    monkeypatch.setenv("SOB_ALLOW_HEURISTIC", "1")
    monkeypatch.setattr("chat_app.chatbot.find_available_model", lambda: None)

    bot = ReflectionChatbot()
    assert isinstance(bot.provider, ReflectionEngine)
    assert bot.model_error is None

    app = create_app(chatbot=bot)
    client = TestClient(app)
    res_session = client.get("/api/session")
    assert res_session.status_code == 200
    assert res_session.json().get("is_heuristic") is True


def test_log_truncation_on_reset(tmp_path, monkeypatch):
    """Truncate leert das Logfile bis auf genau eine Zeile 'Log gelöscht: anlass=...'."""
    log_file = tmp_path / "chat_app.log"
    log_file.write_text("Zeile 1\nZeile 2\nZeile 3\nZeile 4\n", encoding="utf-8")
    monkeypatch.setattr("chat_app.logger.LOG_FILE", log_file)

    deleted_lines = truncate_log_file("reset")
    assert deleted_lines == 4

    content = log_file.read_text(encoding="utf-8")
    lines = content.strip().splitlines()
    assert len(lines) == 1
    assert lines[0] == "Log gelöscht: anlass=reset zeilen=4"


def test_html_contains_legal_hint_and_no_unter_uns():
    """index.html enthält den DSGVO-Hinweis 'Nur berichtsrelevante Inhalte eingeben' und nicht 'Unter uns'."""
    html_path = Path("chat_app/templates/index.html")
    assert html_path.exists()
    content = html_path.read_text(encoding="utf-8")

    assert "Nur berichtsrelevante Inhalte eingeben" in content
    assert "Unter uns" not in content


def test_orchestrator_logs_no_entry_labels(caplog):
    """M2 Sofortfix: orchestrator.py loggt nur Zähler und Indizes, niemals Textstellen aus _entry_label."""
    from sob_generator.importer import VivendiImport
    from sob_generator.llm.generator import GenerationResult
    from sob_generator.llm.schemas import DevelopmentsLLMOutput, DocumentationEntry, SectionLLMOutput
    from sob_generator.llm.sections import SECTION_DEFINITIONS
    from sob_generator.models import ClientData, ReportMeta
    from sob_generator.orchestrator import build_draft

    secret_entry_marker = "ZZ_SYNTH_VERTRAULICHE_NOTIZ_IM_EINTRAG"
    entries = [
        DocumentationEntry(
            datum="01.01.2025",
            kategorie="Wohnen",
            text=secret_entry_marker,
            fachkraft="Fachkraft A",
        )
    ]
    imported = VivendiImport(client=ClientData(), meta=ReportMeta(), entries=entries)

    class DummyGenerator:
        def classify_entry(self, entry, section_definitions):
            return "1.1"

        def generate_section(self, *args, **kwargs):
            return GenerationResult(
                value=SectionLLMOutput(
                    bericht_zielerreichung="Abschnitt",
                    vorschlag_ziele="Ziel",
                    vorschlag_massnahmen="Massnahme",
                    realisierung_andere="",
                ),
                needs_review=False,
                raw_output="",
            )

        def generate_developments(self, *args, **kwargs):
            return GenerationResult(
                value=DevelopmentsLLMOutput(developments="Entwicklungen"),
                needs_review=False,
                raw_output="",
            )

    with caplog.at_level(logging.INFO, logger="sob_generator.orchestrator"):
        build_draft(imported, DummyGenerator(), SECTION_DEFINITIONS)

    assert secret_entry_marker not in caplog.text
    assert "Pass 0: Eintrag 1/1 → 1.1" in caplog.text
