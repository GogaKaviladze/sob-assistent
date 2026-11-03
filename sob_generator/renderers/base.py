"""Base protocol for DOCX renderers (ADR-10, Issue #63)."""
from __future__ import annotations

from pathlib import Path
from typing import BinaryIO, Protocol, Union

from sob_generator.models import ReportDraft


class BaseDocxRenderer(Protocol):
    """Schnittstelle für vorlagenspezifische DOCX-Renderer."""

    def render(
        self, draft: ReportDraft, output_path: Union[Path, str, BinaryIO]
    ) -> Union[Path, BinaryIO]:
        """Befüllt die DOCX-Vorlage aus dem ReportDraft und speichert nach output_path (Datei oder Stream)."""
        ...
