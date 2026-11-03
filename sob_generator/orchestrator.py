"""Orchestrator: verkettet den Durchstich CSV → ReportDraft → docx (Issue #17).

import (ADR-4) → 10 LLM-Calls (9 Abschnitte + developments, ADR-11) →
DocxRenderer (ADR-10) → output/{Pseudonym}_{Datum}_SOB.docx (ADR-1, Issue #115).

Der LLM-Schritt liegt hinter dem `DraftGenerator`-Protokoll (DIP) — hier wird
nicht zwischen echtem Modell und Stub unterschieden.
"""
from __future__ import annotations

import logging
import os
import re
import warnings
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from sob_generator.llm.generator import GenerationResult
from sob_generator.llm.schemas import DocumentationEntry, SectionLLMOutput
from sob_generator.llm.sections import SectionDefinition

from .draft_generator import DraftGenerator
from .importer import VivendiImport, VivendiPDImporter
from .models import ReportDraft, ReportSection
from .opener_diversity import opener_key, surplus_indices
from .previous_sob import PreviousGoals, read_previous_goals, remap_previous_goals
from .renderer import DocxRenderer

logger = logging.getLogger(__name__)

# Neutrale Default-Ziele, wenn kein vorheriger SOB eingelesen wurde (ADR-4).
_NO_GOALS = PreviousGoals(leitziel="", smarte_ziele="", applies=True)

# Kurzlabel eines Eintrags für Log und Klassifikations-Diagnose (#50/K2).
# Bewusst gekürzter Text: identifizierbar für die Fachkraft, aber keine
# vollständigen Klientendaten in Logdateien (DSGVO-Minimierung).
_LABEL_TEXT_LEN = 60


def _entry_label(entry: DocumentationEntry) -> str:
    text = entry.text.strip()
    if len(text) > _LABEL_TEXT_LEN:
        text = text[:_LABEL_TEXT_LEN].rstrip() + "…"
    return f"{entry.datum} | {entry.kategorie} | {text}"

# Obergrenze der Regenerations-Pässe des Opener-Guards (#36). Jeder Pass erzeugt
# nur noch die Kollidierer neu; in der Praxis genügt 1 Pass, 2 Pässe fangen den
# seltenen Fall ab, dass zwei Regenerierte erneut zusammenfallen. Bewusst
# beschränkt: der Guard ist ein Stil-Gate, kein Korrektheits-Gate — bleibt nach
# den Pässen ein Rest, wird er ausgeliefert statt den Lauf zu blockieren.
_OPENER_GUARD_MAX_PASSES = 2


@dataclass(frozen=True)
class SobResult:
    """Ergebnis eines Durchstichs: Pfad + ggf. review-bedürftige Bausteine (#18)."""

    path: Path
    pending_review: List[str]


def build_draft(
    imported: VivendiImport,
    generator: DraftGenerator,
    section_definitions: List[SectionDefinition],
    freitext: str = "",
    previous_goals: Optional[Dict[str, PreviousGoals]] = None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> ReportDraft:
    """Erzeugt den ReportDraft über 10 LLM-Calls (ADR-11).

    `section_definitions` kommt explizit vom Aufrufer (i.d.R. aus einem
    `ReportTemplate` der Registry, #21) — kein versteckter Modul-Import mehr,
    damit der Berichtstyp am Seam austauschbar bleibt.

    `previous_goals` (aus einem vorherigen SOB, ADR-4) liefert pro Abschnitt
    Leitziel/smarte Ziele als Prompt-Kontext; sie werden zugleich in den neuen
    Bericht übernommen. Ohne sie bleiben `leitziel`/`smarte_ziele` leer.

    Zuordnung der Einträge (#50/K1): Pass 0 klassifiziert jeden Eintrag auf
    genau einen Abschnitt; jeder Abschnitts-Call sieht nur „seine" Einträge.
    Nur der `developments`-Call bekommt weiterhin ALLE Einträge (ADR-6/ADR-11).
    Als „unklar" klassifizierte Einträge gehen in KEINEN Abschnitts-Call — sie
    landen in `ReportDraft.unclear_entries` (Review-UI/pending_review) statt
    still verworfen zu werden. Das komplette Mapping steht für Diagnosezwecke
    in `ReportDraft.classification` (#50/K2).

    `progress(done, total)` wird optional vor jedem Abschnitt aufgerufen — der
    Seam, über den die UI (#22) den Fortschritt „Abschnitt N von 9" anzeigt,
    ohne die Schleife zu duplizieren. Ohne Callback ändert sich nichts.
    """
    previous_goals = previous_goals or {}
    entries = imported.entries
    # total = Pass-0-Einträge + 9 Abschnitte + developments
    total = len(entries) + len(section_definitions) + 1

    # Pass 0: jeden Eintrag einem Abschnitt zuordnen.
    classified: Dict[str, List[DocumentationEntry]] = {
        defn.id: [] for defn in section_definitions
    }
    unclear: List[DocumentationEntry] = []
    for i, entry in enumerate(entries):
        if progress is not None:
            progress(i + 1, total)
        sid = generator.classify_entry(entry, section_definitions)
        if sid in classified:
            classified[sid].append(entry)
        else:
            unclear.append(entry)
        logger.info("Pass 0: Eintrag %d/%d → %s", i + 1, len(entries), sid)
    if unclear:
        logger.warning(
            "Pass 0: %d Eintrag/Einträge nicht zugeordnet (unklar) — "
            "erscheinen in keinem Abschnitt, bitte im Review prüfen",
            len(unclear),
        )

    # Diagnose-Mapping (#50/K2): Abschnitts-ID → Eintrags-Labels, inkl. "unklar".
    classification = {
        sid: [_entry_label(e) for e in section_entries]
        for sid, section_entries in classified.items()
    }
    classification["unklar"] = [_entry_label(e) for e in unclear]

    def _generate(defn: SectionDefinition, pg: PreviousGoals, forbidden=()):
        # Kein LLM-Call wenn weder CSV-Einträge noch Freitext vorhanden sind —
        # das LLM würde sonst halluzinieren oder ein leeres Fragment erzeugen
        # (1.1/1.3 ohne Vivendi-Einträge, getestet in test_empty_section_guard).
        if not classified[defn.id] and not freitext.strip():
            return GenerationResult(
                value=SectionLLMOutput.model_construct(
                    bericht_zielerreichung="",
                    vorschlag_ziele="",
                    vorschlag_massnahmen="",
                    realisierung_andere="",
                ),
                needs_review=True,
                raw_output="",
            )
        return generator.generate_section(
            section=defn,
            entries=classified[defn.id],
            leitziel=pg.leitziel,
            smarte_ziele=pg.smarte_ziele,
            previous_goals_apply=pg.applies,
            freitext=freitext,
            anrede=imported.client.anrede,
            nachname=imported.client.name,
            vorname=imported.client.vorname,
            forbidden_openers=forbidden,
        )

    # Pass 1: jeden Abschnitt mit den vorab zugeordneten Einträgen erzeugen.
    pgs = [previous_goals.get(defn.id, _NO_GOALS) for defn in section_definitions]
    results = []
    for i, defn in enumerate(section_definitions):
        if progress is not None:
            progress(len(entries) + i + 1, total)
        results.append(_generate(defn, pgs[i]))

    # Phase 2: Opener-Guard (#36) — Abschnitte mit kollidierendem 3-Wort-Anfang
    # gezielt neu erzeugen, mit Ban-Direktive auf die bereits vergebenen
    # Eröffnungen. Cross-Section-Concern → gehört in den Orchestrator, nicht in
    # den (zustandslosen) Section-Generator.
    results = _enforce_opener_diversity(section_definitions, pgs, results, _generate)

    sections = [
        ReportSection(
            id=defn.id,
            title=defn.title,
            previous_goals_apply=pgs[i].applies,
            leitziel=pgs[i].leitziel,
            smarte_ziele=pgs[i].smarte_ziele,
            bericht_zielerreichung=results[i].value.bericht_zielerreichung,
            vorschlag_ziele=results[i].value.vorschlag_ziele,
            vorschlag_massnahmen=results[i].value.vorschlag_massnahmen,
            realisierung_andere=results[i].value.realisierung_andere,
            needs_review=results[i].needs_review,
            # Guard-Fall (#50/K1): keine Einträge und kein Freitext — Datenlage,
            # kein Generierungs-Fehlschlag. Renderer/UI zeigen einen Hinweis
            # statt des Fehlermarkers.
            no_entries=not classified[defn.id] and not freitext.strip(),
        )
        for i, defn in enumerate(section_definitions)
    ]
    dev = generator.generate_developments(
        entries=entries,
        previous_goals_summary=_goals_summary(previous_goals),
        freitext=freitext,
        anrede=imported.client.anrede,
        nachname=imported.client.name,
        vorname=imported.client.vorname,
    )
    return ReportDraft(
        client=imported.client,
        meta=imported.meta,
        developments=dev.value.developments,
        developments_needs_review=dev.needs_review,
        sections=sections,
        unclear_entries=unclear,
        classification=classification,
    )


def _enforce_opener_diversity(
    section_definitions: List[SectionDefinition],
    pgs: List[PreviousGoals],
    results: list,
    generate: Callable,
) -> list:
    """Erzeugt Abschnitte mit kollidierendem Bericht-Opener neu (#36-Guard).

    Akzeptanzkriterium: höchstens 2 Abschnitte teilen denselben 3-Wort-Anfang.
    Reine Prompt-Nudges können das bei festem Seed (#35) strukturell nicht
    garantieren (Pigeonhole über wenige Few-Shot-Eröffnungen) — deshalb dieser
    harte Post-Pass: erkenne die Überhang-Abschnitte (`surplus_indices`) und
    erzeuge nur sie erneut, diesmal mit einer Ban-Direktive auf die bereits
    vergebenen Eröffnungen. Der geänderte Prompt weicht trotz festem Seed
    deterministisch auf einen anderen Anfang aus.
    """
    for _ in range(_OPENER_GUARD_MAX_PASSES):
        openers = [opener_key(r.value.bericht_zielerreichung) for r in results]
        surplus = surplus_indices(openers)
        if not surplus:
            break
        for idx in surplus:
            forbidden = tuple(
                sorted({openers[j] for j in range(len(openers)) if j != idx and openers[j]})
            )
            results[idx] = generate(section_definitions[idx], pgs[idx], forbidden)
    return results


def _goals_summary(previous_goals: Dict[str, PreviousGoals]) -> str:
    """Auszug der bisherigen Leitziele für den abschnittsübergreifenden
    `developments`-Call (ADR-11)."""
    return "; ".join(
        pg.leitziel for pg in previous_goals.values() if pg.applies and pg.leitziel
    )


#: Erlaubte Zeichen für Pseudonyme/Fallnummern: nur ASCII-Buchstaben, Ziffern, '_' und '-'.
PSEUDONYM_MAX_LEN = 64
_PSEUDONYM_RE = re.compile(r"[A-Za-z0-9_-]{1,%d}" % PSEUDONYM_MAX_LEN)


class InvalidPseudonymError(ValueError):
    """Pseudonym enthält unzulässige Zeichen (Issue #115). Meldung enthält den Wert bewusst nicht."""


def validate_pseudonym(value: str, source: str = "Pseudonym") -> str:
    """Prüft ein Pseudonym strikt auf ``[A-Za-z0-9_-]`` (max. 64 Zeichen).

    Kein stilles Bereinigen: Ungültige Eingaben (Leerzeichen, Umlaute, Sonderzeichen,
    also typischerweise Klarnamen) führen zu einem ❌-Fehler. Der Wert selbst wird
    nicht in die Fehlermeldung übernommen (könnte ein Klarname sein).
    """
    if not _PSEUDONYM_RE.fullmatch(value):
        raise InvalidPseudonymError(
            f"❌ Ungültiges {source}: erlaubt sind nur A–Z, a–z, 0–9, '_' und '-' "
            f"(max. {PSEUDONYM_MAX_LEN} Zeichen, keine Leerzeichen/Umlaute). "
            "Bitte eine Fallnummer statt eines Namens verwenden, z. B. 'F-001'."
        )
    return value


def output_filename(
    *legacy_args: Any,
    pseudonym: Optional[str] = None,
    when: Optional[date] = None,
) -> str:
    """Erzeugt einen pseudonymisierten Dateinamen für den SOB-Bericht (Issue #115).

    Priorität:
    1. Explizites Argument `pseudonym`
    2. Umgebungsvariable `SOB_EXPORT_PSEUDONYM`
    3. Neutrales Token 'Fall' -> 'Fall_{YYYY-MM-DD}_SOB.docx'

    Pseudonyme werden strikt validiert (`validate_pseudonym`, nur ``[A-Za-z0-9_-]``);
    ungültige Werte lösen `InvalidPseudonymError` aus, es wird nichts still bereinigt.
    Leer bzw. nur Whitespace gilt als „nicht gesetzt“.

    Abwärtskompatibel: Der alte Aufruf ``output_filename(vorname, name[, when])``
    funktioniert weiter (kein TypeError), die Namen werden aber **ignoriert**
    (DeprecationWarning) – es landet nie ein Klarname im Dateinamen.
    """
    if legacy_args:
        if len(legacy_args) > 3:
            raise TypeError("output_filename() akzeptiert höchstens 3 Positionsargumente (veraltet).")
        warnings.warn(
            "output_filename(vorname, name) ist veraltet; Namen werden ignoriert. "
            "Bitte output_filename(pseudonym=...) verwenden (Issue #115).",
            DeprecationWarning,
            stacklevel=2,
        )
        if len(legacy_args) == 3 and when is None:
            when = legacy_args[2]
    when = when or date.today()
    if pseudonym and pseudonym.strip():
        label = validate_pseudonym(pseudonym.strip(), "Pseudonym")
    else:
        env_val = os.environ.get("SOB_EXPORT_PSEUDONYM", "").strip()
        label = validate_pseudonym(env_val, "Pseudonym in SOB_EXPORT_PSEUDONYM") if env_val else "Fall"
    return f"{label}_{when:%Y-%m-%d}_SOB.docx"


def unique_output_path(output_dir: Path, filename: str) -> Path:
    """Liefert einen noch nicht existierenden Pfad: ``X_SOB.docx``, sonst ``X_SOB_2.docx``, ``_3`` …

    Verhindert, dass mehrere Läufe am selben Tag einander still überschreiben.
    """
    candidate = output_dir / filename
    if not candidate.exists():
        return candidate
    stem, suffix = Path(filename).stem, Path(filename).suffix
    counter = 2
    while True:
        candidate = output_dir / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def generate_sob(
    csv_path,
    generator: DraftGenerator,
    section_definitions: List[SectionDefinition],
    template_path,
    output_dir: Optional[Union[str, Path]] = None,
    freitext: str = "",
    alt_sob_path=None,
    leistungsart: str = "",
    pseudonym: Optional[str] = None,
    renderer_factory: Optional[Callable[..., Any]] = None,
) -> SobResult:
    """Führt den gesamten Durchstich aus.

    `section_definitions` wird explizit durchgereicht (an `build_draft`); der
    Aufrufer wählt den Berichtstyp über die `TEMPLATE_REGISTRY` (#21).

    `alt_sob_path` (optional) verweist auf einen vorherigen, ausgefüllten SOB,
    dessen Ziele als Kontext einfließen und übernommen werden (ADR-4).

    `leistungsart` (#50/K3) füllt docx-Feld #13 — die Vivendi-CSV enthält
    sie nicht, daher kommt sie vom Aufrufer (CLI-Flag bzw. Upload-Formular).

    `pseudonym` (#115) steuert das Label im Dateinamen (Datenschutz/DSGVO).
    Ohne Angabe greift `SOB_EXPORT_PSEUDONYM` oder der Default 'Fall'.
    Existiert die Datei bereits, wird `_2`, `_3` … angehängt (kein Überschreiben).

    `output_dir` bestimmt das Ausgabeverzeichnis. Wenn None, greift `SOB_OUTPUT_DIR`
    oder der Default 'output'.

    Liefert Pfad des docx und die IDs review-bedürftiger Bausteine (#18) —
    so wird ein gescheiterter Abschnitt nicht stumm leer ausgeliefert.
    """
    # Pseudonym vor den (langen) LLM-Läufen validieren -> früher, klarer ❌-Fehler
    filename = output_filename(pseudonym=pseudonym)
    imported = VivendiPDImporter(csv_path).load()
    if leistungsart:
        imported.client.leistungsart = leistungsart
    previous_goals = read_previous_goals(alt_sob_path) if alt_sob_path else None
    if previous_goals:
        previous_goals = remap_previous_goals(previous_goals, section_definitions)
    draft = build_draft(
        imported,
        generator,
        section_definitions,
        freitext=freitext,
        previous_goals=previous_goals,
    )

    if output_dir is None:
        env_dir = os.environ.get("SOB_OUTPUT_DIR", "").strip()
        output_dir = Path(env_dir) if env_dir else Path("output")
    else:
        output_dir = Path(output_dir)

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = unique_output_path(output_dir, filename)
    renderer_cls = renderer_factory or DocxRenderer
    renderer_cls(template_path).render(draft, out_path)
    return SobResult(path=out_path, pending_review=draft.pending_review)
