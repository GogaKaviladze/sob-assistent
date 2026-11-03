"""End-to-End-Durchstich mit Stub-LLM (Issue #17, DoD).

Verkettet Import → 10 Stub-Calls → docx-Render gegen ausschließlich committete
Daten und liest die befüllten Felder aus der erzeugten docx zurück.
"""
import zipfile
from datetime import date

import pytest
from pathlib import Path

from sob_generator import docx_fields as fields
from sob_generator.llm.generator import GenerationResult
from sob_generator.llm.schemas import DevelopmentsLLMOutput, DocumentationEntry, SectionLLMOutput
from sob_generator.llm.sections import SECTION_DEFINITIONS
from sob_generator.importer import VivendiImport
from sob_generator.models import ClientData, ReportMeta
from sob_generator.orchestrator import (
    InvalidPseudonymError,
    build_draft,
    generate_sob,
    output_filename,
    unique_output_path,
)
from sob_generator.renderer import NO_ENTRIES_HINT, REVIEW_MARKER
from sob_generator.stub_generator import StubDraftGenerator

ROOT = Path(__file__).resolve().parents[1]
VORLAGE_CSV = ROOT / "templates" / "Vivendi" / "Vivendi CSV Vorlage.csv"
TEMPLATE_DOCX = ROOT / "templates" / "hamburg" / "Vorlage.docx"


def test_generate_sob_produces_filled_docx(tmp_path):
    from sob_generator.importer import VivendiPDImporter
    csv_entries = VivendiPDImporter(VORLAGE_CSV).load().entries
    stub = StubDraftGenerator()
    # Die Vorlage-CSV hat genau einen Eintrag — der Stub klassifiziert ihn
    # deterministisch (hashlib) auf einen festen Abschnitt; welchen, ermitteln
    # wir hier dynamisch statt ihn hartzukodieren.
    assert len(csv_entries) == 1
    target = stub.classify_entry(csv_entries[0], SECTION_DEFINITIONS)
    target_index = next(i for i, s in enumerate(SECTION_DEFINITIONS) if s.id == target)

    result = generate_sob(
        csv_path=VORLAGE_CSV,
        generator=stub,
        section_definitions=SECTION_DEFINITIONS,
        template_path=TEMPLATE_DOCX,
        output_dir=tmp_path,
        leistungsart="ASP",
    )
    out_path = result.path

    # Abschnitte ohne CSV-Einträge werden durch den Guard als needs_review
    # markiert (kein LLM-Call, kein Freitext). Die Vorlage-CSV hat nur einen
    # Eintrag — alle anderen Abschnitte sind erwartungsgemäß flagged.
    assert "developments" not in result.pending_review  # Stub-Call läuft
    assert target not in result.pending_review  # hat Eintrag → Stub-Call lief
    assert out_path.exists()
    assert out_path.name.startswith("Fall_")
    assert "Vorname" not in out_path.name
    assert "Nachname" not in out_path.name
    assert out_path.name.endswith("_SOB.docx")

    root = fields.load_document_tree(out_path)

    # Stammdaten aus dem Importer.
    # #50/K3: Feld #1 „Bericht für (Name, Vorname)" wird aus name/vorname gefügt,
    # die Leistungsart kommt vom Aufrufer (CLI-Flag/Formular, nicht CSV).
    assert fields.get_formtext(root, "client_name_vorname") == "Nachname, Vorname"
    assert fields.get_formtext(root, "client_leistungsart") == "ASP"
    assert fields.get_formtext(root, "client_geburtsdatum") == "01.01.1987"
    assert fields.get_formtext(root, "meta_leistungszeitraum_von") == "30.01.2026"
    assert fields.get_formtext(root, "meta_leistungszeitraum_bis") == "31.05.2026"

    # developments läuft immer (alle Einträge) → Stub-Output vorhanden.
    assert "[STUB]" in fields.get_formtext(root, "developments")
    # Zielabschnitt hat den einzigen Eintrag der Vorlage-CSV → Stub-Call.
    assert "[STUB]" in fields.get_formtext(root, f"sections_{target_index}_bericht_zielerreichung")
    # Ein Abschnitt ohne Eintrag → Guard → Datenlage-Hinweis statt
    # Fehlermarker (#50/K1): kein Fehlschlag, es gab schlicht keine Doku.
    empty_index = next(i for i in range(len(SECTION_DEFINITIONS)) if i != target_index)
    assert NO_ENTRIES_HINT in fields.get_formtext(root, f"sections_{empty_index}_bericht_zielerreichung")

    # Listenfeld (vorschlag_ziele): 1. Bullet liegt im Bookmark-Absatz,
    # weitere Bullets in Folge-Absätzen (set_formtext_list, ADR-10).
    assert "Ziel A" in fields.get_formtext(root, f"sections_{target_index}_vorschlag_ziele")
    with zipfile.ZipFile(out_path) as z:
        document_xml = z.read("word/document.xml").decode("utf-8")
    assert "Ziel B" in document_xml  # zweiter Aufzählungspunkt


def test_output_filename_format():
    # Standard: weder Vorname noch Nachname, neutrales Token 'Fall' (Issue #115)
    default_name = output_filename()
    assert default_name.startswith("Fall_")
    assert default_name.endswith("_SOB.docx")
    assert "Vorname" not in default_name
    assert "Nachname" not in default_name
    assert "Max" not in default_name
    assert "Mustermann" not in default_name

    # Explizites Pseudonym
    assert output_filename(pseudonym="Fall-42").startswith("Fall-42_")
    assert output_filename(pseudonym="Fall-42").endswith("_SOB.docx")

    # Leer oder reiner Whitespace fällt auf 'Fall' zurück
    assert output_filename(pseudonym="").startswith("Fall_")
    assert output_filename(pseudonym="   ").startswith("Fall_")

    # Erlaubte Zeichen: A-Z a-z 0-9 _ -  (kein stilles Bereinigen mehr)
    assert output_filename(pseudonym="_Fall-99_").startswith("_Fall-99__")
    assert output_filename(pseudonym="  F-001  ").startswith("F-001_")

    # Festes Datum
    assert (
        output_filename(pseudonym="Fall-01", when=date(2026, 4, 15))
        == "Fall-01_2026-04-15_SOB.docx"
    )


def test_output_filename_env(monkeypatch):
    # Ohne Env: 'Fall'
    monkeypatch.delenv("SOB_EXPORT_PSEUDONYM", raising=False)
    assert output_filename().startswith("Fall_")

    # Mit SOB_EXPORT_PSEUDONYM
    monkeypatch.setenv("SOB_EXPORT_PSEUDONYM", "EnvFall-01")
    assert output_filename().startswith("EnvFall-01_")

    # Explizites Argument hat Vorrang vor Env
    assert output_filename(pseudonym="Prio-Arg").startswith("Prio-Arg_")

    # Ungültige Env-Werte -> klarer ❌-Fehler statt stiller Bereinigung
    monkeypatch.setenv("SOB_EXPORT_PSEUDONYM", "Env / Fall 99")
    with pytest.raises(InvalidPseudonymError, match="SOB_EXPORT_PSEUDONYM"):
        output_filename()


# Fiktive, synthetische Werte (keine echten Personen)
INVALID_PSEUDONYMS = [
    "Łukasz Øberg",
    "Max Mustermann",
    "Müller",
    "Fall 123 / A&B",
    "../etc/passwd",
    "Fall.1",
    "!@#$%",
    "A" * 65,
]


@pytest.mark.parametrize("bad", INVALID_PSEUDONYMS)
def test_invalid_pseudonym_raises_clear_error_without_echo(bad):
    with pytest.raises(InvalidPseudonymError) as excinfo:
        output_filename(pseudonym=bad)
    msg = str(excinfo.value)
    assert msg.startswith("❌")
    assert "A–Z, a–z, 0–9" in msg
    # Der (evtl. klarnamenhaltige) Wert wird nicht zurückgespiegelt
    assert bad.strip() not in msg


@pytest.mark.parametrize("good", ["F-001", "Fall_42", "abc", "A" * 64, "2026-001"])
def test_valid_pseudonyms_are_used_verbatim(good):
    name = output_filename(pseudonym=good, when=date(2026, 4, 15))
    assert name == f"{good}_2026-04-15_SOB.docx"


def test_output_filename_legacy_positional_call_is_backward_compatible():
    # Alter Aufruf (z. B. aus noch nicht migrierten Call-Sites) -> kein TypeError,
    # Namen werden ignoriert, es landet kein Klarname im Dateinamen.
    with pytest.warns(DeprecationWarning):
        name = output_filename("Łukasz", "Øberg")
    assert name.startswith("Fall_")
    assert "Łukasz" not in name and "Øberg" not in name
    with pytest.warns(DeprecationWarning):
        assert (
            output_filename("Max", "Mustermann", date(2026, 4, 15), pseudonym="F-7")
            == "F-7_2026-04-15_SOB.docx"
        )
    with pytest.raises(TypeError):
        output_filename("a", "b", date(2026, 4, 15), "zu viel")


def test_unique_output_path_never_overwrites(tmp_path):
    first = unique_output_path(tmp_path, "F-1_2026-04-15_SOB.docx")
    assert first.name == "F-1_2026-04-15_SOB.docx"
    first.write_bytes(b"x")
    second = unique_output_path(tmp_path, "F-1_2026-04-15_SOB.docx")
    assert second.name == "F-1_2026-04-15_SOB_2.docx"
    second.write_bytes(b"y")
    assert unique_output_path(tmp_path, "F-1_2026-04-15_SOB.docx").name == "F-1_2026-04-15_SOB_3.docx"


def test_generate_sob_twice_same_day_does_not_overwrite(tmp_path):
    kwargs = dict(
        csv_path=VORLAGE_CSV,
        section_definitions=SECTION_DEFINITIONS,
        template_path=TEMPLATE_DOCX,
        output_dir=tmp_path,
        pseudonym="F-001",
    )
    r1 = generate_sob(generator=StubDraftGenerator(), **kwargs)
    first_bytes = r1.path.read_bytes()
    r2 = generate_sob(generator=StubDraftGenerator(), **kwargs)
    assert r1.path != r2.path
    assert r2.path.name.endswith("_SOB_2.docx")
    assert r1.path.read_bytes() == first_bytes  # erste Datei unverändert
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([r1.path.name, r2.path.name])


def test_generate_sob_invalid_pseudonym_fails_before_writing(tmp_path):
    with pytest.raises(InvalidPseudonymError):
        generate_sob(
            csv_path=VORLAGE_CSV,
            generator=StubDraftGenerator(),
            section_definitions=SECTION_DEFINITIONS,
            template_path=TEMPLATE_DOCX,
            output_dir=tmp_path,
            pseudonym="Łukasz Øberg",
        )
    assert list(tmp_path.iterdir()) == []


def test_cli_rejects_invalid_pseudonym(capsys):
    from sob_generator.__main__ import _parse_args

    with pytest.raises(SystemExit) as excinfo:
        _parse_args([str(VORLAGE_CSV), "--pseudonym", "Łukasz Øberg"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "❌ Ungültiges Pseudonym" in err
    assert "Łukasz" not in err
    # Leer = nicht gesetzt
    assert _parse_args([str(VORLAGE_CSV), "--pseudonym", " "]).pseudonym is None


def test_cli_invalid_env_pseudonym_exits_with_error(monkeypatch, tmp_path, capsys):
    from sob_generator.__main__ import main

    monkeypatch.setenv("SOB_EXPORT_PSEUDONYM", "Łukasz Øberg")
    rc = main([str(VORLAGE_CSV), "--stub", "--output", str(tmp_path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "❌" in err and "SOB_EXPORT_PSEUDONYM" in err
    assert "Łukasz" not in err
    assert list(tmp_path.iterdir()) == []


def test_generate_sob_with_pseudonym(tmp_path):
    stub = StubDraftGenerator()
    result = generate_sob(
        csv_path=VORLAGE_CSV,
        generator=stub,
        section_definitions=SECTION_DEFINITIONS,
        template_path=TEMPLATE_DOCX,
        output_dir=tmp_path,
        pseudonym="Fall-123",
    )
    assert result.path.name.startswith("Fall-123_")
    assert result.path.name.endswith("_SOB.docx")
    assert "Vorname" not in result.path.name


def test_generate_sob_with_sob_output_dir_env(tmp_path, monkeypatch):
    custom_dir = tmp_path / "env_output_dir"
    monkeypatch.setenv("SOB_OUTPUT_DIR", str(custom_dir))
    stub = StubDraftGenerator()
    result = generate_sob(
        csv_path=VORLAGE_CSV,
        generator=stub,
        section_definitions=SECTION_DEFINITIONS,
        template_path=TEMPLATE_DOCX,
        output_dir=None,
    )
    assert result.path.parent == custom_dir
    assert result.path.exists()


def test_cli_output_dir_and_pseudonym(monkeypatch, tmp_path):
    from sob_generator.__main__ import DEFAULT_OUTPUT, _parse_args

    # Default ohne Flags und ohne Env
    monkeypatch.delenv("SOB_OUTPUT_DIR", raising=False)
    args = _parse_args([str(VORLAGE_CSV)])
    assert args.output == DEFAULT_OUTPUT
    assert args.pseudonym is None

    # CLI --pseudonym
    args_pseudo = _parse_args([str(VORLAGE_CSV), "--pseudonym", "Fall-CLI"])
    assert args_pseudo.pseudonym == "Fall-CLI"

    # SOB_OUTPUT_DIR setzt Default-Output-Pfad
    custom_dir = tmp_path / "cli_env_dir"
    monkeypatch.setenv("SOB_OUTPUT_DIR", str(custom_dir))
    args_env = _parse_args([str(VORLAGE_CSV)])
    assert args_env.output == custom_dir

    # CLI --output überschreibt SOB_OUTPUT_DIR
    override_dir = tmp_path / "cli_override_dir"
    args_override = _parse_args([str(VORLAGE_CSV), "--output", str(override_dir)])
    assert args_override.output == override_dir


class _FailingDraftGenerator:
    """Simuliert einen LLM, der jeden Baustein als review-bedürftig zurückgibt.

    Entspricht dem realen Fall (Issue #18): nach 2 ungültigen Versuchen liefert
    `LLMDraftGenerator` leere Felder + ``needs_review=True`` (so geschehen bei
    Abschnitt 1.5 im echten 3B-Lauf).
    """

    def classify_entry(self, entry, sections):
        return sections[0].id

    def generate_section(self, section, entries, **kwargs):
        return GenerationResult(
            value=SectionLLMOutput.model_construct(
                bericht_zielerreichung="",
                vorschlag_ziele="",
                vorschlag_massnahmen="",
                realisierung_andere="",
            ),
            needs_review=True,
            raw_output="<invalid>",
        )

    def generate_developments(self, entries, **kwargs):
        return GenerationResult(
            value=DevelopmentsLLMOutput(developments=""),
            needs_review=True,
            raw_output="<invalid>",
        )


class _RecordingDraftGenerator:
    """Merkt sich die an die LLM-Calls übergebenen Anrede-/Namensdaten (#32)."""

    def __init__(self):
        self.section_kwargs: list[dict] = []
        self.developments_kwargs: dict = {}

    def classify_entry(self, entry, sections):
        # Testkonvention: Kategorie = Abschnitts-ID, sonst erster Abschnitt.
        return next((s.id for s in sections if s.id == entry.kategorie), sections[0].id)

    def generate_section(self, section, entries, **kwargs):
        self.section_kwargs.append(kwargs)
        return GenerationResult(
            value=SectionLLMOutput(
                bericht_zielerreichung="ok",
                vorschlag_ziele="ok",
                vorschlag_massnahmen="ok",
                realisierung_andere="",
            ),
            needs_review=False,
            raw_output="<rec>",
        )

    def generate_developments(self, entries, **kwargs):
        self.developments_kwargs = kwargs
        return GenerationResult(
            value=DevelopmentsLLMOutput(developments="ok"),
            needs_review=False,
            raw_output="<rec>",
        )


def test_build_draft_reicht_vorname_an_die_llm_calls_durch():
    """#32: der Vorname muss aus den Stammdaten an die Generierung gelangen,
    damit das Prompt-Scrubbing ihn aus dem Kontext entfernen kann."""
    imported = VivendiImport(
        client=ClientData(name="Mustermann", vorname="Max", anrede="Herr"),
        meta=ReportMeta(),
        entries=[DocumentationEntry(datum="01.01.2026", kategorie="w_Test", text="Max war da.")],
    )
    gen = _RecordingDraftGenerator()

    build_draft(imported, gen, SECTION_DEFINITIONS)

    assert gen.section_kwargs, "mindestens ein Abschnitts-Call erwartet"
    for kw in gen.section_kwargs:
        assert kw["vorname"] == "Max"
        assert kw["nachname"] == "Mustermann"
        assert kw["anrede"] == "Herr"
    assert gen.developments_kwargs["vorname"] == "Max"


def test_empty_section_guard_skips_llm_and_flags_review():
    """#43: Abschnitte ohne CSV-Einträge und ohne Freitext lösen keinen LLM-Call
    aus — statt zu halluzinieren landet ein needs_review-Flag im Draft.

    Im echten Mustermann-Lauf sind 1.1 (Wohnsituation) und 1.3 (Grundpflege)
    ohne Vivendi-Einträge; das LLM halluzinierte für 1.1 und erzeugte für 1.3
    einen Fragment-Wert ("Herr Muster"). Der Guard verhindert beides.
    """
    # Eintrag nur für 1.2 — 1.1 und 1.3 bleiben leer.
    imported = VivendiImport(
        client=ClientData(name="Mustermann", vorname="Max", anrede="Herr"),
        meta=ReportMeta(),
        entries=[
            DocumentationEntry(datum="01.01.2026", kategorie="1.2", text="Max hat aufgeräumt.")
        ],
    )
    gen = _RecordingDraftGenerator()

    draft = build_draft(imported, gen, SECTION_DEFINITIONS)

    # 1.1 und 1.3 müssen needs_review sein (kein LLM-Call, kein Freitext)
    sec_11 = next(s for s in draft.sections if s.id == "1.1")
    sec_13 = next(s for s in draft.sections if s.id == "1.3")
    assert sec_11.needs_review, "1.1 ohne Einträge muss needs_review sein"
    assert sec_13.needs_review, "1.3 ohne Einträge muss needs_review sein"

    # bericht_zielerreichung darf kein LLM-Fabrikat sein (leer oder [REVIEW])
    assert sec_11.bericht_zielerreichung == "" or sec_11.bericht_zielerreichung.startswith("[")
    assert sec_13.bericht_zielerreichung == "" or sec_13.bericht_zielerreichung.startswith("[")

    # 1.2 hat einen Eintrag → LLM-Call wurde ausgeführt
    sec_12 = next(s for s in draft.sections if s.id == "1.2")
    assert not sec_12.needs_review


class _UnclearDraftGenerator(_RecordingDraftGenerator):
    """Klassifiziert Einträge mit Kategorie 'unklar' als nicht zuordenbar."""

    def classify_entry(self, entry, sections):
        if entry.kategorie == "unklar":
            return "unklar"
        return super().classify_entry(entry, sections)


def test_unclear_entries_sind_sichtbar_statt_still_verworfen():
    """#50/K1: 'unklar'-klassifizierte Einträge landen in unclear_entries und
    pending_review — die Fachkraft kann fehlende Ereignisse bemerken (ADR-3)."""
    unklar = DocumentationEntry(datum="02.01.2026", kategorie="unklar", text="Sonstiges Ereignis.")
    imported = VivendiImport(
        client=ClientData(name="Mustermann", vorname="Max", anrede="Herr"),
        meta=ReportMeta(),
        entries=[
            DocumentationEntry(datum="01.01.2026", kategorie="1.2", text="Max hat aufgeräumt."),
            unklar,
        ],
    )

    draft = build_draft(imported, _UnclearDraftGenerator(), SECTION_DEFINITIONS)

    assert draft.unclear_entries == [unklar]
    assert any(p.startswith("unklar") for p in draft.pending_review)
    # Der unklare Eintrag ist in keinem Abschnitts-Call gelandet.
    assert draft.classification["unklar"], "Mapping muss den unklaren Eintrag listen"


def test_classification_mapping_wird_im_draft_abgelegt():
    """#50/K2: das Pass-0-Mapping (Eintrag → Abschnitt/unklar) ist diagnostizierbar."""
    imported = VivendiImport(
        client=ClientData(name="Mustermann", vorname="Max", anrede="Herr"),
        meta=ReportMeta(),
        entries=[
            DocumentationEntry(datum="01.01.2026", kategorie="1.2", text="Max hat aufgeräumt."),
            DocumentationEntry(datum="03.01.2026", kategorie="1.4", text="Max wirkte stabil."),
        ],
    )

    draft = build_draft(imported, _RecordingDraftGenerator(), SECTION_DEFINITIONS)

    assert len(draft.classification["1.2"]) == 1
    assert "01.01.2026" in draft.classification["1.2"][0]
    assert len(draft.classification["1.4"]) == 1
    assert draft.classification["unklar"] == []
    # Abschnitte ohne Einträge sind als Datenlage markiert, nicht als Fehler.
    sec_11 = next(s for s in draft.sections if s.id == "1.1")
    assert sec_11.no_entries
    sec_12 = next(s for s in draft.sections if s.id == "1.2")
    assert not sec_12.no_entries


def test_failed_generation_is_surfaced_not_silent(tmp_path):
    """#18: gescheiterte Bausteine landen nicht stumm leer im Bericht."""
    result = generate_sob(
        csv_path=VORLAGE_CSV,
        generator=_FailingDraftGenerator(),
        section_definitions=SECTION_DEFINITIONS,
        template_path=TEMPLATE_DOCX,
        output_dir=tmp_path,
    )

    # Alle 9 Abschnitte + developments werden als prüfbedürftig gemeldet.
    assert "developments" in result.pending_review
    assert "1.1" in result.pending_review
    assert len(result.pending_review) == 10

    # Der Warnhinweis steht sichtbar im erzeugten Dokument.
    root = fields.load_document_tree(result.path)
    assert REVIEW_MARKER in fields.get_formtext(root, "developments")
    assert REVIEW_MARKER in fields.get_formtext(root, "sections_0_bericht_zielerreichung")
