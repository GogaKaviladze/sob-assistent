"""Behavioral regression coverage for streaming and scroll ownership (#86)."""
import shutil
import subprocess
from pathlib import Path


def test_chat_scroll_behavior():
    node = shutil.which("node")
    assert node is not None, "Node.js is required for chat scroll behavior tests."
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("chat_scroll_behavior.cjs"))],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
