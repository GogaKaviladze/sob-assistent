"""Orchestrator-Guard gegen den Bericht-Opener-Kollaps (#36).

Beweist end-to-end (mit Fake-LLM), dass der Post-Pass den realen 8B-Kollaps
auflöst: ein Modell, das ohne Bans jeden Abschnitt namens-erst mit „Herr
Mustermann hat …" eröffnet, wird durch die Ban-Direktive auf distinkte
Eröffnungen gezwungen — bis höchstens 2 Abschnitte denselben 3-Wort-Anfang
teilen (Akzeptanzkriterium #36).
"""
from sob_generator.llm.generator import GenerationResult
from sob_generator.llm.schemas import DevelopmentsLLMOutput, DocumentationEntry, SectionLLMOutput
from sob_generator.llm.sections import SECTION_DEFINITIONS
from sob_generator.importer import VivendiImport
from sob_generator.models import ClientData, ReportMeta
from sob_generator.opener_diversity import opener_key
from sob_generator.orchestrator import build_draft


class _CollapsingGenerator:
    """Fake-LLM, das den #36-Kollaps reproduziert und auf Bans reagiert.

    - Ohne `forbidden_openers`: jeder Abschnitt eröffnet identisch namens-erst
      (genau der reale 8B-Befund — alle „Herr Mustermann hat …").
    - Mit `forbidden_openers`: weicht auf eine je Abschnitt eindeutige,
      themengeführte Eröffnung aus, die keinen verbotenen Anfang trägt.
    So wie ein instruktionsfolgendes Modell auf die konkrete Ban-Direktive
    reagieren würde — der Guard muss diese Reaktion korrekt orchestrieren.
    """

    COLLAPSE = "Herr Mustermann hat sich im Berichtszeitraum entwickelt."

    def __init__(self):
        self.forbidden_seen: list[tuple[str, ...]] = []

    def classify_entry(self, entry, sections):
        # Testeinträge nutzen die Abschnitts-ID direkt als Kategorie.
        return next((s.id for s in sections if s.id == entry.kategorie), sections[0].id)

    def generate_section(self, section, entries, forbidden_openers=(), **kwargs):
        self.forbidden_seen.append(forbidden_openers)
        if not forbidden_openers:
            text = self.COLLAPSE
        else:
            # Eindeutige, nicht namens-erste Ausweich-Eröffnung je Abschnitt.
            text = (
                f"Im Bereich {section.title} zeigten sich im Berichtszeitraum "
                f"konkrete Fortschritte."
            )
        return GenerationResult(
            value=SectionLLMOutput(
                bericht_zielerreichung=text,
                # #48: Vorschlag-Felder sind jetzt Non-Empty-Pflichtfelder.
                vorschlag_ziele="Ziel.",
                vorschlag_massnahmen="Die Assistenz unterstützt.",
                realisierung_andere="",
            ),
            needs_review=False,
            raw_output="<fake>",
        )

    def generate_developments(self, entries, **kwargs):
        return GenerationResult(
            value=DevelopmentsLLMOutput(developments="Entwicklung."),
            needs_review=False,
            raw_output="<fake>",
        )


def _imported():
    return VivendiImport(
        client=ClientData(name="Mustermann", vorname="Max", anrede="Herr"),
        meta=ReportMeta(),
        entries=[
            # Klassifizierung ausschließlich über generator.classify_entry —
            # Kategorie = Abschnitts-ID (Testkonvention, siehe _CollapsingGenerator).
            DocumentationEntry(datum="01.01.2026", kategorie="1.1", text="Wohnverhältnisse."),
            DocumentationEntry(datum="01.01.2026", kategorie="1.2", text="Haushalt."),
            DocumentationEntry(datum="01.01.2026", kategorie="1.3", text="Grundpflege."),
            DocumentationEntry(datum="01.01.2026", kategorie="1.4", text="Psych."),
            DocumentationEntry(datum="01.01.2026", kategorie="1.5", text="Arzt."),
            DocumentationEntry(datum="01.01.2026", kategorie="2.1", text="Angehörige."),
            DocumentationEntry(datum="01.01.2026", kategorie="2.2", text="Freizeit."),
            DocumentationEntry(datum="01.01.2026", kategorie="2.3", text="Logopädie."),
            DocumentationEntry(datum="01.01.2026", kategorie="3", text="Beruf."),
        ],
    )


def test_guard_loest_namens_ersten_kollaps_auf():
    gen = _CollapsingGenerator()

    draft = build_draft(_imported(), gen, SECTION_DEFINITIONS)

    openers = [opener_key(s.bericht_zielerreichung) for s in draft.sections]
    # Akzeptanzkriterium #36: höchstens 2 Abschnitte mit gleichem 3-Wort-Anfang.
    assert all(openers.count(o) <= 2 for o in set(openers))
    # Der kollabierte Anfang wurde tatsächlich aufgebrochen.
    assert openers.count(opener_key(_CollapsingGenerator.COLLAPSE)) <= 2


def test_guard_uebergibt_kollidierende_eroeffnungen_als_ban():
    gen = _CollapsingGenerator()

    build_draft(_imported(), gen, SECTION_DEFINITIONS)

    # Erste 9 Calls (Phase 1) ohne Bans, danach Regenerations-Calls MIT Ban,
    # deren Verbotsliste den kollabierten Opener enthält.
    regen_bans = [f for f in gen.forbidden_seen if f]
    assert regen_bans, "Guard hätte mindestens einen Abschnitt neu erzeugen müssen"
    assert any("herr mustermann hat" in f for ban in regen_bans for f in ban)


def test_guard_ist_no_op_wenn_eroeffnungen_schon_divers():
    class _DiverseGenerator(_CollapsingGenerator):
        def generate_section(self, section, entries, forbidden_openers=(), **kwargs):
            self.forbidden_seen.append(forbidden_openers)
            return GenerationResult(
                value=SectionLLMOutput(
                    bericht_zielerreichung=f"Abschnitt {section.id} berichtet konkret.",
                    vorschlag_ziele="Ziel.",
                    vorschlag_massnahmen="Maßnahme.",
                    realisierung_andere="",
                ),
                needs_review=False,
                raw_output="<fake>",
            )

    gen = _DiverseGenerator()
    build_draft(_imported(), gen, SECTION_DEFINITIONS)

    # Keine Regeneration: alle 9 Calls ohne Bans, kein zusätzlicher Pass.
    assert len(gen.forbidden_seen) == len(SECTION_DEFINITIONS)
    assert all(f == () for f in gen.forbidden_seen)
