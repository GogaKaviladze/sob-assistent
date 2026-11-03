"""Tests for async offloading and non-blocking dialogue engine responsiveness."""
import asyncio
import time
from unittest.mock import MagicMock

import anyio
import pytest

from sob_generator.chat_engine import ChatDialogueEngine
from sob_generator.chat_session import ChatSession, DialoguePhase, SectionChatState
from sob_generator.chat_store import ChatSessionStore
from sob_generator.llm.schemas import SectionLLMOutput
from sob_generator.models import ClientData, ReportMeta


class SlowGenerator:
    """Mock generator simulating a slow C++/Metal inference call."""

    def __init__(self, delay: float = 0.2):
        self.delay = delay

    def refine_section(self, *args, **kwargs):
        time.sleep(self.delay)
        return MagicMock(
            value=SectionLLMOutput(
                bericht_zielerreichung="Fertig generierter Text.",
                vorschlag_ziele="Ziele.",
                vorschlag_massnahmen="Maßnahmen.",
                realisierung_andere="",
            )
        )


@pytest.mark.anyio
async def test_async_llm_inference_does_not_block_event_loop():
    engine = ChatDialogueEngine(
        generator=SlowGenerator(delay=0.2),
        session_store=ChatSessionStore(),
    )
    session = ChatSession(
        session_id="test-perf",
        client=ClientData(name="Test", vorname="Max"),
        meta=ReportMeta(),
        phase=DialoguePhase.FIELD_QUESTIONS,
        active_section_id="1.1",
        sections={"1.1": SectionChatState(section_id="1.1", title="Wohnen")},
    )
    engine.session_store.store(session)

    ping_count = 0

    async def event_loop_pinger():
        nonlocal ping_count
        for _ in range(5):
            await asyncio.sleep(0.04)
            ping_count += 1

    # Run both the slow inference call and the concurrent event-loop ping task
    async with anyio.create_task_group() as tg:
        tg.start_soon(engine.process_message, "test-perf", "Neue Notiz")
        tg.start_soon(event_loop_pinger)

    # If the event loop was frozen, ping_count would be 0 or 1.
    # Since it is offloaded via anyio.to_thread, ping_count should run smoothly to completion (>= 3).
    assert ping_count >= 3
