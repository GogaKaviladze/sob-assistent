from pathlib import Path

from sob_generator.docx_fields import (
    get_checkbox,
    get_formtext,
    load_document_tree,
    save_document_tree,
    set_checkbox,
    set_formtext,
)

VORLAGE = Path(__file__).resolve().parents[1] / "templates" / "hamburg" / "Vorlage.docx"


def test_formtext_roundtrip(tmp_path):
    root = load_document_tree(VORLAGE)
    set_formtext(root, "client_geburtsdatum", "01.01.1990")

    out = tmp_path / "out.docx"
    save_document_tree(VORLAGE, root, out)

    root2 = load_document_tree(out)
    assert get_formtext(root2, "client_geburtsdatum") == "01.01.1990"


def test_checkbox_roundtrip(tmp_path):
    root = load_document_tree(VORLAGE)
    set_checkbox(root, "client_gesetzliche_betreuung_ja", True)
    set_checkbox(root, "client_gesetzliche_betreuung_nein", False)

    out = tmp_path / "out.docx"
    save_document_tree(VORLAGE, root, out)

    root2 = load_document_tree(out)
    assert get_checkbox(root2, "client_gesetzliche_betreuung_ja") is True
    assert get_checkbox(root2, "client_gesetzliche_betreuung_nein") is False


def test_section_text_roundtrip(tmp_path):
    root = load_document_tree(VORLAGE)
    set_formtext(root, "sections_0_leitziel", "Testziel 1.1")
    set_formtext(root, "sections_8_realisierung_andere", "Testtext 3")

    out = tmp_path / "out.docx"
    save_document_tree(VORLAGE, root, out)

    root2 = load_document_tree(out)
    assert get_formtext(root2, "sections_0_leitziel") == "Testziel 1.1"
    assert get_formtext(root2, "sections_8_realisierung_andere") == "Testtext 3"
