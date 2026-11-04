"""Tests für Issue #34: Vorlagenabhängiger System-Prompt & Vorlagenwahl (Hamburg vs. Op de Wisch).

Prüft alle 4 Akzeptanzkriterien:
1. Vorlage (Hamburg / Op de Wisch) im UI vor/unabhängig vom Upload wählbar, per API im Session-State (RAM-only).
2. Abweichung zwischen gewählter Vorlage und erkanntem Vorbericht beim Upload -> rote ❌-Meldung mit Begründung,
   keine stille Überschreibung der Wahl; ohne Vorwahl gilt Erkennung als bestätigter Vorschlag (`template`).
3. System-Prompt und Kontext richten sich nach der gewählten Vorlage (Op de Wisch 1–9 vs. Hamburg 1.1–3).
4. Bei Op de Wisch stehen für Bereich 1 und Bereich 2 keinerlei Hamburg-Bereichs-IDs (1.1–1.5, 2.1–2.3) oder
   Hamburg-Bereichstitel (z. B. "Emotionale und psychische Entwicklung") im System-Prompt oder in den
   tatsächlich an das Modell gesendeten Messages (`fit_context_for_prompt`, `_build_payload`, `chat_stream`);
   Hamburg behält unverändert die Hamburger IDs.
"""
from __future__ import annotations

import io
import re
from typing import Dict, Generator, List

import pytest
from starlette.testclient import TestClient

from chat_app.app import create_app
from chat_app.chatbot import LLMProvider, ReflectionChatbot, SYSTEM_PROMPT_TEMPLATE
from chat_app.context_builder import ContextBuilder
from chat_app.csv_loader import parse_csv_content
from sob_generator.llm.sections import SECTION_DEFINITIONS
from sob_generator.llm.sections_op_de_wisch import OP_DE_WISCH_SECTION_DEFINITIONS
from tests.fixtures.make_synthetic_vorbericht import build_hamburg_vorbericht
from tests.fixtures.make_synthetic_vorbericht_opdewisch import build_opdewisch_vorbericht


HAMBURG_DOTTED_IDS = ("1.1", "1.2", "1.3", "1.4", "1.5", "2.1", "2.2", "2.3")
HAMBURG_SECTION_TITLES = tuple(s.title for s in SECTION_DEFINITIONS)
ODW_SECTION_TITLES = tuple(s.title for s in OP_DE_WISCH_SECTION_DEFINITIONS)


class RecordingMockLLM(LLMProvider):
    """In-Memory Mock-Provider, der alle an das Modell gesendeten Messages aufzeichnet."""

    model_name: str = "RecordingMockLLM"
    is_real_llm: bool = True
    details: str = "Synthetischer Test-Mock für Issue #34"
    n_ctx: int = 16384
    max_tokens: int = 2048

    def __init__(self, reply: str = "Synthetische Modellantwort.") -> None:
        self.reply = reply
        self.last_messages: List[Dict[str, str]] = []
        self.call_history: List[List[Dict[str, str]]] = []

    def count_tokens(self, text: str) -> int:
        if not text:
            return 0
        return len(text.split())

    def generate(self, messages: List[Dict[str, str]]) -> str:
        copied = [dict(m) for m in messages]
        self.last_messages = copied
        self.call_history.append(copied)
        return self.reply

    def generate_stream(self, messages: List[Dict[str, str]]) -> Generator[str, None, None]:
        copied = [dict(m) for m in messages]
        self.last_messages = copied
        self.call_history.append(copied)
        yield self.reply

    def reset(self) -> None:
        self.last_messages = []
        self.call_history = []


SYNTHETIC_CSV_CONTENT = (
    "Datum;Kategorie;Verlauf;Mitarbeiter\n"
    "10.02.2025;Stimmung / Psyche;Klient wirkte heute ruhig und ausgeglichen, Selbstsorge gelingt besser.;FK_SYNTH\n"
    "15.02.2025;Tagesstruktur;Termine am Vormittag selbstständig wahrgenommen, Wochenplan besprochen.;FK_SYNTH\n"
    "20.02.2025;Gesundheit;Arzttermin zur Blutabnahme wahrgenommen, Medikation regelmäßig.;FK_SYNTH\n"
)


# ===========================================================================
# Kriterium 1: Vorlage im UI vor Upload wählbar & via API im Session-State
# ===========================================================================


def test_ui_visible_template_selector_before_upload_and_red_mismatch_display():
    """Kriterium 1 & 2 (UI): Sichtbarer Vorlagen-Wähler vor dem Upload in <aside> und rote ❌-Fehleranzeige."""
    bot = ReflectionChatbot(provider=RecordingMockLLM())
    client = TestClient(create_app(chatbot=bot))

    res = client.get("/")
    assert res.status_code == 200
    html = res.text

    # 1. Sichtbarer Vorlagen-Wähler (id="templateSelect") im Seitenleisten-Setup (<aside>) vor den Upload-Zonen
    aside_match = re.search(r"<aside\b[^>]*>(.*?)</aside>", html, flags=re.DOTALL | re.IGNORECASE)
    assert aside_match is not None, "<aside>-Bereich im UI nicht gefunden"
    aside_html = aside_match.group(1)

    assert 'id="templateSelect"' in aside_html, (
        "Vorlagen-Wähler #templateSelect muss im <aside>-Bereich (nicht nur als Export-Option im Header) sichtbar sein"
    )
    pos_template = aside_html.index('id="templateSelect"')
    pos_csv_drop = aside_html.index('id="dropZone"')
    pos_plan_drop = aside_html.index('id="planDropZone"')
    assert pos_template < pos_csv_drop and pos_template < pos_plan_drop, (
        "Vorlagen-Wähler #templateSelect muss vor den Upload-Feldern (#dropZone, #planDropZone) platziert sein"
    )

    # Beide Träger-Optionen müssen im Wähler vorhanden sein
    select_match = re.search(
        r'<select[^>]*id="templateSelect"[^>]*>(.*?)</select>',
        aside_html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    assert select_match is not None
    select_inner = select_match.group(1)
    assert 'value="hamburg"' in select_inner
    assert 'value="op-de-wisch"' in select_inner

    # 2. Die Wahl wird per API POST /api/template im Session-State gesetzt
    assert "fetch('/api/template'" in html or 'fetch("/api/template"' in html, (
        "UI muss die Vorlagenwahl per POST /api/template im Session-State setzen"
    )

    # 3. Keine Persistenz der Vorlagenwahl im Browser (kein localStorage/cookie in handleTemplateChange)
    htc_match = re.search(
        r"async\s+function\s+handleTemplateChange\s*\([^)]*\)\s*\{(.*?)\n\s*\}",
        html,
        flags=re.DOTALL,
    )
    assert htc_match is not None
    htc_body = htc_match.group(1)
    assert "localStorage" not in htc_body
    assert "cookie" not in htc_body.lower()
    assert "/api/template" in htc_body

    # 4. Rote ❌-Anzeige für Vorlagen-Abweichung vorhanden
    assert 'id="templateMismatchError"' in aside_html or 'id="planUploadError"' in aside_html
    assert "templateMismatchError" in html


def test_template_select_default_placeholder_and_session_state_sync():
    """Nachbesserung 1: #templateSelect hat '– Vorlage wählen –' (value='') als ausgewählten Default;
    Hamburg ist nicht vorausgewählt; leere Auswahl sendet kein POST /api/template;
    /api/session liefert template_selected (bool) und UI spiegelt dies beim Laden und nach Reset wider.
    """
    bot = ReflectionChatbot(provider=RecordingMockLLM())
    client = TestClient(create_app(chatbot=bot))

    res = client.get("/")
    assert res.status_code == 200
    html = res.text

    # 1. Platzhalter-Option in #templateSelect als Default ausgewählt, Hamburg NICHT vorausgewählt
    select_match = re.search(
        r'<select[^>]*id="templateSelect"[^>]*>(.*?)</select>',
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    assert select_match is not None
    select_inner = select_match.group(1)
    options = re.findall(r"<option\b[^>]*>.*?</option>", select_inner, flags=re.DOTALL | re.IGNORECASE)
    assert len(options) >= 3, "Erwartet Platzhalter-Option + Hamburg + Op de Wisch"

    first_opt = options[0]
    assert 'value=""' in first_opt, f"Erste Option muss value=\"\" haben, war: {first_opt}"
    assert "– Vorlage wählen –" in first_opt, f"Platzhaltertext fehlt in erster Option: {first_opt}"
    assert "selected" in first_opt.lower(), f"Platzhalter-Option muss beim Start selected sein: {first_opt}"

    hh_opts = [o for o in options if 'value="hamburg"' in o]
    assert len(hh_opts) == 1
    assert "selected" not in hh_opts[0].lower(), (
        f"Hamburg darf in #templateSelect NICHT mehr vorausgewählt sein: {hh_opts[0]}"
    )

    # 2. Bei leerer Auswahl wird kein POST /api/template gesendet
    htc_match = re.search(
        r"async\s+function\s+handleTemplateChange\s*\(\s*(\w+)\s*\)\s*\{(.*?)\n\s*\}",
        html,
        flags=re.DOTALL,
    )
    assert htc_match is not None
    param_name, htc_body = htc_match.group(1), htc_match.group(2)
    guard_pos = htc_body.find(f"if (!{param_name})")
    fetch_pos = htc_body.find("/api/template")
    assert guard_pos != -1 and guard_pos < fetch_pos, (
        "handleTemplateChange muss bei leerer Auswahl vor dem POST /api/template abbrechen"
    )

    # 3. /api/session enthält template_selected (bool) und spiegelt Wahl & Reset wider
    sess_initial = client.get("/api/session").json()
    assert "template_selected" in sess_initial
    assert sess_initial["template_selected"] is False

    client.post("/api/template", json={"template": "op-de-wisch"})
    sess_chosen = client.get("/api/session").json()
    assert sess_chosen["template_selected"] is True
    assert sess_chosen["template"] == "op-de-wisch"

    client.post("/api/reset")
    sess_after_reset = client.get("/api/session").json()
    assert sess_after_reset["template_selected"] is False

    # 4. UI-Synchronisation bei Seiten-Neuladen (DOMContentLoaded) und Reset prüft data.template_selected
    assert "data.template_selected" in html, (
        "Seiten-Neuladen muss data.template_selected aus /api/session auswerten"
    )


def test_upload_plan_template_source_and_ui_detected_notice():
    """Nachbesserung 2: Upload-Antwort liefert template_source ('erkannt' vs. 'gewaehlt');
    UI zeigt im Bereich #sidebarTemplateSection einen nicht-roten Hinweis
    'Vorlage aus Vorbericht übernommen: Op de Wisch – bitte prüfen' und setzt #templateSelect.
    """
    # Fall A: Keine Vorlage gewählt -> template_source == "erkannt"
    bot_a = ReflectionChatbot(provider=RecordingMockLLM())
    client_a = TestClient(create_app(chatbot=bot_a))

    odw_bytes = build_opdewisch_vorbericht(client_name="Mustermann", client_vorname="Max")
    res_a = client_a.post(
        "/api/upload-plan",
        files={"file": ("Vorbericht_Mustermann.docx", io.BytesIO(odw_bytes), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert res_a.status_code == 200
    data_a = res_a.json()
    assert data_a["template"] == "op-de-wisch"
    assert data_a.get("template_source") == "erkannt"

    # Fall B: Vorlage explizit gewählt -> template_source == "gewaehlt"
    bot_b = ReflectionChatbot(provider=RecordingMockLLM())
    client_b = TestClient(create_app(chatbot=bot_b))
    client_b.post("/api/template", json={"template": "op-de-wisch"})

    res_b = client_b.post(
        "/api/upload-plan",
        files={"file": ("Vorbericht_Mustermann.docx", io.BytesIO(odw_bytes), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert res_b.status_code == 200
    data_b = res_b.json()
    assert data_b["template"] == "op-de-wisch"
    assert data_b.get("template_source") == "gewaehlt"

    # Fall C: Text-Vorbericht ohne Vorwahl -> template_source == "erkannt"
    bot_c = ReflectionChatbot(provider=RecordingMockLLM())
    client_c = TestClient(create_app(chatbot=bot_c))
    odw_text = (
        "Klient: Max Mustermann\n"
        "### Bereich 1: Psychische Verfassung / Selbstsorge\n"
        "Leitziel: ZZ_SYNTH_ODW_TEXT_1\n"
    )
    res_c = client_c.post("/api/plan", json={"plan_text": odw_text, "filename": "vorbericht.txt"})
    assert res_c.status_code == 200
    assert res_c.json().get("template_source") == "erkannt"

    # UI-Prüfung: Hinweis im Bereich #sidebarTemplateSection (nicht rot) + Setzen von #templateSelect
    html = client_a.get("/").text
    section_match = re.search(
        r'<div[^>]*id="sidebarTemplateSection"[^>]*>(.*?)\n\s*</div>\s*\n\s*<div>\s*\n\s*<div class="section-title">CSV-Falldaten</div>',
        html,
        flags=re.DOTALL,
    )
    assert section_match is not None, "#sidebarTemplateSection nicht erwartungsgemäß im HTML gefunden"
    section_html = section_match.group(1)

    assert 'id="templateDetectedNotice"' in section_html, (
        "Hinweis-Element #templateDetectedNotice muss innerhalb von #sidebarTemplateSection liegen"
    )
    notice_tag_match = re.search(
        r'<div[^>]*id="templateDetectedNotice"[^>]*>',
        section_html,
        flags=re.DOTALL,
    )
    assert notice_tag_match is not None
    notice_tag = notice_tag_match.group(0).lower()
    # Darf nicht rot gestaltet sein (kein Fehler)
    for red_token in ("#ef4444", "#fca5a5", "239, 68, 68", "220, 38, 38", "#dc2626"):
        assert red_token not in notice_tag, (
            f"#templateDetectedNotice darf nicht rot sein (gefunden: {red_token})"
        )

    assert "data.template_source === 'erkannt'" in html or 'data.template_source === "erkannt"' in html
    assert "Vorlage aus Vorbericht übernommen: ${tplLabel} – bitte prüfen" in html
    assert "syncTemplateSelectors(data.template)" in html


def test_template_selection_via_api_stored_only_in_session_state():
    """Kriterium 1: POST /api/template speichert die Vorlagenwahl im RAM-Session-State (vor jedem Upload)."""
    bot = ReflectionChatbot(provider=RecordingMockLLM())
    client = TestClient(create_app(chatbot=bot))

    # Initial ist noch keine Vorlage durch die Fachkraft explizit festgestellt
    assert getattr(bot.case, "template_selected", False) is False

    # Fachkraft wählt Op de Wisch vor dem ersten Upload
    res = client.post("/api/template", json={"template": "op-de-wisch"})
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert data["template"] == "op-de-wisch"
    assert [a["id"] for a in data["available_areas"]] == [str(i) for i in range(1, 10)]

    assert bot.case.template == "op-de-wisch"
    assert getattr(bot.case, "template_selected", False) is True

    # GET /api/session spiegelt die Wahl im Session-State wider, ohne dass ein Plan geladen ist
    sess = client.get("/api/session").json()
    assert sess["has_plan"] is False
    assert sess["template"] == "op-de-wisch"
    assert [a["id"] for a in sess["available_areas"]] == [str(i) for i in range(1, 10)]

    # Fachkraft wechselt auf Hamburg
    res_hh = client.post("/api/template", json={"template": "hamburg"})
    assert res_hh.status_code == 200
    assert res_hh.json()["template"] == "hamburg"
    assert bot.case.template == "hamburg"
    assert getattr(bot.case, "template_selected", False) is True

    # Ungültige Vorlage darf keinen stillen Fallback auslösen, sondern muss ❌ melden
    res_invalid = client.post("/api/template", json={"template": "ungueltiger-traeger"})
    assert res_invalid.status_code == 422
    assert "❌" in res_invalid.json()["detail"]

    # Nach Hard-Reset (/api/reset) ist template_selected wieder zurückgesetzt
    client.post("/api/reset")
    assert getattr(bot.case, "template_selected", None) is False
    assert bot.case.template == "hamburg"


# ===========================================================================
# Kriterium 2: Abweichungsprüfung beim Upload (keine stille Überschreibung)
# ===========================================================================


def test_upload_plan_mismatch_hamburg_selected_odw_uploaded_rejected_with_red_error():
    """Kriterium 2: Gewählt ist Hamburg, hochgeladen wird Op de Wisch -> ❌-Fehler mit Begründung, Wahl bleibt Hamburg."""
    bot = ReflectionChatbot(provider=RecordingMockLLM())
    client = TestClient(create_app(chatbot=bot))

    # 1. Fachkraft wählt explizit Hamburg
    r_tpl = client.post("/api/template", json={"template": "hamburg"})
    assert r_tpl.status_code == 200
    assert bot.case.template == "hamburg"

    # 2. Upload eines Op-de-Wisch-Vorberichts (neutraler Dateiname)
    odw_bytes = build_opdewisch_vorbericht(client_name="Mustermann", client_vorname="Max")
    res = client.post(
        "/api/upload-plan",
        files={"file": ("Vorbericht_Mustermann.docx", io.BytesIO(odw_bytes), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )

    assert res.status_code == 422
    body = res.json()
    detail = body.get("detail", "")
    assert detail.startswith("❌"), f"Fehlermeldung muss mit ❌ beginnen, erhalten: {detail!r}"
    assert "Op de Wisch" in detail
    assert "Hamburg" in detail

    # Wahl der Fachkraft darf NICHT still überschrieben worden sein
    assert bot.case.template == "hamburg"
    assert bot.has_plan is False

    sess = client.get("/api/session").json()
    assert sess["template"] == "hamburg"
    assert sess["has_plan"] is False


def test_upload_plan_mismatch_odw_selected_hamburg_uploaded_rejected_with_red_error():
    """Kriterium 2: Gewählt ist Op de Wisch, hochgeladen wird Hamburg -> ❌-Fehler mit Begründung, Wahl bleibt Op de Wisch."""
    bot = ReflectionChatbot(provider=RecordingMockLLM())
    client = TestClient(create_app(chatbot=bot))

    # 1. Fachkraft wählt explizit Op de Wisch
    r_tpl = client.post("/api/template", json={"template": "op-de-wisch"})
    assert r_tpl.status_code == 200
    assert bot.case.template == "op-de-wisch"

    # 2. Upload eines Hamburger Vorberichts
    hh_bytes = build_hamburg_vorbericht()
    res = client.post(
        "/api/upload-plan",
        files={"file": ("Vorbericht_Mustermann.docx", io.BytesIO(hh_bytes), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )

    assert res.status_code == 422
    body = res.json()
    detail = body.get("detail", "")
    assert detail.startswith("❌"), f"Fehlermeldung muss mit ❌ beginnen, erhalten: {detail!r}"
    assert "Hamburg" in detail
    assert "Op de Wisch" in detail

    # Wahl der Fachkraft bleibt erhalten
    assert bot.case.template == "op-de-wisch"
    assert bot.has_plan is False

    sess = client.get("/api/session").json()
    assert sess["template"] == "op-de-wisch"
    assert sess["has_plan"] is False


def test_upload_text_plan_and_api_plan_mismatch_rejected_with_red_error():
    """Kriterium 2: Auch bei Text-Vorberichten (.txt und POST /api/plan) wird eine Vorlagen-Abweichung mit ❌ abgelehnt."""
    bot = ReflectionChatbot(provider=RecordingMockLLM())
    client = TestClient(create_app(chatbot=bot))

    # Hamburg gewählt, aber Op-de-Wisch-Textplan hochgeladen
    client.post("/api/template", json={"template": "hamburg"})
    odw_text = (
        "Klient: Max Mustermann\n"
        "### Bereich 1: Psychische Verfassung / Selbstsorge\n"
        "Leitziel: ZZ_SYNTH_ODW_TEXT_1\n"
        "### Bereich 2: Alltagsgestaltung / Tagesstruktur\n"
        "Leitziel: ZZ_SYNTH_ODW_TEXT_2\n"
    )
    res_txt = client.post(
        "/api/upload-plan",
        files={"file": ("vorbericht.txt", io.BytesIO(odw_text.encode("utf-8")), "text/plain")},
    )
    assert res_txt.status_code == 422
    assert res_txt.json()["detail"].startswith("❌")
    assert "Op de Wisch" in res_txt.json()["detail"]
    assert bot.case.template == "hamburg"
    assert bot.has_plan is False

    # Und über POST /api/plan
    res_api_plan = client.post("/api/plan", json={"plan_text": odw_text, "filename": "vorbericht.txt"})
    assert res_api_plan.status_code == 422
    assert res_api_plan.json()["detail"].startswith("❌")
    assert bot.case.template == "hamburg"
    assert bot.has_plan is False


def test_upload_plan_matching_and_unselected_suggestion_succeed():
    """Kriterium 2: Passender Upload gelingt; ohne Vorwahl gilt die Erkennung als bestätigter Vorschlag (`template`)."""
    # Fall A: Noch keine Vorlage gewählt -> Erkennung als Vorschlag bestätigt in `template`
    bot_a = ReflectionChatbot(provider=RecordingMockLLM())
    client_a = TestClient(create_app(chatbot=bot_a))

    odw_bytes = build_opdewisch_vorbericht(client_name="Mustermann", client_vorname="Max")
    res_a = client_a.post(
        "/api/upload-plan",
        files={"file": ("Vorbericht_Mustermann.docx", io.BytesIO(odw_bytes), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert res_a.status_code == 200
    data_a = res_a.json()
    assert data_a["success"] is True
    assert data_a["template"] == "op-de-wisch"
    assert [a["id"] for a in data_a["available_areas"]] == [str(i) for i in range(1, 10)]

    # Fall B: Vorlage explizit gewählt ("op-de-wisch") und passender Vorbericht hochgeladen -> OK
    bot_b = ReflectionChatbot(provider=RecordingMockLLM())
    client_b = TestClient(create_app(chatbot=bot_b))
    client_b.post("/api/template", json={"template": "op-de-wisch"})

    res_b = client_b.post(
        "/api/upload-plan",
        files={"file": ("Vorbericht_Mustermann.docx", io.BytesIO(odw_bytes), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert res_b.status_code == 200
    assert res_b.json()["template"] == "op-de-wisch"
    assert bot_b.case.template == "op-de-wisch"


# ===========================================================================
# Kriterium 3 & 4: System-Prompt und echter Payload (Op de Wisch vs. Hamburg)
# ===========================================================================


@pytest.mark.parametrize(
    "bereich_id,expected_odw_title,expected_goal_marker",
    [
        ("1", "Psychische Verfassung / Selbstsorge", "ZZ_SYNTH_ODW_1_LEITZIEL"),
        ("2", "Alltagsgestaltung / Tagesstruktur", "ZZ_SYNTH_ODW_2_LEITZIEL"),
    ],
)
def test_odw_system_prompt_and_payload_for_area_1_and_2_contain_no_hamburg_ids_or_titles(
    bereich_id: str,
    expected_odw_title: str,
    expected_goal_marker: str,
):
    """Kriterium 3 & 4: Bei Op de Wisch enthalten System-Prompt, fit_context_for_prompt, _build_payload
    und die tatsächlich im Chat-Stream gesendeten Messages für Bereich 1 und Bereich 2 KEINE
    Hamburger Bereichs-IDs (1.1–1.5, 2.1–2.3) oder Hamburger Bereichstitel (wie
    'Emotionale und psychische Entwicklung'), sondern die Op-de-Wisch-Bereiche 1–9.
    """
    provider = RecordingMockLLM(
        reply=f"### Bereich {bereich_id} – {expected_odw_title}\n**Bisherige Zielerreichung:**\nSynthetischer Text."
    )
    bot = ReflectionChatbot(provider=provider)
    client = TestClient(create_app(chatbot=bot))

    # 1. Vorlage Op de Wisch wählen, Vorbericht und CSV laden
    r_tpl = client.post("/api/template", json={"template": "op-de-wisch"})
    assert r_tpl.status_code == 200

    odw_bytes = build_opdewisch_vorbericht(client_name="Mustermann", client_vorname="Max")
    r_plan = client.post(
        "/api/upload-plan",
        files={"file": ("Vorbericht_Mustermann.docx", io.BytesIO(odw_bytes), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert r_plan.status_code == 200

    bot.set_csv(parse_csv_content(SYNTHETIC_CSV_CONTENT, filename="verlauf_synth.csv", client_name="Max Mustermann"))

    # 2. Bereich wählen und bestätigen
    r_area = client.post("/api/area", json={"bereich_id": bereich_id})
    assert r_area.status_code == 200
    assert r_area.json()["title"] == expected_odw_title

    r_confirm = client.post("/api/area/confirm", json={"bereich_id": bereich_id})
    assert r_confirm.status_code == 200

    # 3. _build_payload und fit_context_for_prompt prüfen
    payload_msgs = bot._build_payload(bereich_id=bereich_id)
    fit_msgs, _ = bot.fit_context_for_prompt(
        user_message=f"Bitte reflektiere Bereich {bereich_id} mit mir.",
        bereich_id=bereich_id,
    )

    # 4. Echten Chat-Stream ausführen und tatsächlich an das Modell gesendete Messages prüfen
    with client.stream(
        "POST",
        "/api/chat-stream",
        json={"message": f"Bitte reflektiere Bereich {bereich_id} mit mir."},
    ) as stream_res:
        assert stream_res.status_code == 200
        _ = "".join(stream_res.iter_text())

    sent_msgs = provider.last_messages
    assert len(sent_msgs) >= 2, "Es müssen mindestens System-Prompt und User-Message gesendet worden sein"

    for msg_list in (payload_msgs, fit_msgs, sent_msgs):
        sys_msg = msg_list[0]
        assert sys_msg["role"] == "system"
        sys_content = sys_msg["content"]
        combined_content = "\n".join(m.get("content", "") for m in msg_list)

        # Op-de-Wisch-Bereiche 1–9 müssen im System-Prompt stehen
        for odw_sec in OP_DE_WISCH_SECTION_DEFINITIONS:
            assert f"{odw_sec.id} {odw_sec.title}" in sys_content, (
                f"Op-de-Wisch-Bereich '{odw_sec.id} {odw_sec.title}' fehlt im System-Prompt"
            )

        # Der gewählte Bereich und sein OdW-Zielmarker müssen im User-Kontext stehen
        assert f'<vorbericht_bereich id="{bereich_id}">' in combined_content
        assert expected_odw_title in combined_content
        assert expected_goal_marker in combined_content

        # KEINE Hamburger Bereichs-IDs (1.1–1.5, 2.1–2.3) im System-Prompt oder in den gesendeten Messages
        for hh_id in HAMBURG_DOTTED_IDS:
            pattern = rf"(?<![\d.]){re.escape(hh_id)}(?![\d.])"
            assert not re.search(pattern, sys_content), (
                f"Hamburger Bereichs-ID {hh_id!r} darf bei Op de Wisch NICHT im System-Prompt stehen!"
            )
            assert not re.search(pattern, combined_content), (
                f"Hamburger Bereichs-ID {hh_id!r} darf bei Op de Wisch NICHT im gesendeten Payload für Bereich {bereich_id} stehen!"
            )

        # KEINE Hamburger Bereichstitel (insb. "Emotionale und psychische Entwicklung" etc.)
        for hh_title in HAMBURG_SECTION_TITLES:
            assert hh_title not in sys_content, (
                f"Hamburger Bereichstitel {hh_title!r} darf bei Op de Wisch NICHT im System-Prompt stehen!"
            )
            assert hh_title not in combined_content, (
                f"Hamburger Bereichstitel {hh_title!r} darf bei Op de Wisch NICHT im gesendeten Payload für Bereich {bereich_id} stehen!"
            )


def test_odw_area_3_uses_gesundheit_not_hamburg_beschaeftigung():
    """Kriterium 3: Bereich '3' existiert in beiden Vorlagen; bei Op de Wisch muss er 'Gesundheit' heißen."""
    provider = RecordingMockLLM()
    bot = ReflectionChatbot(provider=provider)
    client = TestClient(create_app(chatbot=bot))

    client.post("/api/template", json={"template": "op-de-wisch"})

    # Auch ohne geladenen Vorbericht (nur Vorlage gewählt) muss Bereich 3 als "Gesundheit" aufgelöst werden
    built_empty = ContextBuilder.build(bot.case, "3")
    assert built_empty.bereich_title == "Gesundheit"
    assert "Beschäftigung, Arbeit, Ausbildung" not in built_empty.prompt_user_message

    review_empty = ContextBuilder.get_review_data(bot.case, "3")
    assert review_empty["bereich_title"] == "Gesundheit"
    assert review_empty["alt_sob"]["section_title"] == "Gesundheit"

    # Und über die API
    r_area = client.post("/api/area", json={"bereich_id": "3"})
    assert r_area.status_code == 200
    assert r_area.json()["title"] == "Gesundheit"


def test_hamburg_system_prompt_and_payload_unchanged():
    """Kriterium 3 & 4: Bei Vorlage Hamburg enthält der System-Prompt und Payload unverändert die Hamburger IDs."""
    provider = RecordingMockLLM()
    bot = ReflectionChatbot(provider=provider)
    client = TestClient(create_app(chatbot=bot))

    client.post("/api/template", json={"template": "hamburg"})
    hh_bytes = build_hamburg_vorbericht()
    r_plan = client.post(
        "/api/upload-plan",
        files={"file": ("Vorbericht_Hamburg.docx", io.BytesIO(hh_bytes), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert r_plan.status_code == 200
    assert r_plan.json()["template"] == "hamburg"

    client.post("/api/area", json={"bereich_id": "1.4"})
    client.post("/api/area/confirm", json={"bereich_id": "1.4"})

    fit_msgs, _ = bot.fit_context_for_prompt("Was fällt bei 1.4 auf?", bereich_id="1.4")
    sys_content = fit_msgs[0]["content"]
    combined = "\n".join(m.get("content", "") for m in fit_msgs)

    assert sys_content == SYSTEM_PROMPT_TEMPLATE
    for hh_id in HAMBURG_DOTTED_IDS:
        assert hh_id in sys_content
    assert "1.4 Emotionale und psychische Entwicklung" in sys_content
    assert "3 Beschäftigung, Arbeit, Ausbildung" in sys_content
    assert '<vorbericht_bereich id="1.4">' in combined
    assert "Psychische Verfassung / Selbstsorge" not in sys_content
