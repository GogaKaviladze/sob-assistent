"""Entrypoint: `python -m sob_generator <csv>` (Issue #17).

Erzeugt aus einem Vivendi-CSV-Export eine vorausgefüllte SOB-Vorlage.

- `--stub` nutzt den `StubDraftGenerator` (kein Modell nötig) — so entsteht der
  Durchstich-Artefakt in dieser Session.
- Ohne `--stub` wird das echte `LLMDraftGenerator` mit einem GGUF-Modell
  verdrahtet (separater Schritt, Runbook in Issue #17).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Tuple, Union

from sob_generator.llm.generator import (
    CHAT_FORMAT_AUTO,
    CHAT_FORMATS,
    DEFAULT_N_CTX,
    DEFAULT_N_GPU_LAYERS,
    DEFAULT_N_THREADS,
)
from sob_generator.renderer import HamburgDocxRenderer

from .draft_generator import DraftGenerator
from .orchestrator import InvalidPseudonymError, generate_sob, validate_pseudonym
from .report_template import TEMPLATE_REGISTRY

_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TEMPLATE = "hamburg"
DEFAULT_OUTPUT = _ROOT / "output"


def _build_generator(args) -> DraftGenerator:
    if args.stub:
        from .stub_generator import StubDraftGenerator

        return StubDraftGenerator()
    if not args.model:
        raise SystemExit(
            "Kein Modell angegeben. Für einen Lauf ohne Modell --stub setzen, "
            "sonst --model <pfad-zur-gguf> (siehe Runbook in Issue #17)."
        )
    from sob_generator.llm.generator import LLMDraftGenerator

    return LLMDraftGenerator(
        model_path=str(args.model),
        n_ctx=args.n_ctx,
        n_threads=args.threads,
        n_gpu_layers=args.n_gpu_layers,
        chat_format=args.chat_format,
    )


def _get_default_output() -> Path:
    env_dir = os.environ.get("SOB_OUTPUT_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    return DEFAULT_OUTPUT


def _pseudonym_arg(value: str) -> "str | None":
    """argparse-Typ: Pseudonym strikt validieren (nur [A-Za-z0-9_-])."""
    value = value.strip()
    if not value:
        return None  # leer = nicht gesetzt -> SOB_EXPORT_PSEUDONYM bzw. 'Fall'
    try:
        return validate_pseudonym(value, "Pseudonym (--pseudonym)")
    except InvalidPseudonymError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


CLI_TEMPLATE_ALIASES = {
    "hamburg": "hamburg_soziale_dienste",
    "hamburg_soziale_dienste": "hamburg_soziale_dienste",
    "standard": "hamburg_soziale_dienste",
    "fachamt": "hamburg_soziale_dienste",
    "hamburg-fachamt": "hamburg_soziale_dienste",
    "op-de-wisch": "hamburg_op_de_wisch",
    "op_de_wisch": "hamburg_op_de_wisch",
    "hamburg_op_de_wisch": "hamburg_op_de_wisch",
    "odw": "hamburg_op_de_wisch",
}


def _resolve_cli_template(template_arg: Union[str, Path]) -> Tuple[Path, list, type]:
    """Löst die CLI-Vorlagenangabe auf.

    Unterstützt Aliase ('hamburg', 'op-de-wisch') auf die TEMPLATE_REGISTRY sowie
    direkte Dateipfade zu .docx-Vorlagen. Unbekannte Werte führen zu einem Fehler (kein stiller Fallback).
    """
    str_val = str(template_arg).strip()
    key_norm = str_val.lower()

    if key_norm in CLI_TEMPLATE_ALIASES:
        reg_key = CLI_TEMPLATE_ALIASES[key_norm]
        tmpl = TEMPLATE_REGISTRY[reg_key]
        if not tmpl.docx_path.exists():
            raise SystemExit(f"Fehler: Vorlagendatei für '{tmpl.label}' nicht gefunden: {tmpl.docx_path}")
        return tmpl.docx_path, tmpl.section_definitions, tmpl.renderer_factory

    path_val = Path(str_val)
    if path_val.is_file():
        hamburg_tmpl = TEMPLATE_REGISTRY["hamburg_soziale_dienste"]
        return path_val, hamburg_tmpl.section_definitions, HamburgDocxRenderer

    raise SystemExit(
        f"Fehler: Unbekannte Vorlage oder Datei nicht gefunden: '{str_val}'. "
        f"Gültige Aliase: 'hamburg' (Fachamt), 'op-de-wisch' (Op de Wisch) oder Pfad zu einer existierenden .docx-Datei."
    )


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="sob_generator", description=__doc__)
    parser.add_argument("csv", type=Path, help="Vivendi-PD-CSV-Export")
    parser.add_argument("--stub", action="store_true", help="ohne Modell (Stub-LLM)")
    parser.add_argument("--model", type=Path, help="Pfad zum GGUF-Modell")
    parser.add_argument(
        "--template",
        default=DEFAULT_TEMPLATE,
        help="Vorlagenwahl: 'hamburg' (Fachamt), 'op-de-wisch' (Op de Wisch) oder Pfad zu einer .docx-Datei (Default: hamburg)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_get_default_output(),
        help="Ausgabeverzeichnis (Standard: SOB_OUTPUT_DIR oder ./output)",
    )
    parser.add_argument(
        "--pseudonym",
        default=None,
        type=_pseudonym_arg,
        help="Pseudonym / Fallnummer für den Ausgabedateinamen, nur A-Z a-z 0-9 _ - "
        "(Standard: SOB_EXPORT_PSEUDONYM oder 'Fall')",
    )
    parser.add_argument(
        "--alt-sob",
        type=Path,
        dest="alt_sob",
        help="vorheriger ausgefüllter SOB (.docx): Ziele als Kontext + übernehmen",
    )
    parser.add_argument("--freitext", default="", help="Freitext-Notizen (ADR-7)")
    parser.add_argument(
        "--leistungsart",
        default="",
        help="Leistungsart für den Berichtskopf (z.B. ASP, PBW, WA) — steht "
        "nicht in der Vivendi-CSV (#50/K3)",
    )
    parser.add_argument("--n-ctx", type=int, default=DEFAULT_N_CTX, dest="n_ctx")
    parser.add_argument("--threads", type=int, default=DEFAULT_N_THREADS)
    parser.add_argument(
        "--chat-format",
        choices=CHAT_FORMATS,
        default=CHAT_FORMAT_AUTO,
        dest="chat_format",
        help="Prompt-Wrapping (#49): auto = ChatML für Qwen-Modelle (am "
        "Dateinamen erkannt), sonst raw completion; none/chatml erzwingen.",
    )
    parser.add_argument(
        "--n-gpu-layers",
        type=int,
        default=DEFAULT_N_GPU_LAYERS,
        dest="n_gpu_layers",
        help="LLM-Layer auf die GPU (Apple Metal) auslagern. -1 = alle (Default, "
        "der Performance-Hebel auf Apple Silicon), 0 = CPU-only.",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    generator = _build_generator(args)
    template_path, section_defs, renderer_factory = _resolve_cli_template(args.template)
    started = time.perf_counter()
    try:
        result = generate_sob(
            csv_path=args.csv,
            generator=generator,
            section_definitions=section_defs,
            template_path=template_path,
            output_dir=args.output,
            freitext=args.freitext,
            alt_sob_path=args.alt_sob,
            leistungsart=args.leistungsart,
            renderer_factory=renderer_factory,
            pseudonym=args.pseudonym,
        )
    except InvalidPseudonymError as exc:
        # z. B. ungültiges SOB_EXPORT_PSEUDONYM (#115) — Meldung enthält den Wert nicht
        print(str(exc), file=sys.stderr)
        return 1
    elapsed = time.perf_counter() - started
    print(f"SOB erstellt: {result.path}")
    print(f"Laufzeit: {elapsed:.1f}s ({elapsed / 60:.1f} min)")
    if result.pending_review:
        # Exit-Code 2: docx wurde erzeugt, aber Bausteine brauchen Prüfung (#18).
        # Neutral formuliert (#50/K1): pending_review umfasst neben echten
        # Fehlschlägen auch Datenlage (keine Einträge) und unklare Einträge.
        print(
            "⚠ Manuelle Prüfung nötig für: " + ", ".join(result.pending_review),
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
