"""Statische Abschnittsdefinitionen der Vorlage.docx (ADR-5, ADR-6).

Titel + Beschreibung gehen pro Abschnitt in den Prompt (ADR-11), damit das
LLM selbst entscheiden kann, welche DocumentationEntry-Einträge zu diesem
Abschnitt passen.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class SectionDefinition:
    index: int  # ReportDraft.sections[index], 0..8
    id: str  # Vorlage-Nummerierung, z.B. "1.1"
    title: str
    description: str


SECTION_DEFINITIONS: list[SectionDefinition] = [
    SectionDefinition(
        0, "1.1", "Wohnsituation",
        "Wohnform, Wohnumfeld, Umgang mit der eigenen Wohnung "
        "(z.B. Ordnung, Mietangelegenheiten, Nachbarschaft).",
    ),
    SectionDefinition(
        1, "1.2", "Alltägliche Lebensführung / Hauswirtschaft",
        "Haushaltsaufgaben, Einkäufe, Wäsche, Kochen, Tagesstruktur und "
        "Planung des Alltags.",
    ),
    SectionDefinition(
        2, "1.3", "Basisversorgung / Grundpflege",
        "Körperpflege, Ernährung, An- und Ausziehen, Erledigung "
        "grundlegender Versorgungsbedürfnisse.",
    ),
    SectionDefinition(
        3, "1.4", "Emotionale und psychische Entwicklung",
        "Umgang mit Gefühlen, Stressbewältigung, Selbstwahrnehmung, "
        "psychotherapeutische Begleitung.",
    ),
    SectionDefinition(
        4, "1.5", "Gesundheitsförderung und -erhaltung",
        "Arzttermine, gesundheitsbezogene Gewohnheiten, Begleitung zu "
        "medizinischen oder behördlichen Terminen.",
    ),
    SectionDefinition(
        5, "2.1", "Gestaltung sozialer Beziehungen",
        "Kontakte zu Familie, Freunden und Bezugspersonen, Umgang mit "
        "Nähe und Konflikten im sozialen Umfeld.",
    ),
    SectionDefinition(
        6, "2.2", "Teilnahme am kulturellen und gesellschaftlichen Leben",
        "Freizeitaktivitäten, Vereine, Veranstaltungen, Mobilität und "
        "Teilhabe am öffentlichen Leben.",
    ),
    SectionDefinition(
        7, "2.3", "Kommunikation und Orientierung",
        "Sprachliche Ausdrucksfähigkeit, Verständigung mit anderen, "
        "Orientierung in Alltagssituationen, administrative Angelegenheiten.",
    ),
    SectionDefinition(
        8, "3", "Beschäftigung, Arbeit, Ausbildung",
        "Schule, Ausbildung, Arbeit, WfbM oder vergleichbare Tagesstruktur "
        "sowie berufliche Perspektiven.",
    ),
]
