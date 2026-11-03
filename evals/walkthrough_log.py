"""Persistiert einen synthetischen Notebook-Dialog für spätere Analysen."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sob_generator.chat_engine import ChatDialogueEngine
from sob_generator.chat_session import ChatSession


def _state_snapshot(session: ChatSession) -> dict[str, Any]:
    sections = {}
    for section_id, state in session.sections.items():
        sections[section_id] = {
            "title": state.title,
            "selected": state.is_active_field,
            "status": state.status.value,
            "notes": list(state.user_notes),
            "uncertainties": list(state.uncertainties),
            "goals": state.current_goals,
            "pending_question": state.pending_question,
            "draft": state.bericht_zielerreichung,
        }
    return {
        "session_id": session.session_id,
        "phase": session.phase.value,
        "active_section_id": session.active_section_id,
        "message_count": len(session.messages),
        "developments": session.developments,
        "sections": sections,
    }


def write_walkthrough_log(
    engine: ChatDialogueEngine,
    session: ChatSession,
    output_dir: Path,
) -> tuple[Path, Path]:
    """Schreibt sichtbaren Verlauf und internen Zustand als stabile latest-Dateien."""
    output_dir.mkdir(parents=True, exist_ok=True)
    markdown, _ = engine.export_markdown(session.session_id)
    markdown_path = output_dir / "app_walkthrough_latest.md"
    state_path = output_dir / "app_walkthrough_latest_state.json"
    markdown_path.write_text(markdown, encoding="utf-8")
    state_path.write_text(
        json.dumps(_state_snapshot(session), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return markdown_path, state_path
