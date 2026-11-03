"""Renderer-Verhalten für Aufzählungsfelder.

Bug-Fix: `vorschlag_massnahmen` wurde als Fließtext gerendert, sodass die
einzelnen Maßnahmen ineinander übergingen. Sie werden jetzt — wie
`vorschlag_ziele`/`smarte_ziele` — als getrennte Aufzählungspunkte gerendert
(`set_formtext_list`, ADR-10).
"""
import zipfile
from pathlib import Path

from sob_generator import docx_fields as fields
from sob_generator.models import ClientData, ReportDraft, ReportMeta, ReportSection
from sob_generator.renderer import NO_ENTRIES_HINT, REVIEW_MARKER, DocxRenderer

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DOCX = ROOT / "templates" / "hamburg" / "Vorlage.docx"


def test_massnahmen_werden_als_getrennte_bullets_gerendert(tmp_path):
    draft = ReportDraft(
        client=ClientData(),
        meta=ReportMeta(),
        sections=[
            ReportSection(
                id="1.1",
                title="Wohnsituation",
                vorschlag_massnahmen=(
                    "Die Assistenz reflektiert die Bedürfnisse.\n"
                    "- Die Assistenz übt das Erkennen von Stress.\n"
                    "- Die Assistenz motiviert zur Terminwahrnehmung."
                ),
            )
        ],
    )
    out = tmp_path / "renderer_out.docx"
    DocxRenderer(TEMPLATE_DOCX).render(draft, out)

    # 1. Maßnahme im Bookmark-Absatz, die weiteren in Folge-Absätzen (ADR-10).
    root = fields.load_document_tree(out)
    assert "reflektiert die Bedürfnisse" in fields.get_formtext(
        root, "sections_0_vorschlag_massnahmen"
    )
    with zipfile.ZipFile(out) as z:
        document_xml = z.read("word/document.xml").decode("utf-8")
    assert "Erkennen von Stress" in document_xml  # zweiter Punkt
    assert "motiviert zur Terminwahrnehmung" in document_xml  # dritter Punkt


def _draft_with_section(section: ReportSection) -> ReportDraft:
    return ReportDraft(client=ClientData(), meta=ReportMeta(), sections=[section])


def test_no_entries_rendert_datenlage_hinweis_statt_fehlermarker(tmp_path):
    # #50/K1: keine zugeordneten Einträge = Datenlage, kein Fehlschlag.
    draft = _draft_with_section(
        ReportSection(id="1.1", title="Wohnsituation", needs_review=True, no_entries=True)
    )
    out = tmp_path / "no_entries.docx"
    DocxRenderer(TEMPLATE_DOCX).render(draft, out)

    root = fields.load_document_tree(out)
    text = fields.get_formtext(root, "sections_0_bericht_zielerreichung")
    assert text == NO_ENTRIES_HINT
    assert REVIEW_MARKER not in text


def test_echter_fehlschlag_rendert_weiter_den_fehlermarker(tmp_path):
    # #50/K1: needs_review ohne no_entries bleibt der bestehende Fehler-Pfad.
    draft = _draft_with_section(
        ReportSection(id="1.1", title="Wohnsituation", needs_review=True, no_entries=False)
    )
    out = tmp_path / "failed.docx"
    DocxRenderer(TEMPLATE_DOCX).render(draft, out)

    root = fields.load_document_tree(out)
    text = fields.get_formtext(root, "sections_0_bericht_zielerreichung")
    assert REVIEW_MARKER in text
    assert NO_ENTRIES_HINT not in text


def test_manuell_ergaenzter_text_gewinnt_gegen_datenlage_hinweis(tmp_path):
    # Review-Edit der Fachkraft (#23) darf nicht vom Hinweis überschrieben werden.
    draft = _draft_with_section(
        ReportSection(
            id="1.1",
            title="Wohnsituation",
            bericht_zielerreichung="Manuell ergänzter Bericht.",
            needs_review=False,
            no_entries=True,
        )
    )
    out = tmp_path / "manual.docx"
    DocxRenderer(TEMPLATE_DOCX).render(draft, out)

    root = fields.load_document_tree(out)
    text = fields.get_formtext(root, "sections_0_bericht_zielerreichung")
    assert text == "Manuell ergänzter Bericht."


def _render_client_only(tmp_path, client: ClientData):
    draft = ReportDraft(client=client, meta=ReportMeta(), sections=[])
    out = tmp_path / "renderer_client.docx"
    DocxRenderer(TEMPLATE_DOCX).render(draft, out)
    return fields.load_document_tree(out)


def test_bericht_fuer_wird_als_name_komma_vorname_gerendert(tmp_path):
    # #50/K3: Feld #1 „Bericht für (Name, Vorname)" — ein Formularfeld,
    # ADR-5 trennt name/vorname → Renderer fügt im Vorlagen-Format zusammen.
    root = _render_client_only(
        tmp_path, ClientData(name="Mustermann", vorname="Max")
    )
    assert fields.get_formtext(root, "client_name_vorname") == "Mustermann, Max"


def test_bericht_fuer_laesst_fehlende_teile_kommalos_weg(tmp_path):
    root = _render_client_only(tmp_path, ClientData(name="Mustermann"))
    assert fields.get_formtext(root, "client_name_vorname") == "Mustermann"


def test_leistungsart_wird_gerendert(tmp_path):
    # #50/K3: Leistungsart kommt vom Aufrufer (nicht aus der CSV) und landet
    # in Feld #13.
    root = _render_client_only(
        tmp_path, ClientData(name="Mustermann", leistungsart="ASP")
    )
    assert fields.get_formtext(root, "client_leistungsart") == "ASP"
