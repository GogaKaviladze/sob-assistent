"""Tests für ADR-11: Grammar, Prompt-Templates, Parsing/Validierung.

Ohne lokales GGUF-Modell wird `LLMDraftGenerator._complete` gemockt — die
eigentliche Inferenz ist Gegenstand von #11 (Modellauswahl) und #12
(Acceptance-Tests).

Weg C (#38): ANTI_TEMPLATE_INSTRUCTION und GROUNDING_INSTRUCTION wurden
zurückgenommen. Die zugehörigen Tests wurden entfernt; das neue
tests/test_prompts.py sichert den Weg-C-Zustand.
"""
import json
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sob_generator.llm.generator import (
    DEFAULT_REPEAT_PENALTY,
    LLMDraftGenerator,
    _validate,
)
from sob_generator.llm.grammar import build_grammar
from sob_generator.llm.prompts import (
    _select_section_example,
    _section_few_shot,
    _developments_few_shot,
    build_classification_prompt,
    build_developments_prompt,
    build_section_prompt,
)
from sob_generator.llm.schemas import (
    BatchEntryClassification,
    DevelopmentsLLMOutput,
    DocumentationEntry,
    EntryClassification,
    SectionLLMOutput,
)
from sob_generator.llm.sections import SECTION_DEFINITIONS


VALID_SECTION_JSON = (
    '{"bericht_zielerreichung": "Bericht.", "vorschlag_ziele": "Ziele.", '
    '"vorschlag_massnahmen": "Maßnahmen.", "realisierung_andere": ""}'
)
VALID_DEVELOPMENTS_JSON = '{"developments": "Entwicklung."}'


# --- Grammar -----------------------------------------------------------


def test_grammar_builds_for_section_output():
    grammar = build_grammar(SectionLLMOutput)
    assert "bericht_zielerreichung" in grammar._grammar
    assert "vorschlag_ziele" in grammar._grammar
    assert "vorschlag_massnahmen" in grammar._grammar
    assert "realisierung_andere" in grammar._grammar


def test_grammar_builds_for_developments_output():
    grammar = build_grammar(DevelopmentsLLMOutput)
    assert "developments" in grammar._grammar


def test_grammar_builds_for_entry_classification():
    grammar = build_grammar(EntryClassification)
    assert "section_id" in grammar._grammar
    assert "1.1" in grammar._grammar
    assert "unklar" in grammar._grammar


def test_grammar_builds_for_batch_entry_classification():
    grammar = build_grammar(BatchEntryClassification)
    assert "classifications" in grammar._grammar
    assert "section_id" in grammar._grammar
    assert "1.1" in grammar._grammar



# --- Prompt-Templates ----------------------------------------------------


def test_section_prompt_contains_context():
    section = SECTION_DEFINITIONS[3]  # 1.4 Emotionale und psychische Entwicklung
    entries = [DocumentationEntry(datum="01.01.2026", kategorie="w_Test", text="Eintrag A")]

    prompt = build_section_prompt(
        section=section,
        entries=entries,
        leitziel="Altes Leitziel",
        smarte_ziele="Alte smarte Ziele",
        previous_goals_apply=True,
        freitext="Freitext der Fachkraft",
    )

    assert "1.4 Emotionale und psychische Entwicklung" in prompt
    assert "Altes Leitziel" in prompt
    assert "Alte smarte Ziele" in prompt
    assert "01.01.2026 | Eintrag A" in prompt
    assert "Freitext der Fachkraft" in prompt
    # Few-Shot-Beispiel ist enthalten
    assert "bericht_zielerreichung" in prompt


def test_section_prompt_previous_goals_not_applicable():
    section = SECTION_DEFINITIONS[0]
    prompt = build_section_prompt(section=section, entries=[], previous_goals_apply=False)

    assert "entfällt" in prompt
    aufgabe = prompt.split("## Aufgabe")[1]
    assert "Bisheriges Leitziel" not in aufgabe


def test_developments_prompt_contains_context():
    entries = [DocumentationEntry(datum="02.02.2026", kategorie="w_Kontakt", text="Eintrag B")]
    prompt = build_developments_prompt(
        entries=entries, previous_goals_summary="Zusammenfassung", freitext="Notiz"
    )

    assert "02.02.2026 | Eintrag B" in prompt
    assert "Zusammenfassung" in prompt
    assert "Notiz" in prompt
    assert '"developments"' in prompt


# --- Few-Shot-Beispiel (#38 Weg C: ein fixes Beispiel) -------------------
#
# Weg C: die Rotation über drei Distraktoren-Beispiele wurde zurückgenommen.
# Alle 9 Abschnitte bekommen das gleiche schlanke Beispiel (SECTION_FEW_SHOT_SITUATION).

FELDNAMEN = {
    "bericht_zielerreichung",
    "vorschlag_ziele",
    "vorschlag_massnahmen",
    "realisierung_andere",
}


def _example_block(prompt: str) -> str:
    """Der Beispielteil eines Section-Prompts (zwischen den Abschnittsmarkern)."""
    return prompt.split("## Beispiel", 1)[1].split("## Aufgabe", 1)[0]


def test_section_beispiel_ist_synthetisch_vollstaendig_und_ohne_klient():
    # #34: Platzhaltername "Herr Muster" ersetzt durch echte Bezeichnung.
    ex = _section_few_shot("Herr", "Mustermann")
    text = ex.eingabe + ex.ausgabe
    assert not re.search(r"\bMuster\b", text), "Platzhaltername 'Muster' noch im Few-Shot"
    assert "Herr Mustermann" in text
    assert "Klient" not in text
    assert "Herrn Mustermann" in ex.ausgabe
    data = json.loads(ex.ausgabe)
    assert set(data.keys()) == FELDNAMEN


def test_kein_few_shot_beispiel_eroeffnet_namens_erst():
    # #36: das Modell darf kein namens-erstes Vorbild für die Bericht-Eröffnung sehen.
    ex = _section_few_shot("Herr", "Mustermann")
    eroeffnung = json.loads(ex.ausgabe)["bericht_zielerreichung"]
    assert not eroeffnung.startswith("Herr Mustermann")


def test_section_beispiel_auswahl_liefert_fuer_alle_abschnitte_dasselbe():
    # Weg C: alle 9 Abschnitte bekommen dasselbe Beispiel (#34: mit echter Bezeichnung).
    first = _select_section_example(SECTION_DEFINITIONS[0], "Herr", "Mustermann")
    for section in SECTION_DEFINITIONS:
        gewaehlt = _select_section_example(section, "Herr", "Mustermann")
        assert gewaehlt == first
        block = _example_block(
            build_section_prompt(section=section, entries=[], anrede="Herr", nachname="Mustermann")
        )
        assert gewaehlt.eingabe in block
        assert gewaehlt.ausgabe in block


def test_few_shot_konstanten_enthalten_kein_klient():
    # Die Beispiele dürfen das Modell nicht zurück Richtung "Klient" ziehen.
    dev_input, dev_output = _developments_few_shot("Herr", "Mustermann")
    ex = _section_few_shot("Herr", "Mustermann")
    for text in [dev_input, dev_output, ex.eingabe, ex.ausgabe]:
        assert "Klient" not in text


def test_situation_beispiel_hat_mehrere_eintraege():
    # Ausreichend Material für Auswahl-Demonstration.
    # Format ist [N] datum | text (nummeriert, kein "- ").
    ex = _section_few_shot("Herr", "Mustermann")
    block = ex.eingabe.split("Dokumentationseinträge:", 1)[1]
    eintraege = [z for z in block.splitlines() if z.startswith("[")]
    assert len(eintraege) >= 3


# --- Scrubbing vermeidet kasus-falsche Anrede im Kontext (#36) ----------

def test_scrubbing_vermeidet_kasus_falsche_anrede_im_kontext():
    section = SECTION_DEFINITIONS[0]
    entries = [
        DocumentationEntry(
            datum="01.01.2026", kategorie="w_Begleitung",
            text="Termin gemeinsam mit Max vereinbart.",
        )
    ]
    prompt = build_section_prompt(
        section=section, entries=entries, anrede="Herr", nachname="Mustermann",
        vorname="Max",
    )

    aufgabe = prompt.split("## Aufgabe")[1]
    assert "mit Mustermann vereinbart" in aufgabe
    assert "mit Herr Mustermann" not in aufgabe


# --- Anrede im Fließtext (#27) -------------------------------------------


def test_section_prompt_anrede_herr_mit_nachname():
    section = SECTION_DEFINITIONS[0]
    prompt = build_section_prompt(section=section, entries=[], anrede="Herr", nachname="Müller")

    assert "Herr Müller" in prompt
    assert "Klient" not in prompt
    assert "der Klient" not in prompt
    assert "die Klientin" not in prompt


def test_section_prompt_anrede_frau_mit_nachname():
    section = SECTION_DEFINITIONS[0]
    prompt = build_section_prompt(section=section, entries=[], anrede="Frau", nachname="Schmidt")

    assert "Frau Schmidt" in prompt
    assert "Klient" not in prompt


def test_section_prompt_anrede_keine_nutzt_nur_nachnamen():
    section = SECTION_DEFINITIONS[0]
    prompt = build_section_prompt(section=section, entries=[], anrede="", nachname="Müller")

    assert "Müller" in prompt
    assert "Herr Müller" not in prompt
    assert "Frau Müller" not in prompt
    assert "Klient" not in prompt


def test_developments_prompt_nutzt_anrede():
    prompt = build_developments_prompt(entries=[], anrede="Frau", nachname="Schmidt")

    assert "Frau Schmidt" in prompt
    assert "Klient" not in prompt


# --- Vornamen-Leak: Scrubbing vor dem Prompt-Bau (#32) -------------------


def test_section_prompt_ersetzt_vornamen_in_eintraegen():
    section = SECTION_DEFINITIONS[0]
    entries = [
        DocumentationEntry(
            datum="05.02.2026", kategorie="w_Hausbesuch",
            text="Max hat den Abwasch eigenständig erledigt.",
        )
    ]
    prompt = build_section_prompt(
        section=section, entries=entries, anrede="Herr", nachname="Mustermann",
        vorname="Max",
    )

    aufgabe = prompt.split("## Aufgabe")[1]
    assert "Mustermann hat den Abwasch" in aufgabe
    assert "Herr Mustermann" not in aufgabe
    assert not re.search(r"\bMax\b", aufgabe)


def test_section_prompt_ersetzt_vornamen_im_freitext():
    section = SECTION_DEFINITIONS[0]
    prompt = build_section_prompt(
        section=section, entries=[], anrede="Frau", nachname="Schmidt",
        vorname="Anna", freitext="Anna wirkt zuletzt stabiler.",
    )

    aufgabe = prompt.split("## Aufgabe")[1]
    assert "Schmidt wirkt zuletzt stabiler" in aufgabe
    assert "Frau Schmidt" not in aufgabe
    assert not re.search(r"\bAnna\b", aufgabe)


def test_scrubbing_nur_ganze_woerter():
    section = SECTION_DEFINITIONS[0]
    entries = [
        DocumentationEntry(
            datum="01.01.2026", kategorie="w_Test",
            text="Maximale Belastung für Max, danach war Maxim erschöpft.",
        )
    ]
    prompt = build_section_prompt(
        section=section, entries=entries, anrede="Herr", nachname="Mustermann",
        vorname="Max",
    )

    aufgabe = prompt.split("## Aufgabe")[1]
    assert "Maximale Belastung" in aufgabe
    assert "war Maxim erschöpft" in aufgabe
    assert "für Mustermann, danach" in aufgabe
    assert not re.search(r"\bMax\b", aufgabe)


def test_scrubbing_mehrteiliger_vorname():
    section = SECTION_DEFINITIONS[0]
    entries = [
        DocumentationEntry(
            datum="01.01.2026", kategorie="w_Test",
            text="Hans Peter kam pünktlich; später rief Hans noch an.",
        )
    ]
    prompt = build_section_prompt(
        section=section, entries=entries, anrede="Herr", nachname="Mustermann",
        vorname="Hans Peter",
    )

    aufgabe = prompt.split("## Aufgabe")[1]
    assert "Mustermann kam pünktlich" in aufgabe
    assert "rief Mustermann noch an" in aufgabe
    assert "Mustermann Mustermann" not in aufgabe
    assert not re.search(r"\b(?:Hans|Peter)\b", aufgabe)


def test_scrubbing_bei_leerer_anrede_nutzt_nachnamen():
    section = SECTION_DEFINITIONS[0]
    entries = [
        DocumentationEntry(datum="01.01.2026", kategorie="w_Test", text="Max war da.")
    ]
    prompt = build_section_prompt(
        section=section, entries=entries, anrede="", nachname="Mustermann",
        vorname="Max",
    )

    aufgabe = prompt.split("## Aufgabe")[1]
    assert "Mustermann war da" in aufgabe
    assert not re.search(r"\bMax\b", aufgabe)


def test_kein_scrubbing_ohne_nachnamen():
    section = SECTION_DEFINITIONS[0]
    entries = [
        DocumentationEntry(datum="01.01.2026", kategorie="w_Test", text="Max war da.")
    ]
    prompt = build_section_prompt(
        section=section, entries=entries, anrede="", nachname="", vorname="Max",
    )

    assert "Max war da" in prompt.split("## Aufgabe")[1]


def test_developments_prompt_ersetzt_vornamen():
    entries = [
        DocumentationEntry(datum="01.01.2026", kategorie="w_Test", text="Max war stabil.")
    ]
    prompt = build_developments_prompt(
        entries=entries, anrede="Herr", nachname="Mustermann", vorname="Max",
    )

    aufgabe = prompt.split("## Aufgabe")[1]
    assert "Mustermann war stabil" in aufgabe
    assert "Herr Mustermann" not in aufgabe
    assert not re.search(r"\bMax\b", aufgabe)


# --- Opener-Ban-Direktive des #36-Guards ---------------------------------


def test_prompt_ohne_forbidden_openers_traegt_keine_ban_direktive():
    section = SECTION_DEFINITIONS[0]
    prompt = build_section_prompt(section=section, entries=[])
    assert "beginnen bereits" not in prompt


def test_prompt_mit_forbidden_openers_verbietet_die_eroeffnungen_woertlich():
    section = SECTION_DEFINITIONS[0]
    prompt = build_section_prompt(
        section=section,
        entries=[],
        forbidden_openers=("herr mustermann hat", "im mittelpunkt des"),
    )
    assert "herr mustermann hat" in prompt
    assert "im mittelpunkt des" in prompt
    assert "deutlich anders" in prompt


def test_leere_forbidden_openers_erzeugen_keine_direktive():
    section = SECTION_DEFINITIONS[0]
    prompt = build_section_prompt(
        section=section, entries=[], forbidden_openers=("", "   ")
    )
    assert "beginnen bereits" not in prompt


# --- Klassifikations-Prompt (Pass 0) ------------------------------------


def test_classification_prompt_enthaelt_eintrag_und_abschnitte():
    entry = DocumentationEntry(datum="01.01.2026", kategorie="w_Test", text="Eintrag X")
    prompt = build_classification_prompt(entry, SECTION_DEFINITIONS)
    assert "01.01.2026" in prompt
    assert "Eintrag X" in prompt
    assert "1.1" in prompt
    assert "Wohnsituation" in prompt
    assert "unklar" in prompt


def test_classification_prompt_ist_kategorie_blind():
    """Pass 0 darf weder das rohe w_-Label noch eine vorgefertigte
    Zuordnungstabelle sehen — Klassifikation erfolgt rein inhaltsbasiert."""
    entry = DocumentationEntry(datum="01.01.2026", kategorie="w_Test", text="Eintrag X")
    prompt = build_classification_prompt(entry, SECTION_DEFINITIONS)
    assert "w_Test" not in prompt
    assert "Kategorie" not in prompt
    assert "Zuordnungsregeln" not in prompt


def test_developments_prompt_ist_kategorie_blind():
    """Auch der Entwicklungen-Prompt darf das rohe w_-Label nicht sehen —
    Freitext-Generierung erfolgt rein inhaltsbasiert wie bei Pass 0."""
    entries = [DocumentationEntry(datum="01.01.2026", kategorie="w_Test", text="Eintrag X")]
    prompt = build_developments_prompt(entries=entries, anrede="Herr", nachname="Muster")
    assert "w_Test" not in prompt


def test_classify_entry_gibt_valide_section_id_zurueck():
    gen = _generator()
    entry = DocumentationEntry(datum="01.01.2026", kategorie="w_Test", text="Termin")
    valid_json = '{"section_id": "1.4"}'

    with patch.object(gen, "_complete", return_value=valid_json):
        sid = gen.classify_entry(entry, SECTION_DEFINITIONS)

    assert sid == "1.4"


def test_classify_entry_akzeptiert_unklar():
    gen = _generator()
    entry = DocumentationEntry(datum="01.01.2026", kategorie="w_Test", text="Sonstiges")
    valid_json = '{"section_id": "unklar"}'

    with patch.object(gen, "_complete", return_value=valid_json):
        sid = gen.classify_entry(entry, SECTION_DEFINITIONS)

    assert sid == "unklar"


# --- Validierung ----------------------------------------------------------


def test_validate_accepts_valid_json():
    result = _validate(VALID_SECTION_JSON, SectionLLMOutput)
    assert result.bericht_zielerreichung == "Bericht."
    assert result.realisierung_andere == ""


def test_validate_rejects_invalid_json():
    with pytest.raises(ValueError):
        _validate("kein json", SectionLLMOutput)


def test_validate_rejects_missing_field():
    with pytest.raises(ValueError):
        _validate('{"bericht_zielerreichung": "Bericht."}', SectionLLMOutput)


def test_validate_rejects_empty_bericht():
    with pytest.raises(ValueError):
        _validate(
            VALID_SECTION_JSON.replace('"Bericht."', '""'), SectionLLMOutput
        )


@pytest.mark.parametrize("feld", ["vorschlag_ziele", "vorschlag_massnahmen"])
def test_validate_rejects_leere_vorschlag_felder(feld):
    # #48: "" war für die Vorschlag-Pflichtfelder valide → der Retry in
    # _generate wurde nie ausgelöst, leere Vorschläge landeten ohne
    # needs_review-Alarm im Draft.
    data = json.loads(VALID_SECTION_JSON)
    data[feld] = "  "
    with pytest.raises(ValueError):
        _validate(json.dumps(data), SectionLLMOutput)


def test_leeres_vorschlag_feld_loest_retry_aus():
    # #48: erster Versuch mit leerem vorschlag_ziele → Retry; zweiter Versuch
    # vollständig → kein needs_review.
    gen = _generator()
    invalid = json.loads(VALID_SECTION_JSON)
    invalid["vorschlag_ziele"] = ""

    with patch.object(
        gen, "_complete", side_effect=[json.dumps(invalid), VALID_SECTION_JSON]
    ) as mock_complete:
        result = gen.generate_section(SECTION_DEFINITIONS[0], entries=[])

    assert mock_complete.call_count == 2
    assert result.needs_review is False
    assert result.value.vorschlag_ziele == "Ziele."
    retry_prompt = mock_complete.call_args_list[1].args[0]
    assert "vorschlag_ziele" in retry_prompt  # Reminder benennt die Felder


# --- Generator: Retry- und Fallback-Verhalten ------------------------------


def _generator():
    return LLMDraftGenerator(model_path="dummy.gguf")


def test_generate_section_success_first_try():
    gen = _generator()
    section = SECTION_DEFINITIONS[0]

    with patch.object(gen, "_complete", return_value=VALID_SECTION_JSON) as mock_complete:
        result = gen.generate_section(section, entries=[])

    assert result.needs_review is False
    assert result.error is None
    assert result.value.bericht_zielerreichung == "Bericht."
    mock_complete.assert_called_once()


def test_generate_section_retries_then_succeeds():
    gen = _generator()
    section = SECTION_DEFINITIONS[0]

    with patch.object(
        gen, "_complete", side_effect=["nicht json", VALID_SECTION_JSON]
    ) as mock_complete:
        result = gen.generate_section(section, entries=[])

    assert result.needs_review is False
    assert result.error is None
    assert result.raw_output == VALID_SECTION_JSON
    assert mock_complete.call_count == 2
    retry_prompt = mock_complete.call_args_list[1].args[0]
    assert "gültiges JSON" in retry_prompt


def test_generate_section_falls_back_to_empty_after_two_failures():
    gen = _generator()
    section = SECTION_DEFINITIONS[0]

    with patch.object(
        gen, "_complete", side_effect=["nicht json", "immer noch nicht json"]
    ) as mock_complete:
        result = gen.generate_section(section, entries=[])

    assert result.needs_review is True
    assert result.error is not None
    assert result.value.bericht_zielerreichung == ""
    assert result.value.vorschlag_ziele == ""
    assert result.value.vorschlag_massnahmen == ""
    assert result.value.realisierung_andere == ""
    assert mock_complete.call_count == 2


def test_generate_developments_success():
    gen = _generator()

    with patch.object(gen, "_complete", return_value=VALID_DEVELOPMENTS_JSON):
        result = gen.generate_developments(entries=[])

    assert result.needs_review is False
    assert result.value.developments == "Entwicklung."


# --- repeat_penalty gegen Wiederholungsschleifen (#19) ---------------------


def _fake_llm_returning(text: str) -> MagicMock:
    fake = MagicMock(return_value={"choices": [{"text": text}]})
    # n_ctx-Guard (#50/K5) tokenisiert den Prompt — Fake muss das bedienen.
    fake.tokenize.return_value = [1, 2, 3]
    fake.n_ctx.return_value = 8192
    return fake


def test_complete_reicht_repeat_penalty_an_llama_durch():
    gen = LLMDraftGenerator(model_path="dummy.gguf")
    gen._llm = _fake_llm_returning(VALID_SECTION_JSON)

    gen.generate_section(SECTION_DEFINITIONS[0], entries=[])

    assert gen._llm.call_args.kwargs["repeat_penalty"] == DEFAULT_REPEAT_PENALTY


def test_repeat_penalty_default_liegt_im_sinnvollen_bereich():
    assert 1.1 < DEFAULT_REPEAT_PENALTY <= 1.3


def test_repeat_penalty_ist_konfigurierbar():
    gen = LLMDraftGenerator(model_path="dummy.gguf", repeat_penalty=1.3)
    gen._llm = _fake_llm_returning(VALID_SECTION_JSON)

    gen.generate_section(SECTION_DEFINITIONS[0], entries=[])

    assert gen._llm.call_args.kwargs["repeat_penalty"] == 1.3


def test_developments_call_nutzt_repeat_penalty():
    gen = LLMDraftGenerator(model_path="dummy.gguf", repeat_penalty=1.25)
    gen._llm = _fake_llm_returning(VALID_DEVELOPMENTS_JSON)

    gen.generate_developments(entries=[])

    assert gen._llm.call_args.kwargs["repeat_penalty"] == 1.25
