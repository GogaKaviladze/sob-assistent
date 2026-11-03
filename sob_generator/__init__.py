"""sob-generator — End-to-End-Durchstich (Walking Skeleton, Issue #17).

Verkettet die Module (`docx_fields`, `llm.*` unter `sob_generator/`):
CSV-Import (ADR-4) → ReportDraft (ADR-5) → LLM-Generierung (ADR-11) → docx-Render (ADR-10).
"""
from __future__ import annotations
