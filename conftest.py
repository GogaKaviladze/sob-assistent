"""Pytest-Bootstrap: Repo-Root auf den Importpfad legen.

Erlaubt `import sob_generator` in allen Tests.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    import llama_cpp  # noqa: F401
except ImportError:
    from unittest.mock import MagicMock
    mock_llama = MagicMock()
    class _MockGrammar:
        def __init__(self, grammar_str=""):
            self._grammar = grammar_str
        @classmethod
        def from_json_schema(cls, schema_str, **kwargs):
            return cls(grammar_str=schema_str)
    mock_llama.LlamaGrammar = _MockGrammar
    sys.modules["llama_cpp"] = mock_llama

