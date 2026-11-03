"""Tests für sob_generator/llm/prompts.py (Weg C, #38).

Prüft:
- build_system_prompt enthält Anti-Halluzinations-Zeile, aber NICHT
  ANTI_TEMPLATE_INSTRUCTION / GROUNDING_INSTRUCTION (wurden zurückgenommen).
- _select_section_example liefert für alle 9 Abschnitte dasselbe Beispiel.
- Few-Shot-Beispiele enthalten die echte Klientenbezeichnung, nicht "Herr Muster" (#34).
"""
import re
import sys
from pathlib import Path

from unittest.mock import MagicMock

from sob_generator.llm.prompts import (
    _select_section_example,
    _section_few_shot,
    _developments_few_shot,
    build_section_prompt,
    build_developments_prompt,
    build_system_prompt,
    build_field_goal_clarification_prompt,
)
from sob_generator.llm.schemas import DocumentationEntry


def _section(index: int):
    s = MagicMock()
    s.index = index
    return s


def test_build_system_prompt_enthaelt_anti_halluzination():
    prompt = build_system_prompt(anrede="Herr", nachname="Mustermann")
    assert "erfinde keine Ereignisse" in prompt


def test_build_system_prompt_enthaelt_keine_anti_template_instruction():
    prompt = build_system_prompt()
    # Kernbegriff aus ANTI_TEMPLATE_INSTRUCTION (Weg C: entfernt)
    assert "Tätigkeit oder dem Thema" not in prompt


def test_build_system_prompt_enthaelt_kein_grounding():
    prompt = build_system_prompt()
    # Kernbegriff aus GROUNDING_INSTRUCTION (Weg C: entfernt)
    assert "konkrete Tätigkeiten und Ereignisse" not in prompt


def test_build_system_prompt_enthaelt_anrede_wenn_angegeben():
    prompt = build_system_prompt(anrede="Frau", nachname="Musterfrau")
    assert "Frau Musterfrau" in prompt


def test_select_section_example_liefert_fuer_alle_abschnitte_dasselbe_struktur():
    # Weg C: keine Rotation — alle 9 Abschnitte liefern ein SectionFewShot.
    first = _select_section_example(_section(0), "Herr", "Mustermann")
    for index in range(1, 9):
        result = _select_section_example(_section(index), "Herr", "Mustermann")
        assert result == first


# --- #34: Few-Shot-Platzhaltername durch echte Bezeichnung ersetzen ---

def test_section_few_shot_kein_placeholder_muster():
    """'Herr Muster' (alter Platzhalter) darf im Few-Shot nicht mehr stehen."""
    shot = _section_few_shot(anrede="Herr", nachname="Mustermann")
    combined = shot.eingabe + shot.ausgabe
    # \bMuster\b matcht "Muster" als eigenständiges Wort, aber nicht "Mustermann"
    assert not re.search(r"\bMuster\b", combined), (
        "Alter Platzhaltername 'Muster' noch im Few-Shot-Block gefunden"
    )


def test_section_few_shot_enthaelt_akkusativ():
    """Akkusativform 'Herrn Mustermann' muss im Few-Shot erscheinen."""
    shot = _section_few_shot(anrede="Herr", nachname="Mustermann")
    assert "Herrn Mustermann" in shot.ausgabe


def test_section_few_shot_enthaelt_nominativ():
    shot = _section_few_shot(anrede="Herr", nachname="Mustermann")
    assert "Herr Mustermann" in shot.eingabe


def test_section_few_shot_frau_anrede():
    """Für Frau: Akkusativ == Nominativ (kein 'Fraun')."""
    shot = _section_few_shot(anrede="Frau", nachname="Schmidt")
    assert "Frau Schmidt" in shot.eingabe
    assert "Frau Schmidt" in shot.ausgabe
    assert "Fraun Schmidt" not in shot.ausgabe


def test_developments_few_shot_kein_placeholder_muster():
    inp, out = _developments_few_shot(anrede="Herr", nachname="Mustermann")
    combined = inp + out
    assert not re.search(r"\bMuster\b", combined), (
        "Alter Platzhaltername 'Muster' noch im Developments-Few-Shot gefunden"
    )


def test_developments_few_shot_enthaelt_echten_namen():
    inp, out = _developments_few_shot(anrede="Herr", nachname="Mustermann")
    assert "Herr Mustermann" in inp
    assert "Herr Mustermann" in out


def test_build_section_prompt_few_shot_kein_placeholder(tmp_path):
    """build_section_prompt darf 'Herr Muster' (Platzhalter) nicht enthalten."""
    section = MagicMock()
    section.id = "1.1"
    section.title = "Wohnsituation"
    section.description = "Wohnform und Wohnumfeld."
    prompt = build_section_prompt(
        section=section,
        entries=[],
        anrede="Herr",
        nachname="Mustermann",
    )
    assert not re.search(r"\bMuster\b", prompt), (
        "Alter Platzhaltername 'Muster' im build_section_prompt gefunden"
    )
    assert "Herrn Mustermann" in prompt


def test_build_developments_prompt_few_shot_kein_placeholder():
    """build_developments_prompt darf 'Herr Muster' (Platzhalter) nicht enthalten."""
    prompt = build_developments_prompt(
        entries=[],
        anrede="Herr",
        nachname="Mustermann",
    )
    assert not re.search(r"\bMuster\b", prompt), (
        "Alter Platzhaltername 'Muster' im build_developments_prompt gefunden"
    )
    assert "Herr Mustermann" in prompt


# --- #71: Fachliche Schärfe bei psychischen Krisen, Gegenstandsschärfe & Namens-Scrubbing ---

def test_clinical_guardrails_anti_drift_in_lf1():
    """Prüft, dass LF 1 strikte Gegenstandsschärfe und das Verbot von Lebensfeld-Drift enthält."""
    from sob_generator.llm.sections import SectionDefinition

    sec_lf1 = SectionDefinition(
        index=0,
        id="1",
        title="Psychische Verfassung / Selbstsorge",
        description="Psychische Stabilität, emotionale Verfassung, Selbstfürsorge, Umgang mit Krisen.",
    )
    prompt = build_section_prompt(
        section=sec_lf1,
        entries=[
            DocumentationEntry(datum="01.03.2026", kategorie="", text="stationär in Psychiatrie aufgenommen"),
        ],
        anrede="Herr",
        nachname="Peter",
        vorname="Hans",
    )

    # Verbot von Lebensfeld-Drift
    assert "Strikte Gegenstandsschärfe für Lebensfeld 1" in prompt
    assert "VERBOT von Lebensfeld-Drift" in prompt
    assert "Lebensfeld 6" in prompt
    assert "Lebensfeld 7" in prompt

    # Verbot von Verharmlosungen
    assert "psychologische Phase" in prompt
    assert "STRENGSTES VERBOT: Verharmlosende Laienfloskeln" in prompt
    assert "Ergänze keine Diagnose" in prompt

    # Krisenmanagement-Pflicht
    assert "Krisenmanagement & Notfallplanung" in prompt
    assert "Historische Krisen begründen keine aktuelle Diagnose" in prompt
    assert "niemals als bereits vereinbarte" in prompt


def test_field_goal_clarification_prompt_lf1_includes_guardrails_and_anrede():
    """Prüft, dass build_field_goal_clarification_prompt für LF 1 Leitplanken und Anrede enthält."""
    from sob_generator.llm.sections import SectionDefinition

    sec_lf1 = SectionDefinition(
        index=0,
        id="1",
        title="Psychische Verfassung / Selbstsorge",
        description="Psychische Stabilität, emotionale Verfassung, Selbstfürsorge.",
    )
    prompt = build_field_goal_clarification_prompt(
        section=sec_lf1,
        entries=[],
        anrede="Herr",
        nachname="Peter",
        vorname="Hans",
    )

    assert "Strikte Gegenstandsschärfe für Lebensfeld 1" in prompt
    assert "VERBOT von Lebensfeld-Drift" in prompt
    assert "Herr Peter" in prompt
    assert "Bezeichne die betreute Person im Fließtext und in allen Zielformulierungen durchgehend als „Herr Peter“" in prompt


def test_scrub_vorname_with_name_collision_protects_nachname():
    """Prüft, dass bei Namenskollision (z. B. Vorname 'Hans Peter', Nachname 'Peter') der Nachname nicht überschrieben wird."""
    from sob_generator.llm.prompts import _scrub_vorname

    # Vorname ist 'Hans Peter', Nachname ist 'Peter'
    text = "Hans war gestern spazieren. Peter begleitete ihn nicht, da Peter der Nachname ist."
    scrubbed = _scrub_vorname(text, vorname="Hans Peter", ersatzname="Peter")

    # 'Hans' wird durch 'Peter' ersetzt
    assert "Peter war gestern spazieren." in scrubbed
    # 'Peter' bleibt erhalten und wird nicht gelöscht oder verdoppelt
    assert "Peter begleitete ihn nicht" in scrubbed


def test_sanitize_clinical_terminology_replaces_trivializing_phrases():
    """Prüft, dass verharmlosende Laienfloskeln zuverlässig durch psychiatrische Fachbegriffe ersetzt werden."""
    from sob_generator.llm.generator import sanitize_clinical_terminology

    raw = "Herr Peter war aufgrund akuter Suizidgefahr in einer psychologischen Phase in der Klinik."
    sanitized = sanitize_clinical_terminology(raw)
    assert "psychologischen Phase" not in sanitized
    assert "psychiatrischen Krise" in sanitized

    raw2 = "Es handelte sich um eine psychologische Phase mit anschließender Down-Phase."
    sanitized2 = sanitize_clinical_terminology(raw2)
    assert "psychologische Phase" not in sanitized2
    assert "Down-Phase" not in sanitized2
    assert "psychiatrische Krise" in sanitized2
    assert "depressive Phase" in sanitized2


def test_screening_turn_prompt_mandates_anrede_and_crisis_sensitivity():
    """Prüft, dass Screening-Turn-Prompt Anrede-Konvention und Krisensensibilität instruiert."""
    from sob_generator.llm.prompts import build_screening_turn_prompt
    from sob_generator.llm.sections import SECTION_DEFINITIONS

    prompt = build_screening_turn_prompt(
        message="wohnen ist stabil",
        sections=SECTION_DEFINITIONS,
        active_section_ids={"1.1", "1.4"},
        anrede="Herr",
        nachname="Peter",
        vorname="Hans",
    )

    assert "Anrede-Konvention für die betreute Person: Verwende im Dialog IMMER die volle Anrede „Herr Peter“" in prompt
    assert "Sensible Validierung bei Akutkrisen" in prompt
