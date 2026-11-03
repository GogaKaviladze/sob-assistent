"""GBNF-Grammar-Erzeugung aus den Pydantic-Modellen (ADR-11).

Die Grammar wird direkt aus `Model.model_json_schema()` abgeleitet
(`llama_cpp.LlamaGrammar.from_json_schema`) — kein handgeschriebenes GBNF.
Unsere Output-Modelle sind flache Objekte mit ausschließlich `str`-Feldern;
dieser Schema-Ausschnitt wird von llama.cpps Konverter vollständig
unterstützt (siehe ADR-11).
"""
import json
from functools import lru_cache
from typing import TYPE_CHECKING, Type

from pydantic import BaseModel

if TYPE_CHECKING:
    from llama_cpp import LlamaGrammar


@lru_cache(maxsize=None)
def build_grammar(model_cls: Type[BaseModel]) -> "LlamaGrammar":
    """Erzeugt (und cached) die GBNF-Grammar für ein Pydantic-Output-Modell."""
    from llama_cpp import LlamaGrammar

    schema = model_cls.model_json_schema()
    return LlamaGrammar.from_json_schema(json.dumps(schema, ensure_ascii=False))
