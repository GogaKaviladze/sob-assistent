#!/usr/bin/env python3
"""CLI für die reproduzierbare Issue-76-Dialogevaluation."""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.issue76_runner import load_cases, run_suite, write_results
from sob_generator.stub_generator import StubDraftGenerator


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prüft die Dialogqualität für Issue 76.")
    parser.add_argument("--mode", choices=("stub", "model"), default="stub")
    parser.add_argument("--case", action="append", dest="case_ids", help="Nur diese Fall-ID ausführen.")
    parser.add_argument("--model", default="models/Qwen2.5-7B-Instruct-Q4_K_M.gguf")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    cases = load_cases()
    if args.case_ids:
        selected = set(args.case_ids)
        cases = [case for case in cases if case["id"] in selected]
        missing = selected - {case["id"] for case in cases}
        if missing:
            print(f"Unbekannte Testfälle: {', '.join(sorted(missing))}", file=sys.stderr)
            return 2

    if args.mode == "stub":
        generator = StubDraftGenerator()
    else:
        from sob_generator.llm.generator import LLMDraftGenerator
        generator = LLMDraftGenerator(str(REPO_ROOT / args.model), verbose=False)

    results = await run_suite(cases, generator, use_cache=not args.no_cache)
    path = write_results(results, args.output or None) if args.output else write_results(results)
    for result in results:
        marker = "BESTANDEN" if result.passed else "FEHLER"
        cache_marker = " (Cache)" if result.cached else ""
        print(f"[{marker}] {result.case_id} — {result.duration_seconds:.3f}s{cache_marker}")
        for violation in result.violations:
            print(f"  - {violation}")
    print(f"Ergebnisse: {path}")
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
