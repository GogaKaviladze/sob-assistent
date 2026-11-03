"""Statische Abschnittsdefinitionen der Op de Wisch Vorlage (FO_SVB_Berichtsvorlage_2026.docx).

9 Lebensbereiche mit Nummern '1' bis '9', Titeln und detaillierten Beschreibungen
für die semantische Klassifikation (Pass 0) und das Prompting.
"""
from __future__ import annotations

from sob_generator.llm.sections import SectionDefinition

OP_DE_WISCH_SECTION_DEFINITIONS: list[SectionDefinition] = [
    SectionDefinition(
        0,
        "1",
        "Psychische Verfassung / Selbstsorge",
        "Psychische Stabilität, emotionale Verfassung, Selbstfürsorge, "
        "Umgang mit Krisen, Ängsten oder Antriebslosigkeit, Körperhygiene.",
    ),
    SectionDefinition(
        1,
        "2",
        "Alltagsgestaltung / Tagesstruktur",
        "Tages- und Wochenstruktur, Haushaltsführung, Einkäufe, Kochen, "
        "Wäschepflege, Freizeit- und Alltagsaktivitäten.",
    ),
    SectionDefinition(
        2,
        "3",
        "Gesundheit",
        "Körperliche Gesundheit, Arztbesuche, Medikamenteneinnahme, "
        "Therapien, gesunde Lebensführung, Begleitung zu medizinischen Terminen.",
    ),
    SectionDefinition(
        3,
        "4",
        "Wohnen",
        "Erhalt der Wohnung, Wohnsituation, Ordnung und Sauberkeit in den "
        "eigenen vier Wänden, Mietangelegenheiten, Nachbarschaftskontakte.",
    ),
    SectionDefinition(
        4,
        "5",
        "Finanzen / Institutionen",
        "Geldverwaltung, Budgetplanung, Schulden, Umgang mit Ämtern und "
        "Behörden, Antragsstellung, Postbearbeitung und Administration.",
    ),
    SectionDefinition(
        5,
        "6",
        "Sozialer Lebensraum",
        "Soziale Beziehungen, Kontakte zu Angehörigen, Freunden und Bekannten, "
        "Teilhabe am gesellschaftlichen Leben, Vereinsaktivitäten, soziale Netzwerke.",
    ),
    SectionDefinition(
        6,
        "7",
        "Arbeit / Beschäftigung / Ausbildung",
        "Berufliche Perspektiven, Erwerbstätigkeit, Ausbildung, WfbM, Praktika, "
        "Tagesförderstätte oder andere arbeitsweltbezogene Beschäftigungen.",
    ),
    SectionDefinition(
        7,
        "8",
        "Zusammenarbeit",
        "Zusammenarbeit zwischen Klient*in und Assistenz/Bezugsbegleitung, "
        "Terminzuverlässigkeit, Absprachen, Reflexionsbereitschaft und Beziehungsgestaltung.",
    ),
    SectionDefinition(
        8,
        "9",
        "Sonstiges",
        "Besondere Vorkommnisse, Übergreifendes, rechtliche Betreuung, "
        "Entwicklungen, die keinem anderen Lebensbereich eindeutig zugeordnet werden können.",
    ),
]
