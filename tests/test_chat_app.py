import io
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from chat_app.app import create_app
from chat_app.chatbot import (
    LLMProvider,
    ReflectionChatbot,
    ReflectionEngine,
    create_llm_provider,
    find_available_model,
    list_available_models,
)
from chat_app.csv_loader import CsvData, detect_delimiter, parse_csv_content
from chat_app.plan_loader import (
    PlanData,
    PlanGoalItem,
    clean_form_value,
    extract_client_name_from_filename,
    parse_plan_docx,
)
from sob_generator import docx_fields as fields


# ---------------------------------------------------------------------------
# CSV-Loader Tests
# ---------------------------------------------------------------------------

def test_detect_delimiter():
    assert detect_delimiter("Datum;Name;Notiz\n01.01;Max;Test") == ";"
    assert detect_delimiter("Datum,Name,Notiz\n01.01,Max,Test") == ","
    assert detect_delimiter("Datum\tName\tNotiz\n01.01\tMax\tTest") == "\t"


def test_parse_csv_content_semicolon():
    csv_text = """Datum;Kategorie;Notiz
05.06.2025;Hauswirtschaft;Kochen geübt.
12.06.2025;Einkauf;Einkauf bei Rewe durchgeführt.
"""
    parsed = parse_csv_content(csv_text, filename="verlauf.csv")
    assert parsed.filename == "verlauf.csv"
    assert parsed.headers == ["Datum", "Kategorie", "Notiz"]
    assert parsed.total_rows == 2
    assert len(parsed.rows) == 2
    assert parsed.rows[0] == ["05.06.2025", "Hauswirtschaft", "Kochen geübt."]

    context = parsed.to_context_text()
    assert "verlauf.csv" not in context
    assert "Spalten:" not in context
    assert "[1] Datum: 05.06.2025 | Kategorie: Hauswirtschaft | Notiz: Kochen geübt." in context


def test_parse_csv_content_with_meta_header():
    csv_text = """;;;;Export Zeitraum 2025;;;
;;;;Klient Mustermann;;;
Datum;Details;Handzeichen
01.07.2025;Arztbesuch begleitet;AB
"""
    parsed = parse_csv_content(csv_text, filename="export.csv")
    assert parsed.total_rows >= 1
    assert "Arztbesuch begleitet" in parsed.to_context_text()


def test_to_context_text_full_and_head_tail():
    # 1. 114 Zeilen (wie Hans Peter): vollständig erhalten
    rows = [[f"Datum_{i}", f"Kategorie_{i}", f"Notiz_{i}"] for i in range(1, 115)]
    data = CsvData(filename="hans_peter.csv", headers=["Datum", "Kategorie", "Notiz"], rows=rows, total_rows=114)
    ctx = data.to_context_text()
    assert "[1] Datum: Datum_1" in ctx
    assert "[114] Datum: Datum_114" in ctx
    assert "übersprungen" not in ctx

    # 2. Extrem große CSV (600 Zeilen): Keep-Newest-Strategie behält neueste Einträge
    large_rows = [[f"Datum_{i}", f"Kat_{i}", f"Notiz_{i}"] for i in range(1, 601)]
    large_data = CsvData(filename="giant.csv", headers=["Datum", "Kat", "Notiz"], rows=large_rows, total_rows=600)
    large_ctx = large_data.to_context_text(max_rows=500)
    assert "[1] Datum: Datum_1" not in large_ctx
    assert "[101] Datum: Datum_101" in large_ctx
    assert "[600] Datum: Datum_600" in large_ctx
    assert "übersprungen" in large_ctx


# ---------------------------------------------------------------------------

# Chatbot & Reflection-Engine Tests
# ---------------------------------------------------------------------------

class MockLLM(LLMProvider):
    model_name: str = "Mock-Qwen"
    is_real_llm: bool = True
    details: str = "Mock für Tests"

    def __init__(self, reply: str = "Fachlicher Reflexionsimpuls"):
        self.reply = reply
        self.last_messages = []

    def generate(self, messages):
        self.last_messages = messages
        return self.reply

    def generate_stream(self, messages):
        self.last_messages = messages
        for chunk in self.reply.split(" "):
            yield chunk + " "


def test_reflection_chatbot_basic():
    mock = MockLLM("Fachlicher Reflexionsimpuls")
    bot = ReflectionChatbot(provider=mock)

    assert bot.get_history() == []
    assert bot.model_info["name"] == "Mock-Qwen"
    assert bot.model_info["is_real_llm"] is True

    reply = bot.chat("Ich bin mir unsicher wegen des Einkaufs.")
    assert "Fachlicher Reflexionsimpuls" in reply

    history = bot.get_history()
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "Ich bin mir unsicher wegen des Einkaufs."
    assert history[1]["role"] == "assistant"

    # CSV-Kontext testen
    csv_data = parse_csv_content("Datum;Notiz\n01.01;Test", filename="test.csv")
    bot.set_csv(csv_data)

    bot.chat("Zweite Frage")
    system_msg = mock.last_messages[0]
    assert system_msg["role"] == "system"
    assert "test.csv" not in system_msg["content"]
    assert "01.01" in system_msg["content"]
    assert "Test" in system_msg["content"]

    # History leeren
    bot.clear_history()
    assert bot.get_history() == []
    assert bot.csv_data is not None

    # Komplett zurücksetzen
    bot.clear()
    assert bot.csv_data is None
    assert bot.get_history() == []


def test_chatbot_streaming():
    mock = MockLLM("Token1 Token2 Token3")
    bot = ReflectionChatbot(provider=mock)

    chunks = list(bot.chat_stream("Erzähl mir was."))
    assert len(chunks) > 0
    full = "".join(chunks).strip()
    assert full == "Token1 Token2 Token3"
    assert len(bot.get_history()) == 2


def test_chatbot_export_markdown():
    mock = MockLLM("Reflexionsantwort")
    bot = ReflectionChatbot(provider=mock)
    bot.chat("Frage zur Selbstständigkeit")

    md = bot.export_markdown()
    assert "# Fachdialog & Reflexion" in md
    assert "Frage zur Selbstständigkeit" in md
    assert "Reflexionsantwort" in md


def test_reflection_engine_responses():
    engine = ReflectionEngine()
    assert engine.is_real_llm is False

    # Unsicherheit
    reply_unsicher = engine.generate([
        {"role": "user", "content": "Ich fühle mich bei diesem Konflikt unsicher und überfordert."}
    ])
    assert "Reflexionsfragen" in reply_unsicher
    assert "Verunsicherung" in reply_unsicher or "überfordert" in reply_unsicher or "Erklärungen" in reply_unsicher

    # Ressourcen & Fortschritt
    reply_ressource = engine.generate([
        {"role": "user", "content": "Er hat das Ziel sehr selbständig gemeistert und Fortschritte gemacht."}
    ])
    assert "Ressourcen" in reply_ressource or "Fortschritt" in reply_ressource

    # Überblick / Daten
    reply_daten = engine.generate([
        {"role": "system", "content": "CSV-Datei: test.csv\nEintrag 1: Haushalt"},
        {"role": "user", "content": "Gib mir einen Überblick über die Daten."}
    ])
    assert "Verlaufsdaten" in reply_daten or "Einträge" in reply_daten

    # Allgemeiner Reflexionsimpuls
    reply_generic = engine.generate([
        {"role": "user", "content": "Heute fand das Gespräch wie geplant statt."}
    ])
    assert "reflektieren" in reply_generic


def test_create_llm_provider_fallback(monkeypatch):
    monkeypatch.setenv("SOB_ALLOW_HEURISTIC", "1")
    monkeypatch.setattr("chat_app.chatbot.find_available_model", lambda: None)
    provider = create_llm_provider()
    assert isinstance(provider, ReflectionEngine)
    assert provider.is_real_llm is False


def test_list_and_find_available_models():
    models = list_available_models()
    assert isinstance(models, list)
    if models:
        first = models[0]
        assert "name" in first
        assert "size_gb" in first
        assert first["name"].endswith(".gguf")

    detected = find_available_model()
    if detected:
        assert detected.endswith(".gguf")


# ---------------------------------------------------------------------------
# Befund 3: Modell-Auswahl (14B vor 7B, SOB_MODEL_PATH Vorrang & Validierung)
# ---------------------------------------------------------------------------

def test_find_available_model_prefers_14b_over_7b(tmp_path, monkeypatch):
    """14B-Instruct (bevorzugt Q4_K_M) wird vor 7B als Standard gewählt."""
    m14b = tmp_path / "Qwen2.5-14B-Instruct-Q4_K_M.gguf"
    m7b = tmp_path / "Qwen2.5-7B-Instruct-Q4_K_M.gguf"
    m14b.touch()
    m7b.touch()

    monkeypatch.delenv("SOB_MODEL_PATH", raising=False)
    monkeypatch.delenv("MODEL_PATH", raising=False)
    monkeypatch.setattr(
        "chat_app.chatbot.list_available_models",
        lambda: [{"name": m7b.name, "path": str(m7b)}, {"name": m14b.name, "path": str(m14b)}],
    )

    chosen = find_available_model()
    assert chosen == str(m14b)


def test_find_available_model_only_7b(tmp_path, monkeypatch):
    """Wenn nur 7B vorhanden ist, wird 7B gewählt."""
    m7b = tmp_path / "Qwen2.5-7B-Instruct-Q4_K_M.gguf"
    m7b.touch()

    monkeypatch.delenv("SOB_MODEL_PATH", raising=False)
    monkeypatch.delenv("MODEL_PATH", raising=False)
    monkeypatch.setattr(
        "chat_app.chatbot.list_available_models",
        lambda: [{"name": m7b.name, "path": str(m7b)}],
    )

    chosen = find_available_model()
    assert chosen == str(m7b)


def test_find_available_model_env_precedence(tmp_path, monkeypatch):
    """SOB_MODEL_PATH (und MODEL_PATH) hat Vorrang vor der Discovery."""
    custom = tmp_path / "MeinModell.gguf"
    custom.touch()
    m14b = tmp_path / "Qwen2.5-14B-Instruct-Q4_K_M.gguf"
    m14b.touch()

    monkeypatch.setenv("SOB_MODEL_PATH", str(custom))
    monkeypatch.setattr(
        "chat_app.chatbot.list_available_models",
        lambda: [{"name": m14b.name, "path": str(m14b)}],
    )

    chosen = find_available_model()
    assert chosen == str(custom)


@pytest.mark.parametrize("env_var", ["SOB_MODEL_PATH", "MODEL_PATH"])
def test_find_available_model_nonexistent_env_raises_visible_error(env_var, tmp_path, monkeypatch, caplog):
    """Nicht existierender SOB_MODEL_PATH / MODEL_PATH wirft FileNotFoundError mit ❌ und loggt Error."""
    nonexistent = tmp_path / "NichtVorhanden.gguf"
    fallback_14b = tmp_path / "Qwen2.5-14B-Instruct-Q4_K_M.gguf"
    fallback_14b.touch()

    monkeypatch.delenv("SOB_MODEL_PATH", raising=False)
    monkeypatch.delenv("MODEL_PATH", raising=False)
    monkeypatch.setenv(env_var, str(nonexistent))
    monkeypatch.setattr(
        "chat_app.chatbot.list_available_models",
        lambda: [{"name": fallback_14b.name, "path": str(fallback_14b)}],
    )

    import logging
    with caplog.at_level(logging.ERROR):
        with pytest.raises(FileNotFoundError) as exc_info:
            find_available_model()

    assert "❌" in str(exc_info.value)
    assert env_var in str(exc_info.value)
    # Deutliche ❌-Log-Warnung/Error
    assert any("❌" in record.message and env_var in record.message for record in caplog.records)


def test_create_llm_provider_propagates_nonexistent_env_error(tmp_path, monkeypatch):
    """create_llm_provider reicht FileNotFoundError bei gesetztem fehlerhaften Pfad weiter."""
    nonexistent = tmp_path / "NichtVorhanden.gguf"
    monkeypatch.setenv("SOB_MODEL_PATH", str(nonexistent))

    with pytest.raises(FileNotFoundError) as exc_info:
        create_llm_provider()
    assert "❌" in str(exc_info.value)
    assert "SOB_MODEL_PATH" in str(exc_info.value)


def test_find_available_model_hash_missing_fails(tmp_path, monkeypatch):
    """SHA-256 Integritätsprüfung bleibt für 14B unverändert strikt: ohne Hash Fehler."""
    from chat_app.model_integrity import ModelIntegrityError, verify_model_sha256

    dummy_14b = tmp_path / "Qwen2.5-14B-Instruct-Q4_K_M.gguf"
    dummy_14b.write_text("synthetischer inhalt")

    monkeypatch.delenv("SOB_MODEL_SHA256", raising=False)

    with pytest.raises(ModelIntegrityError, match="❌ Kein SHA-256-Hash für Modell"):
        verify_model_sha256(dummy_14b)


# ---------------------------------------------------------------------------
# FastAPI Endpoints Tests
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    app = create_app(chatbot=ReflectionChatbot(provider=ReflectionEngine()))
    return TestClient(app)


def test_get_index_html(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "Fachdialog & Reflexion" in res.text
    assert "text/html" in res.headers["content-type"]


def test_upload_csv_endpoint(client):
    csv_bytes = b"Datum;Kategorie;Notiz\n01.06.2025;Arzt;Begleitung erfolgt."
    files = {"file": ("test_verlauf.csv", io.BytesIO(csv_bytes), "text/csv")}

    res = client.post("/api/upload", files=files)
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert data["filename"] == "test_verlauf.csv"
    assert data["total_rows"] == 1
    assert "Datum" in data["headers"]


def test_upload_invalid_file(client):
    files = {"file": ("test.txt", io.BytesIO(b"irgendwas"), "text/plain")}
    res = client.post("/api/upload", files=files)
    assert res.status_code == 400


def test_chat_endpoint(client):
    res = client.post("/api/chat", json={"message": "Wie können wir den Klienten motivieren?"})
    assert res.status_code == 200
    data = res.json()
    assert "reply" in data
    assert len(data["reply"]) > 10
    assert len(data["history"]) == 2
    assert "model" in data


def test_chat_stream_endpoint(client):
    res = client.post("/api/chat-stream", json={"message": "Gedanken zum Alltag"})
    assert res.status_code == 200
    assert "text/event-stream" in res.headers["content-type"]
    text = res.text
    assert "data: " in text
    assert '"done": true' in text


def test_chat_endpoint_empty_message(client):
    res = client.post("/api/chat", json={"message": "   "})
    assert res.status_code == 400


def test_get_models_endpoint(client):
    res = client.get("/api/models")
    assert res.status_code == 200
    data = res.json()
    assert "models" in data
    assert "active" in data


def test_select_model_endpoint_invalid(client):
    res = client.post("/api/select-model", json={"model_name": "nicht_existent.gguf"})
    assert res.status_code == 400


def test_export_chat_endpoint(client):
    client.post("/api/chat", json={"message": "Testnachricht"})
    res = client.get("/api/export")
    assert res.status_code == 200
    data = res.json()
    assert "markdown" in data
    assert "Testnachricht" in data["markdown"]


def test_session_and_reset_endpoints(client):
    res = client.get("/api/session")
    assert res.status_code == 200
    data = res.json()
    assert data["has_csv"] is False
    assert len(data["messages"]) == 0
    assert "model" in data

    client.post("/api/chat", json={"message": "Hallo"})
    res = client.get("/api/session")
    assert len(res.json()["messages"]) == 2

    res = client.post("/api/reset-chat")
    assert res.status_code == 200
    assert len(client.get("/api/session").json()["messages"]) == 0


def test_diagnostics_and_logs_endpoints(client):
    res = client.get("/api/diagnostics")
    assert res.status_code == 200
    data = res.json()
    assert "pid" in data
    assert "search_paths" in data
    assert "active_model" in data
    assert "recent_logs" in data

    logs_res = client.get("/api/logs")
    assert logs_res.status_code == 200
    assert isinstance(logs_res.text, str)


def test_plan_api_endpoints(client):
    """Testet Plan-JSON-Upload, Dateiupload, Session-Darstellung und Reset via API."""
    # 1. Plan per JSON setzen
    res = client.post("/api/plan", json={"plan_text": "Alte Ziele: Wohnung sauber halten", "filename": "plan2025.txt"})
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert data["has_plan"] is True
    assert data["plan_info"]["filename"] == "plan2025.txt"

    # Session abfragen
    sess = client.get("/api/session").json()
    assert sess["has_plan"] is True
    assert sess["plan"]["info"]["filename"] == "plan2025.txt"
    assert "Wohnung sauber halten" in sess["plan"]["preview"]

    # Diagnostics abfragen
    diag = client.get("/api/diagnostics").json()
    assert diag["has_plan"] is True

    # 2. Plan leeren
    res_reset = client.post("/api/reset-plan")
    assert res_reset.status_code == 200
    assert res_reset.json()["success"] is True

    sess_after = client.get("/api/session").json()
    assert sess_after["has_plan"] is False
    assert sess_after["plan"] is None

    # 3. Validierungsfehler bei leerem Plan-Text
    res_invalid = client.post("/api/plan", json={"plan_text": "   "})
    assert res_invalid.status_code == 400

    # 4. Plan per Datei-Upload hochladen
    plan_file = io.BytesIO(b"Zielvereinbarung 2024: Teilnahme an Kochgruppe.")
    files = {"file": ("vorbericht.md", plan_file, "text/markdown")}
    res_upload = client.post("/api/upload-plan", files=files)
    assert res_upload.status_code == 200
    assert res_upload.json()["has_plan"] is True
    assert res_upload.json()["filename"] == "vorbericht.md"

    # Export enthält Plan
    exp = client.get("/api/export").json()
    assert "**Vorbericht / Gesamtplan:** vorbericht.md" in exp["markdown"]


# ---------------------------------------------------------------------------
# Vorbericht / Plan Ingestion & API Tests (Issue #96)
# ---------------------------------------------------------------------------

ROOT_DIR = Path(__file__).resolve().parents[1]
TEMPLATE_DOCX = ROOT_DIR / "templates" / "hamburg" / "Vorlage.docx"


def _create_sample_plan_docx(tmp_path: Path, filename: str = "Hans_Peter_SVB_ausgefuellt.docx") -> Path:
    """Erzeugt eine Test-DOCX mit echten Formularfeldern auf Basis der Hamburg-Vorlage."""
    root = fields.load_document_tree(TEMPLATE_DOCX)
    fields.set_formtext(root, "client_name_vorname", "Mustermann, Hans Peter")
    fields.set_formtext(root, "meta_leistungszeitraum_von", "01.01.2025")
    fields.set_formtext(root, "meta_leistungszeitraum_bis", "31.12.2025")

    # Abschnitt 1.1 (index 0): Wohnen
    fields.set_formtext(root, "sections_0_leitziel", "Erhalt der eigenen Wohnung.")
    fields.set_formtext(root, "sections_0_smarte_ziele", "Pünktliche Mietzahlung sicherstellen.")
    fields.set_formtext(root, "sections_0_bericht_zielerreichung", "Miete wurde monatlich pünktlich überwiesen.")

    # Abschnitt 1.2 (index 1): Alltägliche Lebensführung
    fields.set_formtext(root, "sections_1_leitziel", "Führt den Haushalt selbstständig.")
    fields.set_formtext(root, "sections_1_smarte_ziele", "Kocht 2x wöchentlich mit Unterstützung.")
    fields.set_formtext(root, "sections_1_bericht_zielerreichung", "Fortschritte sichtbar, Rezeptsammlung angelegt.")

    out = tmp_path / filename
    fields.save_document_tree(TEMPLATE_DOCX, root, out)
    return out


def _create_fallback_docx(tmp_path: Path, filename: str = "Zielvereinbarung_Hans_Peter.docx") -> Path:
    """Erzeugt ein Dokument via python-docx für den Test des Fallback-Parsers."""
    import docx
    doc = docx.Document()
    doc.add_heading("Zielvereinbarung für Hans Peter", 0)
    doc.add_paragraph("Klient: Hans Peter")
    doc.add_paragraph("Bereich 1: Wohnsituation")
    doc.add_paragraph("Leitziel: Wohnung sauber halten")
    out = tmp_path / filename
    doc.save(out)
    return out


def test_clean_form_value():
    assert clean_form_value("  Hallo Welt  ") == "Hallo Welt"
    assert clean_form_value("\u2002\u2002\u2002") == ""
    assert clean_form_value(None) == ""


def test_extract_client_name_from_filename():
    assert extract_client_name_from_filename("Hans_Peter_SVB_ausgefuellt.docx") == "Hans Peter"
    assert extract_client_name_from_filename("Max_Mustermann_Gesamtplan_2025.docx") == "Max Mustermann"
    assert extract_client_name_from_filename("plan.docx") == ""


def test_parse_plan_docx_from_template(tmp_path):
    sample_file = _create_sample_plan_docx(tmp_path)
    plan = parse_plan_docx(sample_file, filename="Hans_Peter_SVB_ausgefuellt.docx")

    assert plan.filename == "Hans_Peter_SVB_ausgefuellt.docx"
    assert "Hans Peter" in plan.client_name
    assert plan.zeitraum_von == "01.01.2025"
    assert plan.zeitraum_bis == "31.12.2025"
    assert plan.goals_count == 2

    # Überprüfe Abschnitte
    goal_ids = [g.section_id for g in plan.goals]
    assert "1.1" in goal_ids
    assert "1.2" in goal_ids

    # Vorschau & Kontext
    preview = plan.preview
    assert "Klient: Mustermann, Hans Peter" in preview
    assert "2 Lebensbereiche erfasst" in preview
    assert "Wohnsituation" in preview or "1.1" in preview

    context = plan.to_context_text()
    assert "Hans_Peter_SVB_ausgefuellt.docx" in context
    assert "Erhalt der eigenen Wohnung" in context
    assert "Miete wurde monatlich pünktlich überwiesen" in context


def test_parse_plan_docx_fallback(tmp_path):
    fallback_file = _create_fallback_docx(tmp_path)
    plan = parse_plan_docx(fallback_file, filename="Zielvereinbarung_Hans_Peter.docx")

    assert plan.filename == "Zielvereinbarung_Hans_Peter.docx"
    assert "Hans Peter" in plan.client_name
    assert plan.goals_count >= 1


def test_chatbot_with_plan_context(tmp_path):
    sample_file = _create_sample_plan_docx(tmp_path)
    plan = parse_plan_docx(sample_file, filename="Hans_Peter_SVB_ausgefuellt.docx")

    mock = MockLLM("Reflexion zu Vorberichtszielen")
    bot = ReflectionChatbot(provider=mock)
    bot.set_plan(plan)

    assert bot.plan_data is not None
    assert bot.plan_data.goals_count == 2

    reply = bot.chat("Welche Ziele sollten wir weiterverfolgen?")
    assert "Reflexion zu Vorberichtszielen" in reply

    # System-Payload überprüfen
    system_msg = mock.last_messages[0]
    assert system_msg["role"] == "system"
    assert "Hans_Peter_SVB_ausgefuellt.docx" in system_msg["content"]
    assert "Erhalt der eigenen Wohnung" in system_msg["content"]

    # Export überprüfen
    md = bot.export_markdown()
    assert "**Vorbericht / Gesamtplan:** Hans_Peter_SVB_ausgefuellt.docx (2 Ziele" in md


def test_upload_plan_endpoint_success(client, tmp_path):
    sample_file = _create_sample_plan_docx(tmp_path)
    content_bytes = sample_file.read_bytes()

    files = {"file": ("Hans_Peter_SVB_ausgefuellt.docx", io.BytesIO(content_bytes), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}
    res = client.post("/api/upload-plan", files=files)

    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert data["filename"] == "Hans_Peter_SVB_ausgefuellt.docx"
    assert data["goals_count"] == 2
    assert "preview" in data
    assert "client_name" in data
    assert "Hans Peter" in data["client_name"]


def test_upload_plan_endpoint_with_dummy_hans_peter(client):
    dummy_path = ROOT_DIR / "dummy" / "Hans_Peter_SVB_ausgefuellt.docx"
    if not dummy_path.exists():
        pytest.skip("dummy/Hans_Peter_SVB_ausgefuellt.docx nicht vorhanden")

    files = {
        "file": (
            "Hans_Peter_SVB_ausgefuellt.docx",
            io.BytesIO(dummy_path.read_bytes()),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    }
    res = client.post("/api/upload-plan", files=files)
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert data["filename"] == "Hans_Peter_SVB_ausgefuellt.docx"
    assert data["goals_count"] == 7
    assert "Hans Peter" in data["client_name"]
    assert "preview" in data


def test_upload_plan_endpoint_invalid_file(client):
    files = {"file": ("plan.pdf", io.BytesIO(b"%PDF-fake"), "application/pdf")}
    res = client.post("/api/upload-plan", files=files)
    assert res.status_code == 400
    assert "docx" in res.json()["detail"].lower()


def test_reset_plan_endpoint(client, tmp_path):
    sample_file = _create_sample_plan_docx(tmp_path)
    files = {"file": ("Hans_Peter_SVB_ausgefuellt.docx", io.BytesIO(sample_file.read_bytes()), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}
    res = client.post("/api/upload-plan", files=files)
    assert res.status_code == 200

    # Session prüfen: Plan aktiv
    session_res = client.get("/api/session")
    assert session_res.json()["has_plan"] is True
    assert session_res.json()["plan_goals_count"] == 2

    # Reset Plan
    reset_res = client.post("/api/reset-plan")
    assert reset_res.status_code == 200
    assert reset_res.json()["success"] is True

    # Session erneut prüfen: Plan gelöscht
    session_after = client.get("/api/session")
    assert session_after.json()["has_plan"] is False
    assert session_after.json()["plan_filename"] is None
    assert session_after.json()["plan_goals_count"] == 0


def test_chat_stream_endpoint_with_csv_and_plan(client, tmp_path):
    # 1. CSV hochladen
    csv_bytes = b"Datum;Kategorie;Notiz\n01.06.2025;Wohnen;Miete puenktlich ueberwiesen."
    client.post("/api/upload", files={"file": ("verlauf.csv", io.BytesIO(csv_bytes), "text/csv")})

    # 2. Plan hochladen
    sample_file = _create_sample_plan_docx(tmp_path)
    client.post("/api/upload-plan", files={"file": ("Hans_Peter_SVB_ausgefuellt.docx", io.BytesIO(sample_file.read_bytes()), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})

    # 3. Chat Streaming aufrufen
    stream_res = client.post("/api/chat-stream", json={"message": "Wie passen die Verlaufsdaten zu den Vorberichtszielen?"})
    assert stream_res.status_code == 200
    assert "text/event-stream" in stream_res.headers["content-type"]

    body = stream_res.text
    assert "data: " in body
    assert '"done": true' in body


def test_export_chat_endpoint_with_plan_header(client, tmp_path):
    sample_file = _create_sample_plan_docx(tmp_path)
    client.post("/api/upload-plan", files={"file": ("Hans_Peter_SVB_ausgefuellt.docx", io.BytesIO(sample_file.read_bytes()), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
    client.post("/api/chat", json={"message": "Reflexionsfrage zu Zielen"})

    res = client.get("/api/export")
    assert res.status_code == 200
    markdown = res.json()["markdown"]

    assert "**Vorbericht / Gesamtplan:** Hans_Peter_SVB_ausgefuellt.docx (2 Ziele" in markdown
    assert "Reflexionsfrage zu Zielen" in markdown


def test_session_and_diagnostics_with_plan(client, tmp_path):
    sample_file = _create_sample_plan_docx(tmp_path)
    client.post("/api/upload-plan", files={"file": ("Hans_Peter_SVB_ausgefuellt.docx", io.BytesIO(sample_file.read_bytes()), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})

    # GET /api/session
    res_session = client.get("/api/session")
    assert res_session.status_code == 200
    data_session = res_session.json()
    assert data_session["has_plan"] is True
    assert data_session["plan_filename"] == "Hans_Peter_SVB_ausgefuellt.docx"
    assert data_session["plan_goals_count"] == 2
    assert data_session["plan"] is not None
    assert "preview" in data_session["plan"]

    # GET /api/diagnostics
    res_diag = client.get("/api/diagnostics")
    assert res_diag.status_code == 200
    data_diag = res_diag.json()
    assert data_diag["has_plan"] is True
    assert data_diag["plan_filename"] == "Hans_Peter_SVB_ausgefuellt.docx"
    assert data_diag["plan_goals_count"] == 2

    # Reset all
    res_reset = client.post("/api/reset")
    assert res_reset.status_code == 200
    after_reset = client.get("/api/session").json()
    assert after_reset["has_plan"] is False
    assert after_reset["has_csv"] is False
    assert len(after_reset["messages"]) == 0


def test_plan_loader_maps_sections_without_data_loss(monkeypatch, tmp_path):
    """Prüft, dass PreviousPlanData.sections ohne Datenverlust auf PlanData gemappt wird."""
    from sob_generator.previous_sob import PlanSectionGoal, PreviousPlanData
    import sob_generator.previous_sob as prev_module

    fake_plan = PreviousPlanData(
        client_name="Maria Muster",
        zeitraum="01.03.2025 bis 28.02.2026",
        developments="Umzug in eigene Wohnung erfolgt.",
        sections={
            "1.1": PlanSectionGoal(
                section_id="1.1",
                title="Wohnen",
                leitziel="Wohnung halten",
                smarte_ziele="Miete termingerecht überweisen",
                stand_zielerreichung="Keine Mietrückstände vorhanden",
                vorschlag_ziele="Dauerauftrag einrichten",
                applies=True,
            )
        },
    )

    monkeypatch.setattr(prev_module, "read_previous_plan", lambda path: fake_plan)

    dummy_docx = tmp_path / "test_fake_plan.docx"
    import io, zipfile
    _buf = io.BytesIO()
    with zipfile.ZipFile(_buf, "w") as _z:
        _z.writestr("word/document.xml", b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>')
    dummy_docx.write_bytes(_buf.getvalue())

    plan = parse_plan_docx(dummy_docx, filename="test_fake_plan.docx")
    assert plan.client_name == "Maria Muster"
    assert plan.zeitraum_von == "01.03.2025"
    assert plan.zeitraum_bis == "28.02.2026"
    assert "Umzug in eigene Wohnung" in plan.raw_summary
    assert plan.goals_count == 1
    assert len(plan.goals) == 1

    goal = plan.goals[0]
    assert goal.section_id == "1.1"
    assert goal.section_title == "Wohnen"
    assert goal.leitziel == "Wohnung halten"
    assert goal.smarte_ziele == "Miete termingerecht überweisen"
    assert goal.stand_zielerreichung == "Keine Mietrückstände vorhanden"
    assert goal.vorschlag_ziele == "Dauerauftrag einrichten"
    assert goal.applies is True

    # Kontext-String enthält alle Felder
    ctx = plan.to_context_text()
    assert "Dauerauftrag einrichten" in ctx
    assert "Keine Mietrückstände vorhanden" in ctx
    assert "Umzug in eigene Wohnung" in ctx


def test_dsgvo_logging_no_client_name_or_filename(tmp_path, monkeypatch):
    """Prüft, dass weder Klientennamen noch benutzerkontrollierte Dateinamen in die Log-Datei geschrieben werden (DSGVO)."""
    import chat_app.logger as app_logger

    test_log_file = tmp_path / "test_chat_app.log"
    monkeypatch.setattr(app_logger, "LOG_FILE", test_log_file)

    # Filehandler temporär auf Testdatei umbiegen
    import logging
    test_handler = logging.FileHandler(test_log_file, encoding="utf-8")
    app_logger.logger.addHandler(test_handler)
    try:
        app_logger.log_plan_loaded(filename="Hans_Mustermann_Plan.docx", goals_count=5, client_name="Hans Mustermann")
        app_logger.log_csv_loaded(filename="Erika_Musterfrau_Verlauf.csv", total_rows=12, headers=["Klient", "Diagnose"])
        test_handler.flush()

        log_content = test_log_file.read_text(encoding="utf-8")
        assert "Hans Mustermann" not in log_content
        assert "Hans_Mustermann_Plan.docx" not in log_content
        assert "Erika_Musterfrau_Verlauf.csv" not in log_content
        assert "Diagnose" not in log_content
        assert "5 Ziele aktiv" in log_content
        assert "12 Zeilen" in log_content
        assert "(anonymisiert)" in log_content
    finally:
        app_logger.logger.removeHandler(test_handler)
        test_handler.close()


def test_no_duplicate_reset_plan_route(client):
    """Prüft, dass POST /api/reset-plan genau einmal in den Routen registriert ist."""
    app = client.app
    reset_plan_routes = [
        route for route in app.routes
        if getattr(route, "path", None) == "/api/reset-plan" and "POST" in getattr(route, "methods", set())
    ]
    assert len(reset_plan_routes) == 1, f"Erwartet genau 1 Route für POST /api/reset-plan, gefunden: {len(reset_plan_routes)}"


def test_upload_plan_returns_zeitraum_fields(client, tmp_path):
    """Prüft, dass der Upload-Endpunkt zeitraum_von und zeitraum_bis in der Antwort liefert."""
    sample_file = _create_sample_plan_docx(tmp_path)
    files = {"file": ("Hans_Peter_SVB_ausgefuellt.docx", io.BytesIO(sample_file.read_bytes()), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}
    res = client.post("/api/upload-plan", files=files)
    assert res.status_code == 200
    data = res.json()
    assert "zeitraum_von" in data
    assert "zeitraum_bis" in data
    assert data["zeitraum_von"] == "01.01.2025"
    assert data["zeitraum_bis"] == "31.12.2025"


def test_upload_endpoints_do_not_log_client_names_or_filenames(client, tmp_path, monkeypatch):
    """Prüft, dass über Endpunkte hochgeladene Dateinamen nicht in die Logdatei gelangen."""
    import chat_app.logger as app_logger

    test_log_file = tmp_path / "test_upload_privacy.log"
    monkeypatch.setattr(app_logger, "LOG_FILE", test_log_file)

    import logging
    test_handler = logging.FileHandler(test_log_file, encoding="utf-8")
    app_logger.logger.addHandler(test_handler)
    try:
        sample_file = _create_sample_plan_docx(tmp_path)
        files = {"file": ("Hans_Mustermann_Hilfeplan_2025.docx", io.BytesIO(sample_file.read_bytes()), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}
        res = client.post("/api/upload-plan", files=files)
        assert res.status_code == 200

        csv_content = "Datum;Text\n01.01.2025;Gespräch"
        csv_files = {"file": ("Erika_Musterfrau_Vivendi_PD.csv", io.BytesIO(csv_content.encode("utf-8")), "text/csv")}
        res_csv = client.post("/api/upload", data={"new_case": "true"}, files=csv_files)
        assert res_csv.status_code == 200

        test_handler.flush()
        log_content = test_log_file.read_text(encoding="utf-8")
        assert "Hans_Mustermann_Hilfeplan_2025.docx" not in log_content
        assert "Erika_Musterfrau_Vivendi_PD.csv" not in log_content
        assert "Hans_Mustermann" not in log_content
        assert "Erika_Musterfrau" not in log_content
    finally:
        app_logger.logger.removeHandler(test_handler)
        test_handler.close()



def test_ui_accordion_accessibility_markup():
    """Prüft, dass der Accordion-Header als tastaturbedienbarer Button mit ARIA-Attributen implementiert ist."""
    html_path = ROOT_DIR / "chat_app" / "templates" / "index.html"
    html_content = html_path.read_text(encoding="utf-8")

    assert 'id="planAccordionButton"' in html_content
    assert 'aria-expanded="false"' in html_content
    assert 'aria-controls="planAccordionContent"' in html_content
    assert '<button' in html_content
    assert 'class="plan-card-header"' in html_content


def test_quick_prompt_chips_markup_and_accessibility():
    """Prüft die 4 fachspezifischen Schnellimpuls-Buttons in index.html auf Markup, Labels, Prompts und Barrierefreiheit."""
    html_path = ROOT_DIR / "chat_app" / "templates" / "index.html"
    html_content = html_path.read_text(encoding="utf-8")

    # Toolbar-Container
    assert 'class="chips-bar"' in html_content
    assert 'role="toolbar"' in html_content
    assert 'aria-label="Schnellimpulse für den Hilfeplan-Prozess"' in html_content

    # Semantische Button-Elemente
    assert '<button\n          type="button"\n          class="chip"' in html_content or '<button type="button" class="chip"' in html_content

    # 4 Labels
    assert "📊 Bericht zu Zielerreichung" in html_content
    assert "🎯 Vorschlag neue Ziele" in html_content
    assert "🛠️ Vorschlag neue Maßnahmen" in html_content
    assert "👤 Klientenperspektive einnehmen" in html_content

    # 4 Exakte Prompts
    prompt_1 = "Reflexion aus den Verlaufsdaten im Bezug auf die Ziele des letzten Bewilligungszeitraums"
    prompt_2 = "Schlage auf Basis des bisherigen Dialogs konkrete, SMART formulierte neue Ziele für die besprochenen Lebensbereiche vor."
    prompt_3 = "Welche konkreten fachlichen Maßnahmen und Unterstützungsleistungen werden aus Sicht des Leistungsanbieters vorgeschlagen, um diese Ziele zu erreichen?"
    prompt_4 = "Wie stellt sich die Situation und Entwicklung aus der Perspektive der/des Klient*in dar? Welche Wünsche, Motive und Hürden stehen im Vordergrund?"

    assert prompt_1 in html_content
    assert prompt_2 in html_content
    assert prompt_3 in html_content
    assert prompt_4 in html_content

    # Keine veralteten Chips mehr
    assert "🌱 Ressourcen identifizieren" not in html_content
    assert "🔍 Muster reflektieren" not in html_content
    assert "🎯 Selbstständigkeit reflektieren" not in html_content
    assert "👤 Klientenperspektive wagen" not in html_content

    # CSS Barrierefreiheit (Focus, Disabled)
    assert ".chip:focus-visible" in html_content
    assert ".chip:disabled" in html_content

    # JS Deaktivierungs-Handling
    assert "setChipsDisabled(true)" in html_content
    assert "setChipsDisabled(false)" in html_content


def test_reflection_engine_supports_quick_prompt_topics():
    """Prüft, dass ReflectionEngine auf alle 4 Schnellimpuls-Themen fachspezifisch antwortet."""
    from chat_app.chatbot import ReflectionEngine

    engine = ReflectionEngine()

    # 1. Zielerreichung / Bewilligungszeitraum
    reply_ziel = engine.generate([
        {
            "role": "system",
            "content": "### Vorbericht\nLeitziel: Selbständigkeit\n### Verlaufsdaten (CSV-Beobachtungen):\nTermin 1",
        },
        {
            "role": "user",
            "content": "Reflexion aus den Verlaufsdaten im Bezug auf die Ziele des letzten Bewilligungszeitraums",
        },
    ])
    assert "Zielerreichung" in reply_ziel
    assert "Reflexionsfragen zur Zielerreichung" in reply_ziel

    # 2. SMART neue Ziele
    reply_smart = engine.generate([
        {
            "role": "user",
            "content": "Schlage auf Basis des bisherigen Dialogs konkrete, SMART formulierte neue Ziele für die besprochenen Lebensbereiche vor.",
        }
    ])
    assert "SMART" in reply_smart
    assert "Zieldefinition" in reply_smart or "Leitziel" in reply_smart

    # 3. Maßnahmen des Leistungsanbieters
    reply_massnahmen = engine.generate([
        {
            "role": "user",
            "content": "Welche konkreten fachlichen Maßnahmen und Unterstützungsleistungen werden aus Sicht des Leistungsanbieters vorgeschlagen, um diese Ziele zu erreichen?",
        }
    ])
    assert "Leistungsanbieter" in reply_massnahmen
    assert "Maßnahmenvorschlag" in reply_massnahmen or "Assistenzformate" in reply_massnahmen

    # 4. Klientenperspektive
    reply_klient = engine.generate([
        {
            "role": "user",
            "content": "Wie stellt sich die Situation und Entwicklung aus der Perspektive der/des Klient*in dar? Welche Wünsche, Motive und Hürden stehen im Vordergrund?",
        }
    ])
    assert "Klientenperspektive" in reply_klient
    assert "Perspektivwechsel" in reply_klient


def test_parse_plan_docx_zero_disk_footprint(tmp_path, monkeypatch):
    """Sichert ab, dass parse_plan_docx rein speicherbasiert arbeitet und keine temporären Dateien anlegt."""
    import tempfile
    from chat_app.plan_loader import parse_plan_docx
    from sob_generator.previous_sob import UniversalPlanImporter

    sample_file = _create_sample_plan_docx(tmp_path)
    docx_bytes = sample_file.read_bytes()

    def forbidden_tempfile(*args, **kwargs):
        raise AssertionError("tempfile darf für den Zero-Disk-Footprint-Planimport nicht aufgerufen werden!")

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", forbidden_tempfile)

    # 1. Direkter Aufruf von parse_plan_docx mit Bytes
    plan_data = parse_plan_docx(docx_bytes, filename="Erika_Musterfrau_Plan.docx")
    assert plan_data.goals_count > 0
    assert plan_data.client_name == "Mustermann, Hans Peter"

    # 2. Direkter Aufruf von UniversalPlanImporter mit io.BytesIO
    importer = UniversalPlanImporter()
    plan_result = importer.extract(io.BytesIO(docx_bytes), filename="Erika_Musterfrau_Plan.docx")
    assert len(plan_result.sections) > 0
    assert plan_result.client_name == "Mustermann, Hans Peter"


def test_presentation_mode_toggle_and_url_param_markup():
    """Prüft, dass der Umschalter für Fachkraft-Ansicht vs. Entwickler-Modus und die URL-Parameter-Logik vorhanden sind (#110)."""
    html_path = ROOT_DIR / "chat_app" / "templates" / "index.html"
    html_content = html_path.read_text(encoding="utf-8")

    # Toggle Container und Buttons
    assert 'id="viewModeToggle"' in html_content
    assert 'role="group"' in html_content
    assert 'aria-label="Ansichtsmodus"' in html_content
    assert 'id="btnModeFachkraft"' in html_content
    assert 'id="btnModeDev"' in html_content
    assert '✨ Fachkraft-Ansicht' in html_content
    assert '🛠️ Entwickler-Modus' in html_content

    # URL Parameter und LocalStorage Logik
    assert 'getInitialPresentationMode()' in html_content
    assert "params.has('demo')" in html_content
    assert "params.has('view')" in html_content
    assert "localStorage.getItem('sob_presentation_mode')" in html_content
    assert "localStorage.setItem('sob_presentation_mode'" in html_content
    assert 'setPresentationMode(' in html_content


def test_demo_mode_ui_elements_and_ux_cleanup():
    """Prüft, dass technische Interna im Demo-Modus ausgeblendet und durch fachliche Bezeichnungen ersetzt werden (#110)."""
    html_path = ROOT_DIR / "chat_app" / "templates" / "index.html"
    html_content = html_path.read_text(encoding="utf-8")

    # Body hat standardmäßig die demo-mode Klasse
    assert '<body class="demo-mode">' in html_content

    # Modellauswahl ist im Entwickler-Modus (dev-only)
    assert 'class="model-select dev-only"' in html_content
    # Demo-Badge ersetzt Dropdown
    assert 'id="demoModelBadge"' in html_content
    assert '🔒 Lokaler KI-Assistent' in html_content

    # Status-Pill im Demo-Modus
    assert "🟢 Lokaler KI-Assistent (Aktiv)" in html_content

    # Sidebar: Datensicherheit & DSGVO statt roher Tech-Daten
    assert 'id="sidebarSystemStatusSection"' in html_content
    assert 'Datensicherheit & System' in html_content
    assert '100% Offline auf diesem Gerät' in html_content
    assert 'Keine Cloud-Übertragung (DSGVO Art. 25 Privacy by Design)' in html_content

    # Inferenz-Logs & Diagnose ist dev-only
    assert 'id="devDiagnosticsSection"' in html_content
    assert 'class="dev-only" id="devDiagnosticsSection"' in html_content

    # Chat-Bubble Header verwendet im Demo-Modus "Lokaler KI-Assistent"
    assert "const badgeText = isDemo ? 'Lokaler KI-Assistent'" in html_content


def test_csv_fach_card_and_details_toggle_markup():
    """Prüft die strukturierte Fach-Karte für CSV-Falldaten und den Details-Toggle Button (#110)."""
    html_path = ROOT_DIR / "chat_app" / "templates" / "index.html"
    html_content = html_path.read_text(encoding="utf-8")

    # Strukturierte Fach-Karte
    assert 'id="csvFachCard"' in html_content
    assert 'id="csvClientRow"' in html_content
    assert 'id="csvClientName"' in html_content
    assert 'id="csvSourceRow"' in html_content
    assert 'id="csvSource"' in html_content
    assert 'id="csvRecordsRow"' in html_content
    assert 'id="csvRecordsCount"' in html_content

    # Details anzeigen Button
    assert 'id="csvToggleDetailsBtn"' in html_content
    assert 'onclick="toggleCsvDetails()"' in html_content
    assert 'aria-expanded="false"' in html_content
    assert 'aria-controls="csvCardPreview"' in html_content
    assert 'Details anzeigen' in html_content

    # CSS Regeln für Fach-Karte und Toggle
    assert 'body.demo-mode #csvFachCard' in html_content
    assert 'body.demo-mode #csvCardPreview' in html_content
    assert 'body.demo-mode #csvCardPreview.open' in html_content
    assert 'body.dev-mode #csvFachCard' in html_content


def test_system_notifications_decoupled_from_chat_history():
    """Prüft, dass Systemmeldungen nicht mehr als Chatblasen in messagesContainer gerendert werden (#110)."""
    html_path = ROOT_DIR / "chat_app" / "templates" / "index.html"
    html_content = html_path.read_text(encoding="utf-8")

    # In appendSystemNotification darf messagesContainer.appendChild(div) NICHT mehr vorkommen!
    # Extrahiere die Funktion appendSystemNotification
    start_fn = html_content.find("function appendSystemNotification(text)")
    end_fn = html_content.find("function appendUserMessage(content)")
    assert start_fn != -1 and end_fn != -1
    fn_body = html_content[start_fn:end_fn]

    assert "showToast(text)" in fn_body
    assert "messagesContainer.appendChild" not in fn_body
    assert "pulsePill" in fn_body


def test_upload_csv_returns_client_name_and_source_and_dsgvo(client, tmp_path, monkeypatch):
    """Prüft, dass /api/upload Klient und Quelle fachlich extrahiert und strikt DSGVO-konform loggt (#110)."""
    import chat_app.logger as app_logger
    import logging

    test_log_file = tmp_path / "test_csv_privacy_110.log"
    monkeypatch.setattr(app_logger, "LOG_FILE", test_log_file)
    test_handler = logging.FileHandler(test_log_file, encoding="utf-8")
    app_logger.logger.addHandler(test_handler)

    try:
        # 1. Vivendi CSV mit Personennamen im Dateinamen
        csv_content = "Datum;Kategorie;Text\n01.03.2025;Gespräch;Klient schildert Einkaufswunsch."
        files = {"file": ("Erika_Musterfrau_Vivendi_PD.csv", io.BytesIO(csv_content.encode("utf-8")), "text/csv")}
        res = client.post("/api/upload", files=files)
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is True
        assert data["client_name"] == "Erika Musterfrau"
        assert data["source"] == "Vivendi PD Verlaufsbericht"
        assert data["total_rows"] == 1

        # 2. Session Endpoint prüfen
        res_session = client.get("/api/session")
        assert res_session.status_code == 200
        session_data = res_session.json()
        assert session_data["has_csv"] is True
        assert session_data["csv"]["client_name"] == "Erika Musterfrau"
        assert session_data["csv"]["source"] == "Vivendi PD Verlaufsbericht"

        # 3. DSGVO Prüfung der Log-Datei
        test_handler.flush()
        log_content = test_log_file.read_text(encoding="utf-8")
        assert "Erika Musterfrau" not in log_content
        assert "Erika_Musterfrau_Vivendi_PD.csv" not in log_content
    finally:
        app_logger.logger.removeHandler(test_handler)
        test_handler.close()


def test_csv_inherits_client_name_from_plan(client):
    """Prüft, dass CSV den Klientennamen aus einem vorher geladenen Plan erbt oder umgekehrt (#110)."""
    # Reset alles
    client.post("/api/reset")

    # CSV mit neutralem Dateinamen hochladen
    csv_bytes = b"Datum;Text\n02.04.2025;Notiz"
    res_csv = client.post("/api/upload", files={"file": ("verlauf.csv", io.BytesIO(csv_bytes), "text/csv")})
    assert res_csv.status_code == 200
    assert res_csv.json()["client_name"] == "Klient*in (anonymisiert)"
    assert res_csv.json()["source"] == "Verlaufsdokumentation (verlauf.csv)"


def test_card_filename_wrapping_and_containment(client):
    """Prüft, dass lange Dateinamen in CSV- und Plan-Karten nicht aus der Box herausragen und Boxen nicht kollabieren."""
    res = client.get("/")
    assert res.status_code == 200
    html = res.text

    # 1. CSS-Regeln für Ellipsis und Min-Width
    assert "text-overflow: ellipsis" in html
    assert "white-space: nowrap" in html
    assert "#csvCardFilename" in html
    assert "#planCardFilename" in html

    # 2. Card Container Flex-Shrink & Containment Schutz
    assert "aside > *" in html
    assert "flex-shrink: 0" in html
    assert ".csv-card" in html
    assert ".plan-card" in html

    # 3. Tooltip-Attribut bei UI-Updates
    assert "planCardFilename.title = filename" in html
    assert "csvCardFilename.title = filename" in html






