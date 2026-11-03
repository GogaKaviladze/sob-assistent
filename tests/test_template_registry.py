"""ReportTemplate-Registry — der Skalierungs-Seam (Issue #21, #63).

Sichert ab, dass:
- `hamburg_soziale_dienste` (Standard) und `hamburg_op_de_wisch` (Op de Wisch e.V.)
  korrekt registriert sind.
- Mandanten-Scoping über `get_available_templates` strikte Isolation gewährleistet.
"""
from sob_generator.importer import VivendiPDImporter
from sob_generator.llm.sections import SECTION_DEFINITIONS
from sob_generator.llm.sections_op_de_wisch import OP_DE_WISCH_SECTION_DEFINITIONS
from sob_generator.renderer import HamburgDocxRenderer
from sob_generator.renderers.op_de_wisch_renderer import OpDeWischDocxRenderer
from sob_generator.report_template import (
    TEMPLATE_REGISTRY,
    ReportTemplate,
    get_available_templates,
)

HAMBURG_KEY = "hamburg_soziale_dienste"
OP_DE_WISCH_KEY = "hamburg_op_de_wisch"


def test_registry_contains_expected_templates():
    assert HAMBURG_KEY in TEMPLATE_REGISTRY
    assert OP_DE_WISCH_KEY in TEMPLATE_REGISTRY
    assert set(TEMPLATE_REGISTRY.keys()) == {HAMBURG_KEY, OP_DE_WISCH_KEY}


def test_hamburg_entry_bundles_expected_template():
    template = TEMPLATE_REGISTRY[HAMBURG_KEY]

    assert isinstance(template, ReportTemplate)
    assert template.key == HAMBURG_KEY
    assert template.label == "Hamburg — Soziale Dienste (Standard)"
    assert template.tenant is None
    assert template.renderer_factory is HamburgDocxRenderer


def test_template_labels_are_carrier_neutral():
    # Issue #65: Keine herstellerspezifischen Namen in Template-Labels
    for key, tmpl in TEMPLATE_REGISTRY.items():
        assert "vivendi" not in tmpl.label.lower()
    assert TEMPLATE_REGISTRY[OP_DE_WISCH_KEY].label == "Hamburg — Op de Wisch e.V. (Berichtsvorlage 2026)"
    assert TEMPLATE_REGISTRY[HAMBURG_KEY].label == "Hamburg — Soziale Dienste (Standard)"


def test_hamburg_entry_uses_the_nine_section_definitions():
    template = TEMPLATE_REGISTRY[HAMBURG_KEY]

    assert template.section_definitions is SECTION_DEFINITIONS
    assert len(template.section_definitions) == 9
    assert [s.id for s in template.section_definitions] == [
        "1.1", "1.2", "1.3", "1.4", "1.5", "2.1", "2.2", "2.3", "3",
    ]


def test_hamburg_entry_uses_vivendi_importer():
    template = TEMPLATE_REGISTRY[HAMBURG_KEY]

    assert template.importer_factory is VivendiPDImporter


def test_hamburg_template_docx_exists():
    template = TEMPLATE_REGISTRY[HAMBURG_KEY]

    assert template.docx_path.is_file()
    assert template.docx_path.name == "Vorlage.docx"


def test_op_de_wisch_entry_bundles_expected_template():
    template = TEMPLATE_REGISTRY[OP_DE_WISCH_KEY]

    assert isinstance(template, ReportTemplate)
    assert template.key == OP_DE_WISCH_KEY
    assert "Op de Wisch" in template.label
    assert template.tenant == "op_de_wisch"
    assert template.renderer_factory is OpDeWischDocxRenderer
    assert template.importer_factory is VivendiPDImporter
    assert template.docx_path.is_file()
    assert template.docx_path.name == "FO_SVB_Berichtsvorlage_2026.docx"


def test_op_de_wisch_entry_uses_nine_section_definitions():
    template = TEMPLATE_REGISTRY[OP_DE_WISCH_KEY]

    assert template.section_definitions is OP_DE_WISCH_SECTION_DEFINITIONS
    assert len(template.section_definitions) == 9
    assert [s.id for s in template.section_definitions] == [
        "1", "2", "3", "4", "5", "6", "7", "8", "9",
    ]


def test_tenant_scoping_op_de_wisch():
    available = get_available_templates("op_de_wisch")
    assert OP_DE_WISCH_KEY in available
    assert HAMBURG_KEY in available  # Öffentlicher/generischer Standard ist zugänglich
    assert len(available) == 2


def test_tenant_scoping_other_facility():
    # Ein fremder Träger (z. B. 'drk') darf Op de Wisch nicht sehen
    available = get_available_templates("drk")
    assert OP_DE_WISCH_KEY not in available
    assert HAMBURG_KEY in available
    assert len(available) == 1


def test_tenant_scoping_dev_mode():
    # None oder 'all' liefert alle Vorlagen
    assert get_available_templates(None) == TEMPLATE_REGISTRY
    assert get_available_templates("all") == TEMPLATE_REGISTRY
    assert get_available_templates("") == TEMPLATE_REGISTRY
