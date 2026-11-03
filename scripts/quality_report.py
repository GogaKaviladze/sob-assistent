#!/usr/bin/env python
"""SOB-Qualitätsbericht: erzeugt einen vollständigen LLM-Lauf und schreibt eine
Markdown-Datei mit allen LLM-Feldern im Volltext.

Zweck: manuelles DOCX-Öffnen nach jedem Testlauf ersetzen. Die MD-Datei enthält
alle generierten Texte lesbar, maschinenprüfbare Flags (leere Felder, JSON in
Feldern, needs_review) und die bekannten Stil-Checks (#36/#37).

    .venv/bin/python scripts/quality_report.py dummy/Max_Mustermann_Vivendi_Export.csv \\
        --model models/llama-3.1-8b.gguf

Die MD-Datei landet standardmäßig in output/quality_reports/.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sob_generator.llm.generator import (
    CHAT_FORMAT_AUTO,
    CHAT_FORMATS,
    DEFAULT_N_CTX,
    DEFAULT_N_GPU_LAYERS,
    DEFAULT_N_THREADS,
    LLMDraftGenerator,
    resolve_chat_format,
)

from sob_generator.draft_diagnostics import (
    DraftDiagnostics,
    GateResult,
    SectionDiagnostics,
    compare_to_baseline,
    diagnose,
)
from sob_generator.importer import VivendiPDImporter
from sob_generator.models import ReportDraft, ReportSection
from sob_generator.orchestrator import build_draft
from sob_generator.previous_sob import read_previous_goals
from sob_generator.report_template import TEMPLATE_REGISTRY

_TEMPLATE = TEMPLATE_REGISTRY["hamburg_soziale_dienste"]
_DEFAULT_BASELINE = Path(__file__).resolve().parent / "acceptance_baseline.json"

_OK = "✅"
_WARN = "⚠️"
_FAIL = "❌"


def _status(ok: bool, warn: bool = False) -> str:
    if ok:
        return _OK
    return _WARN if warn else _FAIL


def _field_display(value: str, name: str, sec_diag: SectionDiagnostics) -> str:
    if name in sec_diag.json_fields:
        return f"{_FAIL} JSON"
    if name in sec_diag.empty_fields:
        # Guard-Fall (#50/K1): leer wegen fehlender Einträge, kein LLM-Defekt.
        return "— (keine Einträge)" if sec_diag.no_entries else f"{_FAIL} leer"
    return _OK


def _build_md(
    model_label: str,
    draft: ReportDraft,
    diag: DraftDiagnostics,
    elapsed: float,
    gate: GateResult | None,
    timestamp: str,
) -> str:
    lines: list[str] = []

    lines += [
        "# SOB Qualitätsbericht",
        "",
        f"**Modell:** {model_label}  ",
        f"**Datum:** {timestamp}  ",
        f"**Laufzeit:** {elapsed:.1f} s ({elapsed / 60:.1f} min)  ",
        f"**Klient:** {draft.client.anrede} {draft.client.vorname} {draft.client.name}".strip(),
        "",
        "---",
        "",
    ]

    # Kritische Checks
    crit_status = _status(diag.critical_ok)
    lines += [
        "## Kritische Checks",
        "",
        "| Check | Status | Details |",
        "|---|---|---|",
        f"| Leere Pflichtfelder | {_status(diag.total_empty_fields == 0)} | {diag.total_empty_fields} |",
        f"| JSON statt Fließtext | {_status(diag.total_json_fields == 0)} | {diag.total_json_fields} |",
        f"| needs_review | {_status(diag.total_needs_review == 0, warn=True)} | {diag.total_needs_review} Abschnitt(e) |",
        f"| Abschnitte ohne Einträge (Datenlage, #50/K1) | {_status(not diag.no_entries_sections, warn=True)} | {', '.join(diag.no_entries_sections) or '—'} |",
        f"| Nicht zugeordnete Einträge („unklar“, #50/K1) | {_status(not draft.unclear_entries, warn=True)} | {len(draft.unclear_entries)} |",
        "",
    ]

    # Klassifikations-Mapping aus Pass 0 (#50/K2): welcher Eintrag landete wo.
    if draft.classification:
        lines += [
            "## Klassifikation (Pass 0)",
            "",
            "| Abschnitt | Einträge | Zugeordnet |",
            "|---|---|---|",
        ]
        for sid, labels in draft.classification.items():
            joined = "<br>".join(labels) if labels else "—"
            marker = f" {_WARN}" if sid == "unklar" and labels else ""
            lines.append(f"| {sid}{marker} | {len(labels)} | {joined} |")
        lines.append("")

    # Stil-Checks
    lines += [
        "## Stil-Checks",
        "",
        "| Check | Status | Wert |",
        "|---|---|---|",
        f"| Opener-Kollaps (max geteilt) | {_status(diag.opener_ok)} | {diag.max_shared_opener} |",
        f"| Prosa in bericht_zielerreichung | {_status(diag.prose_ok)} | {len(diag.bullet_sections)} Abschnitt(e) mit Listen |",
        f"| Nackter Nachname | {_status(diag.total_bare_surname_hits == 0, warn=True)} | {diag.total_bare_surname_hits} |",
        f'| Platzhalter-Leak „Beispiel“ | {_status(diag.total_placeholder_leaks == 0)} | {diag.total_placeholder_leaks} |',
        f"| Genitiv-Apostroph | {_status(diag.total_genitive_apostrophes == 0, warn=True)} | {diag.total_genitive_apostrophes} |",
        "",
    ]

    if gate is not None:
        passed = _status(gate.passed)
        lines += [
            "## Baseline-Gate",
            "",
            f"**Ergebnis:** {passed}",
            "",
            "| Kriterium | Ist | Limit | Status |",
            "|---|---|---|---|",
        ]
        for c in gate.criteria:
            lines.append(f"| {c.name} | {c.actual} | {c.limit} | {_status(c.passed)} |")
        lines.append("")

    # Abschnitte-Übersicht
    lines += [
        "---",
        "",
        "## Abschnitte — Übersicht",
        "",
        "| ID | bericht | vorschlag_ziele | vorschlag_massnahmen | review | Opener |",
        "|---|---|---|---|---|---|",
    ]

    sec_by_id = {s.id: s for s in diag.sections}
    draft_sec_by_id = {s.id: s for s in draft.sections}

    for s in diag.sections:
        ds = draft_sec_by_id.get(s.id)
        b = _field_display(ds.bericht_zielerreichung if ds else "", "bericht_zielerreichung", s)
        z = _field_display(ds.vorschlag_ziele if ds else "", "vorschlag_ziele", s)
        m = _field_display(ds.vorschlag_massnahmen if ds else "", "vorschlag_massnahmen", s)
        if s.no_entries:
            r = f"{_WARN} keine Einträge"
        else:
            r = _status(not s.needs_review, warn=False) if not s.needs_review else _FAIL
        lines.append(f"| {s.id} | {b} | {z} | {m} | {r} | «{s.opener}» |")

    lines += ["", "---", ""]

    # Developments
    if draft.developments:
        lines += [
            "## Wesentliche Entwicklungen",
            "",
            draft.developments,
            "",
        ]
    elif draft.developments_needs_review:
        lines += [
            "## Wesentliche Entwicklungen",
            "",
            f"{_FAIL} *Generierung fehlgeschlagen — needs_review*",
            "",
        ]

    # Rohtexte je Abschnitt
    lines += ["## Rohtexte aller LLM-Felder", ""]

    for sec in draft.sections:
        sd = sec_by_id.get(sec.id)
        title = sec.title
        review_flag = f" {_FAIL} needs_review" if sec.needs_review else ""
        lines += [f"### {sec.id} {title}{review_flag}", ""]

        for field_name, label in [
            ("bericht_zielerreichung", "bericht_zielerreichung"),
            ("vorschlag_ziele", "vorschlag_ziele"),
            ("vorschlag_massnahmen", "vorschlag_massnahmen"),
            ("realisierung_andere", "realisierung_andere"),
        ]:
            value = getattr(sec, field_name, "")
            is_json = sd and field_name in sd.json_fields
            is_empty = sd and field_name in sd.empty_fields

            flags = ""
            if is_json:
                flags = f" {_FAIL} **JSON erkannt**"
            elif is_empty:
                flags = f" {_FAIL} **leer**"

            lines += [f"**{label}**{flags}"]
            if value.strip():
                lines += ["", value.strip(), ""]
            else:
                lines += ["", "*(leer)*", ""]

    return "\n".join(lines)


def _print_summary(model_label: str, diag: DraftDiagnostics, elapsed: float, out_path: Path) -> None:
    ok = "✓"
    fail = "✗"

    print(f"\n{'=' * 60}")
    print(f"SOB QUALITÄTSBERICHT — {model_label}")
    print(f"Laufzeit: {elapsed:.1f}s")
    print(f"{'=' * 60}")

    crit = diag.critical_ok
    print(f"\n  {'✓' if crit else '✗'}  Kritische Checks {'OK' if crit else 'FEHLGESCHLAGEN'}")
    if not crit:
        print(f"     Leere Felder:   {diag.total_empty_fields}")
        print(f"     JSON in Feldern:{diag.total_json_fields}")
        print(f"     needs_review:   {diag.total_needs_review}")

    style_ok = diag.opener_ok and diag.prose_ok
    print(f"\n  {'✓' if style_ok else '✗'}  Stil-Checks {'OK' if style_ok else 'PROBLEME'}")

    print(f"\n  Bericht: {out_path}")
    print(f"{'=' * 60}\n")


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="quality_report", description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--alt-sob", type=Path, dest="alt_sob")
    parser.add_argument("--freitext", default="")
    parser.add_argument("--n-ctx", type=int, default=DEFAULT_N_CTX, dest="n_ctx")
    parser.add_argument("--threads", type=int, default=DEFAULT_N_THREADS)
    parser.add_argument("--n-gpu-layers", type=int, default=DEFAULT_N_GPU_LAYERS, dest="n_gpu_layers")
    parser.add_argument(
        "--chat-format",
        choices=CHAT_FORMATS,
        default=CHAT_FORMAT_AUTO,
        dest="chat_format",
        help="Prompt-Wrapping (#49): auto = ChatML für Qwen; none/chatml für A/B-Läufe erzwingen.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/quality_reports"),
        dest="output_dir",
        help="Zielverzeichnis für den MD-Bericht (default: output/quality_reports/)",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=_DEFAULT_BASELINE,
    )
    parser.add_argument("--no-baseline", action="store_true", dest="no_baseline")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    if not args.model.exists():
        raise SystemExit(f"Modell nicht gefunden: {args.model}")

    imported = VivendiPDImporter(args.csv).load()
    previous_goals = read_previous_goals(args.alt_sob) if args.alt_sob else None
    # Aufgelöstes Chat-Format ins Report-Label — unterscheidet A/B-Läufe (#49).
    model_label = (
        f"{args.model.name} "
        f"[chat_format={resolve_chat_format(args.chat_format, str(args.model))}]"
    )
    generator = LLMDraftGenerator(
        model_path=str(args.model),
        n_ctx=args.n_ctx,
        n_threads=args.threads,
        n_gpu_layers=args.n_gpu_layers,
        chat_format=args.chat_format,
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

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.output_dir / f"quality_{timestamp}.md"

    md = _build_md(
        model_label=model_label,
        draft=draft,
        diag=diag,
        elapsed=elapsed,
        gate=gate,
        timestamp=timestamp.replace("_", " "),
    )
    out_path.write_text(md, encoding="utf-8")

    _print_summary(model_label, diag, elapsed, out_path)

    if gate is not None and not gate.passed:
        return 1
    if not diag.critical_ok:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
