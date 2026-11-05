"""Tests für Issue #32: Unzugeordnete Verlaufseinträge im „Bereich prüfen“-Modal manuell einem Lebensbereich zuordnen.

Prüft alle 4 Akzeptanzkriterien:
1. Die Review-API (get_review_data) und das Modal listen unzugeordnete Einträge mit stabiler,
   inhaltsfreier ID (CSV-Index) und einem Dropdown mit genau den Bereichen der Session-Vorlage
   (Op de Wisch 1–9, Hamburg mit Unterbereichen). Die API liefert die Optionen (ID + Titel) mit.
2. POST /api/entry/assign {"entry_id","bereich_id"} ordnet zu. Der Eintrag erscheint danach
   im Bereich (Review und Kontext von ContextBuilder.build) und nicht mehr als unzugeordnet;
   die Zähler stimmen.
   422 ❌ bei:
   - unbekannter ID
   - Bereich nicht in der Vorlage (z. B. „1.4“ bei Op de Wisch, „7“ bei Hamburg)
   - keine Vorlage gewählt („Bitte zuerst eine Vorlage wählen“)
   - keine CSV geladen
   - automatisch zugeordnetem Eintrag (manuell zugeordnete dürfen umgehängt werden).
3. Neuer Fall (hard_reset, case.clear), neuer CSV-Upload und TTL (check_ttl) löschen die Zuordnung.
4. Tests für beide Vorlagen in tests/test_eintrag_zuordnen.py.
"""
from __future__ import annotations

import io
from typing import Dict, Generator, List

import pytest
from starlette.testclient import TestClient

from chat_app.app import create_app
from chat_app.chatbot import LLMProvider, ReflectionChatbot
from chat_app.context_builder import ContextBuilder
from chat_app.csv_loader import parse_csv_content
from sob_generator.llm.sections import SECTION_DEFINITIONS
from sob_generator.llm.sections_op_de_wisch import OP_DE_WISCH_SECTION_DEFINITIONS


class DummyMockLLM(LLMProvider):
    """Einfacher Test-Mock für den Chatbot."""

    model_name: str = "DummyMockLLM"
    is_real_llm: bool = True
    details: str = "Synthetischer Test-Mock"
    n_ctx: int = 16384
    max_tokens: int = 2048

    def count_tokens(self, text: str) -> int:
        return len(text.split()) if text else 0

    def generate(self, messages: List[Dict[str, str]]) -> str:
        return "Antwort"

    def generate_stream(self, messages: List[Dict[str, str]]) -> Generator[str, None, None]:
        yield "Antwort"

    def reset(self) -> None:
        pass


# Synthetische CSV mit:
# - Zeile 0: Schlüsselwort "wohnung" -> passt automatisch zu Hamburg 1.1 (Op de Wisch 1)
# - Zeile 1: Allgemeiner Text ohne Schlüsselwörter -> unzugeordnet
# - Zeile 2: Allgemeiner Text ohne Schlüsselwörter -> unzugeordnet
SYNTHETIC_CSV = (
    "Datum;Kategorie;Verlauf;Mitarbeiter\n"
    "10.02.2025;Wohnen;Neue Wohnung besichtigt und Mietvertrag geprüft.;FK_SYNTH\n"
    "15.02.2025;Allgemein;Kurzes Gespräch an der Tür, Klient wirkte entspannt.;FK_SYNTH\n"
    "20.02.2025;Sonstiges;Wetter war heute schön, Spaziergang im Hof.;FK_SYNTH\n"
)


# ===========================================================================
# Kriterium 1: Review-API liefert unzugeordnete Einträge mit stabiler ID & Bereichsoptionen
# ===========================================================================


def test_review_api_returns_unassigned_entries_with_stable_ids_and_options_hamburg():
    """Kriterium 1 (Hamburg): get_review_data und GET /api/area/{id}/review liefern unzugeordnete Einträge

    mit stabiler ID (CSV-Index) und genau den Hamburger Bereichen (1.1 bis 3) als Optionen.
    """
    bot = ReflectionChatbot(provider=DummyMockLLM())
    client = TestClient(create_app(chatbot=bot))

    # Vorlage Hamburg wählen und CSV hochladen
    client.post("/api/template", json={"template": "hamburg"})
    client.post(
        "/api/upload",
        files={"file": ("test.csv", SYNTHETIC_CSV.encode("utf-8"), "text/csv")},
    )

    # Review für Bereich 1.1 aufrufen
    res = client.get("/api/area/1.1/review")
    assert res.status_code == 200
    data = res.json()

    # Bereichsoptionen prüfen
    assert "area_options" in data or "available_areas" in data
    options = data.get("area_options") or data.get("available_areas")
    expected_ids = [s.id for s in SECTION_DEFINITIONS]
    assert [opt["id"] for opt in options] == expected_ids
    # Hamburg hat Unterbereiche wie 1.1, 1.4 und keine '7'
    assert "1.4" in [opt["id"] for opt in options]
    assert "7" not in [opt["id"] for opt in options]

    # Unzugeordnete Einträge prüfen (Zeilen 1 und 2)
    unassigned = data["unassigned_entries"]
    assert len(unassigned) == 2
    # Stabile inhaltsfreie ID prüfen (z. B. "1" und "2")
    entry_ids = [str(e.get("entry_id") or e.get("id")) for e in unassigned]
    assert entry_ids == ["1", "2"]

    # Zugeordnete Einträge für 1.1 prüfen (Zeile 0 hat "wohnung")
    assigned = data["assigned_entries"]
    assert len(assigned) == 1
    assert str(assigned[0].get("entry_id") or assigned[0].get("id")) == "0"


def test_review_api_returns_unassigned_entries_with_stable_ids_and_options_op_de_wisch():
    """Kriterium 1 (Op de Wisch): Review-API liefert unzugeordnete Einträge und genau die Bereiche 1–9."""
    bot = ReflectionChatbot(provider=DummyMockLLM())
    client = TestClient(create_app(chatbot=bot))

    # Vorlage Op de Wisch wählen und CSV hochladen
    client.post("/api/template", json={"template": "op-de-wisch"})
    client.post(
        "/api/upload",
        files={"file": ("test.csv", SYNTHETIC_CSV.encode("utf-8"), "text/csv")},
    )

    # Review für Bereich 1 aufrufen
    res = client.get("/api/area/1/review")
    assert res.status_code == 200
    data = res.json()

    # Optionen müssen genau Op de Wisch 1–9 sein
    options = data.get("area_options") or data.get("available_areas")
    expected_ids = [s.id for s in OP_DE_WISCH_SECTION_DEFINITIONS]
    assert [opt["id"] for opt in options] == expected_ids
    assert "7" in [opt["id"] for opt in options]
    assert "1.4" not in [opt["id"] for opt in options]

    # Unzugeordnete Einträge haben stabile IDs
    unassigned = data["unassigned_entries"]
    assert len(unassigned) == 2
    entry_ids = [str(e.get("entry_id") or e.get("id")) for e in unassigned]
    assert entry_ids == ["1", "2"]


def test_ui_modal_contains_assign_dropdown_and_error_banner():
    """Kriterium 1 (UI): Das Modal im HTML enthält das Fehlerbanner und die Struktur für Bereichszuordnung."""
    bot = ReflectionChatbot(provider=DummyMockLLM())
    client = TestClient(create_app(chatbot=bot))

    res = client.get("/")
    assert res.status_code == 200
    html = res.text

    assert 'id="areaReviewModal"' in html
    assert 'id="reviewAssignError"' in html
    assert 'assignEntryToArea' in html
    assert 'entry-assign-select' in html


# ===========================================================================
# Kriterium 2: POST /api/entry/assign und Fehlerbedingungen
# ===========================================================================


def test_assign_entry_success_hamburg():
    """Kriterium 2: Zuordnung eines unzugeordneten Eintrags zu einem Hamburger Bereich (1.2).

    Eintrag erscheint danach im Bereich (Review und ContextBuilder.build), nicht mehr als
    unzugeordnet; Zähler stimmen.
    """
    bot = ReflectionChatbot(provider=DummyMockLLM())
    client = TestClient(create_app(chatbot=bot))

    client.post("/api/template", json={"template": "hamburg"})
    client.post(
        "/api/upload",
        files={"file": ("test.csv", SYNTHETIC_CSV.encode("utf-8"), "text/csv")},
    )

    # Vor Zuordnung: Bereich 1.2 hat 0 zugeordnete und 2 unzugeordnete
    rev_before = client.get("/api/area/1.2/review").json()
    assert rev_before["assigned_count"] == 0
    assert rev_before["unassigned_count"] == 2

    # Eintrag 1 manuell Bereich 1.2 zuordnen
    res = client.post("/api/entry/assign", json={"entry_id": "1", "bereich_id": "1.2"})
    assert res.status_code == 200
    resp_data = res.json()
    assert resp_data["success"] is True
    assert resp_data["assigned_count"] == 1
    assert resp_data["unassigned_count"] == 1

    # Nach Zuordnung: Bereich 1.2 Review prüfen
    rev_after = client.get("/api/area/1.2/review").json()
    assert rev_after["assigned_count"] == 1
    assert rev_after["unassigned_count"] == 1
    assigned_ids = [str(e.get("entry_id") or e.get("id")) for e in rev_after["assigned_entries"]]
    assert "1" in assigned_ids
    unassigned_ids = [str(e.get("entry_id") or e.get("id")) for e in rev_after["unassigned_entries"]]
    assert "1" not in unassigned_ids
    assert unassigned_ids == ["2"]

    # Prüfen, dass der Eintrag auch im Kontext von ContextBuilder.build(case, "1.2") erscheint
    built_12 = ContextBuilder.build(bot.case, "1.2")
    assert built_12.assigned_count == 1
    assert built_12.unassigned_count == 1
    assert "Kurzes Gespräch an der Tür" in built_12.verlauf_eintraege

    # In einem anderen Bereich (z. B. 1.4) darf Eintrag 1 weder zugeordnet noch unzugeordnet sein
    rev_14 = client.get("/api/area/1.4/review").json()
    assert rev_14["assigned_count"] == 0
    assert rev_14["unassigned_count"] == 1
    unassigned_14_ids = [str(e.get("entry_id") or e.get("id")) for e in rev_14["unassigned_entries"]]
    assert unassigned_14_ids == ["2"]


def test_assign_entry_success_op_de_wisch():
    """Kriterium 2: Zuordnung eines unzugeordneten Eintrags zu einem Op-de-Wisch-Bereich (7)."""
    bot = ReflectionChatbot(provider=DummyMockLLM())
    client = TestClient(create_app(chatbot=bot))

    client.post("/api/template", json={"template": "op-de-wisch"})
    client.post(
        "/api/upload",
        files={"file": ("test.csv", SYNTHETIC_CSV.encode("utf-8"), "text/csv")},
    )

    # Eintrag 2 manuell Bereich 7 zuordnen
    res = client.post("/api/entry/assign", json={"entry_id": "2", "bereich_id": "7"})
    assert res.status_code == 200

    rev_7 = client.get("/api/area/7/review").json()
    assert rev_7["assigned_count"] == 1
    assert rev_7["unassigned_count"] == 1
    assigned_ids = [str(e.get("entry_id") or e.get("id")) for e in rev_7["assigned_entries"]]
    assert "2" in assigned_ids

    built_7 = ContextBuilder.build(bot.case, "7")
    assert built_7.assigned_count == 1
    assert built_7.unassigned_count == 1
    assert "Spaziergang im Hof" in built_7.verlauf_eintraege


def test_reassign_manually_assigned_entry_allowed():
    """Kriterium 2: Manuell zugeordnete Einträge dürfen umgehängt werden."""
    bot = ReflectionChatbot(provider=DummyMockLLM())
    client = TestClient(create_app(chatbot=bot))

    client.post("/api/template", json={"template": "hamburg"})
    client.post(
        "/api/upload",
        files={"file": ("test.csv", SYNTHETIC_CSV.encode("utf-8"), "text/csv")},
    )

    # Zuerst Eintrag 1 zu 1.2 zuordnen
    res1 = client.post("/api/entry/assign", json={"entry_id": "1", "bereich_id": "1.2"})
    assert res1.status_code == 200

    # Dann Eintrag 1 zu 2.1 umhängen
    res2 = client.post("/api/entry/assign", json={"entry_id": "1", "bereich_id": "2.1"})
    assert res2.status_code == 200

    # In 1.2 darf er nicht mehr zugeordnet sein
    rev_12 = client.get("/api/area/1.2/review").json()
    assert rev_12["assigned_count"] == 0
    assigned_12_ids = [str(e.get("entry_id") or e.get("id")) for e in rev_12["assigned_entries"]]
    assert "1" not in assigned_12_ids

    # In 2.1 muss er zugeordnet sein
    rev_21 = client.get("/api/area/2.1/review").json()
    assert rev_21["assigned_count"] == 1
    assigned_21_ids = [str(e.get("entry_id") or e.get("id")) for e in rev_21["assigned_entries"]]
    assert "1" in assigned_21_ids


def test_assign_entry_422_when_no_template_selected():
    """Kriterium 2: 422 ❌ wenn keine Vorlage gewählt wurde ('Bitte zuerst eine Vorlage wählen')."""
    bot = ReflectionChatbot(provider=DummyMockLLM())
    client = TestClient(create_app(chatbot=bot))

    # CSV hochladen ohne Vorlagenwahl (template_selected ist False)
    client.post(
        "/api/upload",
        files={"file": ("test.csv", SYNTHETIC_CSV.encode("utf-8"), "text/csv")},
    )

    res = client.post("/api/entry/assign", json={"entry_id": "1", "bereich_id": "1.2"})
    assert res.status_code == 422
    data = res.json()
    assert "❌" in str(data)
    assert "Bitte zuerst eine Vorlage wählen" in str(data)


def test_assign_entry_422_when_no_csv_loaded():
    """Kriterium 2: 422 ❌ wenn keine CSV geladen ist."""
    bot = ReflectionChatbot(provider=DummyMockLLM())
    client = TestClient(create_app(chatbot=bot))

    client.post("/api/template", json={"template": "hamburg"})

    res = client.post("/api/entry/assign", json={"entry_id": "1", "bereich_id": "1.2"})
    assert res.status_code == 422
    assert "❌" in str(res.json())


def test_assign_entry_422_when_unknown_entry_id():
    """Kriterium 2: 422 ❌ bei unbekannter Eintrags-ID (z. B. Out-of-Bounds oder Text)."""
    bot = ReflectionChatbot(provider=DummyMockLLM())
    client = TestClient(create_app(chatbot=bot))

    client.post("/api/template", json={"template": "hamburg"})
    client.post(
        "/api/upload",
        files={"file": ("test.csv", SYNTHETIC_CSV.encode("utf-8"), "text/csv")},
    )

    # Out of Bounds ID
    res1 = client.post("/api/entry/assign", json={"entry_id": "999", "bereich_id": "1.2"})
    assert res1.status_code == 422
    assert "❌" in str(res1.json())

    # Negative ID
    res2 = client.post("/api/entry/assign", json={"entry_id": "-1", "bereich_id": "1.2"})
    assert res2.status_code == 422
    assert "❌" in str(res2.json())

    # Nicht-numerische ID
    res3 = client.post("/api/entry/assign", json={"entry_id": "invalid_id", "bereich_id": "1.2"})
    assert res3.status_code == 422
    assert "❌" in str(res3.json())


def test_assign_entry_422_when_area_not_in_template():
    """Kriterium 2: 422 ❌ wenn Bereich nicht in der gewählten Vorlage existiert

    (z. B. '1.4' bei Op de Wisch, '7' bei Hamburg).
    """
    bot = ReflectionChatbot(provider=DummyMockLLM())
    client = TestClient(create_app(chatbot=bot))

    # 1. Hamburg gewählt -> '7' ist ungültig
    client.post("/api/template", json={"template": "hamburg"})
    client.post(
        "/api/upload",
        files={"file": ("test.csv", SYNTHETIC_CSV.encode("utf-8"), "text/csv")},
    )

    res_hh = client.post("/api/entry/assign", json={"entry_id": "1", "bereich_id": "7"})
    assert res_hh.status_code == 422
    assert "❌" in str(res_hh.json())

    # 2. Op de Wisch gewählt -> '1.4' ist ungültig
    client.post("/api/template", json={"template": "op-de-wisch"})
    res_odw = client.post("/api/entry/assign", json={"entry_id": "1", "bereich_id": "1.4"})
    assert res_odw.status_code == 422
    assert "❌" in str(res_odw.json())


def test_assign_entry_422_when_entry_already_auto_assigned():
    """Kriterium 2: 422 ❌ bei Versuch, einen automatisch per Schlüsselwort zugeordneten Eintrag zuzuordnen."""
    bot = ReflectionChatbot(provider=DummyMockLLM())
    client = TestClient(create_app(chatbot=bot))

    client.post("/api/template", json={"template": "hamburg"})
    client.post(
        "/api/upload",
        files={"file": ("test.csv", SYNTHETIC_CSV.encode("utf-8"), "text/csv")},
    )

    # Eintrag 0 hat "wohnung" und ist automatisch 1.1 zugeordnet
    res = client.post("/api/entry/assign", json={"entry_id": "0", "bereich_id": "1.2"})
    assert res.status_code == 422
    assert "❌" in str(res.json())


# ===========================================================================
# Kriterium 3: Löschen der Zuordnungen bei Reset, neuem Upload und TTL
# ===========================================================================


def test_assignment_cleared_on_case_clear_and_hard_reset():
    """Kriterium 3: case.clear() und hard_reset() löschen alle manuellen Zuordnungen."""
    bot = ReflectionChatbot(provider=DummyMockLLM())
    client = TestClient(create_app(chatbot=bot))

    client.post("/api/template", json={"template": "hamburg"})
    client.post(
        "/api/upload",
        files={"file": ("test.csv", SYNTHETIC_CSV.encode("utf-8"), "text/csv")},
    )

    client.post("/api/entry/assign", json={"entry_id": "1", "bereich_id": "1.2"})
    assert len(bot.case.manual_assignments) == 1

    # 1. case.clear()
    bot.case.clear()
    assert len(bot.case.manual_assignments) == 0

    # Erneut vorbereiten
    client.post("/api/template", json={"template": "hamburg"})
    client.post(
        "/api/upload",
        files={"file": ("test.csv", SYNTHETIC_CSV.encode("utf-8"), "text/csv")},
    )
    client.post("/api/entry/assign", json={"entry_id": "1", "bereich_id": "1.2"})
    assert len(bot.case.manual_assignments) == 1

    # 2. bot.hard_reset()
    bot.hard_reset(reason="reset")
    assert len(bot.case.manual_assignments) == 0


def test_assignment_cleared_on_new_csv_upload():
    """Kriterium 3: Neuer CSV-Upload löscht die bisherigen Zuordnungen."""
    bot = ReflectionChatbot(provider=DummyMockLLM())
    client = TestClient(create_app(chatbot=bot))

    client.post("/api/template", json={"template": "hamburg"})
    client.post(
        "/api/upload",
        files={"file": ("test1.csv", SYNTHETIC_CSV.encode("utf-8"), "text/csv")},
    )

    client.post("/api/entry/assign", json={"entry_id": "1", "bereich_id": "1.2"})
    assert len(bot.case.manual_assignments) == 1

    # Neuer CSV-Upload
    other_csv = (
        "Datum;Kategorie;Verlauf;Mitarbeiter\n"
        "01.03.2025;Allgemein;Neuer Fallverlauf;FK_SYNTH\n"
    )
    # Upload mit new_case=True
    client.post(
        "/api/upload",
        data={"new_case": "true"},
        files={"file": ("test2.csv", other_csv.encode("utf-8"), "text/csv")},
    )
    assert len(bot.case.manual_assignments) == 0


def test_assignment_cleared_on_ttl_expiry(monkeypatch):
    """Kriterium 3: Nach Ablauf der Inaktivitäts-TTL (check_ttl) werden die Zuordnungen gelöscht."""
    bot = ReflectionChatbot(provider=DummyMockLLM())
    client = TestClient(create_app(chatbot=bot))

    client.post("/api/template", json={"template": "hamburg"})
    client.post(
        "/api/upload",
        files={"file": ("test.csv", SYNTHETIC_CSV.encode("utf-8"), "text/csv")},
    )

    client.post("/api/entry/assign", json={"entry_id": "1", "bereich_id": "1.2"})
    assert len(bot.case.manual_assignments) == 1

    # TTL auf 1 Minute setzen und Zeit simulieren
    monkeypatch.setenv("SOB_CASE_TTL_MIN", "1")
    # Zeit um 70 Sekunden vorspulen
    future_time = bot.case.last_activity + 70.0
    bot.clock = lambda: future_time

    # check_ttl auslösen
    expired = bot.check_ttl()
    assert expired is True
    assert len(bot.case.manual_assignments) == 0


# ===========================================================================
# Datenschutz- & Logging-Regel: Nur IDs und Zähler in Logs, keine Inhalte
# ===========================================================================


def test_no_content_or_names_in_assign_logs(caplog):
    """Regel: Logs nur mit IDs und Zählern, nie Eintragstexte oder Namen."""
    bot = ReflectionChatbot(provider=DummyMockLLM())
    client = TestClient(create_app(chatbot=bot))

    client.post("/api/template", json={"template": "hamburg"})
    client.post(
        "/api/upload",
        files={"file": ("test.csv", SYNTHETIC_CSV.encode("utf-8"), "text/csv")},
    )

    with caplog.at_level("INFO"):
        res = client.post("/api/entry/assign", json={"entry_id": "1", "bereich_id": "1.2"})
        assert res.status_code == 200

    log_text = caplog.text
    # Verbotene Inhalte: Eintragstexte dürfen nicht im Log auftauchen
    assert "Kurzes Gespräch an der Tür" not in log_text
    assert "FK_SYNTH" not in log_text
