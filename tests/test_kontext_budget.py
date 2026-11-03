"""Tests für das Token-Budget mit Preflight-Prüfung und sichtbaren Kürzungshinweisen (Issue #17)."""
from __future__ import annotations

import json
from typing import Generator, List, Dict, Optional
import pytest
from starlette.testclient import TestClient

from chat_app.budget import (
    BASE_CTX,
    BUDGET_SHARES,
    BudgetManager,
    ContextStatus,
    PromptTooLongError,
    calculate_budget_table,
)
from chat_app.chatbot import LLMProvider, ReflectionChatbot, SYSTEM_PROMPT_TEMPLATE
from chat_app.context_builder import ContextBuilder, BuiltContext
from chat_app.app import create_app
from chat_app.case import CaseSession
from chat_app.csv_loader import CsvData
from chat_app.plan_loader import PlanData, PlanGoalItem


class MockTokenizerLLM(LLMProvider):
    """Mock-Provider mit exakter Wort-Tokenisierung (len(text.split()))."""

    model_name: str = "MockTokenizerLLM"
    is_real_llm: bool = True
    details: str = "Synthetischer Mock für Budget-Tests"

    def __init__(
        self,
        n_ctx: int = 16384,
        max_tokens: int = 2048,
        reply: str = "Reflexionsimpuls zu Bereich 3",
    ):
        self._n_ctx = n_ctx
        self._max_tokens = max_tokens
        self.reply = reply
        self.generate_called = False
        self.last_messages: List[Dict[str, str]] = []

    @property
    def n_ctx(self) -> int:
        return self._n_ctx

    @property
    def max_tokens(self) -> int:
        return self._max_tokens

    def count_tokens(self, text: str) -> int:
        if not text:
            return 0
        return len(text.split())

    def generate(self, messages: List[Dict[str, str]]) -> str:
        self.generate_called = True
        self.last_messages = [dict(m) for m in messages]
        return self.reply

    def generate_stream(self, messages: List[Dict[str, str]]) -> Generator[str, None, None]:
        self.generate_called = True
        self.last_messages = [dict(m) for m in messages]
        for w in self.reply.split():
            yield w + " "

    def reset(self) -> None:
        pass


class MockNoTokenizerLLM:
    """Mock-Provider ohne count_tokens / Tokenizer."""

    model_name: str = "NoTokenizerLLM"
    is_real_llm: True
    details: str = "Mock ohne Tokenizer"

    def generate(self, messages: List[Dict[str, str]]) -> str:
        return "Antwort"

    def generate_stream(self, messages: List[Dict[str, str]]) -> Generator[str, None, None]:
        yield "Antwort"

    def reset(self) -> None:
        pass


def test_budget_table_at_16384_and_8192_scales():
    """Prüft Tabelle bei 16384 und Skalierung bei 8192."""
    table_16k = calculate_budget_table(16384)
    assert table_16k["system"] == 900
    assert table_16k["fall_kopf"] == 500
    assert table_16k["vorbericht"] == 2000
    assert table_16k["verlauf_doku"] == 5500
    assert table_16k["history_and_question"] == 4900
    assert table_16k["answer"] == 2048
    assert table_16k["reserve"] == 536
    assert sum(table_16k.values()) == 16384

    table_8k = calculate_budget_table(8192)
    assert table_8k["system"] == 450
    assert table_8k["fall_kopf"] == 250
    assert table_8k["vorbericht"] == 1000
    assert table_8k["verlauf_doku"] == 2750
    assert table_8k["history_and_question"] == 2450
    assert table_8k["answer"] == 1024
    assert table_8k["reserve"] == 268
    assert sum(table_8k.values()) == 8192


def test_vorbericht_leitziel_smart_always_present_and_freitext_truncated():
    """Leitziel/SMART immer vorhanden auch bei kleinem Budget; Bericht gekürzt -> sentences x/y, start+end present."""
    sentences = [f"ZZ_SYNTH_SATZ_{i} Ausführliche Beschreibung zur Zielerreichung im Alltag." for i in range(1, 21)]
    sentences[0] = "ZZ_SYNTH_START Erster Satz zur Zielerreichung im Alltag."
    sentences[-1] = "ZZ_SYNTH_ENDE Letzter Satz zur Zielerreichung im Alltag."
    freitext = " ".join(sentences)

    goal = PlanGoalItem(
        section_id="1.1",
        section_title="Wohnen",
        leitziel="ZZ_SYNTH_LEITZIEL Eigenständige Lebensführung sichern.",
        smarte_ziele="ZZ_SYNTH_SMART Eigene Wohnung bis Ende 2026 finden.",
        stand_zielerreichung=freitext,
    )
    plan = PlanData(
        filename="vorbericht.docx",
        goals=[goal],
    )

    provider = MockTokenizerLLM(n_ctx=1024, max_tokens=128)
    bot = ReflectionChatbot(provider=provider)
    bot.set_plan(plan)
    bot.set_selected_bereich("1.1")
    bot.case.confirm_area("1.1")

    fitted_messages, status = bot.fit_context_for_prompt(user_message="Wie schätzen Sie die Situation ein?")

    assert status.bereich_id == "1.1"
    assert status.vorbericht_complete is False
    assert status.sentences_kept < status.sentences_total
    assert status.sentences_total == 20

    # Im gepackten Prompt muss der Leitziel-, SMART-, Start- und End-Satz enthalten sein
    prompt_text = "".join(m["content"] for m in fitted_messages)
    assert "ZZ_SYNTH_LEITZIEL" in prompt_text
    assert "ZZ_SYNTH_SMART" in prompt_text
    assert "ZZ_SYNTH_START" in prompt_text
    assert "ZZ_SYNTH_ENDE" in prompt_text
    assert "[… gekürzt …]" in prompt_text


def test_doku_over_budget_keeps_newest_drops_oldest():
    """Doku over budget -> newest present, oldest absent, counts correct, assigned preferred."""
    # Erzeuge 60 CSV Zeilen, von alt nach neu für Bereich 1.1 (Wohnen)
    rows = []
    for i in range(1, 61):
        tag = f"{i:02d}" if i <= 30 else f"{i-30:02d}"
        monat = "01" if i <= 30 else "02"
        marker = "ZZ_SYNTH_ALT" if i == 1 else ("ZZ_SYNTH_NEU" if i == 60 else f"ZZ_SYNTH_ROW_{i}")
        # Bereich 1.1 Keywords: wohnung, miete etc.
        kw = "Wohnung Miete Mietvertrag" if i % 2 == 0 else "Allgemeine Notiz ohne Zuordnung"
        rows.append([f"{tag}.{monat}.2026", "10:00", kw, f"{marker} Dokumentationstext für Eintrag {i}"])

    csv_data = CsvData(
        filename="doku.csv",
        headers=["Datum", "Uhrzeit", "Kategorie", "Text"],
        rows=rows,
        total_rows=len(rows),
    )

    # Bei n_ctx=1024 ist verlauf_doku_budget ca. 344 Tokens -> 60 Einträge sprengen das Budget
    provider = MockTokenizerLLM(n_ctx=1024, max_tokens=128)
    bot = ReflectionChatbot(provider=provider)
    bot.set_csv(csv_data)
    bot.set_selected_bereich("1.1")
    bot.case.confirm_area("1.1")

    fitted_messages, status = bot.fit_context_for_prompt(user_message="Was fällt in der Doku auf?")

    prompt_text = "".join(m["content"] for m in fitted_messages)
    assert "ZZ_SYNTH_NEU" in prompt_text
    assert "ZZ_SYNTH_ALT" not in prompt_text
    assert status.entries_kept < status.entries_total
    assert status.entries_total == 60
    assert status.entries_kept > 0


def test_long_history_sliding_window_drops_oldest_pairs_keeps_case_messages():
    """Lange Historie: älteste Paare verworfen, Count korrekt, case.messages im RAM vollständig."""
    provider = MockTokenizerLLM(n_ctx=2048, max_tokens=256)
    bot = ReflectionChatbot(provider=provider)
    bot.set_selected_bereich("1.1")
    bot.case.confirm_area("1.1")

    # Füge 20 Dialogrunden hinzu (große Historie, sprengt n_ctx=2048)
    for i in range(1, 21):
        u_msg = {"role": "user", "content": f"ZZ_SYNTH_USER_MSG_{i} " + ("Wort " * 50), "bereich_id": "1.1"}
        a_msg = {"role": "assistant", "content": f"ZZ_SYNTH_ASST_MSG_{i} " + ("Antwort " * 50), "bereich_id": "1.1"}
        bot.case.messages.append(u_msg)
        bot.case.messages.append(a_msg)
        bot.case.get_area_messages("1.1").append(u_msg)
        bot.case.get_area_messages("1.1").append(a_msg)

    fitted_messages, status = bot.fit_context_for_prompt(user_message="Aktuelle Anschlussfrage?")

    # Im RAM muss alles unverändert sein
    assert len(bot.case.messages) == 40
    assert len(bot.case.get_area_messages("1.1")) == 40

    # Im Prompt müssen ältere Paare gedroppt worden sein
    assert status.dropped_messages > 0
    # dropped_messages muss ein Vielfaches von 2 sein (ganze Paare)
    assert status.dropped_messages % 2 == 0

    prompt_text = "".join(m["content"] for m in fitted_messages)
    assert "ZZ_SYNTH_USER_MSG_1 " not in prompt_text
    assert "ZZ_SYNTH_USER_MSG_20" in prompt_text


def test_huge_question_preflight_prompt_too_long_error():
    """Gigantische Frage -> PromptTooLongError, generate_stream NIE aufgerufen, case.messages unverändert, SSE liefert ❌."""
    provider = MockTokenizerLLM(n_ctx=1024, max_tokens=256)
    bot = ReflectionChatbot(provider=provider)
    bot.set_selected_bereich("1.1")
    bot.case.confirm_area("1.1")

    huge_question = "ZZ_SYNTH_FRAGE " + ("überlangesWort " * 1200)

    # 1. Direkter Aufruf wirft PromptTooLongError
    with pytest.raises(PromptTooLongError) as exc_info:
        bot.fit_context_for_prompt(huge_question)

    err_msg = str(exc_info.value)
    assert "❌ Nachricht zu lang für das Kontextfenster" in err_msg
    assert "Bitte kürzen." in err_msg

    # 2. chat_stream fängt PromptTooLongError ab und yieldet {"error": ...}
    chunks = list(bot.chat_stream(huge_question))
    assert len(chunks) == 1
    assert "error" in chunks[0]
    assert "❌ Nachricht zu lang für das Kontextfenster" in chunks[0]["error"]
    assert provider.generate_called is False

    # Keine Geisternachricht in case.messages oder Bereich
    assert len(bot.case.messages) == 0
    assert len(bot.case.get_area_messages("1.1")) == 0

    # 3. Über FastAPI /api/chat-stream SSE
    app = create_app(chatbot=bot)
    client = TestClient(app)
    res = client.post("/api/chat-stream", json={"message": huge_question})
    assert res.status_code == 200
    assert "text/event-stream" in res.headers["content-type"]
    assert "❌ Nachricht zu lang für das Kontextfenster" in res.text


def test_api_session_and_sse_done_contain_context_status():
    """Prüft, dass /api/session und SSE done ContextStatus enthalten."""
    provider = MockTokenizerLLM(n_ctx=8192, max_tokens=512)
    bot = ReflectionChatbot(provider=provider)
    bot.set_selected_bereich("1.1")
    bot.case.confirm_area("1.1")

    app = create_app(chatbot=bot)
    client = TestClient(app)

    # GET /api/session
    res_session = client.get("/api/session")
    assert res_session.status_code == 200
    s_data = res_session.json()
    assert "context_status" in s_data
    cs = s_data["context_status"]
    assert cs["bereich_id"] == "1.1"
    assert "vorbericht_complete" in cs
    assert "sentences_kept" in cs
    assert "sentences_total" in cs
    assert "entries_kept" in cs
    assert "entries_total" in cs
    assert "unassigned_kept" in cs
    assert "dropped_messages" in cs
    assert "tokens_used" in cs
    assert cs["n_ctx"] == 8192

    # POST /api/chat-stream (SSE done event)
    res_stream = client.post("/api/chat-stream", json={"message": "Gedanken zum Tag"})
    assert res_stream.status_code == 200
    lines = [line.strip() for line in res_stream.text.split("\n") if line.startswith("data: ")]
    done_payload = json.loads(lines[-1].replace("data: ", ""))
    assert done_payload.get("done") is True
    assert "context_status" in done_payload
    stream_cs = done_payload["context_status"]
    assert stream_cs["bereich_id"] == "1.1"
    assert stream_cs["n_ctx"] == 8192


def test_no_tokenizer_returns_error_no_estimate():
    """Kein Tokenizer geladen -> rotes ❌, keine Schätzung."""
    bot = ReflectionChatbot(provider=MockNoTokenizerLLM())
    bot.set_selected_bereich("1.1")
    bot.case.confirm_area("1.1")

    chunks = list(bot.chat_stream("Hallo Begleiter"))
    assert len(chunks) == 1
    assert isinstance(chunks[0], dict)
    assert "error" in chunks[0]
    assert "❌ Kein Modell geladen: Token-Zählung nicht möglich" in chunks[0]["error"]


def test_caplog_contains_no_zz_synth_markers(caplog):
    """DSGVO/Datenschutz: caplog enthält keine synthetischen Daten-Marker."""
    provider = MockTokenizerLLM()
    bot = ReflectionChatbot(provider=provider)
    bot.set_selected_bereich("1.1")
    bot.case.confirm_area("1.1")

    caplog.clear()
    list(bot.chat_stream("ZZ_SYNTH_KLIENTENDATEN_GEHEIM Bitte reflektieren."))

    assert "ZZ_SYNTH" not in caplog.text
