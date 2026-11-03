"""Globaler Test-Guard und Pytest-Konfiguration für sob-generator (Issue #140).

Verhindert zuverlässig, dass Unit-Tests unbemerkt echte GGUF-Modelle aus models/
in den Arbeitsspeicher oder GPU-VRAM allokieren (Schutz vor Watchdog-Timeouts und Kernel Panics).
"""
import pytest
from chat_app.chatbot import LlamaCppProvider


@pytest.fixture(autouse=True)
def guard_no_real_gguf_in_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verhindert, dass in Unit-Tests versehentlich echte GGUF-Modelle in VRAM/RAM geladen werden."""
    orig_init = LlamaCppProvider.__init__

    def blocked_init(self, *args, **kwargs):
        raise RuntimeError("Echtes GGUF-Modell darf in Unit-Tests nicht geladen werden")

    monkeypatch.setattr(LlamaCppProvider, "__init__", blocked_init)
