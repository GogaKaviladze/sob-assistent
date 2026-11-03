"""Unit-Tests der Abnahme-Diagnostik (sob_generator.draft_diagnostics).

Prüft die maschinell entscheidbaren #36/#37-Signale an synthetischen Berichten
(DSGVO: nur „Mustermann"-Beispieldaten, nie echte Klientendaten). Die Diagnose
ist reine Logik — kein Modell, kein I/O.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sob_generator.draft_diagnostics import (  # noqa: E402
    GateResult,
    bare_surname_hits,
    compare_to_baseline,
    diagnose,
    empty_llm_fields,
    first_sentence,
    genitive_apostrophes,
    has_bullets,
    json_in_fields,
    placeholder_leaks,
)
from sob_generator.models import ClientData, ReportDraft, ReportMeta, ReportSection  # noqa: E402


def _draft(*berichte: str, name: str = "Mustermann") -> ReportDraft:
    return ReportDraft(
        client=ClientData(name=name, vorname="Max", anrede="Herr"),
        meta=ReportMeta(),
        sections=[
            ReportSection(id=f"1.{i}", title=f"Abschnitt {i}", bericht_zielerreichung=text)
            for i, text in enumerate(berichte, start=1)
        ],
    )


def test_has_bullets_erkennt_listenmarker():
    assert has_bullets("Fortschritt:\n- erster Punkt\n- zweiter Punkt")
    assert has_bullets("* Aufzählung")
    assert has_bullets("1. nummeriert")
    assert has_bullets("2) ebenfalls")


def test_has_bullets_ignoriert_fliesstext_mit_bindestrich():
    # Bindestrich im Wort/Satz ist kein Listenmarker (kein Zeilenanfang).
    assert not has_bullets("Die Wohn-Situation hat sich verbessert - deutlich sogar.")


def test_bare_surname_hits_zaehlt_nackten_namen_im_satz():
    # „benötigte Mustermann Unterstützung" → 1 nackter Treffer.
    assert bare_surname_hits("Bei der Pflege benötigte Mustermann Unterstützung.", "Mustermann") == 1


def test_bare_surname_hits_ignoriert_anrede_und_satzanfang():
    assert bare_surname_hits("Herr Mustermann hat Fortschritte gemacht.", "Mustermann") == 0
    assert bare_surname_hits("Mit Herrn Mustermann wurde geübt.", "Mustermann") == 0
    # Satzanfang gilt als korrekt (kein nackter Name mitten im Satz).
    assert bare_surname_hits("Es lief gut. Mustermann übte fleißig.", "Mustermann") == 0


def test_bare_surname_hits_leerer_name_ist_null():
    assert bare_surname_hits("Irgendein Text.", "") == 0


def test_first_sentence_schneidet_am_satzende():
    assert first_sentence("Herr Mustermann übte. Danach Pause.") == "Herr Mustermann übte."
    assert first_sentence("Ohne Satzzeichen") == "Ohne Satzzeichen"


def test_diagnose_opener_kollision_wird_erkannt():
    # Drei Abschnitte mit identischem 3-Wort-Anfang → Kollision (>2).
    collapse = "Herr Mustermann hat sich entwickelt."
    diag = diagnose(_draft(collapse, collapse, collapse, "Im Bereich Wohnen lief es gut."))

    assert diag.max_shared_opener == 3
    assert not diag.opener_ok
    assert "herr mustermann hat" in diag.colliding_openers


def test_diagnose_diverse_opener_ist_ok():
    diag = diagnose(
        _draft(
            "Im Wohnbereich gab es Fortschritte.",
            "Bei der Hauswirtschaft zeigte sich Stabilität.",
            "Sozial war Herr Mustermann aktiver.",
        )
    )
    assert diag.opener_ok
    assert diag.max_shared_opener == 1
    assert diag.colliding_openers == ()


def test_diagnose_bullet_sektionen_werden_gelistet():
    diag = diagnose(
        _draft(
            "Sauberer Fließtext über den Wohnbereich.",
            "Fortschritte:\n- Haushalt\n- Einkauf",
        )
    )
    assert not diag.prose_ok
    assert diag.bullet_sections == ("1.2",)


def test_diagnose_aggregiert_nackte_namen():
    diag = diagnose(
        _draft(
            "Bei der Pflege benötigte Mustermann Unterstützung.",
            "Herr Mustermann übte selbstständig.",
        )
    )
    assert diag.total_bare_surname_hits == 1


# --- Platzhalter-Leak (#34/#38) ---

def test_placeholder_leaks_erkennt_beispiel():
    assert placeholder_leaks("Herr Beispiel hat sich gut entwickelt.") == 1
    assert placeholder_leaks("Beispiel für eine gute Entwicklung.") == 1


def test_placeholder_leaks_ignoriert_teilwort():
    # „Beispielhaft" ist kein Platzhalter-Leak (Teilwort).
    assert placeholder_leaks("Beispielhaft nahm er Termine wahr.") == 0


def test_placeholder_leaks_null_bei_sauberem_text():
    assert placeholder_leaks("Herr Mustermann hat Fortschritte erzielt.") == 0


def test_diagnose_zaehlt_placeholder_leaks():
    diag = diagnose(
        _draft(
            "Herr Beispiel hat sich entwickelt.",
            "Sauberer Fließtext ohne Platzhalter.",
        )
    )
    assert diag.total_placeholder_leaks == 1


# --- Genitiv-Apostroph (#37/#38) ---

def test_genitive_apostrophes_erkennt_ascii_apostroph():
    assert genitive_apostrophes("Musterbanns's Ziel", "Mustermann") == 0
    assert genitive_apostrophes("Mustermann's Ziel wurde erreicht.", "Mustermann") == 1


def test_genitive_apostrophes_erkennt_typografischen_apostroph():
    assert genitive_apostrophes("Mustermann’s Plan", "Mustermann") == 1
    assert genitive_apostrophes("Mustermann‘s Plan", "Mustermann") == 1


def test_genitive_apostrophes_erkennt_nackten_apostroph():
    assert genitive_apostrophes("Mustermann' Wohnung", "Mustermann") == 1


def test_genitive_apostrophes_ignoriert_anderen_namen():
    assert genitive_apostrophes("Müller's Plan war gut.", "Mustermann") == 0


def test_genitive_apostrophes_null_bei_leerem_nachname():
    assert genitive_apostrophes("Mustermann's Text", "") == 0


def test_diagnose_zaehlt_genitive_apostrophes():
    diag = diagnose(
        _draft(
            "Mustermann's Ziel wurde weitgehend erreicht.",
            "Herr Mustermann zeigte Fortschritte.",
        )
    )
    assert diag.total_genitive_apostrophes == 1


# --- compare_to_baseline / GateResult (#38) ---

def _baseline(**kwargs) -> dict:
    return kwargs


def test_compare_to_baseline_pass_wenn_alle_werte_im_limit():
    diag = diagnose(_draft("Im Wohnbereich gab es Fortschritte.", "Soziale Kontakte stabiler."))
    baseline = _baseline(max_shared_opener=2, bullet_sections=0, placeholder_leaks=0, genitive_apostrophes=0)
    result = compare_to_baseline(diag, baseline)
    assert result.passed
    assert result.failures == ()


def test_compare_to_baseline_fail_bei_bullet_regression():
    diag = diagnose(_draft("Fortschritte:\n- Haushalt\n- Einkauf", "Sauberer Text."))
    baseline = _baseline(bullet_sections=0)
    result = compare_to_baseline(diag, baseline)
    assert not result.passed
    assert any(f.name == "bullet_sections" for f in result.failures)


def test_compare_to_baseline_fail_bei_opener_regression():
    collapse = "Herr Mustermann hat sich entwickelt."
    diag = diagnose(_draft(collapse, collapse, collapse))
    baseline = _baseline(max_shared_opener=2)
    result = compare_to_baseline(diag, baseline)
    assert not result.passed
    assert any(f.name == "max_shared_opener" for f in result.failures)


def test_compare_to_baseline_fehlende_schluessel_werden_ignoriert():
    diag = diagnose(_draft("Text A.", "Text B."))
    # bare_surname_hits ist nicht in der Baseline → wird nicht geprüft
    result = compare_to_baseline(diag, _baseline(bullet_sections=0))
    assert result.passed


def test_compare_to_baseline_fail_bei_placeholder_leak():
    diag = diagnose(_draft("Herr Beispiel hat sich entwickelt.", "Sauberer Text."))
    baseline = _baseline(placeholder_leaks=0)
    result = compare_to_baseline(diag, baseline)
    assert not result.passed
    assert any(f.name == "placeholder_leaks" for f in result.failures)


def test_compare_to_baseline_fail_bei_genitive_apostroph():
    diag = diagnose(_draft("Mustermann's Ziel wurde erreicht.", "Sauberer Text."))
    baseline = _baseline(genitive_apostrophes=0)
    result = compare_to_baseline(diag, baseline)
    assert not result.passed
    assert any(f.name == "genitive_apostrophes" for f in result.failures)


# --- Leere Felder & JSON-in-Feldern ---

def _section_with(
    bericht: str = "Guter Fließtext.",
    ziele: str = "Ziel erreicht.",
    massnahmen: str = "Maßnahme durchgeführt.",
    realisierung: str = "",
    needs_review: bool = False,
) -> ReportSection:
    return ReportSection(
        id="1.1",
        title="Wohnsituation",
        bericht_zielerreichung=bericht,
        vorschlag_ziele=ziele,
        vorschlag_massnahmen=massnahmen,
        realisierung_andere=realisierung,
        needs_review=needs_review,
    )


def test_empty_llm_fields_erkennt_leeres_pflichtfeld():
    sec = _section_with(bericht="", ziele="", massnahmen="Maßnahme.")
    result = empty_llm_fields(sec)
    assert "bericht_zielerreichung" in result
    assert "vorschlag_ziele" in result
    assert "vorschlag_massnahmen" not in result


def test_empty_llm_fields_ignoriert_realisierung_andere():
    sec = _section_with(realisierung="")
    result = empty_llm_fields(sec)
    assert "realisierung_andere" not in result


def test_empty_llm_fields_leer_bei_vollstaendigem_abschnitt():
    sec = _section_with()
    assert empty_llm_fields(sec) == ()


def test_json_in_fields_erkennt_json_array():
    sec = _section_with(massnahmen='[{"id": 1, "beschreibung": "Begleitung"}]')
    result = json_in_fields(sec)
    assert "vorschlag_massnahmen" in result


def test_json_in_fields_erkennt_json_objekt():
    sec = _section_with(bericht='{"key": "value"}')
    result = json_in_fields(sec)
    assert "bericht_zielerreichung" in result


def test_json_in_fields_leer_bei_normalem_text():
    sec = _section_with()
    assert json_in_fields(sec) == ()


def test_diagnose_zaehlt_leere_felder():
    draft = ReportDraft(
        client=ClientData(name="Mustermann", vorname="Max", anrede="Herr"),
        meta=ReportMeta(),
        sections=[
            ReportSection(id="1.1", title="A", bericht_zielerreichung="Text.", vorschlag_ziele="", vorschlag_massnahmen=""),
            ReportSection(id="1.2", title="B", bericht_zielerreichung="Text.", vorschlag_ziele="Ziel.", vorschlag_massnahmen="Maßnahme."),
        ],
    )
    diag = diagnose(draft)
    assert diag.total_empty_fields == 2
    assert not diag.critical_ok


def test_diagnose_zaehlt_json_felder():
    draft = ReportDraft(
        client=ClientData(name="Mustermann", vorname="Max", anrede="Herr"),
        meta=ReportMeta(),
        sections=[
            ReportSection(id="1.1", title="A", bericht_zielerreichung="Text.", vorschlag_ziele="Ziel.", vorschlag_massnahmen='[{"id":1}]'),
        ],
    )
    diag = diagnose(draft)
    assert diag.total_json_fields == 1
    assert not diag.critical_ok


def test_diagnose_needs_review_wird_gezaehlt():
    draft = ReportDraft(
        client=ClientData(name="Mustermann", vorname="Max", anrede="Herr"),
        meta=ReportMeta(),
        sections=[
            ReportSection(id="1.1", title="A", bericht_zielerreichung="Text.", needs_review=True),
            ReportSection(id="1.2", title="B", bericht_zielerreichung="Text.", needs_review=False),
        ],
    )
    diag = diagnose(draft)
    assert diag.total_needs_review == 1


def test_diagnose_critical_ok_bei_vollstaendigem_bericht():
    draft = ReportDraft(
        client=ClientData(name="Mustermann", vorname="Max", anrede="Herr"),
        meta=ReportMeta(),
        sections=[
            ReportSection(
                id="1.1", title="A",
                bericht_zielerreichung="Guter Fließtext.",
                vorschlag_ziele="Ziel A.",
                vorschlag_massnahmen="Maßnahme A.",
                needs_review=False,
            ),
        ],
    )
    diag = diagnose(draft)
    assert diag.critical_ok


def test_compare_to_baseline_fail_bei_empty_fields():
    draft = ReportDraft(
        client=ClientData(name="Mustermann", vorname="Max", anrede="Herr"),
        meta=ReportMeta(),
        sections=[ReportSection(id="1.1", title="A", bericht_zielerreichung="", vorschlag_ziele="", vorschlag_massnahmen="")],
    )
    diag = diagnose(draft)
    result = compare_to_baseline(diag, _baseline(empty_fields=0))
    assert not result.passed
    assert any(f.name == "empty_fields" for f in result.failures)


def test_compare_to_baseline_fail_bei_json_fields():
    draft = ReportDraft(
        client=ClientData(name="Mustermann", vorname="Max", anrede="Herr"),
        meta=ReportMeta(),
        sections=[ReportSection(id="1.1", title="A", bericht_zielerreichung="Text.", vorschlag_ziele="Ziel.", vorschlag_massnahmen='[{"id":1}]')],
    )
    diag = diagnose(draft)
    result = compare_to_baseline(diag, _baseline(json_fields=0))
    assert not result.passed
    assert any(f.name == "json_fields" for f in result.failures)


# --- Datenlage vs. LLM-Defekt (#50/K1) ---

def test_no_entries_abschnitte_zaehlen_nicht_als_llm_defekt():
    draft = ReportDraft(
        client=ClientData(name="Mustermann", vorname="Max", anrede="Herr"),
        meta=ReportMeta(),
        sections=[
            # Guard-Fall: leer, weil Pass 0 keine Einträge zuordnete.
            ReportSection(id="1.1", title="A", needs_review=True, no_entries=True),
            # Echter Fehlschlag: leer trotz Einträgen.
            ReportSection(id="1.2", title="B", needs_review=True, no_entries=False),
            ReportSection(id="1.3", title="C", bericht_zielerreichung="Text.", vorschlag_ziele="Z.", vorschlag_massnahmen="M."),
        ],
    )
    diag = diagnose(draft)
    assert diag.no_entries_sections == ("1.1",)
    # Nur der echte Fehlschlag zählt in die kritischen Metriken.
    assert diag.total_needs_review == 1
    assert diag.total_empty_fields == 3
    assert not diag.critical_ok


def test_no_entries_allein_blockiert_critical_ok_nicht():
    draft = ReportDraft(
        client=ClientData(name="Mustermann", vorname="Max", anrede="Herr"),
        meta=ReportMeta(),
        sections=[
            ReportSection(id="1.1", title="A", needs_review=True, no_entries=True),
            ReportSection(id="1.2", title="B", bericht_zielerreichung="Text.", vorschlag_ziele="Z.", vorschlag_massnahmen="M."),
        ],
    )
    diag = diagnose(draft)
    assert diag.total_empty_fields == 0
    assert diag.total_needs_review == 0
    assert diag.critical_ok


# --- Pre-Export Quality Gate (#55) ---

def test_vorname_leaks_erkennt_vornamen():
    from sob_generator.draft_diagnostics import vorname_leaks

    assert vorname_leaks("Max erledigte die Aufgaben eigenständig.", "Max") == 1
    assert vorname_leaks("Hans ging zum Termin und Peter auch.", "Hans Peter") == 2
    assert vorname_leaks("Hans Peter ging zum Termin.", "Hans Peter") >= 1
    assert vorname_leaks("Maximaler Erfolg wurde erzielt.", "Max") == 0
    assert vorname_leaks("Herr Mustermann war anwesend.", "Max") == 0


def test_bracket_placeholders_erkennt_stubs():
    from sob_generator.draft_diagnostics import bracket_placeholders

    assert bracket_placeholders("[STUB] Entwurf für Wohnen.") == 1
    assert bracket_placeholders("Text mit [TODO] und [...] Marker.") == 2
    assert bracket_placeholders("Normaler Fließtext ohne Klammern.") == 0


def test_quality_gate_all_passed_and_badges():
    draft = ReportDraft(
        client=ClientData(name="Mustermann", vorname="Max", anrede="Herr"),
        meta=ReportMeta(),
        sections=[
            ReportSection(
                id="1.1",
                title="Wohnen",
                bericht_zielerreichung="Herr Mustermann hielt seine Wohnung sauber und ordentlich.",
                vorschlag_ziele="Herr Mustermann führt Haushaltsaufgaben selbstständig durch.",
                vorschlag_massnahmen="Die Assistenz unterstützt bei Bedarf.",
            )
        ],
    )
    diag = diagnose(draft)
    assert diag.gdpr_ok is True
    assert diag.completeness_ok is True
    assert diag.placeholders_ok is True
    assert diag.prose_ok is True
    assert diag.all_passed is True

    badges = diag.quality_badges
    assert len(badges) == 4
    badge_ids = {b["id"] for b in badges}
    assert badge_ids == {"gdpr", "completeness", "placeholders", "prose"}
    assert all(b["passed"] for b in badges)


def test_quality_gate_fails_on_gdpr_leak():
    draft = ReportDraft(
        client=ClientData(name="Mustermann", vorname="Max", anrede="Herr"),
        meta=ReportMeta(),
        sections=[
            ReportSection(
                id="1.1",
                title="Wohnen",
                bericht_zielerreichung="Max hielt seine Wohnung sauber.",
                vorschlag_ziele="Ziel.",
                vorschlag_massnahmen="Maßnahme.",
            )
        ],
    )
    diag = diagnose(draft)
    assert diag.gdpr_ok is False
    assert diag.total_vorname_leaks == 1
    assert diag.all_passed is False

