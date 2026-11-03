#!/usr/bin/env python
"""Modell-Abnahmelauf: erzeugt einen SOB-Entwurf mit EINEM Modell und druckt die
objektive Abnahme-Diagnostik (#36/#37), ohne docx zu schreiben.

Zweck: den Modellwechsel Llama-3.1-8B → phi-4 an denselben synthetischen
Fixtures objektiv vergleichbar machen. Zweimal aufrufen, je Modell, Reports
vergleichen:

    .venv/bin/python scripts/model_acceptance.py dummy/Max_Mustermann_Vivendi_Export.csv \\
        --model models/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf
    .venv/bin/python scripts/model_acceptance.py dummy/Max_Mustermann_Vivendi_Export.csv \\
        --model models/phi-4-Q4_K_M.gguf

Dünne Klammer: Importer → LLMDraftGenerator → build_draft → diagnose. Die
Wertung lebt in sob_generator.draft_diagnostics (rein, getestet); hier nur
Argument-Parsing, Verdrahtung und Ausgabe.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Repo-Root auf den Importpfad — als Skript (scripts/…) liegt nur das script-dir
# darauf, nicht das Root wie bei `python -m sob_generator`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sob_generator.llm.generator import (
    DEFAULT_N_CTX,
    DEFAULT_N_GPU_LAYERS,
    DEFAULT_N_THREADS,
    LLMDraftGenerator,
)

from sob_generator.draft_diagnostics import DraftDiagnostics, GateResult, compare_to_baseline, diagnose
from sob_generator.importer import VivendiPDImporter
from sob_generator.orchestrator import build_draft
from sob_generator.previous_sob import read_previous_goals
from sob_generator.report_template import TEMPLATE_REGISTRY

_TEMPLATE = TEMPLATE_REGISTRY["hamburg_soziale_dienste"]
_DEFAULT_BASELINE = Path(__file__).resolve().parent / "acceptance_baseline.json"

_CHECK = {True: "✓", False: "✗"}


def _print_report(
    model_label: str,
    diag: DraftDiagnostics,
    elapsed: float,
    gate: GateResult | None = None,
) -> None:
    print(f"\n{'=' * 72}")
    print(f"ABNAHME-DIAGNOSTIK — Modell: {model_label}")
    print(f"Laufzeit: {elapsed:.1f}s ({elapsed / 60:.1f} min)")
    print(f"{'=' * 72}")

    print("\n#36  Opener-Vielfalt (≤2 Abschnitte je 3-Wort-Anfang)")
    print(f"     {_CHECK[diag.opener_ok]}  max. geteilt = {diag.max_shared_opener}", end="")
    if diag.colliding_openers:
        print(f"   Kollisionen: {', '.join(repr(o) for o in diag.colliding_openers)}")
    else:
        print()

    print("\n#37  Prosa statt Stichpunktliste in bericht_zielerreichung")
    if diag.prose_ok:
        print(f"     {_CHECK[True]}  keine Stichpunktlisten")
    else:
        print(f"     {_CHECK[False]}  Listen in Abschnitt(en): {', '.join(diag.bullet_sections)}")

    print("\n#37  Nackter Nachname mitten im Satz (Heuristik, je niedriger desto besser)")
    print(f"     gesamt = {diag.total_bare_surname_hits}")

    print('\n#34  Platzhalter-Leak "Beispiel" im Bericht')
    print(f"     gesamt = {diag.total_placeholder_leaks}")

    print("\n#37  Genitiv-Apostroph (falscher dt. Genitiv \"Mustermann'...\")")
    print(f"     gesamt = {diag.total_genitive_apostrophes}")

    print("\nPro Abschnitt (Opener · Bullets · nackter Name · Platzhalter · 1. Satz)")
    for s in diag.sections:
        flags = []
        if s.has_bullets:
            flags.append("LISTE")
        if s.bare_surname_hits:
            flags.append(f"{s.bare_surname_hits}×Name")
        if s.placeholder_leaks:
            flags.append(f"{s.placeholder_leaks}×PLH")
        if s.genitive_apostrophes:
            flags.append(f"{s.genitive_apostrophes}×GEN")
        flag_str = f"  [{', '.join(flags)}]" if flags else ""
        print(f"  {s.id:<5} «{s.opener}»{flag_str}")
        print(f"        {s.first_sentence}")

    print(f"\n{'=' * 72}")
    verdict = diag.opener_ok and diag.prose_ok
    print(f"Maschinelle Kriterien (#36 + #37-Listen): {_CHECK[verdict]}")
    print("Themen-Verschmierung & Grammatik (#37): MANUELL prüfen — siehe 1. Sätze oben.")

    if gate is not None:
        print(f"\n{'─' * 72}")
        print(f"BASELINE-GATE: {_CHECK[gate.passed]}")
        for c in gate.criteria:
            status = _CHECK[c.passed]
            print(f"  {status}  {c.name}: {c.actual} (Limit: {c.limit})")
        if gate.failures:
            print(f"  REGRESSION in: {', '.join(f.name for f in gate.failures)}")

    print(f"{'=' * 72}\n")


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="model_acceptance", description=__doc__)
    parser.add_argument("csv", type=Path, help="Vivendi-PD-CSV-Export (synthetische Fixture)")
    parser.add_argument("--model", type=Path, required=True, help="Pfad zum GGUF-Modell")
    parser.add_argument("--alt-sob", type=Path, dest="alt_sob", help="vorheriger SOB (.docx)")
    parser.add_argument("--freitext", default="", help="Freitext-Notizen (ADR-7)")
    parser.add_argument("--n-ctx", type=int, default=DEFAULT_N_CTX, dest="n_ctx")
    parser.add_argument("--threads", type=int, default=DEFAULT_N_THREADS)
    parser.add_argument(
        "--n-gpu-layers", type=int, default=DEFAULT_N_GPU_LAYERS, dest="n_gpu_layers"
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=_DEFAULT_BASELINE,
        help="Baseline-JSON für den Gate-Vergleich (default: scripts/acceptance_baseline.json)",
    )
    parser.add_argument(
        "--no-baseline",
        action="store_true",
        dest="no_baseline",
        help="Baseline-Gate deaktivieren (nur Report, kein Exit-Code 1)",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    if not args.model.exists():
        raise SystemExit(f"Modell nicht gefunden: {args.model}")

    imported = VivendiPDImporter(args.csv).load()
    previous_goals = read_previous_goals(args.alt_sob) if args.alt_sob else None
    generator = LLMDraftGenerator(
        model_path=str(args.model),
        n_ctx=args.n_ctx,
        n_threads=args.threads,
        n_gpu_layers=args.n_gpu_layers,
    )

    started = time.perf_counter()
    draft = build_draft(
        imported,
        generator,
        _TEMPLATE.section_definitions,
        freitext=args.freitext,
        previous_goals=previous_goals,
    )
    elapsed = time.perf_counter() - started

    diag = diagnose(draft)

    gate: GateResult | None = None
    if not args.no_baseline and args.baseline.exists():
        raw = json.loads(args.baseline.read_text(encoding="utf-8"))
        baseline = {k: v for k, v in raw.items() if not k.startswith("_")}
        gate = compare_to_baseline(diag, baseline)

    _print_report(args.model.name, diag, elapsed, gate)

    if gate is not None and not gate.passed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
