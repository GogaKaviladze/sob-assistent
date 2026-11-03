"""ReportTemplate-Registry — der Skalierungs-Seam für Bereich/Land (Issue #21, #63).

Bündelt die vorlagenspezifischen Bausteine — Vorlagen-.docx, Abschnittsdefinitionen
(ADR-5/ADR-6), passenden Importer und passenden Renderer — hinter einem benannten Eintrag
mit Mandanten-Scoping (Multi-Tenancy).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from sob_generator.llm.sections import SECTION_DEFINITIONS, SectionDefinition
from sob_generator.llm.sections_op_de_wisch import OP_DE_WISCH_SECTION_DEFINITIONS
from sob_generator.renderer import HamburgDocxRenderer
from sob_generator.renderers.base import BaseDocxRenderer
from sob_generator.renderers.op_de_wisch_renderer import OpDeWischDocxRenderer

from .importer import BaseImporter, VivendiPDImporter

_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class ReportTemplate:
    """Alles, was ein konkreter Berichtstyp braucht, an einer Stelle gebündelt.

    - `key`:                 stabiler Registry-Schlüssel (z.B. in der UI-Auswahl)
    - `label`:               menschenlesbarer Name für die Oberfläche
    - `docx_path`:           Pfad zur auszufüllenden Vorlage.docx (ADR-1/ADR-10)
    - `section_definitions`: Abschnitte für die LLM-Prompts (ADR-5/ADR-6/ADR-11)
    - `importer_factory`:    erzeugt den passenden Importer für die Quelldatei
                             (ADR-4); für Hamburg `VivendiPDImporter`.
    - `tenant`:              Mandanten-Kennung (z.B. 'op_de_wisch') oder None (generisch).
    - `renderer_factory`:    erzeugt den passenden DOCX-Renderer (ADR-10 / Issue #63).
    """

    key: str
    label: str
    docx_path: Path
    section_definitions: List[SectionDefinition]
    importer_factory: Callable[..., BaseImporter]
    tenant: Optional[str] = None
    renderer_factory: Callable[..., BaseDocxRenderer] = HamburgDocxRenderer


TEMPLATE_REGISTRY: dict[str, ReportTemplate] = {
    "hamburg_soziale_dienste": ReportTemplate(
        key="hamburg_soziale_dienste",
        label="Hamburg — Soziale Dienste (Standard)",
        docx_path=_ROOT / "templates" / "hamburg" / "Vorlage.docx",
        section_definitions=SECTION_DEFINITIONS,
        importer_factory=VivendiPDImporter,
        tenant=None,
        renderer_factory=HamburgDocxRenderer,
    ),
    "hamburg_op_de_wisch": ReportTemplate(
        key="hamburg_op_de_wisch",
        label="Hamburg — Op de Wisch e.V. (Berichtsvorlage 2026)",
        docx_path=_ROOT / "templates" / "hamburg" / "OpDerWisch" / "FO_SVB_Berichtsvorlage_2026.docx",
        section_definitions=OP_DE_WISCH_SECTION_DEFINITIONS,
        importer_factory=VivendiPDImporter,
        tenant="op_de_wisch",
        renderer_factory=OpDeWischDocxRenderer,
    ),
}


def get_available_templates(tenant: Optional[str] = None) -> dict[str, ReportTemplate]:
    """Liefert die für den Mandanten zulässigen Vorlagen.

    - `tenant` ist None, leer oder 'all': alle registrierten Vorlagen (Dev- / Test-Modus).
    - `tenant` ist gesetzt (z.B. 'op_de_wisch'): Vorlagen für diesen Mandanten +
      generische Vorlagen (`tenant is None`).
    """
    if tenant is None:
        return dict(TEMPLATE_REGISTRY)

    tenant_clean = tenant.strip()
    if not tenant_clean or tenant_clean.lower() == "all":
        return dict(TEMPLATE_REGISTRY)

    tenant_norm = tenant_clean.lower()
    return {
        key: tmpl
        for key, tmpl in TEMPLATE_REGISTRY.items()
        if tmpl.tenant is None or (tmpl.tenant and tmpl.tenant.lower() == tenant_norm)
    }
