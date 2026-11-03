"""Tests für Kontext 5: Vivendi-CSV-Loader, Kompaktformat & Keep-Newest (Issue #135).

Strikte RAM-Only Invariante, synthetische Testdaten, Zero-Persistence.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chat_app.app import create_app
from chat_app.chatbot import ReflectionChatbot, ReflectionEngine
from chat_app.csv_loader import CsvData, parse_csv_content
from sob_generator.importer import parse_vivendi_text
from sob_generator.llm.schemas import DocumentationEntry

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "hans_peter_synthetisch.csv"


def test_documentation_entry_schicht_backwards_compatible():
    """DocumentationEntry unterstützt optionales Feld 'schicht', Default leer (abwärtskompatibel)."""
    entry_default = DocumentationEntry(
        datum="01.01.2025",
        kategorie="Alltag",
        text="Einkauf geübt",
    )
    assert entry_default.schicht == ""

    entry_with_schicht = DocumentationEntry(
        datum="01.01.2025",
        kategorie="Alltag",
        text="Einkauf geübt",
        schicht="Früh",
    )
    assert entry_with_schicht.schicht == "Früh"


def test_vivendi_importer_parses_schicht_no_handzeichen():
    """parse_vivendi_text liest Schicht (Spalte 8), aber KEINE Handzeichen (Spalte 18)."""
    content = FIXTURE_PATH.read_text(encoding="utf-8-sig")
    imported = parse_vivendi_text(content, strict=False)

    assert len(imported.entries) == 57
    first = imported.entries[0]
    assert first.datum == "08.07.2024"
    assert first.schicht == "Früh"
    assert "Wäsche waschen" in first.text

    # Handzeichen dürfen in keinem Text oder Attribut vorkommen
    for e in imported.entries:
        assert "MAhrens" not in e.text
        assert "SKrause" not in e.text
        assert "TBlohm" not in e.text
        assert not hasattr(e, "handzeichen")


def test_vivendi_fixture_context_rendering():
    """Vivendi-Fixture ergibt genau 57 Einträge im Kompaktformat ohne Metadatenmüll."""
    content = FIXTURE_PATH.read_text(encoding="utf-8-sig")
    csv_data = parse_csv_content(content, filename="hans_peter_synthetisch.csv")

    assert csv_data.entries_total == 57
    assert len(csv_data.entries) == 57

    ctx = csv_data.to_context_text()

    # Zähler nach Rendering
    assert csv_data.entries_in_context == 57
    assert csv_data.oldest_included_date == "08.07.2024"

    # Keine Fragmentierung: keine Zeile beginnt mit "Datum:"
    for line in ctx.splitlines():
        assert not line.strip().startswith("Datum:")

    # Keine Handzeichen / Kürzel
    assert "MAhrens (" not in ctx
    assert "SKrause (" not in ctx
    assert "TBlohm (" not in ctx

    # Keine Spalte_N, kein Dateiname, kein Geburtsdatum
    assert "Spalte_" not in ctx
    assert "hans_peter_synthetisch.csv" not in ctx
    assert "22.11.1985" not in ctx
    assert "Spalten:" not in ctx

    # Kompaktformat erster & letzter Eintrag: TT.MM.JJ Schicht[ · Kategorie]: Text
    lines = [l for l in ctx.splitlines() if l.strip()]
    assert lines[0].startswith("08.07.24 Früh: Wäsche waschen Schritt für Schritt durchgegangen.")
    assert lines[-1].startswith("30.06.26 Früh: Jahresrückblick zum Haushalt.")


def test_vivendi_keep_newest_trimming():
    """Bei Zeichenbegrenzung fallen älteste Einträge weg; neueste bleiben erhalten."""
    content = FIXTURE_PATH.read_text(encoding="utf-8-sig")
    csv_data = parse_csv_content(content, filename="hans_peter_synthetisch.csv")

    # Kleines Zeichenlimit erzwingen (z. B. 600 Zeichen)
    ctx = csv_data.to_context_text(max_chars=600)

    # Zähler prüfen
    assert csv_data.entries_total == 57
    assert 0 < csv_data.entries_in_context < 57
    assert csv_data.oldest_included_date != ""
    assert csv_data.oldest_included_date != "08.07.2024"  # Ältestes Datum ist raus

    # Neuester Eintrag aus der Fixture muss im Kontext sein
    last_entry = csv_data.entries[-1]
    assert last_entry.text[:30] in ctx

    # Ältester Eintrag darf NICHT im Kontext sein
    assert "08.07.24 Früh: Wäsche waschen" not in ctx


def test_generic_csv_keep_newest_and_no_filename():
    """Generische (Nicht-Vivendi-)CSV folgt gleicher 'neueste behalten'-Regel und ohne Dateinamen."""
    generic_csv = (
        "Datum;Kategorie;Notiz\n"
        "01.01.2024;Pflege;Alte Notiz 1\n"
        "02.01.2024;Pflege;Mittlere Notiz 2\n"
        "03.01.2024;Pflege;Neueste Notiz 3\n"
    )
    csv_data = parse_csv_content(generic_csv, filename="generic_test.csv")

    assert csv_data.entries_total == 3
    assert len(csv_data.entries) == 0  # Keine Vivendi-Entries

    # Vollständiger Kontext
    ctx = csv_data.to_context_text()
    assert "generic_test.csv" not in ctx
    assert "Spalten:" not in ctx
    assert "Neueste Notiz 3" in ctx
    assert "Alte Notiz 1" in ctx
    assert csv_data.entries_in_context == 3
    assert csv_data.oldest_included_date == "01.01.2024"

    # Gekürzter Kontext (nur neueste Zeile passt)
    ctx_trimmed = csv_data.to_context_text(max_chars=120)
    assert csv_data.entries_in_context < 3
    assert "Neueste Notiz 3" in ctx_trimmed
    assert "Alte Notiz 1" not in ctx_trimmed


def test_upload_csv_logging_caplog(caplog):
    """Logging beim CSV-Upload protokolliert ausschließlich Anzahlen, keine Inhaltsdaten."""
    app = create_app()
    client = TestClient(app)

    content = FIXTURE_PATH.read_text(encoding="utf-8-sig")

    with caplog.at_level(logging.INFO, logger="chat_app"):
        response = client.post(
            "/api/upload",
            files={"file": ("hans_peter.csv", content.encode("utf-8"), "text/csv")},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["entries_total"] == 57
    assert data["entries_in_context"] == 57
    assert data["oldest_included_date"] == "08.07.2024"

    # caplog darf keinen Text aus den Einträgen enthalten
    log_output = caplog.text
    assert "Wäsche waschen" not in log_output
    assert "MAhrens" not in log_output
    assert "hans_peter.csv" not in log_output
    assert "57 Zeilen" in log_output or "57" in log_output


def test_session_endpoint_includes_csv_counters():
    """Endpoint /api/session liefert die Zähler entries_total, entries_in_context und oldest_included_date."""
    app = create_app()
    client = TestClient(app)

    content = FIXTURE_PATH.read_text(encoding="utf-8-sig")
    client.post(
        "/api/upload",
        files={"file": ("hans_peter.csv", content.encode("utf-8"), "text/csv")},
    )

    response = client.get("/api/session")
    assert response.status_code == 200
    data = response.json()
    assert data["has_csv"] is True
    csv_info = data["csv"]
    assert csv_info["entries_total"] == 57
    assert csv_info["entries_in_context"] == 57
    assert csv_info["oldest_included_date"] == "08.07.2024"


def test_generic_csv_with_similar_headers_not_misidentified_as_vivendi():
    """Generische CSV mit ähnlichen Headern (Datum, Uhrzeit, Schicht, Kategorie) darf NICHT als Vivendi eingestuft werden."""
    generic_csv = (
        "Mitarbeiter;Datum;Uhrzeit;Schicht;Kategorie;Notiz\n"
        "Max;01.01.2025;08:00;Früh;Pflege;Dienstübernahme Dokumentation\n"
        "Erika;02.01.2025;14:00;Spät;Pflege;Begleitung Facharztbesuch\n"
    )
    csv_data = parse_csv_content(generic_csv, filename="dienstplan.csv")
    assert len(csv_data.entries) == 0  # Darf keine Vivendi-Entries haben
    assert csv_data.entries_total == 2
    ctx = csv_data.to_context_text()
    assert "Dienstübernahme Dokumentation" in ctx
    assert "Begleitung Facharztbesuch" in ctx


def test_vivendi_with_quoted_headers_detected_correctly():
    """Vivendi-Dateien mit CSV-Quoting in Headern werden sauber erkannt und geparst."""
    # Synthetischer Vivendi-Ausschnitt mit Quotes
    quoted_vivendi = (
        ";;;;;;;;;;;;;;;;;;\n"
        ";;;;;;;;;;;;;;;;;;\n"
        ";;;;;;;;;;;;;;;;;;\n"
        "Mustermann, Erika (*15.05.1970);;;;;;;;;;;;;;;;;;\n"
        ';;;;"Datum";;"Uhrzeit";;"Schicht";;"Kategorie";;;;;;;;"Handzeichen"\n'
        ";;;;01.03.2025;;09:00;;Früh;;Alltag;;;;;;;;EMustermann (01.03.2025 09:00)\n"
        ";;;;Einkauf gemeinsam erledigt.;;;;;;;;;;;;;;\n"
    )
    csv_data = parse_csv_content(quoted_vivendi, filename="export.csv")
    assert len(csv_data.entries) == 1
    assert csv_data.client_name == "Erika Mustermann"
    ctx = csv_data.to_context_text()
    assert "01.03.25 Früh · Alltag: Einkauf gemeinsam erledigt." in ctx
    assert "EMustermann" not in ctx
    assert "15.05.1970" not in ctx


def test_vivendi_fail_closed_on_corrupt_export():
    """Wird eine Vivendi-Datei erkannt, schlägt aber fehl, darf nicht fail-open in den generischen Parser gefallen werden."""
    corrupt_vivendi = (
        ";;;;;;;;;;;;;;;;;;\n"
        ";;;;;;;;;;;;;;;;;;\n"
        ";;;;;;;;;;;;;;;;;;\n"
        "Mustermann, Erika (*15.05.1970);;;;;;;;;;;;;;;;;;\n"
        ";;;;Datum;;Uhrzeit;;Schicht;;Kategorie;;;;;;;;Handzeichen\n"
        # Abrupter Abbruch ohne Zeilenpaare
    )
    with pytest.raises(ValueError, match="Vivendi-Export fehlerhaft"):
        parse_csv_content(corrupt_vivendi, filename="corrupt.csv")


def test_vivendi_client_name_priority_over_filename():
    """Klientenname aus Zeile 4 hat Vorrang vor Heuristiken des Dateinamens."""
    content = FIXTURE_PATH.read_text(encoding="utf-8-sig")
    # Dateiname ohne Klientenname
    csv_data = parse_csv_content(content, filename="export_2026.csv")
    assert csv_data.client_name == "Hans Peter"

    # Auch im Upload-Endpoint muss parsed_csv.client_name zurückgeliefert werden
    app = create_app()
    client = TestClient(app)
    resp = client.post(
        "/api/upload",
        files={"file": ("export_2026.csv", content.encode("utf-8"), "text/csv")},
    )
    assert resp.status_code == 200
    assert resp.json()["client_name"] == "Hans Peter"


def test_multiline_text_in_vivendi_entry_sanitized_to_single_line():
    """Zeilenumbrüche innerhalb von Vivendi-Freitexten werden zu Leerzeichen normalisiert (Einzeiligkeits-Invariante)."""
    vivendi_multiline = (
        ";;;;;;;;;;;;;;;;;;\n"
        ";;;;;;;;;;;;;;;;;;\n"
        ";;;;;;;;;;;;;;;;;;\n"
        "Mustermann, Max (*01.01.1980);;;;;;;;;;;;;;;;;;\n"
        ";;;;Datum;;Uhrzeit;;Schicht;;Kategorie;;;;;;;;Handzeichen\n"
        ";;;;01.04.2025;;10:00;;Früh;;Pflege;;;;;;;;MMustermann\n"
        ';;;;"Zeile 1 des Berichts.\nZeile 2 des Berichts.\r\nZeile 3 des Berichts.";;;;;;;;;;;;;;\n'
    )
    csv_data = parse_csv_content(vivendi_multiline, filename="max.csv")
    ctx = csv_data.to_context_text()
    # Der gesamte Eintrag muss auf genau einer Zeile stehen
    lines = [l for l in ctx.splitlines() if l.strip()]
    assert len(lines) == 1
    assert "Zeile 1 des Berichts. Zeile 2 des Berichts. Zeile 3 des Berichts." in lines[0]


def test_chronological_sorting_before_budgeting():
    """Einträge werden chronologisch sortiert, sodass die zeitlich neuesten behalten werden."""
    # Unsortierte Einträge: Ältester Eintrag steht am Ende
    vivendi_unsorted = (
        ";;;;;;;;;;;;;;;;;;\n"
        ";;;;;;;;;;;;;;;;;;\n"
        ";;;;;;;;;;;;;;;;;;\n"
        "Mustermann, Max (*01.01.1980);;;;;;;;;;;;;;;;;;\n"
        ";;;;Datum;;Uhrzeit;;Schicht;;Kategorie;;;;;;;;Handzeichen\n"
        ";;;;01.06.2025;;10:00;;Früh;;Pflege;;;;;;;;MM\n"
        ";;;;Neuester Eintrag im Juni;;;;;;;;;;;;;;\n"
        ";;;;01.01.2025;;10:00;;Früh;;Pflege;;;;;;;;MM\n"
        ";;;;Ältester Eintrag im Januar;;;;;;;;;;;;;;\n"
    )
    csv_data = parse_csv_content(vivendi_unsorted, filename="unsorted.csv")
    # Nur 1 Eintrag ins Budget lassen
    ctx = csv_data.to_context_text(max_rows=1)
    assert "Neuester Eintrag im Juni" in ctx
    assert "Ältester Eintrag im Januar" not in ctx


def test_strict_max_chars_budgeting_never_exceeds_budget():
    """to_context_text überschreitet NIEMALS max_chars, auch bei extrem kleinem Limit."""
    content = FIXTURE_PATH.read_text(encoding="utf-8-sig")
    csv_data = parse_csv_content(content, filename="hans_peter_synthetisch.csv")

    for max_limit in [20, 50, 100, 200, 500]:
        ctx = csv_data.to_context_text(max_chars=max_limit)
        assert len(ctx) <= max_limit, f"ctx length {len(ctx)} exceeded max_chars {max_limit}"
        if ctx:
            assert "neuesten 0 Einträge" not in ctx


def test_generic_csv_with_birthdates_in_first_column_not_misidentified_as_vivendi():
    """Generische CSV mit Klientengeburtsdaten in Spalte 1 darf NICHT als Vivendi eingestuft und blockiert werden."""
    csv_content = (
        "Klient;Stadt;Leistung\n"
        "Mustermann, Max (*01.01.1980);Köln;ASP\n"
        "Schmidt, Anna (*15.03.1975);Bonn;ASP\n"
    )
    csv_data = parse_csv_content(csv_content, filename="klienten.csv")
    assert len(csv_data.entries) == 0
    assert csv_data.entries_total == 2
    ctx = csv_data.to_context_text()
    assert "Mustermann, Max (*01.01.1980)" in ctx
    assert "Schmidt, Anna (*15.03.1975)" in ctx


def test_all_tracked_docx_metadata_no_pii():
    """Alle DOCX-Dateien im Repository (synthetic_test_data und templates) haben neutrale Metadaten (Autor, Bearbeiter, Firma, Manager leer oder erlaubt)."""
    import re
    import zipfile
    repo_root = Path(__file__).parent.parent
    docx_files = [
        p for p in list((repo_root / "synthetic_test_data").glob("**/*.docx")) + list(
            (repo_root / "templates").glob("**/*.docx")
        )
        if not p.name.startswith("~$")
    ]
    assert len(docx_files) >= 6

    allowed = {"", "Assistent", "SOB"}

    def tag_values(xml: str, tag: str) -> list[str]:
        values = re.findall(rf"<{tag}(?:\s[^>]*)?>(.*?)</{tag}>", xml, re.S)
        return [v.strip() for v in values]

    for docx_path in docx_files:
        with zipfile.ZipFile(docx_path, "r") as z:
            names = z.namelist()
            assert "docProps/core.xml" in names
            core_xml = z.read("docProps/core.xml").decode("utf-8", errors="ignore")
            for tag in ("dc:creator", "cp:lastModifiedBy"):
                for value in tag_values(core_xml, tag):
                    assert value in allowed, f"{docx_path.name}: {tag} nicht erlaubt"
            if "docProps/app.xml" in names:
                app_xml = z.read("docProps/app.xml").decode("utf-8", errors="ignore")
                for tag in ("Company", "Manager"):
                    for value in tag_values(app_xml, tag):
                        assert value in allowed, f"{docx_path.name}: {tag} nicht erlaubt"


def test_vivendi_entry_sanitizes_newlines_in_schicht_and_kategorie():
    """Zeilenumbrüche in Schicht und Kategorie werden normalisiert, Einzeiligkeits-Invariante bleibt gewahrt."""
    from chat_app.csv_loader import _format_vivendi_entry

    entry = DocumentationEntry(
        datum="10.05.2025",
        schicht="Früh\nSchicht",
        kategorie="Alltag\r\nHilfe",
        text="Unterstützung bei der\nOrganisation.",
    )
    line = _format_vivendi_entry(entry)
    assert "\n" not in line
    assert "\r" not in line
    assert "10.05.25 Früh Schicht · Alltag Hilfe: Unterstützung bei der Organisation." == line


def test_parse_entry_date_supports_iso8601_and_chronological_sort():
    """_parse_entry_date unterstützt ISO-8601 (YYYY-MM-DD) und sortiert chronologisch."""
    from chat_app.csv_loader import _parse_entry_date

    assert _parse_entry_date("2025-06-15") == (2025, 6, 15)
    assert _parse_entry_date("15.06.2025") == (2025, 6, 15)

    generic_iso_csv = (
        "date,event\n"
        "2025-03-01,Altes Event\n"
        "2025-09-01,Neuestes Event\n"
        "2025-06-01,Mittleres Event\n"
    )
    csv_data = parse_csv_content(generic_iso_csv, filename="iso.csv")
    ctx = csv_data.to_context_text(max_rows=1)
    assert "Neuestes Event" in ctx
    assert "Altes Event" not in ctx


def test_case_conflict_detection_uses_csv_client_name_not_filename():
    """Konfliktprüfung in upload_csv erkennt Klientenkonflikt auch bei neutralem Dateinamen (z.B. export.csv)."""
    bot = ReflectionChatbot(provider=ReflectionEngine())
    bot.plan_data = type("PlanMock", (), {"client_name": "Hans Peter"})()
    app = create_app(chatbot=bot)
    client = TestClient(app)

    # 2. Vivendi-CSV für "Erika Mustermann" mit neutralem Dateinamen hochladen
    vivendi_content = (
        ";;;;;;;;;;;;;;;;;;\n"
        ";;;;;;;;;;;;;;;;;;\n"
        ";;;;;;;;;;;;;;;;;;\n"
        "Mustermann, Erika (*15.05.1970);;;;;;;;;;;;;;;;;;\n"
        ";;;;Datum;;Uhrzeit;;Schicht;;Kategorie;;;;;;;;Handzeichen\n"
        ";;;;01.03.2025;;09:00;;Früh;;Alltag;;;;;;;;EMustermann\n"
        ";;;;Einkauf gemeinsam erledigt.;;;;;;;;;;;;;;\n"
    )
    resp = client.post(
        "/api/upload",
        files={"file": ("export.csv", vivendi_content.encode("utf-8"), "text/csv")},
    )
    assert resp.status_code == 409
    assert resp.json()["needs_confirmation"] is True


def test_chronological_sorting_with_same_day_different_times():
    """Einträge am selben Tag werden anhand der Uhrzeit sortiert; neuester Nachmittagseintrag bleibt erhalten."""
    vivendi_same_day = (
        ";;;;;;;;;;;;;;;;;;\n"
        ";;;;;;;;;;;;;;;;;;\n"
        ";;;;;;;;;;;;;;;;;;\n"
        "Mustermann, Max (*01.01.1980);;;;;;;;;;;;;;;;;;\n"
        ";;;;Datum;;Uhrzeit;;Schicht;;Kategorie;;;;;;;;Handzeichen\n"
        ";;;;01.06.2025;;16:00;;Spät;;Pflege;;;;;;;;MM\n"
        ";;;;Späterer Eintrag am Nachmittag;;;;;;;;;;;;;;\n"
        ";;;;01.06.2025;;09:00;;Früh;;Pflege;;;;;;;;MM\n"
        ";;;;Früherer Eintrag am Vormittag;;;;;;;;;;;;;;\n"
    )
    csv_data = parse_csv_content(vivendi_same_day, filename="same_day.csv")
    ctx = csv_data.to_context_text(max_rows=1)
    assert "Späterer Eintrag am Nachmittag" in ctx
    assert "Früherer Eintrag am Vormittag" not in ctx


def test_incomplete_vivendi_export_rejected_fail_closed():
    """Vivendi-Datei mit Metazeile ohne Textzeile am Ende wird mit ValueError abgelehnt."""
    incomplete_vivendi = (
        ";;;;;;;;;;;;;;;;;;\n"
        ";;;;;;;;;;;;;;;;;;\n"
        ";;;;;;;;;;;;;;;;;;\n"
        "Mustermann, Max (*01.01.1980);;;;;;;;;;;;;;;;;;\n"
        ";;;;Datum;;Uhrzeit;;Schicht;;Kategorie;;;;;;;;Handzeichen\n"
        ";;;;01.06.2025;;09:00;;Früh;;Pflege;;;;;;;;MM\n"
        ";;;;Eintrag 1 Text;;;;;;;;;;;;;;\n"
        ";;;;02.06.2025;;10:00;;Früh;;Pflege;;;;;;;;MM\n"
        # Textzeile fehlt!
    )
    with pytest.raises(ValueError, match="unvollständig|fehlerhaft"):
        parse_csv_content(incomplete_vivendi, filename="incomplete.csv")


def test_generic_table_with_vivendi_like_header_stays_in_generic_path():
    """Generische Tabelle mit Spalten Datum/Uhrzeit/Schicht/Kategorie/Handzeichen/Notiz bleibt generische CSV."""
    generic_csv = (
        "Datum;Uhrzeit;Schicht;Kategorie;Handzeichen;Notiz\n"
        "01.01.2025;08:00;Früh;Pflege;MM;Reguläre Verlaufsnotiz\n"
    )
    csv_data = parse_csv_content(generic_csv, filename="tabelle.csv")
    assert len(csv_data.entries) == 0
    assert csv_data.entries_total == 1
    ctx = csv_data.to_context_text()
    assert "Reguläre Verlaufsnotiz" in ctx


def test_extreme_small_budget_does_not_count_partial_entry():
    """Wenn nicht mal ein vollständiger Eintrag ins max_chars Budget passt, ist entries_in_context 0."""
    single_vivendi = (
        ";;;;;;;;;;;;;;;;;;\n"
        ";;;;;;;;;;;;;;;;;;\n"
        ";;;;;;;;;;;;;;;;;;\n"
        "Mustermann, Max (*01.01.1980);;;;;;;;;;;;;;;;;;\n"
        ";;;;Datum;;Uhrzeit;;Schicht;;Kategorie;;;;;;;;Handzeichen\n"
        ";;;;01.06.2025;;10:00;;Früh;;Pflege;;;;;;;;MM\n"
        ";;;;Dies ist ein relativ langer Dokumentationstext für den Test.;;;;;;;;;;;;;;\n"
    )
    csv_data = parse_csv_content(single_vivendi, filename="single.csv")
    # Budget von 25 Zeichen reicht nicht für den ca. 75 Zeichen langen Eintrag
    ctx = csv_data.to_context_text(max_chars=25)
    assert ctx == ""
    assert csv_data.entries_in_context == 0


def test_generic_csv_ignores_birthdate_for_sorting_and_oldest_date():
    """Generische CSV mit Geburtsdatum ignoriert dieses bei Sortierung und oldest_included_date."""
    generic_csv = (
        "Geburtsdatum;Datum;Notiz\n"
        "01.01.1980;10.05.2025;Neuerer Verlauf\n"
        "15.05.1990;01.02.2025;Älterer Verlauf\n"
    )
    csv_data = parse_csv_content(generic_csv, filename="daten.csv")
    assert csv_data.oldest_included_date == "01.02.2025"
    ctx = csv_data.to_context_text(max_rows=1)
    assert "Neuerer Verlauf" in ctx
    assert "Älterer Verlauf" not in ctx



