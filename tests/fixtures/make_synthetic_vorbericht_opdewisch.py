"""Synthetischer Fixture-Builder für Op-de-Wisch-Vorberichte (Zero-Disk-Footprint in Tests).

Erstellt im RAM einen vollständigen synthetischen Vorbericht auf Basis von
templates/hamburg/OpDerWisch/FO_SVB_Berichtsvorlage_2026.docx mit eindeutigen
Test-Markern je Lebensbereich (Bereiche 1–9).
"""
from __future__ import annotations

import argparse
import io
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sob_generator.llm.sections_op_de_wisch import OP_DE_WISCH_SECTION_DEFINITIONS
from sob_generator.models import ClientData, ReportDraft, ReportMeta, ReportSection
from sob_generator.renderers.op_de_wisch_renderer import OpDeWischDocxRenderer

ODW_VORLAGE_PATH = ROOT_DIR / "templates" / "hamburg" / "OpDerWisch" / "FO_SVB_Berichtsvorlage_2026.docx"


def build_opdewisch_vorbericht(
    client_name: str = "Peter",
    client_vorname: str = "Hans",
    fallback: bool = False,
) -> bytes:
    """Erzeugt einen synthetischen Op-de-Wisch-Vorbericht im Speicher als DOCX-Bytes.

    Args:
        client_name: Erfundener Nachname des Klienten.
        client_vorname: Erfundener Vorname des Klienten.
        fallback: Wenn True, werden SDT-Steuerelemente entfernt/aufgelöst,
            sodass der Fallback-Parser statt des Vorlagen-Fastpaths greift.

    Returns:
        bytes der erzeugten .docx-Datei.
    """
    if not ODW_VORLAGE_PATH.exists():
        raise FileNotFoundError(f"Vorlage nicht gefunden: {ODW_VORLAGE_PATH}")

    sections = []
    for defn in OP_DE_WISCH_SECTION_DEFINITIONS:
        id_slug = defn.id.replace(".", "_")
        sections.append(
            ReportSection(
                id=defn.id,
                title=defn.title,
                previous_goals_apply=True,
                leitziel=f"ZZ_SYNTH_ODW_{id_slug}_LEITZIEL",
                smarte_ziele=f"ZZ_SYNTH_ODW_{id_slug}_SMARTE_ZIELE",
                bericht_zielerreichung=f"ZZ_SYNTH_ODW_{id_slug}_BERICHT",
                vorschlag_ziele=f"ZZ_SYNTH_ODW_{id_slug}_VORSCHLAG",
                vorschlag_massnahmen=f"ZZ_SYNTH_ODW_{id_slug}_MASSNAHMEN",
                realisierung_andere=f"ZZ_SYNTH_ODW_{id_slug}_REALISIERUNG",
            )
        )

    draft = ReportDraft(
        client=ClientData(
            name=client_name,
            vorname=client_vorname,
            geburtsdatum="01.01.1990",
            anschrift="Synthetikweg 1, 20095 Hamburg",
            gesetzliche_betreuung=True,
            gdb="50",
            pflegegrad="2",
            leistungsart="ASP",
        ),
        meta=ReportMeta(
            leistungszeitraum_von="01.01.2025",
            leistungszeitraum_bis="31.12.2025",
            abwesenheitszeiten="Keine",
            bezugsbetreuer="Synthetische Fachkraft",
        ),
        developments="ZZ_SYNTH_ODW_DEVELOPMENTS",
        sections=sections,
    )

    buf = io.BytesIO()
    renderer = OpDeWischDocxRenderer(ODW_VORLAGE_PATH)
    renderer.render(draft, buf)

    if fallback:
        import docx

        buf.seek(0)
        doc = docx.Document(buf)
        w_ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        for sdt in list(doc.element.xpath(".//w:sdt")):
            parent = sdt.getparent()
            if parent is None:
                continue
            idx = list(parent).index(sdt)
            for content in sdt.xpath("./w:sdtContent", namespaces=w_ns):
                for child in list(content):
                    parent.insert(idx, child)
                    idx += 1
            parent.remove(sdt)
        out = io.BytesIO()
        doc.save(out)
        return out.getvalue()

    return buf.getvalue()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Erstellt synthetischen Op-de-Wisch-Vorbericht im DOCX-Format.")
    parser.add_argument("--out", required=True, help="Zielpfad für die generierte .docx-Datei")
    parser.add_argument("--fallback", action="store_true", help="SDT-Elemente auflösen (Fallback-Modus)")
    args = parser.parse_args()

    data_bytes = build_opdewisch_vorbericht(fallback=args.fallback)
    out_path = Path(args.out)
    out_path.write_bytes(data_bytes)
    print(f"Synthetischer Op-de-Wisch-Vorbericht erfolgreich erstellt: {out_path} ({len(data_bytes)} Bytes)")
