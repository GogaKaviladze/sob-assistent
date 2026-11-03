"""Tests für den VivendiPDImporter (ADR-4, Issue #17)."""
from pathlib import Path

import pytest

from sob_generator.importer import VivendiPDImporter, parse_vivendi_text

ROOT = Path(__file__).resolve().parents[1]
VORLAGE_CSV = ROOT / "templates" / "Vivendi" / "Vivendi CSV Vorlage.csv"
DUMMY_CSV = ROOT / "dummy" / "Max_Mustermann_Vivendi_Export.csv"


def test_importer_parses_template_csv():
    """Committete Mini-Vorlage: 1 Klient, 1 Eintrag."""
    result = VivendiPDImporter(VORLAGE_CSV).load()

    assert result.client.name == "Nachname"
    assert result.client.vorname == "Vorname"
    assert result.client.geburtsdatum == "01.01.1987"
    assert result.meta.leistungszeitraum_von == "30.01.2026"
    assert result.meta.leistungszeitraum_bis == "31.05.2026"
    assert result.meta.leistungsanbieter == "Die Name der Einrichtung"

    assert len(result.entries) == 1
    entry = result.entries[0]
    assert entry.datum == "31.05.2026"
    assert entry.kategorie == "w_Kontakt Angehörige"
    assert "Berichtseintrag" in entry.text


@pytest.mark.skipif(not DUMMY_CSV.exists(), reason="dummy/ ist lokal (gitignored)")
def test_importer_parses_dummy_export():
    """Realer 40+-Einträge-Export (lokal, gitignored)."""
    result = VivendiPDImporter(DUMMY_CSV).load()

    assert result.client.name == "Mustermann"
    assert result.client.vorname == "Max"
    assert result.client.geburtsdatum == "01.01.1987"
    assert result.meta.leistungszeitraum_von == "01.06.2025"
    assert result.meta.leistungszeitraum_bis == "31.05.2026"

    assert len(result.entries) == 43
    first = result.entries[0]
    assert first.datum == "05.06.2025"
    assert first.kategorie == "w_Hauswirtschaft"
    assert first.text  # nicht leer
    # Jeder Eintrag hat einen Fließtext (paarweise Struktur, ADR-4).
    assert all(e.text for e in result.entries)


def test_parse_vivendi_text_in_memory():
    """Prüft das speicherbasierte Einlesen eines Vivendi-CSV-Texts (Zero-Disk)."""
    text = VORLAGE_CSV.read_text(encoding="utf-8-sig")
    result = parse_vivendi_text(text)

    assert result.client.name == "Nachname"
    assert result.client.vorname == "Vorname"
    assert result.client.geburtsdatum == "01.01.1987"
    assert result.meta.leistungszeitraum_von == "30.01.2026"
    assert result.meta.leistungszeitraum_bis == "31.05.2026"
    assert result.meta.leistungsanbieter == "Die Name der Einrichtung"
    assert len(result.entries) == 1


def test_parse_vivendi_text_inline_header_sample():
    """Prüft parse_vivendi_text mit einem rein fiktiven Inline-Vivendi-Header (ohne Dateizugriff)."""
    csv_text = (
        ";;;;;;;;;;;;;;;;01.04.2024 08:00 - 31.03.2026 18:00;;\n"
        "Fiktive Assistenzdienste Hamburg-Nord;;;;;;;;;;;;;;;;;;\n"
        ";;;;;;;;;;;;;;;;;;\n"
        "Mustermann, Erika (*15.08.1988);;;;;;;;;;;;;;;;;;\n"
        ";;;;;Datum;;Uhrzeit;;Schicht;;Kategorie;;Kennzeichen;;Details;;;;Handzeichen\n"
        ";;;;15.04.2024;;10:00;;Früh;;;;;;;;;;MA (15.04.2024 10:00)\n"
        ";;;;Dokumentationseintrag zur Unterstützung bei der Haushaltsführung.;;;;;;;;;;;;;;\n"
    )
    result = parse_vivendi_text(csv_text, strict=False)

    assert result.client.name == "Mustermann"
    assert result.client.vorname == "Erika"
    assert result.client.geburtsdatum == "15.08.1988"
    assert result.meta.leistungszeitraum_von == "01.04.2024"
    assert result.meta.leistungszeitraum_bis == "31.03.2026"
    assert result.meta.leistungsanbieter == "Fiktive Assistenzdienste Hamburg-Nord"
    assert len(result.entries) == 1
    assert "Dokumentationseintrag" in result.entries[0].text


