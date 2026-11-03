"""Schnelle, reproduzierbare Dialogevaluation für Issue 76."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from sob_generator.chat_engine import ChatDialogueEngine
from sob_generator.chat_session import ChatSession, DialoguePhase, SectionChatState
from sob_generator.chat_store import ChatSessionStore
from sob_generator.llm.schemas import DocumentationEntry
from sob_generator.models import ClientData

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES_PATH = REPO_ROOT / "evals" / "issue76_cases.json"
DEFAULT_RESULTS_PATH = REPO_ROOT / "output" / "evals" / "issue76_results.json"
DEFAULT_CACHE_PATH = REPO_ROOT / "output" / "evals" / "issue76_cache.json"


@dataclass(frozen=True)
class EvaluationResult:
    case_id: str
    title: str
    passed: bool
    violations: list[str]
    duration_seconds: float
    replies: list[str]
    phase: str
    goals: str
    notes: list[str]
    uncertainties: list[str]
    cached: bool = False


def load_cases(path: Path = DEFAULT_CASES_PATH) -> list[dict[str, Any]]:
    cases = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise ValueError("Die Evaluationsdatei muss eine nicht leere Liste enthalten.")
    ids = [case.get("id") for case in cases]
    if any(not case_id for case_id in ids) or len(ids) != len(set(ids)):
        raise ValueError("Jeder Evaluationsfall benötigt eine eindeutige ID.")
    return cases


def _repository_revision() -> str:
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    changes = subprocess.run(
        ["git", "diff", "--no-ext-diff", "--binary", "HEAD", "--", "sob_generator", "app"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
    )
    payload = (revision.stdout.strip() or "unbekannt").encode("utf-8") + changes.stdout
    return hashlib.sha256(payload).hexdigest()


def _cache_key(case: dict[str, Any], generator_name: str) -> str:
    payload = {
        "case": case,
        "generator": generator_name,
        "revision": _repository_revision(),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _load_cache(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _build_session(case: dict[str, Any]) -> ChatSession:
    section_id = case.get("section_id", "1.1")
    section_title = case.get("section_title", "Wohnsituation")
    session = ChatSession(
        client=ClientData(name="Mustermann", vorname="Max", anrede="Herr"),
        phase=DialoguePhase.FIELD_REFLECT,
        active_section_id=section_id,
        sections={
            section_id: SectionChatState(
                section_id=section_id,
                title=section_title,
                smarte_ziele=case.get("previous_goal", ""),
            )
        },
    )
    session.memory.section_entries = {
        section_id: [DocumentationEntry.model_validate(entry) for entry in case.get("entries", [])]
    }
    return session


def evaluate_state(case: dict[str, Any], session: ChatSession, replies: list[str]) -> list[str]:
    """Prüft ausschließlich harte, nachvollziehbare Akzeptanzregeln."""
    expected = case.get("expected", {})
    state = session.sections[session.active_section_id]
    combined_replies = "\n".join(replies)
    violations: list[str] = []

    if re.search(r"(?:^|[.!?]\s)Max\s*[,!]", combined_replies, re.IGNORECASE):
        violations.append("Die betreute Person wird direkt angesprochen.")
    if re.search(r"(?:\[\s*\{\s*\"|\{\s*\"(?:goals|reply|zielinhalt)\")", combined_replies):
        violations.append("Technisches JSON ist im Chat sichtbar.")

    for phrase in expected.get("forbidden_phrases", []):
        if phrase.casefold() in combined_replies.casefold():
            violations.append(f"Verbotene Formulierung in der Antwort: {phrase}")
    for phrase in expected.get("required_phrases", []):
        if phrase.casefold() not in (combined_replies + "\n" + state.current_goals).casefold():
            violations.append(f"Erwartete Information fehlt: {phrase}")

    goal_state = expected.get("goal_state")
    if goal_state == "empty" and state.current_goals.strip():
        violations.append("Es wurde ohne erwartete Grundlage ein Ziel erzeugt.")
    if goal_state == "present" and not state.current_goals.strip():
        violations.append("Trotz vollständigem Zielauftrag wurde kein Zielvorschlag erzeugt.")
    if expected.get("last_reply_has_question") and (not replies or "?" not in replies[-1]):
        violations.append("Die erwartete fachliche Rückfrage fehlt.")

    notes_text = "\n".join(state.user_notes)
    for phrase in expected.get("notes_contain", []):
        if phrase.casefold() not in notes_text.casefold():
            violations.append(f"Erwartete korrigierte Notiz fehlt: {phrase}")
    for phrase in expected.get("notes_forbid", []):
        if phrase.casefold() in notes_text.casefold():
            violations.append(f"Überholte Notiz wurde nicht entfernt: {phrase}")
    uncertainty_text = "\n".join(state.uncertainties)
    for phrase in expected.get("uncertainty_contains", []):
        if phrase.casefold() not in uncertainty_text.casefold():
            violations.append(f"Unsicherheit wurde nicht erhalten: {phrase}")
    return violations


async def run_case(case: dict[str, Any], generator: Any) -> EvaluationResult:
    session = _build_session(case)
    store = ChatSessionStore()
    store.store(session)
    engine = ChatDialogueEngine(generator, session_store=store)
    replies: list[str] = []
    started = time.monotonic()
    for message in case.get("messages", []):
        before = len(session.messages)
        session = await engine.process_message(session.session_id, message)
        replies.extend(item.content for item in session.messages[before:] if item.role.value == "assistant")
    duration = round(time.monotonic() - started, 3)
    state = session.sections[session.active_section_id]
    violations = evaluate_state(case, session, replies)
    return EvaluationResult(
        case_id=case["id"],
        title=case.get("title", case["id"]),
        passed=not violations,
        violations=violations,
        duration_seconds=duration,
        replies=replies,
        phase=session.phase.value,
        goals=state.current_goals,
        notes=list(state.user_notes),
        uncertainties=list(state.uncertainties),
    )


async def run_suite(
    cases: Iterable[dict[str, Any]],
    generator: Any,
    *,
    use_cache: bool = True,
    cache_path: Path = DEFAULT_CACHE_PATH,
) -> list[EvaluationResult]:
    model_path = str(getattr(generator, "_model_path", ""))
    generator_name = f"{generator.__class__.__name__}:{model_path}"
    cache = _load_cache(cache_path) if use_cache else {}
    results: list[EvaluationResult] = []
    for case in cases:
        key = _cache_key(case, generator_name)
        if key in cache:
            cached_value = {**cache[key], "cached": True}
            results.append(EvaluationResult(**cached_value))
            continue
        result = await run_case(case, generator)
        results.append(result)
        cache[key] = asdict(result)
    if use_cache:
        _write_json(cache_path, cache)
    return results


def write_results(results: Iterable[EvaluationResult], path: Path = DEFAULT_RESULTS_PATH) -> Path:
    result_list = list(results)
    payload = {
        "summary": {
            "total": len(result_list),
            "passed": sum(result.passed for result in result_list),
            "failed": sum(not result.passed for result in result_list),
        },
        "results": [asdict(result) for result in result_list],
    }
    _write_json(path, payload)
    return path
