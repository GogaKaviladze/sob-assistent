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


# ---------------------------------------------------------------------------
# Op-de-Wisch Vorbericht-Parser, Klientenname & Falltrennung
# ---------------------------------------------------------------------------


def test_odw_vorbericht_parses_all_areas_without_fallback():
    """(a) Parser erkennt ALLE 9 Bereiche der Op-de-Wisch-Vorlage ohne Fallback (is_fallback False)."""
    from tests.fixtures.make_synthetic_vorbericht_opdewisch import build_opdewisch_vorbericht

    odw_bytes = build_opdewisch_vorbericht()

    plan = read_previous_plan(io.BytesIO(odw_bytes), filename="Hans_Peter_Vorbericht_OpDeWisch.docx")
    assert plan.is_fallback is False
    assert list(plan.sections.keys()) == EXPECTED_ODW_IDS
    assert len(plan.sections) == 9
    for idx in EXPECTED_ODW_IDS:
        sec = plan.sections[idx]
        assert sec.applies is True
        assert f"ZZ_SYNTH_ODW_{idx}_LEITZIEL" in sec.leitziel
        assert f"ZZ_SYNTH_ODW_{idx}_SMARTE_ZIELE" in sec.smarte_ziele
        assert f"ZZ_SYNTH_ODW_{idx}_BERICHT" in sec.stand_zielerreichung
        assert f"ZZ_SYNTH_ODW_{idx}_VORSCHLAG" in sec.vorschlag_ziele
        assert f"ZZ_SYNTH_ODW_{idx}_MASSNAHMEN" in sec.vorschlag_massnahmen
        assert f"ZZ_SYNTH_ODW_{idx}_REALISIERUNG" in sec.realisierung_andere

    plan_data = parse_plan_docx(odw_bytes, filename="Hans_Peter_Vorbericht_OpDeWisch.docx")
    assert plan_data.is_fallback is False
    assert [g.section_id for g in plan_data.goals] == EXPECTED_ODW_IDS
    assert plan_data.goals_count == 9

    # Auch für die bestehende synthetische Datei mit "entfällt"-Bereichen müssen alle 9 Bereiche in plan_data.goals enthalten sein
    synth_odw_path = (
        Path(__file__).resolve().parents[1]
        / "synthetic_test_data"
        / "01_fall_hans_peter"
        / "Hans_Peter_Vorbericht_OpDeWisch.docx"
    )
    if synth_odw_path.exists():
        synth_pd = parse_plan_docx(synth_odw_path, filename=synth_odw_path.name)
        assert synth_pd.is_fallback is False
        assert [g.section_id for g in synth_pd.goals] == EXPECTED_ODW_IDS
        assert len(synth_pd.goals) == 9


def test_odw_vorbericht_recognizes_client_name_correctly():
    """(b) Der erkannte Klientenname aus der Op-de-Wisch-Vorlage ist korrekt (und bei leerem Feld leer statt Platzhalter)."""
    from tests.fixtures.make_synthetic_vorbericht_opdewisch import build_opdewisch_vorbericht

    odw_bytes = build_opdewisch_vorbericht(client_name="Peter", client_vorname="Hans")
    plan = read_previous_plan(io.BytesIO(odw_bytes), filename="vorbericht_odw.docx")
    assert plan.client_name == "Peter, Hans"

    plan_data = parse_plan_docx(odw_bytes, filename="vorbericht_odw.docx")
    assert plan_data.client_name == "Peter, Hans"
    assert plan_data.client_name_from_filename is False

    # Blanko-Vorlage mit Platzhalter "Name, Vorname der Klient*in" darf keinen Platzhalter als Namen erkennen
    blank_plan = read_previous_plan(ODW_VORLAGE_PATH, filename="vorbericht_blanko.docx")
    assert blank_plan.client_name == ""
    assert blank_plan.is_fallback is False


@pytest.mark.parametrize("order", ["plan_then_csv", "csv_then_plan"])
def test_upload_odw_vorbericht_and_matching_csv_any_order_preserves_both(order):
    """(c) Upload Vorbericht + passende CSV desselben Klienten in beliebiger Reihenfolge -> CSV bleibt erhalten, kein Falltrennungs-Reset."""
    from tests.fixtures.make_synthetic_vorbericht_opdewisch import build_opdewisch_vorbericht

    bot = ReflectionChatbot(provider=ReflectionEngine())
    client = TestClient(create_app(chatbot=bot))

    odw_bytes = build_opdewisch_vorbericht(client_name="Peter", client_vorname="Hans")
    csv_path = Path(__file__).resolve().parent / "fixtures" / "hans_peter_synthetisch.csv"
    csv_bytes = csv_path.read_bytes()

    if order == "plan_then_csv":
        res_plan = client.post(
            "/api/upload-plan",
            files={
                "file": (
                    "Hans_Peter_Vorbericht_OpDeWisch.docx",
                    io.BytesIO(odw_bytes),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
        assert res_plan.status_code == 200, res_plan.text

        res_csv = client.post(
            "/api/upload",
            files={"file": ("Hans_Peter_Vivendi_Export.csv", io.BytesIO(csv_bytes), "text/csv")},
        )
        assert res_csv.status_code == 200, res_csv.text
    else:
        res_csv = client.post(
            "/api/upload",
            files={"file": ("Hans_Peter_Vivendi_Export.csv", io.BytesIO(csv_bytes), "text/csv")},
        )
        assert res_csv.status_code == 200, res_csv.text

        res_plan = client.post(
            "/api/upload-plan",
            files={
                "file": (
                    "Hans_Peter_Vorbericht_OpDeWisch.docx",
                    io.BytesIO(odw_bytes),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
        assert res_plan.status_code == 200, res_plan.text

    sess = client.get("/api/session").json()
    assert sess["has_csv"] is True
    assert sess["has_plan"] is True
    assert sess["csv"]["entries_total"] == 57
    assert sess["plan_goals_count"] == 9


def test_uncertain_client_name_shows_warning_instead_of_reset():
    """Falltrennung setzt nur bei sicher erkanntem, abweichendem Namen zurück; unsicherer Name -> sichtbare Warnung statt Reset."""
    from tests.fixtures.make_synthetic_vorbericht_opdewisch import build_opdewisch_vorbericht

    bot = ReflectionChatbot(provider=ReflectionEngine())
    client = TestClient(create_app(chatbot=bot))

    # 1. Sicher erkannter Klient via Vivendi-CSV ("Hans Peter" aus Zeile 4)
    csv_path = Path(__file__).resolve().parent / "fixtures" / "hans_peter_synthetisch.csv"
    res_csv = client.post(
        "/api/upload",
        files={"file": ("export_neutral.csv", io.BytesIO(csv_path.read_bytes()), "text/csv")},
    )
    assert res_csv.status_code == 200

    # 2. Vorbericht OHNE Namen im Dokument, aber mit abweichendem Dateinamen (unsicherer Name aus Dateinamen)
    odw_no_name_bytes = build_opdewisch_vorbericht(client_name="", client_vorname="")
    res_plan_uncertain = client.post(
        "/api/upload-plan",
        files={
            "file": (
                "Erika_Musterfrau_Vorbericht.docx",
                io.BytesIO(odw_no_name_bytes),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    # Darf KEINEN 409-Reset auslösen, sondern 200 mit sichtbarer Warnung liefern; CSV bleibt erhalten!
    assert res_plan_uncertain.status_code == 200, res_plan_uncertain.text
    data_uncertain = res_plan_uncertain.json()
    assert data_uncertain.get("warning")
    assert bot.has_csv is True
    assert bot.has_plan is True

    # 3. Gegenprobe: Sicher erkannter, abweichender Name im Dokument -> 409 Falltrennungs-Reset-Abfrage
    bot2 = ReflectionChatbot(provider=ReflectionEngine())
    client2 = TestClient(create_app(chatbot=bot2))
    client2.post(
        "/api/upload",
        files={"file": ("export_neutral.csv", io.BytesIO(csv_path.read_bytes()), "text/csv")},
    )
    odw_other_client = build_opdewisch_vorbericht(client_name="Musterfrau", client_vorname="Erika")
    res_conflict = client2.post(
        "/api/upload-plan",
        files={
            "file": (
                "vorbericht.docx",
                io.BytesIO(odw_other_client),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert res_conflict.status_code == 409
    assert res_conflict.json()["needs_confirmation"] is True

    # 4. Umgekehrte Richtung: Sicher erkannter Plan + generische CSV mit abweichendem Dateinamen -> 200 mit Warnung statt Reset
    bot3 = ReflectionChatbot(provider=ReflectionEngine())
    client3 = TestClient(create_app(chatbot=bot3))
    odw_hans_peter = build_opdewisch_vorbericht(client_name="Peter", client_vorname="Hans")
    client3.post(
        "/api/upload-plan",
        files={
            "file": (
                "vorbericht.docx",
                io.BytesIO(odw_hans_peter),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    generic_csv = b"Datum;Kategorie;Text\n01.05.2025;Wohnen;Synthetischer Eintrag"
    res_csv_uncertain = client3.post(
        "/api/upload",
        files={"file": ("Erika_Musterfrau_Export.csv", io.BytesIO(generic_csv), "text/csv")},
    )
    assert res_csv_uncertain.status_code == 200, res_csv_uncertain.text
    assert res_csv_uncertain.json().get("warning")
    assert bot3.has_plan is True
    assert bot3.has_csv is True

