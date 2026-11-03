"""Unit- und Integrationstests für den DOCX-Export der Chat-App (Issue #105, #114, ADR-10, Zero-Disk)."""
from __future__ import annotations

import csv
import hashlib
import inspect
import io
import logging
import re
import urllib.parse
import zipfile
from pathlib import Path
from typing import Generator

import pytest
from docx import Document
from fastapi.testclient import TestClient

from chat_app.app import create_app
from chat_app.chatbot import LLMProvider, ReflectionChatbot, SYSTEM_PROMPT_TEMPLATE
from chat_app.csv_loader import parse_csv_content
from chat_app.docx_exporter import (
    CLIENT_NAME_MANUAL_ITEM,
    DocxExportError,
    DocxExportResult,
    EmptyExportError,
    UnknownTemplateError,
    UnmappedChatContentError,
    build_report_draft_from_chatbot,
    clean_markdown_for_docx,
    export_chatbot_to_docx,
    extract_section_data_from_chat,
    is_meta_heading,
    resolve_template,
    split_client_name,
    strip_trailing_closing_paragraph,
)
from chat_app.plan_loader import PlanData, parse_plan_docx
from sob_generator import docx_fields as fields
from sob_generator.__main__ import _resolve_cli_template
from sob_generator.models import ClientData, ReportDraft, ReportMeta, ReportSection
from sob_generator.renderer import HamburgDocxRenderer
from sob_generator.renderers.op_de_wisch_renderer import OpDeWischDocxRenderer
from sob_generator.report_template import TEMPLATE_REGISTRY
from tests.test_chat_app import _create_sample_plan_docx

ROOT_DIR = Path(__file__).resolve().parent.parent


class DummyMockLLM(LLMProvider):
    model_name: str = "MockLLM"
    is_real_llm: bool = False
    details: str = "Test Mock"

    def generate(self, messages):
        return "Test Antwort"

    def generate_stream(self, messages):
        yield "Test Antwort"


@pytest.fixture
def test_bot() -> ReflectionChatbot:
    return ReflectionChatbot(provider=DummyMockLLM())


@pytest.fixture
def client(test_bot) -> Generator[TestClient, None, None]:
    app = create_app(chatbot=test_bot)
    with TestClient(app) as c:
        yield c


def test_split_client_name():
    """Prüft verschiedene Namensformate."""
    assert split_client_name("Hans Peter") == ("Hans", "Peter")
    assert split_client_name("Peter, Hans") == ("Hans", "Peter")
    assert split_client_name("Hans_Peter") == ("Hans", "Peter")
    assert split_client_name("Mustermann") == ("", "Mustermann")
    assert split_client_name("") == ("", "")


def test_extract_section_data_from_chat():
    """Prüft die Extraktion strukturierter Abschnitte aus Assistant-Nachrichten für Hamburg und Op de Wisch."""
    sample_text = (
        "Lassen Sie uns den Zwischenstand ansehen:\n\n"
        "### Bereich 1.2 – Alltägliche Lebensführung / Hauswirtschaft\n"
        "**Verlaufsdaten Reflexion:**\n"
        "- Bad selbständig gereinigt (18.09.2024)\n"
        "- Wäsche gelingt zunehmend routinehaft\n\n"
        "**Vorschlag neue Ziele:**\n"
        "- Herr Peter übernimmt das Bügeln selbständig.\n\n"
        "**Maßnahmen des Leistungsanbieters:**\n"
        "- Begleitung bei der Haushaltsführung 2h wöchentlich.\n\n"
        "### Bereich 4 – Wohnen\n"
        "**Bisherige Zielerreichung:**\n"
        "- Wohnungssuche aktiv fortgesetzt.\n\n"
        "**Vorschlag neue Ziele:**\n"
        "- Mietvertrag selbständig unterzeichnen.\n"
    )
    messages = [
        {"role": "user", "content": "Bericht zur Zielerreichung"},
        {"role": "assistant", "content": sample_text},
    ]

    extracted = extract_section_data_from_chat(messages)
    assert "1.2" in extracted
    assert "Bad selbständig gereinigt" in extracted["1.2"]["bericht_zielerreichung"]
    assert "Bügeln selbständig" in extracted["1.2"]["vorschlag_ziele"]
    assert "2h wöchentlich" in extracted["1.2"]["vorschlag_massnahmen"]

    assert "4" in extracted
    assert "Wohnungssuche aktiv fortgesetzt" in extracted["4"]["bericht_zielerreichung"]
    assert "Mietvertrag selbständig unterzeichnen" in extracted["4"]["vorschlag_ziele"]


def test_build_report_draft_empty_raises(test_bot):
    """Prüft, dass bei leerem Chatbot-Zustand EmptyExportError ausgelöst wird."""
    with pytest.raises(EmptyExportError):
        build_report_draft_from_chatbot(test_bot)


def test_resolve_template_valid_and_invalid():
    """Prüft die Vorlagenauflösung und strikte Fehlerbehandlung ohne stillen Fallback."""
    # Gültige Vorlagen
    hamburg = resolve_template("hamburg")
    assert hamburg.key == "hamburg_soziale_dienste"

    odw = resolve_template("op-de-wisch")
    assert odw.key == "hamburg_op_de_wisch"

    # Fehler bei leerer oder unbekannter Vorlage
    with pytest.raises(UnknownTemplateError):
        resolve_template("")

    with pytest.raises(UnknownTemplateError):
        resolve_template(None)

    with pytest.raises(UnknownTemplateError) as exc_info:
        resolve_template("unbekannte_vorlage")
    assert "Unbekannte Vorlage" in str(exc_info.value)


def test_export_zero_disk_no_tempfile(test_bot, tmp_path, monkeypatch):
    """Verifiziert die Zero-Disk-Invariante: Exporter nutzt kein tempfile und erzeugt keine Plattendateien."""
    import chat_app.docx_exporter as exporter_module
    import tempfile

    # Statische Code-Prüfung: docx_exporter darf tempfile gar nicht importieren
    source = inspect.getsource(exporter_module)
    assert "import tempfile" not in source
    assert "mkdtemp" not in source

    # Laufzeit-Prüfung: Falls tempfile aufgerufen wird, soll der Test sofort fehlschlagen
    def forbidden_tempfile(*args, **kwargs):
        raise AssertionError("tempfile darf im Zero-Disk DOCX-Export nicht aufgerufen werden!")

    monkeypatch.setattr(tempfile, "mkdtemp", forbidden_tempfile)
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", forbidden_tempfile)

    sample_docx = _create_sample_plan_docx(tmp_path)
    plan = parse_plan_docx(sample_docx.read_bytes(), filename="Hans_Peter_SVB.docx")
    test_bot.set_plan(plan)

    # Hamburg-Export
    docx_bytes, filename = export_chatbot_to_docx(test_bot, "hamburg")
    assert isinstance(docx_bytes, bytes)
    assert len(docx_bytes) > 10000

    # Op-de-Wisch-Export
    odw_bytes, odw_filename = export_chatbot_to_docx(test_bot, "op-de-wisch")
    assert isinstance(odw_bytes, bytes)
    assert len(odw_bytes) > 10000


def test_export_chatbot_to_docx_hamburg(test_bot, tmp_path):
    """Prüft, dass export_chatbot_to_docx für Hamburg ein valides Word-Dokument erzeugt."""
    sample_docx = _create_sample_plan_docx(tmp_path)
    plan = parse_plan_docx(sample_docx.read_bytes(), filename="Hans_Peter_SVB.docx")
    test_bot.set_plan(plan)

    test_bot.messages.append({"role": "user", "content": "Reflexion"})
    test_bot.messages.append({
        "role": "assistant",
        "content": (
            "### Bereich 1.2 – Alltägliche Lebensführung\n"
            "**Verlaufsdaten Reflexion:**\n"
            "- Herr Mustermann kocht eigenständig.\n\n"
            "**Vorschlag neue Ziele:**\n"
            "- Mahlzeitenplanung für eine ganze Woche.\n"
        ),
    })

    docx_bytes, filename = export_chatbot_to_docx(test_bot, "hamburg")
    assert re.fullmatch(r"SOB_hamburg_\d{4}-\d{2}-\d{2}\.docx", filename)
    assert "Hans" not in filename and "Peter" not in filename
    assert len(docx_bytes) > 15000

    # Validiere ZIP-Archiv
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as z:
        assert "word/document.xml" in z.namelist()

    # Validiere mit python-docx
    doc = Document(io.BytesIO(docx_bytes))
    assert doc is not None


def test_export_chatbot_to_docx_op_de_wisch(test_bot, tmp_path):
    """Prüft, dass export_chatbot_to_docx für Op de Wisch ein valides Word-Dokument erzeugt."""
    sample_docx = _create_sample_plan_docx(tmp_path)
    plan = parse_plan_docx(sample_docx.read_bytes(), filename="Hans_Peter_SVB.docx")
    test_bot.set_plan(plan)

    docx_bytes, filename = export_chatbot_to_docx(test_bot, "op-de-wisch")
    assert re.fullmatch(r"SOB_op-de-wisch_\d{4}-\d{2}-\d{2}\.docx", filename)
    assert "Hans" not in filename and "Peter" not in filename
    assert len(docx_bytes) > 15000

    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as z:
        assert "word/document.xml" in z.namelist()

    doc = Document(io.BytesIO(docx_bytes))
    assert doc is not None


def test_api_export_docx_endpoint_validations(client):
    """Prüft Fehlerbehandlung von /api/export/docx bei fehlender Vorlage oder leerem Bot."""
    # 1. Fehlender Query-Parameter
    res_no_param = client.get("/api/export/docx")
    assert res_no_param.status_code == 400
    assert "Bitte eine Vorlage angeben" in res_no_param.json()["detail"]

    # 2. Unbekannte Vorlage
    res_unknown = client.get("/api/export/docx?template=nicht_existent")
    assert res_unknown.status_code == 400
    assert "Unbekannte Vorlage" in res_unknown.json()["detail"]

    # 3. Gültige Vorlage, aber Bot ist noch leer
    res_empty = client.get("/api/export/docx?template=hamburg")
    assert res_empty.status_code == 400
    assert "weder ein Vorbericht noch Chat-Nachrichten" in res_empty.json()["detail"]


def test_api_export_docx_endpoint_success_both_templates(client, tmp_path):
    """Prüft erfolgreichen Download von /api/export/docx für beide Vorlagen."""
    sample_docx = _create_sample_plan_docx(tmp_path)
    files = {
        "file": (
            "Hans_Peter_SVB_ausgefuellt.docx",
            io.BytesIO(sample_docx.read_bytes()),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    }
    upload_res = client.post("/api/upload-plan", files=files)
    assert upload_res.status_code == 200

    # 1. Hamburg
    res_hh = client.get("/api/export/docx?template=hamburg")
    assert res_hh.status_code == 200
    assert res_hh.headers["content-type"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert "attachment;" in res_hh.headers["content-disposition"]
    assert "SOB_hamburg_" in res_hh.headers["content-disposition"]
    assert "Hans" not in res_hh.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(res_hh.content)) as z:
        assert "word/document.xml" in z.namelist()

    # 2. Op de Wisch
    res_odw = client.get("/api/export/docx?template=op-de-wisch")
    assert res_odw.status_code == 200
    assert res_odw.headers["content-type"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert "attachment;" in res_odw.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(res_odw.content)) as z:
        assert "word/document.xml" in z.namelist()


def test_ui_index_contains_docx_export_elements():
    """Prüft, dass der Word-Export Button, die Vorlagenwahl und das rote ❌ Fehler-Modal in index.html vorhanden sind."""
    html_path = ROOT_DIR / "chat_app" / "templates" / "index.html"
    html_content = html_path.read_text(encoding="utf-8")

    # Button und Vorlagenauswahl
    assert 'id="docxExportBtn"' in html_content
    assert 'id="exportTemplateSelect"' in html_content
    assert 'value="hamburg"' in html_content
    assert 'value="op-de-wisch"' in html_content
    assert "downloadDocxExport" in html_content

    # Großes rotes ❌ Fehler-Modal
    assert 'id="exportErrorModal"' in html_content
    assert "❌" in html_content
    assert "showExportError" in html_content
    assert "/api/export/docx" in html_content
    assert 'id="exportWarningNotice"' in html_content
    assert "X-Export-Warnings" in html_content
    assert "Nicht zugeordnet:" in html_content


def test_cli_template_resolution():
    """Prüft die Vorlagenauflösung der CLI (Aliase vs. Fehler bei ungültiger Angabe)."""
    # Gültige Aliase
    hh_path, hh_defs, hh_rnd = _resolve_cli_template("hamburg")
    assert hh_path.name == "Vorlage.docx"
    assert len(hh_defs) == 9

    odw_path, odw_defs, odw_rnd = _resolve_cli_template("op-de-wisch")
    assert odw_path.name == "FO_SVB_Berichtsvorlage_2026.docx"
    assert len(odw_defs) == 9

    # Unbekannter Wert wirft SystemExit mit klarer Meldung
    with pytest.raises(SystemExit) as exc:
        _resolve_cli_template("ungueltige_vorlage")
    assert "Unbekannte Vorlage" in str(exc.value)


# ---------------------------------------------------------------------------
# Neue Tests für Issue #114 / Robusten Export-Parser & Fehlerbehandlung
# ---------------------------------------------------------------------------

def test_heading_formats_and_realistic_enumerated_sample():
    """Prüft alle geforderten Überschriftenformate inklusive des realistischen Aufzählungsbeispiels."""
    # 1. Verschiedene Überschriftenformate
    varied_text = (
        "### Bereich 1.2\n"
        "**Vorschlag neue Ziele:**\n- Ziel A\n\n"
        "### Bereich 1.3 – Basisversorgung\n"
        "**Vorschlag neue Ziele:**\n- Ziel B\n\n"
        "### 1.4 Emotionale und psychische Entwicklung\n"
        "**Vorschlag neue Ziele:**\n- Ziel C\n\n"
        "### 1.5 **Gesundheitsförderung und -erhaltung:**\n"
        "**Vorschlag neue Ziele:**\n- Ziel D\n\n"
        "## Bereich 3\n"
        "**Vorschlag neue Ziele:**\n- Ziel E\n"
    )
    res_varied = extract_section_data_from_chat([
        {"role": "user", "content": "Ziele"},
        {"role": "assistant", "content": varied_text},
    ])
    assert "1.2" in res_varied
    assert "1.3" in res_varied
    assert "1.4" in res_varied
    assert "1.5" in res_varied
    assert "3" in res_varied

    # 2. Realistisches Aufzählungsbeispiel (Turn 1: SMART-Ziele)
    turn_1_text = (
        "Basierend auf den bisherigen Verlaufsdaten ... können wir konkrete, SMART-formulierte Ziele vorschlagen:\n\n"
        "### 1. **Haushalt und Einkauf:**\n"
        "   - **Ziel:** Herr Peter wird im Haushalt und beim Einkaufen in der Lage sein, künftig alle Aufgaben selbständig zu erledigen.\n"
        "   - **Messbar:** Durch regelmäßige Beobachtungen wird der Fortschritt geprüft.\n"
        "   - **Time-bound:** In den nächsten 3 Monaten.\n\n"
        "### 2. **Arbeit und Kegeln:**\n"
        "   - **Ziel:** Herr Peter bewältigt den erhöhten Arbeitszeitumfang in der WfbM.\n\n"
        "### 3. **Physiotherapie und Gesundheit:**\n"
        "   - **Ziel:** Herr Peter plant Physiotherapie-Termine selbstständig.\n\n"
        "### 6. **Emotionale Unterstützung und Stressmanagement:**\n"
        "   - **Mental Health-Unterstützung:** Unterstützung durch einen Berater.\n\n"
        "Diese SMART-Ziele ..."
    )
    messages = [
        {
            "role": "user",
            "content": "Schlage auf Basis des bisherigen Dialogs konkrete, SMART formulierte neue Ziele für die besprochenen Lebensbereiche vor.",
        },
        {"role": "assistant", "content": turn_1_text},
    ]

    extracted = extract_section_data_from_chat(messages)
    # 1.2: Haushalt und Einkauf
    assert "1.2" in extracted
    assert "Herr Peter wird im Haushalt" in extracted["1.2"]["vorschlag_ziele"]
    assert "- **Messbar:**" in extracted["1.2"]["vorschlag_ziele"]

    # 'Arbeit und Kegeln' mischt Arbeit (3) und Freizeit (2.2): mehrdeutig -> NICHT geraten, sondern gemeldet
    assert "3" not in extracted
    assert any(
        "Arbeit und Kegeln" in b.heading and "mehrdeutig" in b.reason for b in extracted.unmapped_blocks
    )

    # 1.5: Physiotherapie und Gesundheit
    assert "1.5" in extracted
    assert "plant Physiotherapie-Termine" in extracted["1.5"]["vorschlag_ziele"]

    # 1.4: Emotionale Unterstützung und Stressmanagement
    assert "1.4" in extracted
    assert "Mental Health-Unterstützung" in extracted["1.4"]["vorschlag_ziele"]

    # Genau dieser eine Block bleibt unzugeordnet (und wird beim Export sichtbar gemeldet)
    assert len(extracted.unmapped_blocks) == 1


def test_enumeration_number_never_used_as_section_id():
    """Prüft strikt, dass eine Aufzählungsnummer niemals fälschlich als Bereichs-ID interpretiert wird."""
    # Bei '### 3. **Physiotherapie und Gesundheit:**' darf NICHT Bereich 3 (Arbeit) gemappt werden!
    messages = [
        {"role": "user", "content": "Ziele vorschlagen"},
        {
            "role": "assistant",
            "content": (
                "### 3. **Physiotherapie und Gesundheit:**\n"
                "- Regelmäßige Krankengymnastik wahrnehmen.\n\n"
                "### 2. **Arbeit und Beschäftigung in der WfbM:**\n"
                "- Arbeitszeiten einhalten.\n"
            ),
        },
    ]
    extracted = extract_section_data_from_chat(messages)
    # Physiotherapie gehört zu 1.5, NICHT 3!
    assert "1.5" in extracted
    assert "Krankengymnastik wahrnehmen" in extracted["1.5"]["vorschlag_ziele"]

    # Arbeit gehört zu 3, NICHT 2!
    assert "3" in extracted
    assert "Arbeitszeiten einhalten" in extracted["3"]["vorschlag_ziele"]
    assert "2" not in extracted


def test_intent_based_field_selection_multi_turn():
    """Prüft die Zuordnung zu vorschlag_ziele bzw. vorschlag_massnahmen anhand des Nutzer-Intents."""
    messages = [
        # Turn 1: Ziele
        {
            "role": "user",
            "content": "Schlage konkrete, SMART formulierte neue Ziele für die Lebensbereiche vor.",
        },
        {
            "role": "assistant",
            "content": (
                "### 1. **Haushalt und Einkauf:**\n"
                "- Selbständiger Wocheneinkauf.\n\n"
                "### 2. **Fortdauernde Unterstützung in der WfbM:**\n"
                "- Pünktlicher Arbeitsbeginn.\n"
            ),
        },
        # Turn 2: Maßnahmen
        {
            "role": "user",
            "content": "Welche konkreten fachlichen Maßnahmen und Unterstützungsleistungen werden vorgeschlagen?",
        },
        {
            "role": "assistant",
            "content": (
                "### 1. **Erhöhung der Selbstständigkeit in Haushalts- und Einkaufsaufgaben:**\n"
                "- **Kochübungen:** Gemeinsames Kochen freitags.\n"
                "- **Einkaufslisten:** Planung mit Assistenz.\n\n"
                "### 2. **Fortdauernde Unterstützung in der WfbM:**\n"
                "- **Arbeitsassistenz:** Wöchentliche Reflexion der Arbeitsleistung.\n"
            ),
        },
    ]

    extracted = extract_section_data_from_chat(messages)

    # 1.2: Haushalt
    assert "1.2" in extracted
    assert "Selbständiger Wocheneinkauf" in extracted["1.2"]["vorschlag_ziele"]
    assert "Kochübungen" in extracted["1.2"]["vorschlag_massnahmen"]
    assert "Einkaufslisten" in extracted["1.2"]["vorschlag_massnahmen"]

    # 3: WfbM / Arbeit
    assert "3" in extracted
    assert "Pünktlicher Arbeitsbeginn" in extracted["3"]["vorschlag_ziele"]
    assert "Arbeitsassistenz" in extracted["3"]["vorschlag_massnahmen"]


def test_unmapped_reporting_ambiguous_and_unknown():
    """Prüft, dass nicht zuordenbare oder mehrdeutige Überschriften sauber mit Grund erfasst werden."""
    messages = [
        {"role": "user", "content": "Ziele"},
        {
            "role": "assistant",
            "content": (
                "### 1. **Selbstreflexion und Selbstbewusstsein:**\n"
                "- Eigene Grenzen wahrnehmen.\n\n"
                "### 2. **Haushalt und Einkauf:**\n"
                "- Wohnung sauber halten.\n"
            ),
        },
    ]
    extracted = extract_section_data_from_chat(messages)

    # Haushalt (1.2) wurde gemappt
    assert "1.2" in extracted

    # Selbstreflexion konnte keinem Bereich zugeordnet werden
    assert len(extracted.unmapped_blocks) == 1
    unmapped = extracted.unmapped_blocks[0]
    assert "Selbstreflexion und Selbstbewusstsein" in unmapped.heading
    assert "kein eindeutiger Bereich" in unmapped.reason


def test_api_export_docx_red_cross_422_when_nothing_mappable(client):
    """Prüft, dass /api/export/docx HTTP 422 mit rotem ❌ liefert, wenn kein einziger Dialog-Block gemappt werden kann."""
    # Sende eine Nachricht, auf die der Mock-LLM antwortet (ohne Vorlage-Bereiche)
    client.post("/api/chat", json={"message": "Reflexionsanfrage"})

    # Versuche DOCX-Export -> Mock antwortet 'Test Antwort' (keine Bereichsüberschrift)
    res = client.get("/api/export/docx?template=hamburg")
    assert res.status_code == 422
    data = res.json()
    assert "detail" in data
    assert "❌ Dialog-Inhalt konnte keinem Bereich zugeordnet werden" in data["detail"]


def test_api_export_docx_partial_mapping_warning_header(client, test_bot):
    """Prüft, dass bei teilweiser Zuordnung das Dokument geliefert wird, aber mit X-Export-Warnings Header."""
    test_bot.messages.append({"role": "user", "content": "Ziele vorschlagen"})
    test_bot.messages.append({
        "role": "assistant",
        "content": (
            "### 1. **Haushalt und Einkauf:**\n"
            "- Selbständig einkaufen.\n\n"
            "### 2. **Selbstreflexion und Selbstbewusstsein:**\n"
            "- Grenzen setzen lernen.\n"
        ),
    })

    res = client.get("/api/export/docx?template=hamburg")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    # Warnungs-Header vorhanden
    assert "x-export-warnings" in res.headers
    import urllib.parse
    decoded_warnings = urllib.parse.unquote(res.headers["x-export-warnings"])
    assert "Selbstreflexion und Selbstbewusstsein" in decoded_warnings
    assert "Access-Control-Expose-Headers" in res.headers
    assert "X-Export-Warnings" in res.headers["Access-Control-Expose-Headers"]


def test_system_prompt_contains_required_heading_format():
    """Prüft, dass der Reflexionsbegleiter-Systemprompt das vom Parser erwartete Überschriftenformat explizit vorgibt."""
    assert "### Bereich <id> – <Titel>" in SYSTEM_PROMPT_TEMPLATE
    assert "**Bisherige Zielerreichung:**" in SYSTEM_PROMPT_TEMPLATE
    assert "**Vorschlag neue Ziele:**" in SYSTEM_PROMPT_TEMPLATE
    assert "**Vorschlag Maßnahmen:**" in SYSTEM_PROMPT_TEMPLATE
    assert "1.1 Wohnsituation" in SYSTEM_PROMPT_TEMPLATE
    assert "1.2 Alltägliche Lebensführung / Hauswirtschaft" in SYSTEM_PROMPT_TEMPLATE
    assert "3 Beschäftigung, Arbeit, Ausbildung" in SYSTEM_PROMPT_TEMPLATE



def test_unambiguous_work_heading_maps_to_section_3():
    """Eine reine Arbeits-/WfbM-Überschrift (Aufzählung) wird eindeutig Bereich 3 zugeordnet."""
    messages = [
        {"role": "user", "content": "Welche Maßnahmen schlägst du vor?"},
        {
            "role": "assistant",
            "content": (
                "### 2. **Fortdauernde Unterstützung in der WfbM:**\n"
                "   - **Arbeitszeitmanagement:** Wöchentliche Besprechung mit der Gruppenleitung.\n"
            ),
        },
    ]
    extracted = extract_section_data_from_chat(messages)
    assert "3" in extracted
    assert "Arbeitszeitmanagement" in extracted["3"]["vorschlag_massnahmen"]
    assert extracted.unmapped_blocks == []


def test_hamburg_heading_with_op_de_wisch_template_is_translated():
    """Hamburger Format aus dem System-Prompt wird bei Op-de-Wisch-Vorlage korrekt umgesetzt.

    '### Bereich 3 – Beschäftigung, Arbeit, Ausbildung' darf bei Op de Wisch NICHT in
    Lebensbereich 3 (Gesundheit) landen, sondern in 7 (Arbeit / Beschäftigung / Ausbildung).
    """
    odw = resolve_template("op-de-wisch")
    messages = [
        {"role": "user", "content": "Schlage SMART-Ziele vor."},
        {
            "role": "assistant",
            "content": (
                "### Bereich 3 – Beschäftigung, Arbeit, Ausbildung\n"
                "**Vorschlag neue Ziele:**\n- Herr Muster arbeitet 6 Stunden täglich in der Werkstatt.\n\n"
                "### Bereich 1.2 – Alltägliche Lebensführung / Hauswirtschaft\n"
                "**Vorschlag neue Ziele:**\n- Herr Muster kauft wöchentlich selbständig ein.\n"
            ),
        },
    ]
    extracted = extract_section_data_from_chat(messages, template=odw)
    assert "3" not in extracted
    assert "6 Stunden täglich" in extracted["7"]["vorschlag_ziele"]
    assert "kauft wöchentlich" in extracted["2"]["vorschlag_ziele"]
    assert extracted.unmapped_blocks == []


def test_meta_question_blocks_skipped_silently():
    """Prüft, dass Meta- und Fragenblöcke (z. B. ### Reflexionsfragen:) still ignoriert werden."""
    sample_text = (
        "Hier ist der Entwurf:\n\n"
        "### Bereich 1.2 – Alltägliche Lebensführung / Hauswirtschaft\n"
        "**Verlaufsdaten Reflexion:**\n"
        "- Bad selbständig gereinigt (18.09.2024)\n\n"
        "### Reflexionsfragen:\n"
        "1. Wie lief das gemeinsame Kochen?\n"
        "2. Gab es Unklarheiten bei den Einkäufen?\n\n"
        "### 2. **Hinweise:**\n"
        "Bitte den Vorbericht gegenprüfen.\n"
    )
    messages = [
        {"role": "user", "content": "Bericht zur Zielerreichung"},
        {"role": "assistant", "content": sample_text},
    ]

    extracted = extract_section_data_from_chat(messages)
    assert "1.2" in extracted
    assert "Bad selbständig gereinigt" in extracted["1.2"]["bericht_zielerreichung"]
    assert "Reflexionsfragen" not in extracted["1.2"]["bericht_zielerreichung"]
    assert "gemeinsame Kochen" not in extracted["1.2"]["bericht_zielerreichung"]
    assert "Hinweise" not in extracted["1.2"]["bericht_zielerreichung"]

    # Darf nicht in unmapped_blocks landen!
    assert len(extracted.unmapped_blocks) == 0

    # Verschiedene Meta-Überschriften prüfen
    meta_headings = [
        "### Reflexionsfragen:",
        "### 1. **Reflexionsfragen:**",
        "### Fragen",
        "### Offene Fragen",
        "### Fragen an Sie:",
        "### Fragen an dich",
        "### Fragen an euch",
        "### Hinweis:",
        "### Hinweise",
        "### Anmerkung",
        "### Anmerkungen:",
        "### Anmerkung(en)",
        "### Zusammenfassung",
        "### Fazit:",
        "### Rückfragen",
    ]
    for mh in meta_headings:
        assert is_meta_heading(mh) is True


def test_pure_question_headingless_message_skipped(test_bot, tmp_path):
    """Reine Fragenachricht ohne Überschriften wird als Meta übersprungen und nicht als (Fließtext) gewertet."""
    messages = [
        {"role": "user", "content": "Wie ist der Stand?"},
        {
            "role": "assistant",
            "content": (
                "Hier sind einige Fragen an dich zur weiteren Klärung:\n"
                "1. Hat Herr Muster die Termine wahrgenommen?\n"
                "2. Wurden die Ziele für Bereich 1.2 bereits erreicht?"
            ),
        },
    ]

    extracted = extract_section_data_from_chat(messages)
    assert len(extracted) == 0
    assert len(extracted.unmapped_blocks) == 0
    assert extracted.had_assistant_content is False

    # Ohne Plan und nur mit dieser Nachricht -> EmptyExportError, nicht 422
    test_bot.messages = list(messages)
    with pytest.raises(EmptyExportError):
        build_report_draft_from_chatbot(test_bot)

    # Mit Plan und dieser Nachricht -> Export gelingt über Plan, kein 422 UnmappedChatContentError
    sample_docx = _create_sample_plan_docx(tmp_path)
    plan = parse_plan_docx(sample_docx.read_bytes(), filename="Test_Plan.docx")
    test_bot.set_plan(plan)
    draft = build_report_draft_from_chatbot(test_bot)
    assert draft is not None
    assert len(draft.sections) > 0


def test_closing_sentences_dropped_from_last_section():
    """Typische Schlussfloskeln am Ende einer Nachricht werden nicht an den letzten Bereich angehängt."""
    sample_text = (
        "### Bereich 1.2 – Alltägliche Lebensführung\n"
        "**Verlaufsdaten Reflexion:**\n"
        "- Bad selbständig geputzt\n\n"
        "Diese SMART-Ziele unterstützen Herrn Muster bei der Haushaltsführung. Bei Fragen stehe ich gerne zur Verfügung."
    )
    messages = [
        {"role": "user", "content": "Bitte Entwurf erstellen."},
        {"role": "assistant", "content": sample_text},
    ]

    extracted = extract_section_data_from_chat(messages)
    assert "1.2" in extracted
    bericht = extracted["1.2"]["bericht_zielerreichung"]
    assert "Bad selbständig geputzt" in bericht
    assert "Diese SMART-Ziele" not in bericht
    assert "Bei Fragen" not in bericht

    # Trailing Text ohne Abschlussfloskel bleibt erhalten
    sample_keep = (
        "### Bereich 1.2 – Alltägliche Lebensführung\n"
        "**Verlaufsdaten Reflexion:**\n"
        "- Bad selbständig geputzt\n\n"
        "Der Klient wünscht sich hierbei weiterhin regelmäßige Unterstützung."
    )
    extracted_keep = extract_section_data_from_chat([
        {"role": "user", "content": "Bitte Entwurf erstellen."},
        {"role": "assistant", "content": sample_keep},
    ])
    assert "Der Klient wünscht sich hierbei weiterhin regelmäßige Unterstützung." in extracted_keep["1.2"]["bericht_zielerreichung"]


def test_markdown_cleanup_and_rendered_docx_no_stars(test_bot):
    """Bereinigt Markdown (keine **, keine ###, Aufzählungszeichen) und prüft gerendertes Hamburg-DOCX."""
    dirty_text = (
        "### Bereich 1.2 – Alltägliche Lebensführung\n"
        "  - **Haushaltsroutine:**\n"
        "    * _Kochen_ gelingt gut\n"
        "    + Putzen nach Plan\n\n\n\n"
        "Weiterer Absatz mit *wichtigem* Hinweis."
    )
    cleaned = clean_markdown_for_docx(dirty_text)
    assert "**" not in cleaned
    assert "###" not in cleaned
    assert "_" not in cleaned
    assert "*wichtigem*" not in cleaned
    assert "• Haushaltsroutine:" in cleaned
    assert "• Kochen gelingt gut" in cleaned
    assert "• Putzen nach Plan" in cleaned
    assert "\n\n\n" not in cleaned

    # Integrationstest: Export in Hamburg-DOCX
    assistant_msg = (
        "### 1. **Haushaltsroutine:**\n"
        "**Verlaufsdaten Reflexion:**\n"
        "- **Kochen:** Klient kocht selbständig\n"
        "- **Wäsche:** Wäsche gelingt nach Anleitung\n\n"
        "**Vorschlag neue Ziele:**\n"
        "- **Ziel 1:** Bügeln selbständig lernen\n"
    )
    test_bot.messages = [
        {"role": "user", "content": "Zwischenstand"},
        {"role": "assistant", "content": assistant_msg},
    ]

    result = export_chatbot_to_docx(test_bot, "hamburg")
    docx_bytes = result[0]
    root = fields.load_document_tree(io.BytesIO(docx_bytes))

    bericht_text = fields.get_formtext(root, "sections_1_bericht_zielerreichung")
    assert "**" not in bericht_text
    assert "###" not in bericht_text
    assert "Kochen: Klient kocht selbständig" in bericht_text

    ziele_text = fields.get_formtext(root, "sections_1_vorschlag_ziele")
    assert "**" not in ziele_text
    assert "Ziel 1: Bügeln selbständig lernen" in ziele_text


def test_multiline_bericht_rendered_as_multiple_paragraphs(tmp_path):
    """Mehrzeiliger Bericht zur Zielerreichung wird als separate Absätze ohne numPr gerendert."""
    multiline_text = "Erste Zeile des Berichts.\nZweite Zeile des Berichts.\nDritte Zeile des Berichts."
    draft = ReportDraft(
        client=ClientData(name="Muster", vorname="Hans"),
        meta=ReportMeta(),
        developments="",
        sections=[
            ReportSection(
                id="1.2",
                title="Alltägliche Lebensführung",
                previous_goals_apply=True,
                bericht_zielerreichung=multiline_text,
            )
        ],
    )

    renderer = HamburgDocxRenderer(TEMPLATE_REGISTRY["hamburg_soziale_dienste"].docx_path)
    buffer = io.BytesIO()
    renderer.render(draft, buffer)

    root = fields.load_document_tree(io.BytesIO(buffer.getvalue()))
    read_text = fields.get_formtext(root, "sections_1_bericht_zielerreichung")
    assert read_text == multiline_text

    # Prüfe XML-Struktur: Das Feld erstreckt sich über mehrere w:p ohne w:numPr
    bm_start = root.find(f'.//{fields.wtag("bookmarkStart")}[@{fields.wtag("name")}="sections_1_bericht_zielerreichung"]')
    first_para = bm_start.getparent()
    # Der Absatz und seine Geschwister bis zum bookmarkEnd
    bm_id = bm_start.get(fields.wtag("id"))
    current = first_para
    paras = [current]
    while current is not None:
        if current.find(f'{fields.wtag("bookmarkEnd")}[@{fields.wtag("id")}="{bm_id}"]') is not None:
            break
        current = current.getnext()
        if current is not None and current.tag == fields.wtag("p"):
            paras.append(current)

    assert len(paras) == 3
    for p in paras:
        ppr = p.find(fields.wtag("pPr"))
        numpr = ppr.find(fields.wtag("numPr")) if ppr is not None else None
        assert numpr is None, "Berichts-Absatz darf kein numPr (Listenaufzählung) haben"


def test_vivendi_header_parsing_in_memory_csv(test_bot):
    """Liest Zeitraum, Einrichtung, Geburtsdatum speicherbasiert aus Vivendi-CSV in ReportDraft ein."""
    csv_text = (
        ";;;;;;;;;;;;;;;;01.01.2025 08:00 - 31.12.2025 17:00;;\n"
        "Fiktive Soziale Einrichtung Nord;;;;;;;;;;;;;;;;;;\n"
        ";;;;;;;;;;;;;;;;;;\n"
        "Mustermann, Erika (*15.05.1985);;;;;;;;;;;;;;;;;;\n"
        ";;;;;Datum;;Uhrzeit;;Schicht;;Kategorie;;Kennzeichen;;Details;;;Handzeichen\n"
        ";;;;10.03.2025;;09:00;;Früh;;Alltag;;;;;;;;MA (10.03.2025)\n"
        ";;;;Dokumentationseintrag zur Haushaltsführung;;;;;;;;;;;;;;\n"
    )
    test_bot.set_csv(parse_csv_content(csv_text, filename="vivendi_export.csv"))
    test_bot.messages = [
        {"role": "user", "content": "Bericht"},
        {"role": "assistant", "content": "### Bereich 1.2\n- Bad gereinigt"},
    ]

    hamburg_template = resolve_template("hamburg")
    draft, warnings, manual = build_report_draft_from_chatbot(
        test_bot, template=hamburg_template, return_unmapped=True, return_manual=True
    )

    assert draft.meta.leistungszeitraum_von == "01.01.2025"
    assert draft.meta.leistungszeitraum_bis == "31.12.2025"
    assert draft.meta.leistungsanbieter == "Fiktive Soziale Einrichtung Nord"
    assert draft.client.geburtsdatum == "15.05.1985"
    assert draft.client.name == "Mustermann"
    assert draft.client.vorname == "Erika"

    # Manuelle Liste prüfen
    assert "Leistungsart" in manual
    assert "Leistung erbracht von/bis" not in manual
    assert "Leistungsanbieter" not in manual
    assert "Geburtsdatum" not in manual
    assert "Folgeantrag auf Haushaltshilfen (Auswahlfeld 1.2)" in manual
    assert "Leitziel/Smarte Ziele (Gesamtplan)" in manual


def test_vivendi_entry_date_fallback_info_item(test_bot):
    """Wenn Header-Zeitraum fehlt, wird Zeitraum aus erstem/letztem Eintrag abgeleitet und Info gemeldet."""
    csv_text = (
        ";;;;;;;;;;;;;;;;;;;\n"
        "Fiktive Einrichtung;;;;;;;;;;;;;;;;;;\n"
        ";;;;;;;;;;;;;;;;;;\n"
        "Mustermann, Max (*01.02.1990);;;;;;;;;;;;;;;;;;\n"
        ";;;;;Datum;;Uhrzeit;;Schicht;;Kategorie;;Kennzeichen;;Details;;;Handzeichen\n"
        ";;;;15.03.2025;;09:00;;Früh;;Kategorie;;;;;;;;MA\n"
        ";;;;Eintrag 1;;;;;;;;;;;;;;\n"
        ";;;;20.10.2025;;10:00;;Spät;;Kategorie;;;;;;;;MA\n"
        ";;;;Eintrag 2;;;;;;;;;;;;;;\n"
    )
    test_bot.set_csv(parse_csv_content(csv_text, filename="vivendi_no_header_period.csv"))
    test_bot.messages = [
        {"role": "user", "content": "Bericht"},
        {"role": "assistant", "content": "### Bereich 1.2\n- Bad gereinigt"},
    ]

    draft, warnings, manual = build_report_draft_from_chatbot(
        test_bot, template=resolve_template("hamburg"), return_unmapped=True, return_manual=True
    )

    assert draft.meta.leistungszeitraum_von == "15.03.2025"
    assert draft.meta.leistungszeitraum_bis == "20.10.2025"
    assert "Leistungszeitraum aus erstem/letztem Eintrag abgeleitet – bitte prüfen" in manual
    assert "Leistung erbracht von/bis" not in manual


def test_non_vivendi_csv_does_not_crash_and_manual_list_has_zeitraum(test_bot):
    """Nicht-Vivendi-CSV führt nicht zum Absturz, Leistungszeitraum landet in der manuellen Liste."""
    csv_text = (
        "Datum,Mitarbeiter,Inhalt\n"
        "01.01.2025,P. Schmidt,Alltägliche Unterstützung geleistet\n"
        "15.02.2025,P. Schmidt,Einkaufshilfe\n"
    )
    test_bot.set_csv(parse_csv_content(csv_text, filename="custom_dokumentation.csv"))
    test_bot.messages = [
        {"role": "user", "content": "Bericht"},
        {"role": "assistant", "content": "### Bereich 1.2\n- Bad gereinigt"},
    ]

    # Hamburg: Leistung erbracht von/bis
    draft_h, _, manual_h = build_report_draft_from_chatbot(
        test_bot, template=resolve_template("hamburg"), return_unmapped=True, return_manual=True
    )
    assert draft_h.meta.leistungszeitraum_von == ""
    assert "Leistung erbracht von/bis" in manual_h

    # Op de Wisch: Leistungszeitraum
    draft_odw, _, manual_odw = build_report_draft_from_chatbot(
        test_bot, template=resolve_template("op-de-wisch"), return_unmapped=True, return_manual=True
    )
    assert draft_odw.meta.leistungszeitraum_von == ""
    assert "Leistungszeitraum" in manual_odw


def test_x_export_manual_header_via_testclient(client, test_bot):
    """Prüft, dass der X-Export-Manual Header über die FastAPI-Endstelle ausgeliefert wird."""
    test_bot.messages = [
        {"role": "user", "content": "Bericht"},
        {"role": "assistant", "content": "### Bereich 1.2\n- Bad selbständig gereinigt"},
    ]

    res = client.get("/api/export/docx?template=hamburg")
    assert res.status_code == 200

    assert "X-Export-Manual" in res.headers
    import json
    import urllib.parse

    manual_list = json.loads(urllib.parse.unquote(res.headers["X-Export-Manual"]))
    assert isinstance(manual_list, list)
    assert "Leistungsart" in manual_list
    assert "Leistung erbracht von/bis" in manual_list
    assert "Folgeantrag auf Haushaltshilfen (Auswahlfeld 1.2)" in manual_list

    expose = res.headers.get("Access-Control-Expose-Headers", "")
    assert "X-Export-Manual" in expose


def test_op_de_wisch_leistungsart_not_defaulted_to_asp():
    """Prüft, dass Op de Wisch Renderer Leistungsart nicht fälschlich mit 'ASP' vorbefüllt."""
    draft = ReportDraft(
        client=ClientData(name="Muster", vorname="Max", leistungsart=""),
        meta=ReportMeta(),
        developments="",
        sections=[],
    )

    renderer = OpDeWischDocxRenderer(TEMPLATE_REGISTRY["hamburg_op_de_wisch"].docx_path)
    buffer = io.BytesIO()
    renderer.render(draft, buffer)

    doc = Document(io.BytesIO(buffer.getvalue()))
    # Tabelle 0, Zeile 3, Zelle 0, SDT 0 (Leistungsart)
    sdts_c3_0 = doc.tables[0].rows[3].cells[0]._tc.xpath(".//w:sdt")
    assert len(sdts_c3_0) > 0
    full_ns = dict(doc.element.nsmap)
    leistungsart_text = "".join(sdts_c3_0[0].xpath(".//w:t/text()", namespaces=full_ns))
    assert leistungsart_text != "ASP"
    assert leistungsart_text == ""


# ---------------------------------------------------------------------------
# Tests für 14B-Befunde (A: Plain-Text Subheadings, B: Frage-Bereich, C: Markdown)
# ---------------------------------------------------------------------------

def test_clean_markdown_for_docx_bullet_bold_and_indentation():
    """Prüft Bereinigung von - **Titel:** Text, **Titel:** am Zeilenanfang und eingerückten Aufzählungen (C)."""
    # 1. '- **Titel:** Text' -> '• Titel: Text'
    sample1 = "- **Kochtraining:** Wöchentliches Kochen mit Assistenz."
    assert clean_markdown_for_docx(sample1) == "• Kochtraining: Wöchentliches Kochen mit Assistenz."

    # 1b. '-**Titel:** Text' ohne Leerzeichen nach Spiegelstrich
    sample1b = "-**Einkauf:** Selbstständiger Einkauf."
    assert clean_markdown_for_docx(sample1b) == "• Einkauf: Selbstständiger Einkauf."

    # 2. '**Titel:**' und '**Titel:** Text' am Zeilenanfang
    sample2a = "**Wichtiger Hinweis:**"
    assert clean_markdown_for_docx(sample2a) == "Wichtiger Hinweis:"

    sample2b = "**Haushalt:** Herr Peter führt die Liste selbstständig."
    assert clean_markdown_for_docx(sample2b) == "Haushalt: Herr Peter führt die Liste selbstständig."

    # 3. Verschachtelte und eingerückte Bullets
    nested = (
        "- **Haushalt und Einkauf:**\n"
        "  - **Kochen:** Einfache Mahlzeiten zubereiten\n"
        "    * **Herd:** Nach dem Kochen reinigen\n"
        "\t- **Wäsche:** Eigenständig waschen"
    )
    cleaned = clean_markdown_for_docx(nested)
    lines = cleaned.splitlines()
    assert lines[0] == "• Haushalt und Einkauf:"
    assert lines[1] == "• Kochen: Einfache Mahlzeiten zubereiten"
    assert lines[2] == "• Herd: Nach dem Kochen reinigen"
    assert lines[3] == "• Wäsche: Eigenständig waschen"
    assert "**" not in cleaned
    assert "*" not in cleaned


def test_plain_text_subheadings_recognized_as_fields():
    """Prüft Erkennung aller geforderten Subheadings als Field-Marker in verschiedenen Formatierungen (A)."""
    text_samples = [
        "Bisherige Zielerreichung:\nErledigt Aufgaben gut.\nVorschlag neue Ziele:\n- Neues Ziel 1\nVorschlag Maßnahmen:\n- Maßnahme A",
        "Bericht zur Zielerreichung:\nErledigt Aufgaben gut.\nVorschlag (für) Ziele:\n- Neues Ziel 1\nVorschlag für Maßnahmen:\n- Maßnahme A",
        "- **Bisherige Zielerreichung:**\nErledigt Aufgaben gut.\n* **Vorschlag für Ziele:**\n- Neues Ziel 1\n• **Vorschlag Maßnahmen:**\n- Maßnahme A",
        "### Bisherige Zielerreichung\nErledigt Aufgaben gut.\n### Vorschlag Ziele:\n- Neues Ziel 1\n### Vorschlag für Maßnahmen\n- Maßnahme A",
    ]

    for sample in text_samples:
        messages = [
            {"role": "user", "content": "Wie hat sich die Haushaltsführung entwickelt?"},
            {"role": "assistant", "content": sample},
        ]
        extracted = extract_section_data_from_chat(messages)
        assert "1.2" in extracted
        assert "Erledigt Aufgaben gut." in extracted["1.2"]["bericht_zielerreichung"]
        assert "Neues Ziel 1" in extracted["1.2"]["vorschlag_ziele"]
        assert "Maßnahme A" in extracted["1.2"]["vorschlag_massnahmen"]


def test_section_from_question_and_headingless_field_markers():
    """Prüft Zuordnung über Nutzerfrage bei nachrichten ohne Bereichsüberschrift und Fehlermeldung bei unklarer Frage (B)."""
    # 1. Frage zielt klar auf 1.2
    messages_clear = [
        {"role": "user", "content": "Wie hat sich die Haushaltsführung im Berichtszeitraum entwickelt?"},
        {
            "role": "assistant",
            "content": (
                "Bisherige Zielerreichung:\nHerr Peter kocht selbstständig.\n"
                "Vorschlag neue Ziele:\n- Mehrmals wöchentlich kochen."
            ),
        },
    ]
    extracted_clear = extract_section_data_from_chat(messages_clear)
    assert "1.2" in extracted_clear
    assert "Herr Peter kocht selbstständig." in extracted_clear["1.2"]["bericht_zielerreichung"]
    assert "Mehrmals wöchentlich kochen." in extracted_clear["1.2"]["vorschlag_ziele"]
    assert len(extracted_clear.unmapped_blocks) == 0

    # 2. Frage ist generisch/unklar -> unzugeordnet mit exaktem Grund
    messages_generic = [
        {"role": "user", "content": "Wie ist der aktuelle Stand im Verlauf?"},
        {
            "role": "assistant",
            "content": (
                "Bisherige Zielerreichung:\nHerr Peter kocht selbstständig.\n"
                "Vorschlag neue Ziele:\n- Mehrmals wöchentlich kochen."
            ),
        },
    ]
    extracted_generic = extract_section_data_from_chat(messages_generic)
    assert len(extracted_generic.unmapped_blocks) == 1
    unmapped = extracted_generic.unmapped_blocks[0]
    assert unmapped.reason == "kein Bereich erkennbar (keine Überschrift, Frage nicht eindeutig)"


def test_conflict_detection_question_vs_heading():
    """Prüft strikte Konflikterkennung wenn Frage auf 1.2 zielt, Modell aber 1.3 schreibt (B)."""
    messages_conflict = [
        {"role": "user", "content": "Wie hat sich die Haushaltsführung im Berichtszeitraum entwickelt?"},
        {
            "role": "assistant",
            "content": (
                "### 1.3 Basisversorgung / Grundpflege\n"
                "Bisherige Zielerreichung:\nHerr Peter kocht regelmäßig und reinigt den Herd."
            ),
        },
    ]
    extracted = extract_section_data_from_chat(messages_conflict)
    # Block darf nirgends einsortiert werden
    assert "1.3" not in extracted
    assert "1.2" not in extracted
    assert len(extracted.unmapped_blocks) == 1
    unmapped = extracted.unmapped_blocks[0]
    assert unmapped.heading == "1.3 Basisversorgung / Grundpflege"
    assert unmapped.reason == "Bereich unklar: Frage zielt auf 1.2, Modell schreibt 1.3"

    # Ausnahme: Mehrere Bereiche in der Nutzerfrage -> kein Q -> kein Konflikt
    messages_multi_q = [
        {"role": "user", "content": "Wie läuft es in der Haushaltsführung und bei der Arbeit?"},
        {
            "role": "assistant",
            "content": (
                "### Bereich 1.2 – Alltägliche Lebensführung\n- Putzen gelingt gut.\n\n"
                "### Bereich 3 – Beschäftigung und Arbeit\n- WfbM pünktlich besucht."
            ),
        },
    ]
    extracted_multi = extract_section_data_from_chat(messages_multi_q)
    assert "1.2" in extracted_multi
    assert "3" in extracted_multi
    assert len(extracted_multi.unmapped_blocks) == 0


def test_odw_template_translation_with_question_section():
    """Prüft, dass die Fragebereich-Erkennung und Konflikte auch mit der Op-de-Wisch-Vorlage funktionieren (B)."""
    odw = resolve_template("op-de-wisch")

    # 1. Frage zielt auf Haushaltsführung (Hamburg 1.2 -> OdW 2)
    messages_clear = [
        {"role": "user", "content": "Wie hat sich die Haushaltsführung im Berichtszeitraum entwickelt?"},
        {
            "role": "assistant",
            "content": (
                "Bisherige Zielerreichung:\nHerr Peter erledigt den Einkauf selbstständig.\n"
                "Vorschlag neue Ziele:\n- Einkauf planen."
            ),
        },
    ]
    extracted = extract_section_data_from_chat(messages_clear, template=odw)
    assert "2" in extracted
    assert "Einkauf selbstständig" in extracted["2"]["bericht_zielerreichung"]

    # 2. Konflikt bei OdW: Frage zielt auf 1.2, Modell schreibt 1.3 (bzw. OdW 1)
    messages_conflict = [
        {"role": "user", "content": "Wie hat sich die Haushaltsführung im Berichtszeitraum entwickelt?"},
        {
            "role": "assistant",
            "content": (
                "### 1.3 Basisversorgung / Grundpflege\n"
                "Bisherige Zielerreichung:\nHerr Peter kocht regelmäßig."
            ),
        },
    ]
    extracted_conflict = extract_section_data_from_chat(messages_conflict, template=odw)
    assert "1" not in extracted_conflict
    assert "2" not in extracted_conflict
    assert len(extracted_conflict.unmapped_blocks) == 1
    assert extracted_conflict.unmapped_blocks[0].reason == "Bereich unklar: Frage zielt auf 1.2, Modell schreibt 1.3"


def test_end_to_end_14b_pattern_with_synthetic_vivendi_fixture(test_bot):
    """End-to-End-Test mit hans_peter_synthetisch.csv, 14B-Subheadings und 1.3-Konflikt."""
    fixture_path = ROOT_DIR / "tests" / "fixtures" / "hans_peter_synthetisch.csv"
    assert fixture_path.exists(), "Fixture tests/fixtures/hans_peter_synthetisch.csv muss existieren"

    csv_content = fixture_path.read_text(encoding="utf-8-sig")
    parsed_csv = parse_csv_content(csv_content, filename=fixture_path.name)
    test_bot.set_csv(parsed_csv)

    # 14B-Muster: User fragt nach Haushaltsführung, Modell antwortet mit Plain-Text Subheadings
    # gefolgt von einem Block '### 1.3 Basisversorgung / Grundpflege' voller Haushaltsinhalte
    chat_dialogue = [
        {"role": "user", "content": "Wie hat sich die Haushaltsführung im Berichtszeitraum entwickelt?"},
        {
            "role": "assistant",
            "content": (
                "Bisherige Zielerreichung:\n"
                "Herr Peter erledigt den Wocheneinkauf inzwischen mit Einkaufsliste weitgehend selbstständig.\n\n"
                "Vorschlag neue Ziele:\n"
                "- Herr Peter kocht bis Ende des Quartals zweimal pro Woche ein einfaches Gericht selbstständig.\n\n"
                "Vorschlag Maßnahmen:\n"
                "- **Kochtraining:** wöchentliches Kochen mit Assistenz\n\n"
                "### 1.3 Basisversorgung / Grundpflege\n"
                "Bisherige Zielerreichung:\n"
                "Herr Peter kocht regelmäßig und reinigt den Herd. Er übernimmt den Wocheneinkauf selbstständig.\n"
            ),
        },
    ]
    test_bot.messages = chat_dialogue

    # Export nach Hamburg via export_chatbot_to_docx
    result = export_chatbot_to_docx(test_bot, "hamburg")
    docx_bytes = result[0]
    filename = result[1]
    warnings = result.warnings
    manual_fields = result.manual_fields

    assert filename.startswith("SOB_hamburg_")
    assert "Hans" not in filename and "Peter" not in filename

    root = fields.load_document_tree(io.BytesIO(docx_bytes))

    # Kopfdaten prüfen
    meta_von = fields.get_formtext(root, "meta_leistungszeitraum_von")
    meta_bis = fields.get_formtext(root, "meta_leistungszeitraum_bis")
    geb = fields.get_formtext(root, "client_geburtsdatum")
    anb = fields.get_formtext(root, "meta_leistungsanbieter")
    name_vorname = fields.get_formtext(root, "client_name_vorname")

    assert meta_von == "01.07.2024"
    assert meta_bis == "30.06.2026"
    assert geb == "22.11.1985"
    assert anb == "Ambulante Assistenz Hamburg-Ost"
    assert name_vorname == "Peter, Hans"

    # 1.2: Alltägliche Lebensführung (Bookmark Index 1)
    sec_1_bericht = fields.get_formtext(root, "sections_1_bericht_zielerreichung")
    sec_1_vorschlag = fields.get_formtext(root, "sections_1_vorschlag_ziele")
    sec_1_massnahmen = fields.get_formtext(root, "sections_1_vorschlag_massnahmen")

    assert "Wocheneinkauf inzwischen mit Einkaufsliste weitgehend selbstständig" in sec_1_bericht
    assert "**" not in sec_1_bericht

    assert "einfaches Gericht selbstständig" in sec_1_vorschlag
    assert "**" not in sec_1_vorschlag

    assert "Kochtraining: wöchentliches Kochen mit Assistenz" in sec_1_massnahmen
    assert "**" not in sec_1_massnahmen

    # 1.3: Basisversorgung (Bookmark Index 2) darf KEINE Haushaltsinhalte enthalten
    sec_2_bericht = fields.get_formtext(root, "sections_2_bericht_zielerreichung")
    assert "Kochen" not in sec_2_bericht
    assert "Herd" not in sec_2_bericht
    assert "Wocheneinkauf" not in sec_2_bericht

    # Warnungen: exakter Grund für den verworfenen 1.3-Block
    assert any("Bereich unklar: Frage zielt auf 1.2, Modell schreibt 1.3" in w for w in warnings)

    # Manuelle Felder: Leistungsart enthalten, Leistungszeitraum/Geburtsdatum/Leistungsanbieter NICHT enthalten
    assert "Leistungsart" in manual_fields
    assert "Leistungszeitraum" not in manual_fields
    assert "Leistung erbracht von/bis" not in manual_fields
    assert "Geburtsdatum" not in manual_fields
    assert "Leistungsanbieter" not in manual_fields


def test_parse_vivendi_text_inline_header_sample_direct():
    """Unit-Test für parse_vivendi_text mit fiktivem Inline-Header ohne Fixture-Datei."""
    from sob_generator.importer import parse_vivendi_text

    sample = (
        ";;;;;;;;;;;;;;;;01.01.2024 00:00 - 31.12.2025 23:59;;\n"
        "Muster-Einrichtung Hamburg-Nord;;;;;;;;;;;;;;;;;;\n"
        ";;;;;;;;;;;;;;;;;;\n"
        "Schmidt, Walter (*10.04.1979);;;;;;;;;;;;;;;;;;\n"
        ";;;;;Datum;;Uhrzeit;;Schicht;;Kategorie;;Kennzeichen;;Details;;;;Handzeichen\n"
        ";;;;15.02.2024;;10:00;;Früh;;;;;;;;;;MA (15.02.2024 10:00)\n"
        ";;;;Unterstützung bei der Alltagsstrukturierung erfolgreich durchgeführt.;;;;;;;;;;;;;;\n"
    )
    imported = parse_vivendi_text(sample, strict=False)

    assert imported.meta.leistungszeitraum_von == "01.01.2024"
    assert imported.meta.leistungszeitraum_bis == "31.12.2025"
    assert imported.meta.leistungsanbieter == "Muster-Einrichtung Hamburg-Nord"
    assert imported.client.name == "Schmidt"
    assert imported.client.vorname == "Walter"
    assert imported.client.geburtsdatum == "10.04.1979"
    assert len(imported.entries) == 1
    assert "Alltagsstrukturierung" in imported.entries[0].text




def test_inline_field_markers_with_content_on_same_line():
    """Feld-Marker mit Inhalt in derselben Zeile ('**Vorschlag neue Ziele:** …') werden erkannt."""
    messages = [
        {"role": "user", "content": "Wie hat sich die Haushaltsführung entwickelt?"},
        {
            "role": "assistant",
            "content": (
                "**Bisherige Zielerreichung:** Herr Muster kauft mit Einkaufsliste ein.\n"
                "**Vorschlag neue Ziele:** Herr Muster kocht zweimal pro Woche selbstständig.\n"
                "- **Vorschlag Maßnahmen:** wöchentliches Kochtraining mit Assistenz\n\n"
                "### Reflexionsfragen:\n"
                "1. Wie erlebst du seine Motivation?\n\n"
                "Diese Fragen könnten dir helfen."
            ),
        },
    ]
    extracted = extract_section_data_from_chat(messages)
    assert set(extracted.keys()) == {"1.2"}
    assert extracted["1.2"]["bericht_zielerreichung"] == "Herr Muster kauft mit Einkaufsliste ein."
    assert extracted["1.2"]["vorschlag_ziele"] == "Herr Muster kocht zweimal pro Woche selbstständig."
    assert extracted["1.2"]["vorschlag_massnahmen"] == "wöchentliches Kochtraining mit Assistenz"
    assert extracted.unmapped_blocks == []


# ---------------------------------------------------------------------------
# Pilot-Readiness-Fixes (#118): neutraler Dateiname, kein stiller Fallback,
# keine Falldaten in Logs/HTTP-Detail. Alle Namen/Texte sind fiktiv.
# ---------------------------------------------------------------------------

FICTIONAL_NAME = "Łukasz Øberg"
FICTIONAL_SECRET = "Fiktivgeheimnis Zebra-4711"


def _vivendi_csv(client_line: str, zeitraum: str = "01.01.2025 08:00 - 31.12.2025 17:00") -> str:
    return (
        f";;;;;;;;;;;;;;;;{zeitraum};;\n"
        "Fiktive Soziale Einrichtung Nord;;;;;;;;;;;;;;;;;;\n"
        ";;;;;;;;;;;;;;;;;;\n"
        f"{client_line};;;;;;;;;;;;;;;;;;\n"
        ";;;;;Datum;;Uhrzeit;;Schicht;;Kategorie;;Kennzeichen;;Details;;;Handzeichen\n"
        ";;;;10.03.2025;;09:00;;Früh;;Alltag;;;;;;;;MA (10.03.2025)\n"
        ";;;;Dokumentationseintrag zur Haushaltsführung;;;;;;;;;;;;;;\n"
    )


def test_export_filename_neutral_for_non_latin1_name(client, test_bot):
    """Name außerhalb von latin-1 führt nicht mehr zu HTTP 500; Dateiname bleibt neutral."""
    test_bot.set_csv(parse_csv_content(_vivendi_csv("Øberg, Łukasz (*01.02.1980)"), filename="Lukasz_Oberg.csv"))
    test_bot.messages = [
        {"role": "user", "content": "Bericht"},
        {"role": "assistant", "content": "### Bereich 1.2\n- Bad gereinigt"},
    ]
    for tmpl, slug in (("hamburg", "hamburg"), ("op-de-wisch", "op-de-wisch")):
        res = client.get(f"/api/export/docx?template={tmpl}")
        assert res.status_code == 200, res.text
        disposition = res.headers["content-disposition"]
        assert re.fullmatch(rf'attachment; filename="SOB_{slug}_\d{{4}}-\d{{2}}-\d{{2}}\.docx"', disposition)
        for part in ("Łukasz", "Øberg", "Lukasz", "Oberg"):
            assert part not in disposition


def test_export_filename_helper_is_ascii():
    from datetime import date

    from chat_app.docx_exporter import export_filename

    when = date(2026, 10, 28)
    assert export_filename(resolve_template("hamburg"), when) == "SOB_hamburg_2026-10-28.docx"
    assert export_filename(resolve_template("op-de-wisch"), when) == "SOB_op-de-wisch_2026-10-28.docx"


def test_unexpected_csv_header_error_is_not_swallowed(test_bot, monkeypatch):
    """Unerwartete Fehler werden nicht verschluckt, sondern propagieren (-> generischer 500er)."""
    import sob_generator.importer as importer_module

    def broken(*_a, **_k):
        raise RuntimeError("unerwartet")

    monkeypatch.setattr(importer_module, "parse_vivendi_text", broken)
    test_bot.set_csv(parse_csv_content(_vivendi_csv("Øberg, Łukasz (*01.02.1980)"), filename="kaputt.csv"))
    test_bot.messages = [
        {"role": "user", "content": "Bericht"},
        {"role": "assistant", "content": "### Bereich 1.2\n- Bad gereinigt"},
    ]
    with pytest.raises(RuntimeError):
        build_report_draft_from_chatbot(test_bot, template=resolve_template("hamburg"))


@pytest.mark.parametrize("exc", [ValueError, IndexError, csv.Error])
def test_unreadable_csv_header_reports_manual_field_instead_of_silent_pass(test_bot, monkeypatch, exc):
    """Kopfdaten nicht parsebar: kein stilles `except: pass`, sondern ❌-Hinweis ohne Rohzeile."""
    import sob_generator.importer as importer_module

    def broken(*_a, **_k):
        raise exc("Klientenzeile 'Øberg, Łukasz (*01.02.1980)' nicht parsebar")

    monkeypatch.setattr(importer_module, "parse_vivendi_text", broken)
    test_bot.set_csv(parse_csv_content(_vivendi_csv("Øberg, Łukasz (*01.02.1980)"), filename="kaputt.csv"))
    test_bot.messages = [
        {"role": "user", "content": "Bericht"},
        {"role": "assistant", "content": "### Bereich 1.2\n- Bad gereinigt"},
    ]
    _draft, _warnings, manual = build_report_draft_from_chatbot(
        test_bot, template=resolve_template("hamburg"), return_unmapped=True, return_manual=True
    )
    assert any(m.startswith("❌ CSV-Kopfdaten nicht lesbar") for m in manual)
    assert not any("Łukasz" in m or "Øberg" in m for m in manual)


def test_non_vivendi_csv_reports_manual_header_item(test_bot):
    test_bot.set_csv(parse_csv_content("Datum;Text\n10.03.2025;Eintrag\n", filename="custom.csv"))
    test_bot.messages = [
        {"role": "user", "content": "Bericht"},
        {"role": "assistant", "content": "### Bereich 1.2\n- Bad gereinigt"},
    ]
    _draft, _warnings, manual = build_report_draft_from_chatbot(
        test_bot, template=resolve_template("hamburg"), return_unmapped=True, return_manual=True
    )
    assert any("kein Vivendi-Export" in m for m in manual)


def test_docx_exporter_has_no_silent_except_pass():
    import chat_app.docx_exporter as exporter_module

    source = inspect.getsource(exporter_module)
    assert not re.search(r"except Exception:\s*\n\s*pass", source)


def test_422_detail_and_log_contain_no_client_text(client, test_bot, caplog):
    """Nicht zuordenbarer Dialog: 422-Detail und Log enthalten keine Überschriften oder Inhalte."""
    test_bot.messages = [
        {"role": "user", "content": "Bericht"},
        {
            "role": "assistant",
            "content": (
                f"### {FICTIONAL_NAME} erzählt vom Wochenende\n"
                f"- {FICTIONAL_SECRET} im Gespräch mit {FICTIONAL_NAME}.\n"
            ),
        },
    ]
    with caplog.at_level(logging.DEBUG):
        res = client.get("/api/export/docx?template=hamburg")
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert "❌ Dialog-Inhalt konnte keinem Bereich zugeordnet werden" in detail
    for needle in (FICTIONAL_NAME, "Łukasz", "Øberg", FICTIONAL_SECRET, "Wochenende"):
        assert needle not in detail
        assert needle not in caplog.text


@pytest.mark.parametrize(
    "exc_factory, code",
    [
        (lambda: DocxExportError(f"Vorlage kaputt bei {FICTIONAL_NAME}: {FICTIONAL_SECRET}"), "DOCX_EXPORT_ERROR"),
        (lambda: RuntimeError(f"Zeile 'Øberg, Łukasz (*01.02.1980)' {FICTIONAL_SECRET}"), "DOCX_INTERNAL"),
        (lambda: UnicodeEncodeError("latin-1", FICTIONAL_NAME, 0, 1, "ordinal not in range"), "DOCX_INTERNAL"),
    ],
)
def test_500_detail_and_log_are_generic(client, monkeypatch, caplog, exc_factory, code):
    import chat_app.app as app_module

    def boom(*_args, **_kwargs):
        raise exc_factory()

    monkeypatch.setattr(app_module, "export_chatbot_to_docx", boom)
    with caplog.at_level(logging.DEBUG):
        res = client.get("/api/export/docx?template=hamburg")
    assert res.status_code == 500
    detail = res.json()["detail"]
    assert detail == f"❌ Export fehlgeschlagen (Fehlercode: {code})"
    for needle in (FICTIONAL_NAME, "Łukasz", "Øberg", FICTIONAL_SECRET, "01.02.1980"):
        assert needle not in detail
        assert needle not in caplog.text
    assert "Export" in caplog.text  # generische Meldung wurde geloggt


def test_warning_labels_are_short_and_do_not_echo_section_text(client, test_bot):
    long_heading = "Selbstreflexion " + "und noch mehr Rohtext " * 6 + FICTIONAL_NAME
    test_bot.messages = [
        {"role": "user", "content": "Ziele vorschlagen"},
        {
            "role": "assistant",
            "content": (
                "### 1. **Haushalt und Einkauf:**\n- Selbständig einkaufen.\n\n"
                f"### 2. **{long_heading}:**\n- {FICTIONAL_SECRET}.\n"
            ),
        },
    ]
    res = client.get("/api/export/docx?template=hamburg")
    assert res.status_code == 200
    import json as _json

    warnings = _json.loads(urllib.parse.unquote(res.headers["x-export-warnings"]))
    assert warnings
    for w in warnings:
        heading_part = w.split(" (", 1)[0]
        assert len(heading_part) <= 60
        assert FICTIONAL_NAME not in w
        assert FICTIONAL_SECRET not in w


def test_strict_vivendi_error_does_not_echo_raw_client_line():
    from sob_generator.importer import parse_vivendi_text

    with pytest.raises(ValueError) as exc_info:
        parse_vivendi_text(_vivendi_csv(f"{FICTIONAL_NAME} ohne Geburtsdatum"), strict=True)
    assert FICTIONAL_NAME not in str(exc_info.value)
    assert "Łukasz" not in repr(exc_info.value)


# --- Klientenname nur aus Vivendi-Kopfzeile, nie aus dem Upload-Dateinamen (Pilot-Bug #118) ---

HANS_PETER_FIXTURE = ROOT_DIR / "tests" / "fixtures" / "hans_peter_synthetisch.csv"
_NAME_DIALOGUE = [
    {"role": "user", "content": "Haushalt?"},
    {"role": "assistant", "content": "### 1.2 Alltägliche Lebensführung\nBisherige Zielerreichung:\nHerr Peter kauft selbstständig ein."},
]


def _rendered_name(docx_bytes: bytes, template_key: str) -> str:
    if template_key == "hamburg":
        return fields.get_formtext(fields.load_document_tree(io.BytesIO(docx_bytes)), "client_name_vorname")
    import docx as _docx

    doc = _docx.Document(io.BytesIO(docx_bytes))
    sdts = doc.tables[0].rows[0].cells[2]._tc.xpath(".//w:sdt")
    return "".join(sdts[0].xpath(".//w:t/text()", namespaces=doc.element.nsmap))


@pytest.mark.parametrize("upload_name", ["export_xyz.csv", "hans_peter_synthetisch.csv"])
@pytest.mark.parametrize("template_key", ["hamburg", "op-de-wisch"])
def test_client_name_comes_from_vivendi_header_not_filename(client, test_bot, upload_name, template_key):
    """Upload über /api/upload: Dateiname weicht von der Kopfzeile ab -> exakt 'Peter' / 'Hans'."""
    res = client.post(
        "/api/upload",
        files={"file": (upload_name, HANS_PETER_FIXTURE.read_bytes(), "text/csv")},
    )
    assert res.status_code == 200, res.text
    test_bot.messages = list(_NAME_DIALOGUE)

    draft = build_report_draft_from_chatbot(test_bot, resolve_template(template_key))
    assert draft.client.name == "Peter"
    assert draft.client.vorname == "Hans"
    assert draft.client.geburtsdatum == "22.11.1985"

    result = export_chatbot_to_docx(test_bot, template_key)
    assert _rendered_name(result[0], template_key) == "Peter, Hans"
    assert not any("Name, Vorname" in item for item in result.manual_fields)


def test_vivendi_header_wins_over_plan_filename_name(test_bot):
    """Auch ein aus dem Vorbericht-Dateinamen abgeleiteter Name überstimmt die CSV-Kopfzeile nicht."""
    test_bot.set_csv(parse_csv_content(HANS_PETER_FIXTURE.read_text(encoding="utf-8-sig"), filename="x.csv", client_name="hans peter synthetisch"))
    test_bot.set_plan(PlanData(filename="Fiktiv_Anders_Gesamtplan.docx", client_name="Fiktiv Anders", client_name_from_filename=True, goals=[]))
    test_bot.messages = list(_NAME_DIALOGUE)
    draft = build_report_draft_from_chatbot(test_bot, resolve_template("hamburg"))
    assert (draft.client.name, draft.client.vorname) == ("Peter", "Hans")


@pytest.mark.parametrize("template_key", ["hamburg", "op-de-wisch"])
def test_non_vivendi_csv_leaves_name_empty_and_lists_it_manually(client, test_bot, template_key):
    """Kein Vivendi-Kopf -> Name bleibt leer (nie aus dem Dateinamen) und steht auf der ❌-Liste."""
    content = "Datum;Notiz\n01.03.2025;Fiktiver Eintrag\n".encode("utf-8")
    res = client.post("/api/upload", files={"file": ("Fiktiva_Musterfrau_Vivendi.csv", content, "text/csv")})
    assert res.status_code == 200, res.text
    test_bot.messages = list(_NAME_DIALOGUE)

    draft = build_report_draft_from_chatbot(test_bot, resolve_template(template_key))
    assert draft.client.name == ""
    assert draft.client.vorname == ""

    result = export_chatbot_to_docx(test_bot, template_key)
    assert CLIENT_NAME_MANUAL_ITEM in result.manual_fields
    assert CLIENT_NAME_MANUAL_ITEM == "❌ Name, Vorname manuell ergänzen"
    rendered = _rendered_name(result[0], template_key)
    for part in ("Fiktiva", "Musterfrau", "Klient"):
        assert part not in rendered


def test_plan_filename_name_is_flagged_and_not_used(test_bot):
    """Vorbericht ohne Namensfeld: Name aus dem Dateinamen nur als UI-Hinweis, nicht im Bericht."""
    test_bot.set_csv(parse_csv_content("Datum;Notiz\n01.03.2025;Fiktiver Eintrag\n", filename="daten.csv"))
    test_bot.set_plan(PlanData(filename="Fiktiv_Anders_Gesamtplan.docx", client_name="Fiktiv Anders", client_name_from_filename=True, goals=[]))
    test_bot.messages = list(_NAME_DIALOGUE)
    result = export_chatbot_to_docx(test_bot, "hamburg")
    assert CLIENT_NAME_MANUAL_ITEM in result.manual_fields
    assert "Fiktiv" not in _rendered_name(result[0], "hamburg")


def test_plan_document_name_field_used_when_no_vivendi_header(test_bot):
    """Namensfeld aus dem Vorbericht-Dokument (nicht Dateiname) bleibt als zweite Quelle zulässig."""
    test_bot.set_csv(parse_csv_content("Datum;Notiz\n01.03.2025;Fiktiver Eintrag\n", filename="daten.csv"))
    test_bot.set_plan(PlanData(filename="plan.docx", client_name="Musterfrau, Fiktiva", goals=[]))
    test_bot.messages = list(_NAME_DIALOGUE)
    draft = build_report_draft_from_chatbot(test_bot, resolve_template("hamburg"))
    assert (draft.client.name, draft.client.vorname) == ("Musterfrau", "Fiktiva")


# ===========================================================================
# Tests für Pilot-Befunde (T-001): Befund 1 (Mapping & Varianten) & Befund 2
# ===========================================================================

def _load_pilot_fixture() -> Tuple[str, str]:
    """Liest tests/fixtures/abschlusstest-chat-2026-10-29.md programmatisch ein.

    - Frage: Text nach 'Fachkraft: '
    - Sektionen: Text von '### 2.2' bis vor 'Export-Ergebnis:'
    - Davor: synthetische Intro-Liste ('1. ...', '2. ...')
    """
    fixture_path = ROOT_DIR / "tests" / "fixtures" / "abschlusstest-chat-2026-10-29.md"
    assert fixture_path.exists(), f"Fixture nicht gefunden unter {fixture_path}"
    content = fixture_path.read_text(encoding="utf-8")

    m_q = re.search(r"^Fachkraft:\s*(.+)$", content, re.MULTILINE)
    assert m_q, "Fachkraft-Frage in Fixture nicht gefunden"
    user_msg = m_q.group(1).strip()

    start_idx = content.index("### 2.2")
    end_idx = content.index("Export-Ergebnis:")
    sections_text = content[start_idx:end_idx].strip()

    intro_list = (
        "1. Mögliche Ursachen für die Erschöpfung analysieren\n"
        "2. Ziele für die Zusammenarbeit mit der Familie festlegen\n\n"
    )
    assistant_msg = intro_list + sections_text
    return user_msg, assistant_msg


def test_pilot_fixture_abschlusstest_2026_10_29_extract_hamburg():
    """Befund 1 Fixture auf Extraktionsebene für Vorlage Hamburg (liest Datei ein)."""
    user_msg, assistant_msg = _load_pilot_fixture()
    messages = [
        {"role": "user", "content": user_msg},
        {"role": "assistant", "content": assistant_msg},
    ]
    template = resolve_template("hamburg")
    extracted = extract_section_data_from_chat(messages, template=template)

    # 1.2, 2.1, 2.2 müssen alle extrahiert werden (keine Verwerfung durch Frage-Konflikt)
    assert "1.2" in extracted
    assert "2.1" in extracted
    assert "2.2" in extracted

    # Keine ❌-Warnungen für diese drei Sektionen
    assert len(extracted.unmapped_blocks) == 0

    # 1.2 Felder prüfen
    s12 = extracted["1.2"]
    assert "selbständig aus" in s12["bericht_zielerreichung"]
    assert "Unterstützung bei der Planung" in s12["vorschlag_ziele"]
    assert "Mahlzeiten planen und kochen" in s12["vorschlag_massnahmen"]

    # 2.1 Felder prüfen
    s21 = extracted["2.1"]
    assert "Kegelgruppe eingefügt" in s21["bericht_zielerreichung"]
    assert "Konflikten und Anspannungen" in s21["vorschlag_ziele"]
    assert "soziale Unterstützung" in s21["vorschlag_massnahmen"]

    # 2.2 Felder prüfen
    s22 = extracted["2.2"]
    assert "Kegeln teil" in s22["bericht_zielerreichung"]
    assert "Vereinsmitglieder sollen gemeinsam" in s22["vorschlag_ziele"]
    assert "regelmäßige Treffen" in s22["vorschlag_massnahmen"]

    # Intro-Liste landet nirgends
    for sec_data in extracted.values():
        for field_text in sec_data.values():
            assert "Mögliche Ursachen" not in field_text
            assert "Ziele für die Zusammenarbeit" not in field_text


def test_pilot_fixture_abschlusstest_2026_10_29_extract_odw():
    """Befund 1 Fixture auf Extraktionsebene für Vorlage Op de Wisch: 1.2->2, 2.1+2.2->6 (liest Datei ein)."""
    user_msg, assistant_msg = _load_pilot_fixture()
    messages = [
        {"role": "user", "content": user_msg},
        {"role": "assistant", "content": assistant_msg},
    ]
    template = resolve_template("op-de-wisch")
    extracted = extract_section_data_from_chat(messages, template=template)

    assert "2" in extracted
    assert "6" in extracted
    assert len(extracted.unmapped_blocks) == 0

    # Bereich 2 (aus 1.2)
    assert "selbständig aus" in extracted["2"]["bericht_zielerreichung"]

    # Bereich 6 (aus 2.1 und 2.2 zusammengeführt)
    b6 = extracted["6"]["bericht_zielerreichung"]
    z6 = extracted["6"]["vorschlag_ziele"]
    m6 = extracted["6"]["vorschlag_massnahmen"]

    # Beide Inhalte in 6 vorhanden
    assert "Kegelgruppe eingefügt" in b6 and "Kegeln teil" in b6
    assert "Konflikten" in z6 and "Vereinsmitglieder" in z6
    assert "soziale Unterstützung" in m6 and "regelmäßige Treffen" in m6


def test_pilot_fixture_abschlusstest_2026_10_29_e2e(test_bot):
    """Befund 1 End-to-End über export_chatbot_to_docx für Hamburg und Op de Wisch.

    Prüft zusätzlich Ziele & Maßnahmen und schließt Kreuz-Kontamination aus.
    """
    user_msg, assistant_msg = _load_pilot_fixture()

    fixture_path = ROOT_DIR / "tests" / "fixtures" / "hans_peter_synthetisch.csv"
    csv_content = fixture_path.read_text(encoding="utf-8-sig")
    parsed_csv = parse_csv_content(csv_content, filename=fixture_path.name)
    test_bot.set_csv(parsed_csv)

    test_bot.messages = [
        {"role": "user", "content": user_msg},
        {"role": "assistant", "content": assistant_msg},
    ]

    # Hamburg E2E
    res_hamburg = export_chatbot_to_docx(test_bot, "hamburg")
    assert not any("im Chat erwähnt, aber nicht zugeordnet" in w for w in res_hamburg.warnings)
    root_h = fields.load_document_tree(io.BytesIO(res_hamburg[0]))

    b_12 = fields.get_formtext(root_h, "sections_1_bericht_zielerreichung")
    z_12 = fields.get_formtext(root_h, "sections_1_vorschlag_ziele")
    m_12 = fields.get_formtext(root_h, "sections_1_vorschlag_massnahmen")

    b_21 = fields.get_formtext(root_h, "sections_5_bericht_zielerreichung")
    z_21 = fields.get_formtext(root_h, "sections_5_vorschlag_ziele")
    m_21 = fields.get_formtext(root_h, "sections_5_vorschlag_massnahmen")

    b_22 = fields.get_formtext(root_h, "sections_6_bericht_zielerreichung")
    z_22 = fields.get_formtext(root_h, "sections_6_vorschlag_ziele")
    m_22 = fields.get_formtext(root_h, "sections_6_vorschlag_massnahmen")

    # Hamburg 1.2: Alltägliche Lebensführung
    assert "selbständig aus" in b_12
    assert "Kegel" not in b_12
    assert "Unterstützung bei der Planung" in z_12
    assert "Kegel" not in z_12
    assert "Mahlzeiten planen und kochen" in m_12

    # Hamburg 2.1: Soziale Beziehungen
    assert "Kegelgruppe eingefügt" in b_21
    assert "hauswirtschaftlichen" not in b_21
    assert "Konflikten und Anspannungen" in z_21
    assert "soziale Unterstützung" in m_21

    # Hamburg 2.2: Teilhabe am kulturellen/gesellschaftlichen Leben
    assert "Kegeln teil" in b_22
    assert "hauswirtschaftlichen" not in b_22
    assert "Vereinsmitglieder sollen gemeinsam" in z_22
    assert "hauswirtschaftlichen" not in z_22
    assert "regelmäßige Treffen" in m_22

    # Op de Wisch E2E
    res_odw = export_chatbot_to_docx(test_bot, "op-de-wisch")
    assert not any("im Chat erwähnt, aber nicht zugeordnet" in w for w in res_odw.warnings)
    doc_odw = Document(io.BytesIO(res_odw[0]))

    # Tabelle 2 = Lebensbereich 2 (aus Hamburg 1.2)
    t2 = doc_odw.tables[2]
    b_2_odw = t2.rows[2].cells[0].text
    z_2_odw = t2.rows[3].cells[0].text
    m_2_odw = t2.rows[3].cells[1].text
    assert "selbständig aus" in b_2_odw
    assert "Kegel" not in b_2_odw
    assert "Unterstützung bei der Planung" in z_2_odw
    assert "Kegel" not in z_2_odw
    assert "Mahlzeiten planen und kochen" in m_2_odw

    # Tabelle 6 = Lebensbereich 6 (aus Hamburg 2.1 und 2.2 zusammengeführt)
    t6 = doc_odw.tables[6]
    b_6_odw = t6.rows[2].cells[0].text
    z_6_odw = t6.rows[3].cells[0].text
    m_6_odw = t6.rows[3].cells[1].text
    assert "Kegelgruppe eingefügt" in b_6_odw and "Kegeln teil" in b_6_odw
    assert "hauswirtschaftlichen" not in b_6_odw
    assert "Konflikten" in z_6_odw and "Vereinsmitglieder" in z_6_odw
    assert "hauswirtschaftlichen" not in z_6_odw
    assert "soziale Unterstützung" in m_6_odw and "regelmäßige Treffen" in m_6_odw


def test_strict_conflict_for_explicit_section_question():
    """Explizit nummerierte Nutzerfragen behalten die strikte Konfliktprüfung."""
    messages = [
        {"role": "user", "content": "Was gibt es zu Bereich 1.2 zu berichten?"},
        {
            "role": "assistant",
            "content": (
                "### 1.3 Basisversorgung / Grundpflege\n"
                "**Bisherige Zielerreichung:**\nHerr Peter duscht selbstständig.\n\n"
                "### 2.1 Soziale Beziehungen\n"
                "**Bisherige Zielerreichung:**\nHerr Peter hat Kontakt zur Schwester."
            ),
        },
    ]
    extracted = extract_section_data_from_chat(messages, template=resolve_template("hamburg"))
    # Da die Frage explizit "Bereich 1.2" nannte, darf die Ausnahme nicht greifen -> Konflikt!
    assert "1.3" not in extracted
    assert any("Bereich unklar: Frage zielt auf 1.2, Modell schreibt 1.3" in b.reason for b in extracted.unmapped_blocks)


def test_14b_variant_a_bold_line_heading():
    """Variante a: Fette Zeile ohne ###: **1.2 Hauswirtschaft** als eigene Zeile."""
    msg_valid = [
        {"role": "user", "content": "Wie sieht es aus?"},
        {
            "role": "assistant",
            "content": (
                "**1.2 Alltägliche Lebensführung / Hauswirtschaft**\n"
                "**Bisherige Zielerreichung:**\n"
                "Herr Peter kocht selbstständig.\n"
                "**Vorschlag neue Ziele:**\n"
                "- Neues Ziel 1\n"
                "**Vorschlag Maßnahmen:**\n"
                "- Maßnahme 1"
            ),
        },
    ]
    extracted = extract_section_data_from_chat(msg_valid, template=resolve_template("hamburg"))
    assert "1.2" in extracted
    assert "kocht selbstständig" in extracted["1.2"]["bericht_zielerreichung"]

    # Gegenprobe: Fette Zeile ohne passenden Titel oder Field-Marker darf NICHT als Sektion gelten
    msg_invalid = [
        {"role": "user", "content": "Wie sieht es aus?"},
        {
            "role": "assistant",
            "content": (
                "**1.2 Unbekanntes Thema ohne Bezug**\n"
                "Fließtext ohne Struktur."
            ),
        },
    ]
    extracted_inv = extract_section_data_from_chat(msg_invalid, template=resolve_template("hamburg"))
    assert "1.2" not in extracted_inv


def test_14b_variant_b_bereich_prefix():
    """Variante b: ### Bereich 1.2: Hauswirtschaft."""
    messages = [
        {"role": "user", "content": "Wie sieht es aus?"},
        {
            "role": "assistant",
            "content": (
                "### Bereich 1.2: Hauswirtschaft\n"
                "**Bisherige Zielerreichung:**\nHauswirtschaft läuft gut."
            ),
        },
    ]
    extracted = extract_section_data_from_chat(messages, template=resolve_template("hamburg"))
    assert "1.2" in extracted
    assert "Hauswirtschaft läuft gut" in extracted["1.2"]["bericht_zielerreichung"]


def test_14b_variant_c_parenthesized_id():
    """Variante c: ### Hauswirtschaft (1.2)."""
    messages = [
        {"role": "user", "content": "Wie sieht es aus?"},
        {
            "role": "assistant",
            "content": (
                "### Hauswirtschaft (1.2)\n"
                "**Bisherige Zielerreichung:**\nHauswirtschaft läuft gut."
            ),
        },
    ]
    extracted = extract_section_data_from_chat(messages, template=resolve_template("hamburg"))
    assert "1.2" in extracted
    assert "Hauswirtschaft läuft gut" in extracted["1.2"]["bericht_zielerreichung"]


def test_14b_variant_d_numbered_enumeration_not_id_1():
    """Variante d: nummerierte Listen-Überschrift ### 1. Haushaltsführung darf NIE als ID 1 gelten."""
    messages = [
        {"role": "user", "content": "Wie sieht es aus?"},
        {
            "role": "assistant",
            "content": (
                "### 1. Haushaltsführung\n"
                "**Bisherige Zielerreichung:**\nEinkauf gelingt."
            ),
        },
    ]
    extracted = extract_section_data_from_chat(messages, template=resolve_template("hamburg"))
    assert "1.2" in extracted
    assert "1.1" not in extracted
    assert "1" not in extracted


def test_14b_variant_e_multiple_sections_without_question_sec():
    """Variante e: mehrere Sektionen in EINER Nachricht ohne Fragebereich."""
    messages = [
        {"role": "user", "content": "Berichte über alle Bereiche."},
        {
            "role": "assistant",
            "content": (
                "### 1.2 Alltägliche Lebensführung\n**Bisherige Zielerreichung:**\nGut.\n\n"
                "### 2.1 Soziale Beziehungen\n**Bisherige Zielerreichung:**\nAuch gut."
            ),
        },
    ]
    extracted = extract_section_data_from_chat(messages, template=resolve_template("hamburg"))
    assert "1.2" in extracted
    assert "2.1" in extracted


def test_14b_variant_f_section_bullets_without_subheadings_user_intent():
    """Variante f: Sektion ohne die drei Unterüberschriften (nur Bullets) – Feld per Nutzer-Intention."""
    messages = [
        {"role": "user", "content": "Welche neuen Ziele schlägst du für den Haushalt vor?"},
        {
            "role": "assistant",
            "content": "### 1.2 Alltägliche Lebensführung\n- Kochen lernen\n- Wäsche waschen",
        },
    ]
    extracted = extract_section_data_from_chat(messages, template=resolve_template("hamburg"))
    assert "1.2" in extracted
    assert "- Kochen lernen" in extracted["1.2"]["vorschlag_ziele"]
    assert extracted["1.2"]["bericht_zielerreichung"] == ""


def test_14b_variant_g_trailing_meta_and_reflection_block_ignored():
    """Variante g: Schlusssatz/Meta-Block direkt nach Sektion – Sektionsinhalt bleibt erhalten."""
    messages = [
        {"role": "user", "content": "Wie läuft der Haushalt?"},
        {
            "role": "assistant",
            "content": (
                "### 1.2 Alltägliche Lebensführung\n"
                "**Bisherige Zielerreichung:**\nGut.\n\n"
                "### Reflexionsfragen\n"
                "1. Was meinst du dazu?\n\n"
                "Diese Ziele können im Hilfeplangespräch besprochen werden."
            ),
        },
    ]
    extracted = extract_section_data_from_chat(messages, template=resolve_template("hamburg"))
    assert "1.2" in extracted
    assert extracted["1.2"]["bericht_zielerreichung"] == "Gut."
    assert len(extracted.unmapped_blocks) == 0


def test_14b_variant_h_title_mapping_teilhabe_to_2_2():
    """Variante h: ### 2.2 Teilhabe bzw. Teilhabe am gesellschaftlichen Leben ohne Nummer -> 2.2."""
    hamburg = resolve_template("hamburg")
    extracted1 = extract_section_data_from_chat(
        [{"role": "user", "content": "?"}, {"role": "assistant", "content": "### 2.2 Teilhabe\nInhalt"}],
        template=hamburg,
    )
    assert "2.2" in extracted1

    extracted2 = extract_section_data_from_chat(
        [
            {"role": "user", "content": "?"},
            {"role": "assistant", "content": "### Teilhabe am gesellschaftlichen Leben\nInhalt"},
        ],
        template=hamburg,
    )
    assert "2.2" in extracted2


def test_14b_variant_i_multiple_assistant_messages_merge():
    """Variante i: zwei Assistant-Nachrichten nacheinander (gleicher Bereich -> zusammengeführt)."""
    messages = [
        {"role": "user", "content": "Haushalt?"},
        {"role": "assistant", "content": "### 1.2 Alltägliche Lebensführung\n**Bisherige Zielerreichung:**\nKochen."},
        {"role": "user", "content": "Noch was zum Haushalt?"},
        {"role": "assistant", "content": "### 1.2 Alltägliche Lebensführung\n**Vorschlag neue Ziele:**\nWäsche."},
        {"role": "user", "content": "Freizeit?"},
        {"role": "assistant", "content": "### 2.2 Kulturelles Leben\n**Bisherige Zielerreichung:**\nKegeln."},
    ]
    extracted = extract_section_data_from_chat(messages, template=resolve_template("hamburg"))
    assert "1.2" in extracted
    assert "2.2" in extracted
    assert extracted["1.2"]["bericht_zielerreichung"] == "Kochen."
    assert extracted["1.2"]["vorschlag_ziele"] == "Wäsche."
    assert extracted["2.2"]["bericht_zielerreichung"] == "Kegeln."


def test_diagnose_helper_positive_and_negative(test_bot):
    """Diagnose-Helfer: Erwähnte, aber unzugeordnete Bereiche melden; keine Falsch-Positiven."""
    test_bot.set_csv(parse_csv_content("Datum;Notiz\n01.03.2025;Eintrag\n", filename="daten.csv"))

    # Positiv: 1.3 im Chat als Überschrift/Bereich erwähnt, während 2.1 zugeordnet wird -> Warnung für 1.3
    test_bot.messages = [
        {"role": "user", "content": "Wie laufen die sozialen Beziehungen?"},
        {
            "role": "assistant",
            "content": (
                "### 2.1 Gestaltung sozialer Beziehungen\n"
                "**Bisherige Zielerreichung:**\nHans pflegt gute Kontakte.\n\n"
                "Bereich 1.3: Basisversorgung\n"
                "Hierzu liegen keine aktuellen Angaben vor."
            ),
        },
    ]
    res = export_chatbot_to_docx(test_bot, "hamburg")
    assert any("❌ Bereich 1.3 im Chat erwähnt, aber nicht zugeordnet" in w for w in res.warnings)

    # Negativ: Beiläufige Zahl ("1.2 kg") im Fließtext erzeugt KEINE Warnung
    test_bot.messages = [
        {"role": "user", "content": "Wie lief das Treffen mit den Freunden?"},
        {"role": "assistant", "content": "### 2.1 Gestaltung sozialer Beziehungen\n**Bisherige Zielerreichung:**\nEr hat 1.2 kg Obst mit Freunden geteilt."},
    ]
    res_neg = export_chatbot_to_docx(test_bot, "hamburg")
    assert not any("Bereich 1.2 im Chat erwähnt" in w for w in res_neg.warnings)

    # Negativ: Befüllter Bereich erzeugt KEINE Warnung
    test_bot.messages = [
        {"role": "user", "content": "Haushalt?"},
        {"role": "assistant", "content": "### 1.2 Alltägliche Lebensführung\n**Bisherige Zielerreichung:**\nKochen gelingt."},
    ]
    res_pop = export_chatbot_to_docx(test_bot, "hamburg")
    assert not any("Bereich 1.2 im Chat erwähnt" in w for w in res_pop.warnings)


def test_hamburg_duplicate_bericht_row_removed_in_memory():
    """Befund 2: Hamburg-Duplikat-Zeile wird in-memory entfernt, Vorlage bleibt byte-identisch."""
    template_path = Path("templates/hamburg/Vorlage.docx")
    assert template_path.exists()

    # SHA-256 vor Render
    hash_before = hashlib.sha256(template_path.read_bytes()).hexdigest()

    renderer = HamburgDocxRenderer(template_path)
    buf = io.BytesIO()
    draft = ReportDraft(
        client=ClientData(name="Peter", vorname="Hans"),
        meta=ReportMeta(leistungsanbieter="Träger"),
        sections=[
            ReportSection(
                id=f"1.{i+1}" if i < 5 else f"2.{i-4}" if i < 8 else "3",
                title="Titel",
                bericht_zielerreichung=f"Bericht {i+1}",
                vorschlag_ziele=f"Ziel {i+1}",
                vorschlag_massnahmen=f"Maßnahme {i+1}",
            )
            for i in range(9)
        ],
    )
    renderer.render(draft, buf)

    # SHA-256 nach Render: Vorlage auf Disk unverändert!
    hash_after = hashlib.sha256(template_path.read_bytes()).hexdigest()
    assert hash_before == hash_after

    # Prüfe generiertes Dokument: "Bericht zur Zielerreichung" pro Abschnitt genau 1 Mal (insgesamt 9)
    buf.seek(0)
    root = fields.load_document_tree(buf)
    all_text = "".join(root.itertext())
    assert all_text.count("Bericht zur Zielerreichung") == 9

    # Alle Abschnitte befüllt
    for i in range(9):
        val = fields.get_formtext(root, f"sections_{i}_bericht_zielerreichung")
        assert val == f"Bericht {i+1}"


def test_hamburg_duplicate_row_not_removed_if_formfield_or_different_label():
    """Befund 2 Schutz: Zeilen mit Formularfeld (z. B. Checkbox) oder anderem Label werden NICHT entfernt."""
    from lxml import etree
    from sob_generator.renderer import _remove_duplicate_bericht_rows

    w_ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    # Synthetischer Baum mit zwei Tabellenzeilen:
    # Row 1: Original mit sections_1_bericht_zielerreichung
    # Row 2: Zweite Zeile mit abweichendem Label -> darf NICHT entfernt werden
    xml_str = f"""<w:document xmlns:w="{w_ns}">
        <w:body>
            <w:tbl>
                <w:tr>
                    <w:tc>
                        <w:p><w:t>Bericht zur Zielerreichung:</w:t></w:p>
                        <w:p><w:bookmarkStart w:id="1" w:name="sections_1_bericht_zielerreichung"/><w:bookmarkEnd w:id="1"/></w:p>
                    </w:tc>
                </w:tr>
                <w:tr>
                    <w:tc>
                        <w:p><w:t>Anderes Label:</w:t></w:p>
                    </w:tc>
                </w:tr>
            </w:tbl>
        </w:body>
    </w:document>"""
    root_diff_label = etree.fromstring(xml_str.encode("utf-8"))
    removed = _remove_duplicate_bericht_rows(root_diff_label)
    assert removed == 0

    # Synthetischer Baum mit gleicher Labelzeile, aber FORMCHECKBOX -> darf NICHT entfernt werden
    xml_str_cb = f"""<w:document xmlns:w="{w_ns}">
        <w:body>
            <w:tbl>
                <w:tr>
                    <w:tc>
                        <w:p><w:t>Bericht zur Zielerreichung / ausgeführte Maßnahme bzw. Methodik zur Zielerreichung: FORMTEXT</w:t></w:p>
                        <w:p><w:bookmarkStart w:id="1" w:name="sections_1_bericht_zielerreichung"/><w:bookmarkEnd w:id="1"/></w:p>
                    </w:tc>
                </w:tr>
                <w:tr>
                    <w:tc>
                        <w:p><w:t>Bericht zur Zielerreichung / ausgeführte Maßnahme bzw. Methodik zur Zielerreichung: FORMTEXT</w:t></w:p>
                        <w:p><w:fldChar w:fldCharType="begin"><w:ffData><w:checkBox/></w:ffData></w:fldChar></w:p>
                    </w:tc>
                </w:tr>
            </w:tbl>
        </w:body>
    </w:document>"""
    root_cb = etree.fromstring(xml_str_cb.encode("utf-8"))
    removed_cb = _remove_duplicate_bericht_rows(root_cb)
    assert removed_cb == 0


def test_hamburg_duplicate_row_not_removed_if_default_or_result_or_cell2_text():
    """Befund 3 Review-Verschärfung: Default-Text, Ergebnistext oder 2. Zelle verhindern Löschung."""
    from lxml import etree
    from sob_generator.renderer import _remove_duplicate_bericht_rows

    w_ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    # 1. Duplikat-Zeile mit Default-Text im FORMTEXT
    xml_default = f"""<w:document xmlns:w="{w_ns}">
        <w:body>
            <w:tbl>
                <w:tr>
                    <w:tc>
                        <w:p><w:t>Bericht zur Zielerreichung: FORMTEXT</w:t></w:p>
                        <w:p><w:bookmarkStart w:id="1" w:name="sections_1_bericht_zielerreichung"/><w:bookmarkEnd w:id="1"/></w:p>
                    </w:tc>
                </w:tr>
                <w:tr>
                    <w:tc>
                        <w:p><w:t>Bericht zur Zielerreichung: FORMTEXT</w:t></w:p>
                        <w:p><w:fldChar w:fldCharType="begin"><w:ffData><w:textInput><w:default w:val="Standardwert"/></w:textInput></w:ffData></w:fldChar></w:p>
                    </w:tc>
                </w:tr>
            </w:tbl>
        </w:body>
    </w:document>"""
    root_def = etree.fromstring(xml_default.encode("utf-8"))
    assert _remove_duplicate_bericht_rows(root_def) == 0

    # 2. Duplikat-Zeile mit abweichendem Text in zweiter Zelle
    xml_cell2 = f"""<w:document xmlns:w="{w_ns}">
        <w:body>
            <w:tbl>
                <w:tr>
                    <w:tc><w:p><w:t>Bericht zur Zielerreichung: FORMTEXT</w:t></w:p><w:p><w:bookmarkStart w:id="1" w:name="sections_1_bericht_zielerreichung"/><w:bookmarkEnd w:id="1"/></w:p></w:tc>
                </w:tr>
                <w:tr>
                    <w:tc><w:p><w:t>Bericht zur Zielerreichung: FORMTEXT</w:t></w:p></w:tc>
                    <w:tc><w:p><w:t>Zusätzliche Spalte mit Text</w:t></w:p></w:tc>
                </w:tr>
            </w:tbl>
        </w:body>
    </w:document>"""
    root_c2 = etree.fromstring(xml_cell2.encode("utf-8"))
    assert _remove_duplicate_bericht_rows(root_c2) == 0

    # 3. Duplikat-Zeile mit echtem Text zwischen separate und end
    xml_result = f"""<w:document xmlns:w="{w_ns}">
        <w:body>
            <w:tbl>
                <w:tr>
                    <w:tc><w:p><w:t>Bericht: FORMTEXT</w:t></w:p><w:p><w:bookmarkStart w:id="1" w:name="sections_1_bericht_zielerreichung"/><w:bookmarkEnd w:id="1"/></w:p></w:tc>
                </w:tr>
                <w:tr>
                    <w:tc>
                        <w:p><w:t>Bericht: FORMTEXT</w:t></w:p>
                        <w:p>
                            <w:fldChar w:fldCharType="begin"/>
                            <w:instrText> FORMTEXT </w:instrText>
                            <w:fldChar w:fldCharType="separate"/>
                            <w:r><w:t>Echter gespeicherter Inhalt</w:t></w:r>
                            <w:fldChar w:fldCharType="end"/>
                        </w:p>
                    </w:tc>
                </w:tr>
            </w:tbl>
        </w:body>
    </w:document>"""
    root_res = etree.fromstring(xml_result.encode("utf-8"))
    assert _remove_duplicate_bericht_rows(root_res) == 0


def test_bracket_single_digit_not_treated_as_section_id_hamburg():
    """Review-Punkt 1: '### Weitere Ideen (3)' darf Hamburg 3 (Arbeit) NIE befüllen."""
    messages = [
        {"role": "user", "content": "Welche weiteren Ideen hast du?"},
        {
            "role": "assistant",
            "content": (
                "### Weitere Ideen (3)\n"
                "**Bisherige Zielerreichung:**\nHier steht unspezifischer Text."
            ),
        },
    ]
    extracted = extract_section_data_from_chat(messages, template=resolve_template("hamburg"))
    # Darf keinesfalls in Bereich 3 landen
    assert "3" not in extracted
    assert len(extracted.unmapped_blocks) == 1
    assert "Weitere Ideen (3)" in extracted.unmapped_blocks[0].heading


def test_bracket_single_digit_not_treated_as_section_id_odw():
    """Review-Punkt 1: '### Vorschläge (2)' darf Op de Wisch 2 (Alltag) NIE befüllen."""
    messages = [
        {"role": "user", "content": "Welche Vorschläge hast du?"},
        {
            "role": "assistant",
            "content": (
                "### Vorschläge (2)\n"
                "**Bisherige Zielerreichung:**\nHier steht unspezifischer Text."
            ),
        },
    ]
    extracted = extract_section_data_from_chat(messages, template=resolve_template("op-de-wisch"))
    assert "2" not in extracted
    assert len(extracted.unmapped_blocks) == 1
    assert "Vorschläge (2)" in extracted.unmapped_blocks[0].heading


def test_diagnose_helper_negative_line_numbers_and_odw_fallback(test_bot):
    """Review-Punkt 2: Keine Falsch-Positiven bei '3 - 4 Mal pro Woche' und OdW-Fallback."""
    test_bot.set_csv(parse_csv_content("Datum;Notiz\n01.03.2025;Eintrag\n", filename="daten.csv"))

    # 1. '3 - 4 Mal pro Woche' und '2: ...' dürfen keine Warnung für 3 oder 2 erzeugen
    test_bot.messages = [
        {"role": "user", "content": "Wie lief die Woche?"},
        {
            "role": "assistant",
            "content": (
                "### 2.1 Gestaltung sozialer Beziehungen\n"
                "**Bisherige Zielerreichung:**\n"
                "3 - 4 Mal pro Woche gemeinsam gekocht.\n"
                "2: Kaffee getrunken mit Nachbarn."
            ),
        },
    ]
    res_num = export_chatbot_to_docx(test_bot, "hamburg")
    assert not any("Bereich 3 im Chat erwähnt" in w for w in res_num.warnings)
    assert not any("Bereich 2 im Chat erwähnt" in w for w in res_num.warnings)

    # 2. Hamburg-Vorlage, Chat antwortet mit Op-de-Wisch-Nummer 2 (landet per Fallback in 1.2)
    # Darf KEINE Warnung '❌ Bereich 1.2 im Chat erwähnt, aber nicht zugeordnet' erzeugen!
    test_bot.messages = [
        {"role": "user", "content": "Wie sieht es im Alltag aus?"},
        {
            "role": "assistant",
            "content": (
                "### Bereich 2 – Alltagsgestaltung / Tagesstruktur\n"
                "**Bisherige Zielerreichung:**\n"
                "Herr Peter meistert den Alltag gut."
            ),
        },
    ]
    res_fallback = export_chatbot_to_docx(test_bot, "hamburg")
    # Bereich 1.2 muss im gerenderten docx befüllt sein
    root_h = fields.load_document_tree(io.BytesIO(res_fallback[0]))
    val_12 = fields.get_formtext(root_h, "sections_1_bericht_zielerreichung")
    assert "meistert den Alltag gut" in val_12
    # Keine falsche Warnung für 1.2
    assert not any("Bereich 1.2 im Chat erwähnt" in w for w in res_fallback.warnings)
