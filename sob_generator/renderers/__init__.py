"""Renderers package for pluggable template rendering (ADR-10, Issue #63)."""
from __future__ import annotations

from .base import BaseDocxRenderer
from .op_de_wisch_renderer import OpDeWischDocxRenderer
from sob_generator.renderer import HamburgDocxRenderer, DocxRenderer

__all__ = ["BaseDocxRenderer", "HamburgDocxRenderer", "DocxRenderer", "OpDeWischDocxRenderer"]

