r"""Tests für Vorbericht-Parser (Issue #131 / [Kontext 1/8]).

Prüft:
- Fastpath (XML-Bookmarks) liest alle 9 Lebensbereiche inklusive Maßnahmen und Realisierung durch andere
- Fallback (Textmarken umbenannt) liest alle 9 Lebensbereiche anhand von Sub-IDs (\d\.\d) statt zusammenzufallen
- Duplikate führen zu IncompletePlanError
- Unbekannte IDs ("9.9") werden ignoriert
- /api/upload-plan antwortet bei unvollständigem Plan mit 422 und erhält bisherigen Zustand
- Op de Wisch Vorlage parst weiterhin sauber
- Privacy: Parsing loggt keinerlei ZZ_SYNTH Marker oder Klientendaten
"""
from __future__ import annotations

import copy
import io
import logging
from pathlib import Path
import pytest
import docx
from fastapi.testclient import TestClient

from chat_app.app import create_app
from chat_app.chatbot import ReflectionChatbot, ReflectionEngine
from chat_app.plan_loader import parse_plan_docx
from sob_generator.previous_sob import (
    IncompletePlanError,
    read_previous_plan,
)
from tests.fixtures.make_synthetic_vorbericht import (
    build_hamburg_vorbericht,
    VORLAGE_PATH,
)

ODW_VORLAGE_PATH = (
    Path(__file__).resolve().parents[1]
    / "templates"
    / "hamburg"
    / "OpDerWisch"
    / "FO_SVB_Berichtsvorlage_2026.docx"
)

EXPECTED_HAMBURG_IDS = ["1.1", "1.2", "1.3", "1.4", "1.5", "2.1", "2.2", "2.3", "3"]
EXPECTED_ODW_IDS = ["1", "2", "3", "4", "5", "6", "7", "8", "9"]


@pytest.fixture
def test_client():
    app = create_app(chatbot=ReflectionChatbot(provider=ReflectionEngine()))
    return TestClient(app)


def _make_duplicate_area_docx() -> bytes:
    """Erzeugt im RAM einen Fallback-Vorbericht mit einer duplizierten Bereichstabelle."""
    base_bytes = build_hamburg_vorbericht(fallback=True)
    doc = docx.Document(io.BytesIO(base_bytes))
    # Tabelle 4 entspricht Bereich 1.4
    tbl4 = doc.tables[4]._tbl
    tbl_clone = copy.deepcopy(tbl4)
    doc._body._body.append(tbl_clone)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def _make_unknown_id_docx() -> bytes:
    """Erzeugt im RAM einen Fallback-Vorbericht mit einer unbekannten Bereichs-ID '9.9'."""
    base_bytes = build_hamburg_vorbericht(fallback=True)
    doc = docx.Document(io.BytesIO(base_bytes))
    table = doc.add_table(rows=2, cols=1)
    table.cell(0, 0).text = "9. Bereich Fantasie"
    table.cell(1, 0).text = "9.9 Fantasiewelt"
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def test_fastpath_all_nine_areas_and_new_fields():
    """Fastpath liest 9 Bereiche mit IDs [1.1..3] inkl. Maßnahmen & Realisierung durch andere."""
    docx_bytes = build_hamburg_vorbericht(fallback=False)
    plan_data = parse_plan_docx(docx_bytes, filename="vorbericht_synth.docx")

    # 9 Bereiche erfasst
    parsed_ids = [g.section_id for g in plan_data.goals]
    assert parsed_ids == EXPECTED_HAMBURG_IDS
    assert plan_data.goals_count == 9

    # Marker in 1.4 prüfen
    goal_1_4 = next(g for g in plan_data.goals if g.section_id == "1.4")
    assert goal_1_4.leitziel == "ZZ_SYNTH_1_4_LEITZIEL"
    assert goal_1_4.vorschlag_massnahmen == "ZZ_SYNTH_1_4_MASSNAHMEN"
    assert goal_1_4.realisierung_andere == "ZZ_SYNTH_1_4_REALISIERUNG"

    # Context-Text prüfen
    ctx = plan_data.to_context_text()
    assert "Vorschlag Maßnahmen: ZZ_SYNTH_1_4_MASSNAHMEN" in ctx
    assert "Realisierung durch andere: ZZ_SYNTH_1_4_REALISIERUNG" in ctx

    # Keine Stammdaten im Kontext
    for forbidden in ["Geburtsdatum", "GdB", "Pflegegrad", "Anschrift"]:
        assert forbidden not in ctx


def test_fallback_renamed_bookmarks_reads_all_nine_areas():
    """Fallback liest mit umbenannten Bookmarks dieselben 9 IDs mit korrekten Markern."""
    docx_bytes = build_hamburg_vorbericht(fallback=True)
    plan = read_previous_plan(io.BytesIO(docx_bytes), filename="synth_fallback.docx")

    # Bisher fiel dies auf 3 Bereiche ('1', '2', '3') zusammen. Nun müssen es 9 sein.
    assert list(plan.sections.keys()) == EXPECTED_HAMBURG_IDS

    # Marker landeten im richtigen Bereich
    s_1_4 = plan.sections["1.4"]
    assert s_1_4.leitziel == "ZZ_SYNTH_1_4_LEITZIEL"
    assert s_1_4.vorschlag_massnahmen == "ZZ_SYNTH_1_4_MASSNAHMEN"
    assert s_1_4.realisierung_andere == "ZZ_SYNTH_1_4_REALISIERUNG"

    s_2_2 = plan.sections["2.2"]
    assert s_2_2.leitziel == "ZZ_SYNTH_2_2_LEITZIEL"

    s_3 = plan.sections["3"]
    assert s_3.leitziel == "ZZ_SYNTH_3_LEITZIEL"


def test_duplicate_area_table_raises_incomplete_plan_error():
    """Eine duplizierte Bereichstabelle wirft IncompletePlanError, nichts wird überschrieben."""
    dup_bytes = _make_duplicate_area_docx()

    with pytest.raises(IncompletePlanError) as exc_info:
        read_previous_plan(io.BytesIO(dup_bytes), filename="synth_dup.docx")

    assert exc_info.value.found < exc_info.value.expected or exc_info.value.found == 9
    assert "Vorbericht unvollständig gelesen" in str(exc_info.value)


def test_unknown_id_ignored():
    """Unbekannte IDs wie '9.9' werden ignoriert und erscheinen nicht im Ergebnis."""
    unknown_bytes = _make_unknown_id_docx()
    plan = read_previous_plan(io.BytesIO(unknown_bytes), filename="synth_unknown.docx")

    assert "9.9" not in plan.sections
    assert list(plan.sections.keys()) == EXPECTED_HAMBURG_IDS


def test_api_upload_plan_with_incomplete_plan_returns_422_and_preserves_state(test_client):
    """API antwortet bei unvollständigem Plan mit 422 und behält bisherigen Plan-Zustand."""
    # 1. Zunächst gültigen Plan hochladen
    valid_bytes = build_hamburg_vorbericht(fallback=False)
    res_valid = test_client.post(
        "/api/upload-plan",
        files={
            "file": (
                "vorbericht_valid.docx",
                io.BytesIO(valid_bytes),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert res_valid.status_code == 200
    assert res_valid.json()["goals_count"] == 9

    # Zustand prüfen
    session_before = test_client.get("/api/session").json()
    assert session_before["has_plan"] is True
    assert session_before["plan_goals_count"] == 9

    # 2. Defekten/duplizierten Plan hochladen
    dup_bytes = _make_duplicate_area_docx()
    res_dup = test_client.post(
        "/api/upload-plan",
        files={
            "file": (
                "vorbericht_dup.docx",
                io.BytesIO(dup_bytes),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert res_dup.status_code == 422
    detail = res_dup.json()["detail"]
    assert "❌ Vorbericht unvollständig gelesen:" in detail
    assert "Bereichen erkannt. Bitte Datei prüfen." in detail

    # 3. Vorheriger Plan bleibt unverändert aktiv (kein Teilimport)
    session_after = test_client.get("/api/session").json()
    assert session_after["has_plan"] is True
    assert session_after["plan_goals_count"] == 9


def test_odw_template_still_parses():
    """Die originale Op de Wisch Berichtsvorlage parst weiterhin fehlerfrei 9 Bereiche."""
    assert ODW_VORLAGE_PATH.exists()
    plan = read_previous_plan(ODW_VORLAGE_PATH)
    assert list(plan.sections.keys()) == EXPECTED_ODW_IDS
    assert len(plan.sections) == 9


def test_caplog_during_parsing_contains_no_zz_synth_markers(caplog):
    """Parsing loggt zu keinem Zeitpunkt Klientendaten oder Test-Marker (Zero Persistence/DSGVO)."""
    docx_bytes = build_hamburg_vorbericht(fallback=False)
    with caplog.at_level(logging.DEBUG):
        read_previous_plan(io.BytesIO(docx_bytes), filename="synth.docx")
        parse_plan_docx(docx_bytes, filename="synth.docx")

    assert "ZZ_SYNTH" not in caplog.text
    assert "Synthetischer Klient" not in caplog.text


def test_upload_plan_fails_when_no_parser_greift(test_client):
    """Prüft Issue #23: Wenn kein Parser-Pfad greift (0 Bereiche erkannt), liefert API 422 statt stiller Teil-Erkennung."""
    doc = docx.Document()
    doc.add_paragraph("Kaffee kochen und Zeitung lesen.")
    buf = io.BytesIO()
    doc.save(buf)
    unparseable_bytes = buf.getvalue()

    res = test_client.post(
        "/api/upload-plan",
        files={
            "file": (
                "kein_plan.docx",
                io.BytesIO(unparseable_bytes),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert res.status_code == 422
    assert "❌ Kein gültiger Vorbericht erkannt:" in res.json()["detail"]


def test_upload_plan_fallback_returns_visible_warning(test_client):
    """Prüft Issue #23: Fallback-Parser liefert sichtbare Warnung in UI/API statt stiller Erkennung."""
    fallback_bytes = build_hamburg_vorbericht(fallback=True)
    res = test_client.post(
        "/api/upload-plan",
        files={
            "file": (
                "vorbericht_fallback.docx",
                io.BytesIO(fallback_bytes),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert data["goals_count"] == 9
    assert data["is_fallback"] is True
    assert "⚠️" in data["warning"]

    # Auch /api/session enthält die Warnung für UI-Wiederherstellung
    session_data = test_client.get("/api/session").json()
    assert session_data["has_plan"] is True
    assert session_data["plan"]["is_fallback"] is True
    assert "⚠️" in session_data["plan"]["warning"]
