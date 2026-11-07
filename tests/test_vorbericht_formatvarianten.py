"""Robustheitstests für Vorbericht-Import mit unterschiedlich formatierten DOCX-Dateien.

Prüft alle Varianten a bis i aus tests/fixtures/vorbericht_varianten/:
- a) Original Op-de-Wisch-Aufbau (Referenz)
- b) Original Hamburg-Aufbau (Referenz)
- c) Andere Überschriften (z. B. 'Lebensbereich 1' statt Bereichsname, Groß-/Kleinschreibung)
- c2) Nur Bereichsnamen ohne Nummerierung (xfail: noch nicht ohne Bereichsnummern unterstützt)
- d) Ziele als Fließtext / Aufzählung statt Tabelle
- e) Bereiche in anderer Reihenfolge
- f) Zusätzliche fremde Abschnitte (Deckblatt, Fußnoten, leere Tabellen)
- g) Verbundene Tabellenzellen / verschachtelte Tabellen
- h) Fehlende Bereiche (nur 3 von 9)
- i) Leeres bzw. fast leeres Dokument (HTTP 422)

Invariante:
- Strikt RAM-only: keine Persistenz auf Festplatte
- Upload ohne Absturz
- Keine stillen Datenverluste
- Eindeutige Warnungen bei Heuristik/Fallback
"""
from __future__ import annotations

import io
import logging
from pathlib import Path
import pytest
from starlette.testclient import TestClient

from chat_app.app import create_app
from chat_app.chatbot import ReflectionChatbot, ReflectionEngine
from chat_app.plan_loader import parse_plan_docx
from tests.fixtures.make_vorbericht_varianten import (
    FIXTURES_DIR,
    generate_all_fixtures,
    VARIANTS,
)

FIXTURES_PATH = Path(__file__).resolve().parent / "fixtures" / "vorbericht_varianten"


@pytest.fixture(scope="session", autouse=True)
def ensure_fixtures_exist():
    """Stellt sicher, dass alle Fixtures vor dem Testlauf vorhanden sind."""
    if not FIXTURES_PATH.exists() or len(list(FIXTURES_PATH.glob("*.docx"))) < len(VARIANTS):
        generate_all_fixtures(FIXTURES_PATH)


@pytest.fixture
def test_client():
    """Erstellt einen isolierten TestClient mit In-Memory ReflectionEngine."""
    app = create_app(chatbot=ReflectionChatbot(provider=ReflectionEngine()))
    return TestClient(app)


def _read_fixture(filename: str) -> bytes:
    p = FIXTURES_PATH / filename
    assert p.is_file(), f"Fixture-Datei nicht gefunden: {p}"
    return p.read_bytes()


# ==============================================================================
# 1. PARSER-TESTS: parse_plan_docx
# ==============================================================================

def test_var_a_opdewisch_referenz():
    """Variante a: Original Op-de-Wisch Vorlage via Fastpath ohne Fallback."""
    data = _read_fixture("a_opdewisch_referenz.docx")
    plan = parse_plan_docx(data, filename="a_opdewisch_referenz.docx")

    assert plan.template == "op-de-wisch"
    assert plan.is_fallback is False
    assert plan.warning == ""
    assert plan.goals_count == 6
    assert "Peter" in plan.client_name

    # Lebensbereich 1 prüfen
    sec1 = next((g for g in plan.goals if g.section_id == "1"), None)
    assert sec1 is not None
    assert sec1.applies is True
    assert "Herr Peter geht achtsam" in sec1.leitziel
    assert "belastende Situationen" in sec1.smarte_ziele


def test_var_b_hamburg_referenz():
    """Variante b: Original Hamburg Vorlage via Bookmarks Fastpath ohne Fallback."""
    data = _read_fixture("b_hamburg_referenz.docx")
    plan = parse_plan_docx(data, filename="b_hamburg_referenz.docx")

    assert plan.template == "hamburg"
    assert plan.is_fallback is False
    assert plan.warning == ""
    assert plan.goals_count == 7
    assert "Hans Peter" in plan.client_name

    # Bereich 1.2 prüfen
    sec12 = next((g for g in plan.goals if g.section_id == "1.2"), None)
    assert sec12 is not None
    assert sec12.applies is True
    assert "führt seinen Haushalt zunehmend selbständig" in sec12.leitziel


def test_var_c_andere_ueberschriften():
    """Variante c: Tabellen mit 'Lebensbereich 1', 'LEBENSBEREICH 2' etc. werden über tolerante Heuristik erkannt."""
    data = _read_fixture("c_andere_ueberschriften.docx")
    plan = parse_plan_docx(data, filename="c_andere_ueberschriften.docx")

    assert plan.is_fallback is True
    assert "⚠️" in plan.warning
    assert plan.goals_count == 8  # 6 aktive Ziele + 2 Status-Berichte ohne neue Ziele

    section_ids = [g.section_id for g in plan.goals]
    assert "1" in section_ids
    assert "2" in section_ids
    assert "3" in section_ids
    assert "5" in section_ids
    assert "6" in section_ids
    assert "7" in section_ids

    # Keine Fragment-Überschriften ("Leitziel:" etc.)
    for g in plan.goals:
        assert not g.section_title.startswith("Leitziel")


@pytest.mark.xfail(
    reason="Überschriften ohne jedwede Bereichsnummerierung (1..9 / 1.1..3) werden heuristisch noch nicht automatisch den Lebensbereichen zugeordnet",
    strict=True,
)
def test_var_c2_nur_bereichsnamen_ohne_nummern():
    """Variante c2: Reine Bereichsnamen ohne Bereichsnummern (erfordert zukünftiges semantisches Mapping)."""
    data = _read_fixture("c2_nur_bereichsnamen_ohne_nummern.docx")
    plan = parse_plan_docx(data, filename="c2_nur_bereichsnamen_ohne_nummern.docx")

    # Sollte im Idealfall die 9 Standard-IDs 1..9 liefern, liefert aktuell 26 Fragmente
    assert plan.goals_count in (6, 8, 9)
    assert set(g.section_id for g in plan.goals).issubset({"1", "2", "3", "4", "5", "6", "7", "8", "9"})


def test_var_d_fliesstext_aufzaehlung():
    """Variante d: Ziele als Fließtext und Listen ohne Tabellen werden über Paragraph-Scan erkannt."""
    data = _read_fixture("d_fliesstext_aufzaehlung.docx")
    plan = parse_plan_docx(data, filename="d_fliesstext_aufzaehlung.docx")

    assert plan.is_fallback is True
    assert "⚠️" in plan.warning
    assert plan.goals_count >= 6

    sec1 = next((g for g in plan.goals if g.section_id == "1"), None)
    assert sec1 is not None
    assert "Herr Peter geht achtsam" in sec1.leitziel
    assert "belastende Situationen" in sec1.smarte_ziele


def test_var_e_bereiche_andere_reihenfolge():
    """Variante e: Geänderte Reihenfolge der Bereiche führt zu korrekter Zuordnung zu den Ziel-IDs."""
    data = _read_fixture("e_bereiche_andere_reihenfolge.docx")
    plan = parse_plan_docx(data, filename="e_bereiche_andere_reihenfolge.docx")

    assert plan.is_fallback is True
    assert plan.goals_count == 8

    # Bereich 7 (Arbeit) muss trotz Voranstellung ID 7 haben
    sec7 = next((g for g in plan.goals if g.section_id == "7"), None)
    assert sec7 is not None
    assert "Arbeit" in sec7.section_title
    assert "Verpackungsgruppe" in sec7.smarte_ziele


def test_var_f_fremde_abschnitte():
    """Variante f: Zusätzliche Deckblätter, fremde Tabellen und Fußnoten stören das Parsing nicht."""
    data = _read_fixture("f_fremde_abschnitte_deckblatt_fussnoten.docx")
    plan = parse_plan_docx(data, filename="f_fremde_abschnitte_deckblatt_fussnoten.docx")

    assert plan.is_fallback is True
    assert plan.goals_count == 8

    # Klient muss trotz Deckblatt erkannt werden
    assert "Peter" in plan.client_name


def test_var_g_verbundene_verschachtelte_tabellen():
    """Variante g: Verbundene Tabellenzellen und verschachtelte SMART-Ziele-Tabellen werden erfasst."""
    data = _read_fixture("g_verbundene_verschachtelte_tabellen.docx")
    plan = parse_plan_docx(data, filename="g_verbundene_verschachtelte_tabellen.docx")

    assert plan.is_fallback is True
    assert plan.goals_count == 8

    sec1 = next((g for g in plan.goals if g.section_id == "1"), None)
    assert sec1 is not None
    # Text der verschachtelten Tabelle muss im smarten Ziel vorhanden sein
    assert "belastende Situationen" in sec1.smarte_ziele


def test_var_h_fehlende_bereiche_3_von_9():
    """Variante h: Unvollständiger Bericht (nur Bereiche 1..3) liefert genau 3 Ziele ohne Absturz."""
    data = _read_fixture("h_fehlende_bereiche_3_von_9.docx")
    plan = parse_plan_docx(data, filename="h_fehlende_bereiche_3_von_9.docx")

    assert plan.is_fallback is True
    assert "⚠️" in plan.warning
    assert plan.goals_count == 3
    assert set(g.section_id for g in plan.goals) == {"1", "2", "3"}


def test_var_i_leeres_fast_leeres_dokument():
    """Variante i: Fast leeres Dokument führt zu goals_count == 0."""
    data = _read_fixture("i_leeres_fast_leeres_dokument.docx")
    plan = parse_plan_docx(data, filename="i_leeres_fast_leeres_dokument.docx")

    assert plan.goals_count == 0


# ==============================================================================
# 2. ENDPUNKT-TESTS: POST /api/upload-plan
# ==============================================================================

def test_api_upload_var_a_referenz(test_client):
    """POST /api/upload-plan mit Op-de-Wisch-Referenz liefert 200 ohne Warnung."""
    data = _read_fixture("a_opdewisch_referenz.docx")
    res = test_client.post(
        "/api/upload-plan",
        files={"file": ("a_opdewisch_referenz.docx", io.BytesIO(data), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert res.status_code == 200
    payload = res.json()
    assert payload["success"] is True
    assert payload["goals_count"] == 6
    assert payload["is_fallback"] is False
    assert "warning" not in payload or payload["warning"] == ""


def test_api_upload_var_b_referenz(test_client):
    """POST /api/upload-plan mit Hamburg-Referenz liefert 200 ohne Warnung."""
    data = _read_fixture("b_hamburg_referenz.docx")
    res = test_client.post(
        "/api/upload-plan",
        files={"file": ("b_hamburg_referenz.docx", io.BytesIO(data), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert res.status_code == 200
    payload = res.json()
    assert payload["success"] is True
    assert payload["goals_count"] == 7
    assert payload["is_fallback"] is False
    assert "warning" not in payload or payload["warning"] == ""


def test_api_upload_var_c_andere_ueberschriften(test_client):
    """POST /api/upload-plan mit alternativen Überschriften liefert 200 mit sichtbarer Warnung."""
    data = _read_fixture("c_andere_ueberschriften.docx")
    res = test_client.post(
        "/api/upload-plan",
        files={"file": ("c_andere_ueberschriften.docx", io.BytesIO(data), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert res.status_code == 200
    payload = res.json()
    assert payload["success"] is True
    assert payload["goals_count"] == 8
    assert payload["is_fallback"] is True
    assert "⚠️" in payload.get("warning", "")


def test_api_upload_var_d_fliesstext(test_client):
    """POST /api/upload-plan mit Fließtext liefert 200 mit sichtbarer Warnung."""
    data = _read_fixture("d_fliesstext_aufzaehlung.docx")
    res = test_client.post(
        "/api/upload-plan",
        files={"file": ("d_fliesstext_aufzaehlung.docx", io.BytesIO(data), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert res.status_code == 200
    payload = res.json()
    assert payload["success"] is True
    assert payload["goals_count"] >= 6
    assert payload["is_fallback"] is True
    assert "⚠️" in payload.get("warning", "")


def test_api_upload_var_e_andere_reihenfolge(test_client):
    """POST /api/upload-plan mit anderer Reihenfolge liefert 200."""
    data = _read_fixture("e_bereiche_andere_reihenfolge.docx")
    res = test_client.post(
        "/api/upload-plan",
        files={"file": ("e_bereiche_andere_reihenfolge.docx", io.BytesIO(data), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert res.status_code == 200
    assert res.json()["goals_count"] == 8


def test_api_upload_var_f_fremde_abschnitte(test_client):
    """POST /api/upload-plan mit Deckblatt und Fußnoten liefert 200."""
    data = _read_fixture("f_fremde_abschnitte_deckblatt_fussnoten.docx")
    res = test_client.post(
        "/api/upload-plan",
        files={"file": ("f_fremde_abschnitte_deckblatt_fussnoten.docx", io.BytesIO(data), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert res.status_code == 200
    assert res.json()["goals_count"] == 8


def test_api_upload_var_g_verschachtelt(test_client):
    """POST /api/upload-plan mit verschachtelten Tabellen liefert 200."""
    data = _read_fixture("g_verbundene_verschachtelte_tabellen.docx")
    res = test_client.post(
        "/api/upload-plan",
        files={"file": ("g_verbundene_verschachtelte_tabellen.docx", io.BytesIO(data), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert res.status_code == 200
    assert res.json()["goals_count"] == 8


def test_api_upload_var_h_fehlende_bereiche(test_client):
    """POST /api/upload-plan mit 3 von 9 Bereichen liefert 200 mit Warnung und exakt 3 Zielen."""
    data = _read_fixture("h_fehlende_bereiche_3_von_9.docx")
    res = test_client.post(
        "/api/upload-plan",
        files={"file": ("h_fehlende_bereiche_3_von_9.docx", io.BytesIO(data), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert res.status_code == 200
    payload = res.json()
    assert payload["goals_count"] == 3
    assert payload["is_fallback"] is True
    assert "⚠️" in payload.get("warning", "")


def test_api_upload_var_i_leeres_dokument_returns_422(test_client):
    """POST /api/upload-plan mit leerem Dokument antwortet mit 422 und Fehlermeldung."""
    data = _read_fixture("i_leeres_fast_leeres_dokument.docx")
    res = test_client.post(
        "/api/upload-plan",
        files={"file": ("i_leeres_fast_leeres_dokument.docx", io.BytesIO(data), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert res.status_code == 422
    assert "❌ Kein gültiger Vorbericht erkannt" in res.json()["detail"]


def test_api_upload_empty_0_byte_file_returns_400(test_client):
    """POST /api/upload-plan mit 0-Byte-Datei liefert 400 ohne Absturz."""
    res = test_client.post(
        "/api/upload-plan",
        files={"file": ("empty.docx", io.BytesIO(b""), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert res.status_code == 400
    assert "❌ Datei abgelehnt" in res.json()["detail"]


# ==============================================================================
# 3. DATENSCHUTZ & ZERO-PERSISTENZ INVARIANTE
# ==============================================================================

def test_privacy_no_client_names_in_logs(test_client, caplog):
    """Prüft, dass bei keinem Varianten-Upload Klientennamen in den Logs landen."""
    caplog.set_level(logging.DEBUG)
    for fn in ["a_opdewisch_referenz.docx", "c_andere_ueberschriften.docx", "d_fliesstext_aufzaehlung.docx"]:
        data = _read_fixture(fn)
        res = test_client.post(
            "/api/upload-plan",
            files={"file": (fn, io.BytesIO(data), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
            data={"new_case": "true"},
        )
        assert res.status_code == 200

    log_text = caplog.text
    # Niemals Personen-Identifikatoren loggen
    assert "Hans Peter" not in log_text
    assert "Synthetischer Weg" not in log_text
