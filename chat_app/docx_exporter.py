"""DOCX-Export-Adapter für die Reflexions-Chat-App (Issue #105, #114, ADR-10, Zero-Disk).

Überführt den internen Zustand des ReflectionChatbot (PlanData, CsvData, Dialogverlauf)
in ein standardkonformes ReportDraft-Modell und befüllt die gewählte Word-Vorlage
(Hamburg-Fachamt oder Op-de-Wisch) über die zentrale ReportTemplate-Registry.
Die Erzeugung erfolgt vollständig im flüchtigen Arbeitsspeicher (io.BytesIO, Zero-Disk).
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union

from sob_generator.llm.sections import SECTION_DEFINITIONS
from sob_generator.models import ClientData, ReportDraft, ReportMeta, ReportSection
from sob_generator.report_template import TEMPLATE_REGISTRY, ReportTemplate

if TYPE_CHECKING:
    from .chatbot import ReflectionChatbot


class DocxExportError(Exception):
    """Basis-Ausnahme für Fehler beim Word-Export."""


class EmptyExportError(DocxExportError):
    """Wird ausgelöst, wenn keinerlei exportierbare Daten vorliegen."""


class UnknownTemplateError(DocxExportError):
    """Wird ausgelöst, wenn eine unbekannte oder fehlende Vorlage angefragt wird."""


class UnmappedChatContentError(DocxExportError):
    """Wird ausgelöst, wenn Dialog-Inhalte vorliegen, aber kein einziger Abschnitt zugeordnet werden konnte."""

    def __init__(self, message: str, unmapped_blocks: Optional[List[UnmappedBlock]] = None):
        super().__init__(message)
        self.unmapped_blocks = unmapped_blocks or []


#: Maximale Länge eines Überschriften-Labels in ❌-Warnungen (DSGVO: kein Volltext-Echo).
MAX_WARNING_LABEL_CHARS = 60

#: Manuell-Eintrag, wenn kein verlässlicher Klientenname vorliegt (nie aus Dateinamen ableiten)
CLIENT_NAME_MANUAL_ITEM = "❌ Name, Vorname manuell ergänzen"


def short_label(text: str, limit: int = MAX_WARNING_LABEL_CHARS) -> str:
    """Kürzt eine (Modell-)Überschrift auf ein einzeiliges Stichwort ohne Markdown.

    Warnungen sollen nur auf die Überschrift verweisen, nie den Abschnittstext
    oder lange Klientenpassagen wiederholen.
    """
    first_line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    clean = re.sub(r"[#*_`>]+", "", first_line)
    clean = re.sub(r"\s+", " ", clean).strip(" :–-—")
    if len(clean) > limit:
        clean = clean[: limit - 1].rstrip() + "…"
    return clean or "(ohne Überschrift)"


@dataclass
class UnmappedBlock:
    heading: str
    reason: str

    def __post_init__(self) -> None:
        # Nur gekürztes Stichwort speichern, nie den Rohtext
        self.heading = short_label(self.heading)

    def label(self) -> str:
        return f"{self.heading} ({self.reason})"


class ExtractedChatSections(dict):
    """Spezialisiertes Dict für extrahierte Abschnitte mit Diagnoseinformationen."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.unmapped_blocks: List[UnmappedBlock] = []
        self.had_assistant_content: bool = False

    @property
    def has_unmapped(self) -> bool:
        return len(self.unmapped_blocks) > 0

    @property
    def has_mapped_sections(self) -> bool:
        return any(
            bool(data.get("bericht_zielerreichung") or data.get("vorschlag_ziele") or data.get("vorschlag_massnahmen"))
            for data in self.values()
        )


class DocxExportResult(tuple):
    """2-Tupel (docx_bytes, filename) mit optionalen Warnungen und manuellen Feldern als Attribut (abwärtskompatibel)."""

    docx_bytes: bytes
    filename: str
    warnings: List[str]
    manual_fields: List[str]

    def __new__(
        cls,
        docx_bytes: bytes,
        filename: str,
        warnings: Optional[List[str]] = None,
        manual_fields: Optional[List[str]] = None,
    ):
        inst = super().__new__(cls, (docx_bytes, filename))
        inst.docx_bytes = docx_bytes
        inst.filename = filename
        inst.warnings = warnings or []
        inst.manual_fields = manual_fields or []
        return inst


# Alias-Mapping für flexible und benutzerfreundliche Vorlagenauswahl
TEMPLATE_ALIASES: Dict[str, str] = {
    "hamburg": "hamburg_soziale_dienste",
    "hamburg_soziale_dienste": "hamburg_soziale_dienste",
    "standard": "hamburg_soziale_dienste",
    "fachamt": "hamburg_soziale_dienste",
    "hamburg-fachamt": "hamburg_soziale_dienste",
    "op-de-wisch": "hamburg_op_de_wisch",
    "op_de_wisch": "hamburg_op_de_wisch",
    "hamburg_op_de_wisch": "hamburg_op_de_wisch",
    "odw": "hamburg_op_de_wisch",
}

# Mapping zwischen Hamburger Abschnitts-IDs und Op-de-Wisch-Lebensbereichen
HAMBURG_TO_OP_DE_WISCH_MAP: Dict[str, str] = {
    "1.1": "4",  # Wohnsituation -> 4 Wohnen
    "1.2": "2",  # Alltägliche Lebensführung -> 2 Alltagsgestaltung / Tagesstruktur
    "1.3": "1",  # Basisversorgung -> 1 Psychische Verfassung / Selbstsorge
    "1.4": "1",  # Emotionale Entwicklung -> 1 Psychische Verfassung / Selbstsorge
    "1.5": "3",  # Gesundheitsförderung -> 3 Gesundheit
    "2.1": "6",  # Soziale Beziehungen -> 6 Sozialer Lebensraum
    "2.2": "6",  # Kulturelles Leben -> 6 Sozialer Lebensraum
    "2.3": "5",  # Kommunikation / Admin -> 5 Finanzen / Institutionen
    "3": "7",    # Arbeit Hamburg -> 7 Arbeit / Beschäftigung
}

OP_DE_WISCH_TO_HAMBURG_MAP: Dict[str, List[str]] = {
    "1": ["1.3", "1.4"],
    "2": ["1.2"],
    "3": ["1.5"],
    "4": ["1.1"],
    "5": ["2.3"],
    "6": ["2.1", "2.2"],
    "7": ["3"],
}


def resolve_template(template_name: Optional[str]) -> ReportTemplate:
    """Löst einen Alias oder Registry-Key auf die ReportTemplate-Definition auf.

    Löst UnknownTemplateError aus, wenn keine oder eine unbekannte Vorlage übergeben wird.
    Keine stille Standardvorlage!
    """
    if not template_name or not str(template_name).strip():
        raise UnknownTemplateError(
            "Keine Vorlage angegeben. Bitte wählen Sie eine Vorlage: 'hamburg' (Fachamt) oder 'op-de-wisch' (Op de Wisch)."
        )
    key_norm = str(template_name).strip().lower()
    reg_key = TEMPLATE_ALIASES.get(key_norm)
    if not reg_key or reg_key not in TEMPLATE_REGISTRY:
        raise UnknownTemplateError(
            f"Unbekannte Vorlage '{template_name}'. Gültige Vorlagen: 'hamburg' (Fachamt), 'op-de-wisch' (Op de Wisch)."
        )

    template = TEMPLATE_REGISTRY[reg_key]
    if not template.docx_path.exists():
        raise DocxExportError(
            f"Vorlagendatei für '{template.label}' wurde nicht gefunden unter: {template.docx_path}"
        )
    return template


def split_client_name(full_name: str) -> Tuple[str, str]:
    """Trennt Klientennamen wie 'Hans Peter' oder 'Peter, Hans' in (Vorname, Nachname)."""
    if not full_name:
        return ("", "")
    clean = full_name.replace("_", " ").strip()
    if "," in clean:
        parts = [p.strip() for p in clean.split(",", 1)]
        return (parts[1], parts[0])
    parts = clean.split()
    if len(parts) == 1:
        return ("", parts[0])
    return (parts[0], " ".join(parts[1:]))


HAMBURG_SECTION_KEYWORDS: Dict[str, List[str]] = {
    "1.1": [
        "wohnsituation", "wohnen", "wohnung", "wohnform", "wohnumfeld", "miet", "miete",
        "mietvertrag", "mietangelegenheiten", "nachbarschaft", "nachbar", "unterkunft",
    ],
    "1.2": [
        "alltägliche lebensführung", "hauswirtschaft", "haushalt", "haushaltsführung",
        "haushaltsaufgaben", "einkauf", "einkäufe", "einkaufen", "einkaufsaufgaben",
        "einkaufslisten", "kochen", "kochübung", "kochübungen", "wäsche", "putzen",
        "reinigung", "tagesstruktur", "alltag", "lebensführung", "mahlzeit", "mahlzeiten",
    ],
    "1.3": [
        "basisversorgung", "grundpflege", "körperpflege", "hygiene", "körperhygiene",
        "ernährung", "kleidung", "anziehen", "ausziehen", "selbstsorge",
    ],
    "1.4": [
        "emotionale und psychische entwicklung", "emotionale entwicklung", "psychische entwicklung",
        "emotional", "emotionale", "psychisch", "psychische", "seelisch", "gefühle", "gefühl",
        "stress", "stressmanagement", "stressbewältigung", "mental health", "psychotherapie",
        "psychotherapeutisch", "selbstfürsorge",
    ],
    "1.5": [
        "gesundheitsförderung und -erhaltung", "gesundheitsförderung", "gesundheitserhaltung",
        "körperliche gesundheit", "gesundheit", "physiotherapie", "physio", "arzt",
        "arzttermin", "arzttermine", "arztbesuch", "arztbesuche", "medizinisch", "medikament",
        "medikamente", "medikamenteneinnahme", "therapie",
    ],
    "2.1": [
        "gestaltung sozialer beziehungen", "soziale beziehungen", "soziale kontakte",
        "familie", "familiär", "familiäre", "freunde", "freundschaft", "freundschaften",
        "bezugsperson", "bezugspersonen", "angehörige", "partnerschaft", "soziales umfeld",
    ],
    "2.2": [
        "teilnahme am kulturellen und gesellschaftlichen leben", "kulturelles leben",
        "gesellschaftliches leben", "kulturell", "freizeit", "freizeitaktivitäten",
        "vereine", "verein", "veranstaltung", "veranstaltungen", "mobilität", "teilhabe",
        "sport", "kegel", "hobby",
    ],
    "2.3": [
        "kommunikation und orientierung", "kommunikation", "orientierung", "sprachlich",
        "ausdrucksfähigkeit", "verständigung", "administration", "administrativ",
        "administrative", "administrative angelegenheiten", "finanzen", "behörde",
        "behörden", "post",
    ],
    "3": [
        "beschäftigung, arbeit, ausbildung", "beschäftigung", "arbeit", "arbeitsplatz",
        "ausbildung", "wfbm", "werkstatt", "arbeitszeit", "arbeitszeitumfang", "beruf",
        "beruflich", "berufliche", "tagesförderstätte", "erwerbstätigkeit",
    ],
}


def map_title_to_section(title: str, valid_section_ids: Optional[set[str]] = None) -> Tuple[Optional[str], str]:
    """Ordnet einen Freitext-Titel anhand von Schlüsselwörtern eindeutig einem Lebensbereich zu.

    Gibt (section_id, 'eindeutig') zurück oder (None, reason) falls kein oder kein eindeutiger Treffer vorliegt.
    """
    if not title or not title.strip():
        return None, "kein Titel vorhanden"

    t_clean = title.lower()
    scores: Dict[str, int] = {}

    kw_source = (
        {sid: kws for sid, kws in HAMBURG_SECTION_KEYWORDS.items() if sid in valid_section_ids}
        if valid_section_ids and any(sid in valid_section_ids for sid in HAMBURG_SECTION_KEYWORDS)
        else HAMBURG_SECTION_KEYWORDS
    )

    for sec_id, keywords in kw_source.items():
        score = 0
        for kw in keywords:
            if " " in kw:
                if kw in t_clean:
                    score += 2
            else:
                pattern = r"\b" + re.escape(kw) + r"\w*"
                matches = re.findall(pattern, t_clean)
                score += len(matches)
        if score > 0:
            scores[sec_id] = score

    if not scores:
        return None, "kein eindeutiger Bereich"

    sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    best_sec, best_score = sorted_scores[0]

    if len(sorted_scores) > 1:
        second_sec, second_score = sorted_scores[1]
        if best_score == second_score:
            return None, f"mehrdeutig: {best_sec} / {second_sec}"

    return best_sec, "eindeutig"


def detect_user_intent(user_msg: str) -> Optional[str]:
    """Erkennt aus der Nutzernachricht das Zielfeld für Abschnitte ohne explizite Unterüberschriften."""
    if not user_msg:
        return None
    low = user_msg.lower()

    if any(k in low for k in ["maßnahme", "maßnahmen", "unterstützungsleistung", "unterstützungsleistungen", "assistenzleistung", "assistenzleistungen", "förderleistung"]):
        return "vorschlag_massnahmen"

    if any(k in low for k in ["smart", "neue ziele", "zielvorschlag", "zielvorschläge", "zielformulierung", "ziele vorschlagen", "zukünftige ziele"]):
        return "vorschlag_ziele"
    if ("ziel" in low or "ziele" in low) and not any(k in low for k in ["zielerreichung", "stand der ziele", "bisherige ziele"]):
        return "vorschlag_ziele"

    if any(k in low for k in ["zielerreichung", "verlauf", "rückblick", "stand", "bisherig", "zwischenstand"]):
        return "bericht_zielerreichung"

    return None


CLOSING_PHRASES = (
    "Diese SMART-Ziele",
    "Diese Ziele",
    "Diese Maßnahmen",
    "Diese Fragen",
    "Diese Vorschläge",
    "Zusammenfassend",
    "Insgesamt",
    "Ich hoffe",
    "Gerne",
    "Falls du",
    "Falls Sie",
    "Wenn du",
    "Wenn Sie",
    "Bei Fragen",
)

META_HEADING_PATTERN = re.compile(
    r"^(?:"
    r"(?:offene\s+)?frage(?:n|\(n\))?(?:\s+an\s+(?:dich|sie|euch))?"
    r"|reflexionsfrage(?:n|\(n\))?"
    r"|rückfrage(?:n|\(n\))?"
    r"|hinweis(?:e|\(e\))?"
    r"|anmerkung(?:en|\(en\))?"
    r"|zusammenfassung"
    r"|fazit"
    r")$",
    re.IGNORECASE,
)


def is_meta_heading(heading: str) -> bool:
    """Prüft, ob eine Überschrift ein Meta-/Fragenblock ist, der ignoriert werden soll."""
    if not heading:
        return False
    h = heading.strip().lstrip("#").strip()
    h = re.sub(r"^(?:\(?[0-9]+[\.\)\-]?\s*)+", "", h).strip()
    h = re.sub(r"^[\*\_#\s:–\-—]+|[\*\_#\s:–\-—]+$", "", h).strip()
    return bool(META_HEADING_PATTERN.match(h))


def is_meta_message_content(text: str) -> bool:
    """Prüft, ob eine Fließtext-Nachricht ohne Überschriften überwiegend aus Fragen besteht."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return False
    q_lines = sum(1 for l in lines if l.endswith("?"))
    if q_lines > len(lines) / 2:
        return True
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]
    if sentences:
        q_sentences = sum(1 for s in sentences if s.endswith("?"))
        if q_sentences > len(sentences) / 2:
            return True
    return False


def strip_trailing_closing_paragraph(text: str) -> str:
    """Entfernt abschließende Floskel-/Schlussabsätze am Ende einer Nachricht oder eines Abschnitts."""
    if not text:
        return text
    while True:
        parts = re.split(r"\n\s*\n", text.rstrip())
        if len(parts) < 2:
            break
        last_p = parts[-1].strip()
        first_line = parts[-1].lstrip("\r\n").splitlines()[0] if parts[-1].strip() else ""
        if first_line.startswith((" ", "\t", "-", "*", "+", "•")):
            break
        low_last = last_p.lower()
        matched = False
        for phrase in CLOSING_PHRASES:
            if low_last.startswith(phrase.lower()):
                text = "\n\n".join(parts[:-1]).rstrip()
                matched = True
                break
        if not matched:
            break
    return text


def clean_markdown_for_docx(text: str) -> str:
    """Bereinigt Markdown-Artefakte für die Übernahme in DOCX-Formularfelder.

    - Entfernt Fett- und Kursivmarkierungen (**x**, __x__, *x*, _x_ als Hervorhebung)
    - Entfernt führende '#' Überschriftsmarker
    - Wandelt Aufzählungen (- , * , + ) in Aufzählungszeichen '• ' um
    - Entfernt Zeileneinrückungen
    - Reduziert 3+ aufeinanderfolgende Leerzeilen auf maximal eine Leerzeile
    """
    if not text:
        return ""

    lines = text.splitlines()
    cleaned_lines = []

    for line in lines:
        l = line.strip()
        if not l:
            cleaned_lines.append("")
            continue

        # Führende '#' entfernen
        l = re.sub(r"^#{1,6}\s*", "", l).strip()

        # Listenzeilen in '• ' umwandeln (Standard-Bullets oder Spiegelstrich direkt vor Fett -**x**)
        l = re.sub(r"^(?:[-*+•]\s+|-(?=\*\*|__)|•\s*)", "• ", l)

        # Fett-/Kursiv-Marker entfernen
        l = re.sub(r"\*{2,3}(.*?)\*{2,3}", r"\1", l)
        l = re.sub(r"_{2,3}(.*?)_{2,3}", r"\1", l)
        l = re.sub(r"(?<!\*)\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\*)", r"\1", l)
        l = re.sub(r"(?<!\w)_(?!\s)([^_\n]+?)(?<!\s)_(?!\w)", r"\1", l)

        cleaned_lines.append(l)

    result = "\n".join(cleaned_lines)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


FIELD_MARKER_LINE_RE = re.compile(
    r"^[ \t]*(?:(?:[-*+•]|#{1,6})[ \t]*)*(?:\*\*|__)?\s*"
    r"(?:"
    r"bisherige\s+zielerreichung"
    r"|bericht\s+zur\s+zielerreichung"
    r"|verlaufsdaten(?:\s+reflexion)?"
    r"|stand(?:\s+der)?\s+zielerreichung"
    r"|vorschlag\s+(?:\(?für\)?\s+)?(?:aktuelle\s+|neue\s+)?ziele"
    r"|aktuelle\s+vorschläge"
    r"|vorschlag\s+(?:für\s+)?(?:neue\s+)?maßnahmen"
    r"|maßnahmen(?:\s+des\s+leistungsanbieters)?"
    r"|reflexion\b"
    r")"
    # Marker allein auf der Zeile ODER mit Doppelpunkt und Inhalt in derselben Zeile
    # ('**Vorschlag neue Ziele:** Herr P. kocht …'); der Inhalt danach gehört zum Feld.
    r"(?:\s*(?:\*\*|__)?\s*:\s*(?:\*\*|__)?[ \t]*|\s*(?:\*\*|__)?[ \t]*$)",
    re.MULTILINE | re.IGNORECASE,
)


def classify_field_marker(marker_text: str) -> Optional[str]:
    """Ordnet einen erkannten Field-Marker einem Zielfeld im ReportSection zu."""
    low = marker_text.lower()
    if any(k in low for k in ["zielerreichung", "reflexion", "verlauf", "stand"]):
        return "bericht_zielerreichung"
    if any(k in low for k in ["maßnahme", "unterstützung", "assistenz"]):
        return "vorschlag_massnahmen"
    if any(k in low for k in ["vorschlag", "ziel"]):
        return "vorschlag_ziele"
    return None


def detect_question_section(user_msg: str) -> Optional[str]:
    """Ermittelt den Zielbereich aus der vorangegangenen Fachkraft-Frage anhand von Schlüsselwörtern.

    Muss eindeutig genau einen Lebensbereich betreffen:
    - Genau ein Bereich mit Treffern -> dieser Bereich (Hamburg-ID, z.B. '1.2')
    - Keine Treffer oder mehrere Bereiche genannt -> None (keine Frage-Sektion)
    """
    if not user_msg or not user_msg.strip():
        return None

    t_clean = user_msg.lower()
    scores: Dict[str, int] = {}

    for sec_id, keywords in HAMBURG_SECTION_KEYWORDS.items():
        score = 0
        for kw in keywords:
            if " " in kw:
                if kw in t_clean:
                    score += 2
            else:
                pattern = r"\b" + re.escape(kw) + r"\w*"
                matches = re.findall(pattern, t_clean)
                score += len(matches)
        if score > 0:
            scores[sec_id] = score

    explicit = set(re.findall(r"\b([1-9]\.[0-9]+)\b", t_clean))
    explicit_bereich = set(re.findall(r"\bbereich\s+([1-9](?:\.[0-9]+)?)\b", t_clean))
    all_explicit = explicit | explicit_bereich
    for sid in all_explicit:
        if sid in HAMBURG_SECTION_KEYWORDS:
            scores[sid] = scores.get(sid, 0) + 5

    if len(scores) == 1:
        return list(scores.keys())[0]

    return None


def has_explicit_section_number(text: str) -> bool:
    """Prüft, ob ein Text eine explizite Bereichsnummer wie '1.2' oder 'Bereich 1.2' nennt."""
    if not text:
        return False
    low = text.lower()
    return bool(
        re.search(r"\b[1-9]\.[0-9]+\b", low)
        or re.search(r"\bbereich\s+[1-9](?:\.[0-9]+)?\b", low)
    )


def parse_heading_parts(raw_heading: str) -> Tuple[Optional[str], str]:
    """Zerlegt eine Überschrift in (candidate_id, clean_title).

    Unterstützt Formate:
    - 'Bereich 1.2 – Hauswirtschaft'
    - '1.2 Hauswirtschaft' / '1.2 **Hauswirtschaft:**'
    - 'Hauswirtschaft (1.2)'
    - '1. Haushaltsführung' (Enumeration -> candidate_id ist None)
    - '2 – Titel'
    - 'Titel' (ohne Nummer)
    """
    candidate_id: Optional[str] = None
    title_part: str = ""

    # 1. 'Bereich 1.2' oder 'Lebensbereich 1.2'
    m_bereich = re.match(
        r"^(?:(?:Lebens)?Bereich(?:e)?\s+)([1-9](?:\.[0-9]+)?)(?:\s*[–\-:\s]\s*(.*))?$",
        raw_heading,
        re.I,
    )
    if m_bereich:
        candidate_id = m_bereich.group(1).strip()
        title_part = (m_bereich.group(2) or "").strip()
    else:
        # 2. Punkt-ID am Anfang '1.2 Titel' oder '1.2 **Titel:**'
        m_dotted = re.match(r"^([1-9]\.[0-9]+(?:\.[0-9]+)?)\.?\s*(?:\*\*)?(.*?)(?:\*\*)?:?$", raw_heading)
        if m_dotted:
            candidate_id = m_dotted.group(1).strip()
            title_part = m_dotted.group(2).strip()
        else:
            # 3. ID in Klammern am Ende: NUR Punkt-IDs wie '(1.2)' oder '(Bereich N)' / '(Lebensbereich N)'
            # Einzelne Ziffern wie '(3)' oder '(2)' dürfen NIE zur Bereichs-ID werden (Ideen-Zählung/Enumeration).
            m_paren = re.match(
                r"^(.*?)\s*\((?:(?:(?:Lebens)?Bereich\s+)?([1-9]\.[0-9]+(?:\.[0-9]+)?)|(?:(?:Lebens)?Bereich\s+)([1-9]))\)\s*$",
                raw_heading,
                re.I,
            )
            if m_paren:
                candidate_id = (m_paren.group(2) or m_paren.group(3)).strip()
                title_part = m_paren.group(1).strip()
            else:
                # 4. Reine Aufzählungsnummer '1. **Titel:**' -> Enumeration, NIEMALS als ID nutzen!
                m_enum = re.match(r"^([1-9][0-9]?)\.\s*(?:\*\*)?(.*?)(?:\*\*)?:?$", raw_heading)
                if m_enum:
                    candidate_id = None
                    title_part = m_enum.group(2).strip()
                else:
                    # 5. Einzelziffer mit Trenner '2 – Titel'
                    m_single = re.match(r"^([1-9])\s*[–\-]\s*(.*)$", raw_heading)
                    if m_single:
                        candidate_id = m_single.group(1).strip()
                        title_part = m_single.group(2).strip()
                    else:
                        title_part = raw_heading

    clean_title = re.sub(r"^[\*\s:–\-—]+|[\*\s:–\-—]+$", "", title_part).strip()
    return candidate_id, clean_title


class _HeadingMatch:
    """Repräsentiert einen gefundenen Überschriften-Abschnitt im Chattext."""

    def __init__(self, start: int, end: int, raw_heading: str, full_match_text: str):
        self._start = start
        self._end = end
        self.raw_heading = raw_heading
        self.full_match_text = full_match_text

    def start(self) -> int:
        return self._start

    def end(self) -> int:
        return self._end

    def group(self, idx: int = 0) -> str:
        return self.full_match_text if idx == 0 else self.raw_heading


def find_heading_matches(
    content: str,
    valid_ids: set[str],
    odw_target: bool,
    hamburg_ids: set[str],
) -> List[_HeadingMatch]:
    """Findet alle Markdown- und fettformatierten Sektionsüberschriften im Nachrichtentext."""
    heading_line_re = re.compile(r"^(#{2,4})\s+(.+)$", re.MULTILINE)
    bold_line_re = re.compile(r"^[ \t]*(?:\*\*|__)(.+?)(?:\*\*|__):?[ \t]*$", re.MULTILINE)

    matches: List[_HeadingMatch] = []

    # 1. Standard-Markdown-Überschriften
    for m in heading_line_re.finditer(content):
        line_str = m.group(0).strip()
        if FIELD_MARKER_LINE_RE.match(line_str):
            continue
        raw_heading = m.group(2).strip()
        matches.append(_HeadingMatch(m.start(), m.end(), raw_heading, line_str))

    # 2. Fette Zeilen-Überschriften (Variante a: nur wenn explizite Nummer UND passender Titel)
    for m in bold_line_re.finditer(content):
        line_str = m.group(0).strip()
        if FIELD_MARKER_LINE_RE.match(line_str):
            continue
        inner = m.group(1).strip()
        cand_id, clean_title = parse_heading_parts(inner)
        if not cand_id:
            continue
        id_valid = (cand_id in valid_ids) or (odw_target and cand_id in HAMBURG_TO_OP_DE_WISCH_MAP)
        if not id_valid or not clean_title:
            continue
        raw_t_id, reason = map_title_to_section(
            clean_title, valid_section_ids=hamburg_ids if odw_target else valid_ids
        )
        if reason == "eindeutig" and raw_t_id == cand_id:
            matches.append(_HeadingMatch(m.start(), m.end(), inner, line_str))

    matches.sort(key=lambda x: x.start())
    return matches


def check_unmapped_mentions_in_chat(
    history: List[Dict[str, str]],
    template: ReportTemplate,
    chat_sections: Optional[ExtractedChatSections] = None,
    populated_section_ids: Optional[set[str]] = None,
) -> List[str]:
    """Diagnose-Helfer: Prüft, ob unbefüllte Bereiche im Chat erwähnt wurden.

    populated_section_ids: Menge der im tatsächlichen Export befüllten Bereiche
    (inkl. Fallback-Zuordnungen wie OdW -> Hamburg).
    """
    warnings: List[str] = []
    hamburg_ids = {s.id for s in SECTION_DEFINITIONS}
    valid_ids = {s.id for s in template.section_definitions}
    odw_target = not (valid_ids & (hamburg_ids - {"3"}))

    for s_def in template.section_definitions:
        sec_id = s_def.id

        # 1. Befüllt-Prüfung: Primär über die im Export tatsächlich befüllten Bereiche
        if populated_section_ids is not None:
            if sec_id in populated_section_ids:
                continue
        elif chat_sections is not None:
            chat_data = chat_sections.get(sec_id, {})
            has_content = bool(
                chat_data.get("bericht_zielerreichung", "").strip()
                or chat_data.get("vorschlag_ziele", "").strip()
                or chat_data.get("vorschlag_massnahmen", "").strip()
            )
            if has_content:
                continue

        mentioned = False
        for msg in history:
            if msg.get("role") != "assistant":
                continue
            text = msg.get("content", "")
            for line in text.splitlines():
                l_str = line.strip()
                if not l_str:
                    continue

                # 1. Überschrift (Markdown oder Fett)
                m_md = re.match(r"^(#{2,4})\s+(.+)$", l_str)
                m_bold = re.match(r"^[ \t]*(?:\*\*|__)(.+?)(?:\*\*|__):?[ \t]*$", l_str)
                heading_cand = None
                if m_md and not FIELD_MARKER_LINE_RE.match(l_str):
                    heading_cand = m_md.group(2).strip()
                elif m_bold and not FIELD_MARKER_LINE_RE.match(l_str):
                    heading_cand = m_bold.group(1).strip()

                if heading_cand:
                    cand_id, clean_title = parse_heading_parts(heading_cand)
                    if cand_id:
                        if odw_target:
                            mapped_cand = HAMBURG_TO_OP_DE_WISCH_MAP.get(cand_id, cand_id)
                            if mapped_cand == sec_id:
                                mentioned = True
                                break
                        else:
                            if cand_id == sec_id:
                                mentioned = True
                                break
                    if clean_title:
                        raw_t_id, reason = map_title_to_section(
                            clean_title, valid_section_ids=hamburg_ids if odw_target else valid_ids
                        )
                        if reason == "eindeutig":
                            target_t = HAMBURG_TO_OP_DE_WISCH_MAP.get(raw_t_id, raw_t_id) if odw_target else raw_t_id
                            if target_t == sec_id:
                                mentioned = True
                                break

                # 2. Zeilenanfang mit Bereichsnummer außerhalb von Überschriften:
                # NUR Punkt-IDs ("1.2 Hauswirtschaft...") oder explizit "Bereich N" / "Lebensbereich N"
                # Keine nackten Zahlen wie "3 - 4 Mal pro Woche" oder "2: ..."
                m_line_bereich = re.match(
                    r"^(?:Lebens)?Bereich\s+([1-9](?:\.[0-9]+)?)\s*[:–\-–\s]\s*(.*)$",
                    l_str,
                    re.I,
                )
                m_line_dotted = re.match(r"^([1-9]\.[0-9]+(?:\.[0-9]+)?)\s*[:–\-–]?\s+([A-ZÄÖÜ].*)$", l_str)
                matched_id = None
                if m_line_bereich:
                    matched_id = m_line_bereich.group(1).strip()
                elif m_line_dotted:
                    rest = m_line_dotted.group(2).strip()
                    if not rest.lower().startswith(("kg", "km", "prozent", "%", "std", "stunden", "jahre", "tage")):
                        matched_id = m_line_dotted.group(1).strip()

                if matched_id:
                    if odw_target:
                        if HAMBURG_TO_OP_DE_WISCH_MAP.get(matched_id, matched_id) == sec_id:
                            mentioned = True
                            break
                    else:
                        if matched_id == sec_id:
                            mentioned = True
                            break

            if mentioned:
                break

        if mentioned:
            warn = f"❌ Bereich {sec_id} im Chat erwähnt, aber nicht zugeordnet"
            if warn not in warnings:
                warnings.append(warn)

    return warnings


def parse_block_fields(sec_text: str, default_intent: Optional[str] = None) -> Dict[str, str]:
    """Extrahiert die Felder bericht_zielerreichung, vorschlag_ziele, vorschlag_massnahmen aus einem Textblock.

    Erkennt Unterüberschriften / Field-Marker sowohl in Markdown als auch in Plain-Text (auf eigener Zeile).
    """
    sub_matches = list(FIELD_MARKER_LINE_RE.finditer(sec_text))
    result: Dict[str, str] = {
        "bericht_zielerreichung": "",
        "vorschlag_ziele": "",
        "vorschlag_massnahmen": "",
    }
    if not sub_matches:
        if default_intent == "vorschlag_massnahmen":
            key = "vorschlag_massnahmen"
        elif default_intent == "vorschlag_ziele":
            key = "vorschlag_ziele"
        else:
            key = "bericht_zielerreichung"
        result[key] = sec_text.strip()
        return result

    pre_text = sec_text[: sub_matches[0].start()].strip()
    if pre_text:
        pre_key = default_intent if default_intent in result else "bericht_zielerreichung"
        result[pre_key] = pre_text

    for j, sm in enumerate(sub_matches):
        marker_str = sm.group(0).strip()
        key = classify_field_marker(marker_str) or "bericht_zielerreichung"
        sub_start = sm.end()
        sub_end = sub_matches[j + 1].start() if j + 1 < len(sub_matches) else len(sec_text)
        block_content = sec_text[sub_start:sub_end].strip()
        if block_content:
            cur = result[key]
            result[key] = f"{cur}\n\n{block_content}".strip() if cur else block_content

    return result


def extract_section_data_from_chat(
    messages: List[Dict[str, str]],
    template: Optional[ReportTemplate] = None,
) -> ExtractedChatSections:
    """Extrahiert aus den Antworten des Reflexions-Begleiters strukturierte Abschnitte.

    Unterstützt Überschriftenformate:
    - '### Bereich 1.2' / '### Bereich 1.2 – Titel'
    - '### 1.2 Titel' / '### 1.2 **Titel:**'
    - '## 2 – Titel' / '## Bereich 2'
    - Aufzählungs-Überschriften wie '### 1. **Haushalt und Einkauf:**' (Mapping per Schlüsselwörter)

    Ordnet Inhalte den Feldern bericht_zielerreichung, vorschlag_ziele und vorschlag_massnahmen zu:
    - Explizite Zwischenüberschriften (**Vorschlag neue Ziele:**, ...) haben Vorrang.
    - Ohne Zwischenüberschrift entscheidet die Intention der vorangegangenen Nutzerfrage.
    - Bullet-Items mit fetten Präfixen (- **Ziel:**, - **Kochübungen:**) bleiben als Inhalt erhalten.
    """
    sections_data = ExtractedChatSections()

    hamburg_ids = {s.id for s in SECTION_DEFINITIONS}
    if template:
        valid_ids = {s.id for s in template.section_definitions}
    else:
        # Ohne Vorlage: IDs aller registrierten Vorlagen (Hamburg + Op de Wisch) akzeptieren
        valid_ids = {s.id for t in TEMPLATE_REGISTRY.values() for s in t.section_definitions}
    # Zielvorlage mit Op-de-Wisch-Nummerierung (1–9): Hamburger IDs/Schlüsselwörter werden über
    # HAMBURG_TO_OP_DE_WISCH_MAP umgesetzt, statt Ziffern blind zu übernehmen.
    odw_target = bool(template) and not (valid_ids & (hamburg_ids - {"3"}))
    # IDs, die in beiden Nummerierungen vorkommen (aktuell nur '3'): hier entscheidet ein eindeutiger Titel
    odw_ids = {s.id for s in TEMPLATE_REGISTRY["hamburg_op_de_wisch"].section_definitions}
    overlap_ids = hamburg_ids & odw_ids

    heading_line_re = re.compile(r"^(#{2,4})\s+(.+)$", re.MULTILINE)
    last_user_msg = ""

    for msg in messages:
        role = msg.get("role")
        if role == "user":
            last_user_msg = msg.get("content", "")
            continue
        if role != "assistant":
            continue

        content = msg.get("content", "")
        if not content or not content.strip():
            continue

        # Schlusssätze am Ende der Nachricht entfernen
        content = strip_trailing_closing_paragraph(content)
        if not content.strip():
            continue

        intent = detect_user_intent(last_user_msg)
        question_sec = detect_question_section(last_user_msg)
        question_has_explicit = has_explicit_section_number(last_user_msg)
        question_guessed_by_keyword = bool(question_sec) and not question_has_explicit

        heading_matches = find_heading_matches(
            content, valid_ids=valid_ids, odw_target=odw_target, hamburg_ids=hamburg_ids
        )

        # Prüfe Text vor der ersten Bereichsüberschrift (oder gesamte Nachricht, wenn keine Überschriften)
        first_heading_start = heading_matches[0].start() if heading_matches else len(content)
        pre_heading_text = content[:first_heading_start].strip()
        pre_heading_text = strip_trailing_closing_paragraph(pre_heading_text)

        if pre_heading_text:
            has_fields = bool(FIELD_MARKER_LINE_RE.search(pre_heading_text))
            if has_fields:
                sections_data.had_assistant_content = True
                if question_sec:
                    target_id = (
                        HAMBURG_TO_OP_DE_WISCH_MAP.get(question_sec, question_sec)
                        if odw_target
                        else question_sec
                    )
                    if target_id not in sections_data:
                        sections_data[target_id] = {
                            "bericht_zielerreichung": "",
                            "vorschlag_ziele": "",
                            "vorschlag_massnahmen": "",
                        }
                    extracted_fields = parse_block_fields(pre_heading_text, default_intent=intent)
                    for k, val in extracted_fields.items():
                        if val:
                            cur = sections_data[target_id][k]
                            sections_data[target_id][k] = f"{cur}\n\n{val}".strip() if cur else val
                else:
                    sections_data.unmapped_blocks.append(
                        UnmappedBlock(
                            heading="(Fließtext)",
                            reason="kein Bereich erkennbar (keine Überschrift, Frage nicht eindeutig)",
                        )
                    )
            elif not heading_matches:
                if is_meta_message_content(content):
                    # Reine Frage-/Reflexionsnachricht ohne Überschriften still überspringen
                    continue
                sections_data.had_assistant_content = True
                sections_data.unmapped_blocks.append(
                    UnmappedBlock(
                        heading="(Fließtext)",
                        reason="keine erkennbare Bereichsüberschrift gefunden",
                    )
                )
                continue

        # Prüfe, ob die Assistant-Nachricht >= 2 Überschriften mit voneinander verschiedener expliziter Bereichsnummer enthält
        explicit_ids_in_msg = set()
        for hm in heading_matches:
            if not is_meta_heading(hm.raw_heading):
                c_id, _ = parse_heading_parts(hm.raw_heading)
                if c_id:
                    explicit_ids_in_msg.add(c_id)
        has_multiple_explicit_sections = len(explicit_ids_in_msg) >= 2

        for i, match in enumerate(heading_matches):
            raw_heading = match.raw_heading
            if is_meta_heading(raw_heading):
                # Meta-/Fragenblöcke still überspringen: nicht zuordnen, nicht in unmapped_blocks, nicht ins docx
                continue

            start_pos = match.end()
            end_pos = heading_matches[i + 1].start() if i + 1 < len(heading_matches) else len(content)
            sec_text = content[start_pos:end_pos].strip()
            sec_text = strip_trailing_closing_paragraph(sec_text)

            candidate_id, clean_title = parse_heading_parts(raw_heading)

            if clean_title and is_meta_heading(clean_title):
                # Titel entspricht einem Meta-/Fragenblock
                continue

            sections_data.had_assistant_content = True

            sec_id: Optional[str] = None
            unmapped_reason: str = ""

            # Titel-Zuordnung (immer im Hamburger ID-Raum), bei Op-de-Wisch-Ziel anschließend umgesetzt
            title_id: Optional[str] = None
            raw_title_id: Optional[str] = None
            title_reason = ""
            if clean_title:
                raw_title_id, title_reason = map_title_to_section(
                    clean_title, valid_section_ids=hamburg_ids if odw_target else valid_ids
                )
                title_id = raw_title_id
                if title_id and odw_target:
                    title_id = HAMBURG_TO_OP_DE_WISCH_MAP.get(title_id)

            # Konflikt-Prüfung zwischen Fachkraft-Frage und Modell-Überschrift (ADR-10 / Vorgabe B)
            heading_sec = candidate_id or raw_title_id
            conflict = False
            if question_sec:
                if odw_target:
                    q_odw = HAMBURG_TO_OP_DE_WISCH_MAP.get(question_sec)
                    if candidate_id and candidate_id in valid_ids and candidate_id not in hamburg_ids:
                        if candidate_id != q_odw:
                            conflict = True
                    elif candidate_id and candidate_id in HAMBURG_TO_OP_DE_WISCH_MAP:
                        if HAMBURG_TO_OP_DE_WISCH_MAP[candidate_id] != q_odw:
                            conflict = True
                    elif title_id:
                        if title_id != q_odw:
                            conflict = True
                    elif heading_sec and heading_sec != question_sec and heading_sec != q_odw:
                        conflict = True
                else:
                    # Hamburg-Vorlage
                    if candidate_id and candidate_id in valid_ids:
                        if candidate_id != question_sec:
                            if candidate_id in overlap_ids and raw_title_id == question_sec:
                                conflict = False
                            else:
                                conflict = True
                    elif candidate_id and candidate_id in OP_DE_WISCH_TO_HAMBURG_MAP:
                        # Modell verwendet Op-de-Wisch-Nummerierung im Hamburg-Modus
                        mapped_h_ids = OP_DE_WISCH_TO_HAMBURG_MAP.get(candidate_id, [])
                        if question_sec not in mapped_h_ids and raw_title_id != question_sec:
                            conflict = True
                    elif raw_title_id:
                        if raw_title_id != question_sec:
                            conflict = True
                    elif heading_sec and heading_sec != question_sec:
                        conflict = True

            if conflict:
                # Gewünschter Fix: Die Frage-Konfliktprüfung darf eine Sektion NICHT verwerfen, wenn ALLE gelten:
                # (a) der Fragebereich wurde nur per Schlüsselwort erraten (keine explizite Nummer)
                # (b) Assistant-Nachricht enthält >= 2 Überschriften mit expliziter, verschiedener Bereichsnummer
                # (c) Überschrift hat explizite Nummer in Vorlage UND Titel passt eindeutig zu dieser Nummer
                cond_a = question_guessed_by_keyword
                cond_b = has_multiple_explicit_sections
                cond_c = False
                if candidate_id:
                    cand_valid = (candidate_id in valid_ids) or (odw_target and candidate_id in HAMBURG_TO_OP_DE_WISCH_MAP)
                    if cand_valid and title_reason == "eindeutig" and raw_title_id == candidate_id:
                        cond_c = True

                if not (cond_a and cond_b and cond_c):
                    target_display = candidate_id if (odw_target and candidate_id in valid_ids and candidate_id not in hamburg_ids) else (candidate_id or heading_sec)
                    sections_data.unmapped_blocks.append(
                        UnmappedBlock(
                            heading=match.group(0).strip(),
                            reason=f"Bereich unklar: Frage zielt auf {question_sec}, Modell schreibt {target_display}",
                        )
                    )
                    continue

            if (
                candidate_id in overlap_ids
                and candidate_id in valid_ids
                and title_id
                and title_id != candidate_id
            ):
                # ID existiert in beiden Nummerierungen (z. B. '3' = Hamburg Arbeit / Op de Wisch Gesundheit):
                # Der eindeutig zuordenbare Titel entscheidet, nicht die Ziffer.
                sec_id = title_id
            elif candidate_id and candidate_id in valid_ids:
                # Dotted ID oder expliziter Bereich existiert in der aktuellen Vorlage
                sec_id = candidate_id
            elif odw_target and candidate_id in HAMBURG_TO_OP_DE_WISCH_MAP and not clean_title:
                # Hamburger ID ohne Titel bei Op-de-Wisch-Vorlage: feste Umsetzungstabelle
                sec_id = HAMBURG_TO_OP_DE_WISCH_MAP[candidate_id]
            elif not odw_target and candidate_id in OP_DE_WISCH_TO_HAMBURG_MAP and not clean_title:
                # Op-de-Wisch-ID ohne Titel bei Hamburg-Vorlage: feste Umsetzungstabelle
                mapped = OP_DE_WISCH_TO_HAMBURG_MAP[candidate_id]
                if len(mapped) == 1:
                    sec_id = mapped[0]
                else:
                    unmapped_reason = f"Op-de-Wisch-Bereich '{candidate_id}' ist im Hamburger Berichtsformat mehrdeutig ({', '.join(mapped)})"
            elif clean_title:
                # Reine Aufzählung oder nicht direkt passende ID: Mapping über Schlüsselwörter
                if title_id:
                    sec_id = title_id
                else:
                    unmapped_reason = title_reason or "kein eindeutiger Bereich"
            else:
                if candidate_id:
                    unmapped_reason = f"Bereich '{candidate_id}' existiert nicht in der gewählten Vorlage"
                else:
                    unmapped_reason = "kein Titel zur Zuordnung vorhanden"

            if not sec_id:
                sections_data.unmapped_blocks.append(
                    UnmappedBlock(
                        heading=match.group(0).strip(),
                        reason=unmapped_reason or "kein eindeutiger Bereich",
                    )
                )
                continue

            if sec_id not in sections_data:
                sections_data[sec_id] = {
                    "bericht_zielerreichung": "",
                    "vorschlag_ziele": "",
                    "vorschlag_massnahmen": "",
                }

            block_fields = parse_block_fields(sec_text, default_intent=intent)
            for k, val in block_fields.items():
                if val:
                    cur = sections_data[sec_id][k]
                    sections_data[sec_id][k] = f"{cur}\n\n{val}".strip() if cur else val

    return sections_data


def build_report_draft_from_chatbot(
    bot: ReflectionChatbot,
    template: Optional[ReportTemplate] = None,
    return_unmapped: bool = False,
    return_manual: bool = False,
) -> Any:
    """Erstellt ein vollständiges ReportDraft-Objekt aus dem aktuellen Chatbot-Zustand.

    Passt Abschnitte und Feld-Zuordnungen an die übergebene Vorlage an.
    """
    history = bot.get_history()
    plan_data = bot.plan_data

    # Validierung: Mindestens Plan oder Chatnachrichten müssen vorliegen
    if not plan_data and not history:
        raise EmptyExportError("Es sind weder ein Vorbericht noch Chat-Nachrichten für den DOCX-Export vorhanden.")

    # Abschnitte aus Chat extrahieren (damit Meta-Nachrichten erkannt und ignoriert werden)
    chat_sections = extract_section_data_from_chat(history, template=template)

    # Validierung: Wenn Assistant-Nachrichten vorhanden sind, aber nichts gemappt werden konnte
    if chat_sections.had_assistant_content and not chat_sections.has_mapped_sections:
        unmapped = chat_sections.unmapped_blocks
        if unmapped:
            reasons = "; ".join(b.label() for b in unmapped)
        else:
            reasons = "keine Bereichsüberschriften erkannt"
        raise UnmappedChatContentError(
            f"❌ Dialog-Inhalt konnte keinem Bereich zugeordnet werden: {reasons}",
            unmapped_blocks=unmapped,
        )

    # Wenn nur Meta-/Fragen vorlagen und kein Vorbericht vorhanden ist -> EmptyExportError
    if not plan_data and not chat_sections.has_mapped_sections:
        raise EmptyExportError("Es sind weder ein Vorbericht noch exportierbare Chat-Inhalte für den DOCX-Export vorhanden.")

    # CSV-Kopfdaten parsen (in-memory, nur verlässliche Felder, kein Absturz bei anderem Format)
    csv_zeitraum_von = ""
    csv_zeitraum_bis = ""
    csv_leistungsanbieter = ""
    csv_geburtsdatum = ""
    csv_name = ""
    csv_vorname = ""
    zeitraum_derived_from_entries = False
    csv_header_issue = ""

    viv_import = None
    if bot.csv_data and getattr(bot.csv_data, "raw_text", None):
        from sob_generator.importer import parse_vivendi_text

        try:
            viv_import = parse_vivendi_text(bot.csv_data.raw_text, strict=False)
        except (ValueError, IndexError, csv.Error):
            # Kein stiller Fallback: Kopfdaten sind nicht lesbar -> als ❌-Hinweis melden.
            # Bewusst ohne Exception-Text (könnte Rohzeilen der Klient*innen-CSV enthalten).
            viv_import = None
            csv_header_issue = "❌ CSV-Kopfdaten nicht lesbar – Kopffelder bitte manuell ergänzen"
        # Nur echte Vivendi-Exporte auswerten: Erkennungsmerkmal ist die Klientenzeile
        # 'Nachname, Vorname (*TT.MM.JJJJ)'. Bei anderen CSV-Formaten keine Kopfdaten
        # übernehmen (sonst landen beliebige Zellen als Leistungsanbieter/Zeitraum im Bericht).
        if viv_import is not None and not (
            viv_import.client and viv_import.client.name and viv_import.client.geburtsdatum
        ):
            viv_import = None
            csv_header_issue = "Kopfdaten: CSV ist kein Vivendi-Export – Kopffelder bitte manuell ergänzen"

    if viv_import is not None:
        if viv_import.meta:
            csv_zeitraum_von = viv_import.meta.leistungszeitraum_von or ""
            csv_zeitraum_bis = viv_import.meta.leistungszeitraum_bis or ""
            csv_leistungsanbieter = viv_import.meta.leistungsanbieter or ""

        if (not csv_zeitraum_von or not csv_zeitraum_bis) and viv_import.entries:
            valid_dates: List[Tuple[Tuple[int, int, int], str]] = []
            for entry in viv_import.entries:
                d_val = (getattr(entry, "datum", "") or "").strip()
                if d_val:
                    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", d_val)
                    if m:
                        d, mth, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                        valid_dates.append(((y, mth, d), f"{d:02d}.{mth:02d}.{y:04d}"))
            if valid_dates:
                valid_dates.sort(key=lambda x: x[0])
                if not csv_zeitraum_von:
                    csv_zeitraum_von = valid_dates[0][1]
                    zeitraum_derived_from_entries = True
                if not csv_zeitraum_bis:
                    csv_zeitraum_bis = valid_dates[-1][1]
                    zeitraum_derived_from_entries = True

        if viv_import.client:
            csv_geburtsdatum = viv_import.client.geburtsdatum or ""
            csv_name = viv_import.client.name or ""
            csv_vorname = viv_import.client.vorname or ""

    # 1. Klientendaten — Name ausschließlich aus verlässlichen Quellen:
    #    1) Vivendi-Klientenzeile 'Nachname, Vorname (*TT.MM.JJJJ)', Schreibweise unverändert
    #    2) Namensfeld im Vorbericht-Dokument selbst (nicht aus dessen Dateinamen abgeleitet)
    #    Niemals aus Upload-Dateinamen (CsvData.client_name / extract_client_name_from_filename):
    #    das lieferte z. B. 'peter synthetisch, hans' für 'hans_peter_synthetisch.csv'.
    if csv_name or csv_vorname:
        vorname, name = csv_vorname, csv_name
    elif (
        plan_data
        and plan_data.client_name
        and not getattr(plan_data, "client_name_from_filename", False)
    ):
        vorname, name = split_client_name(plan_data.client_name)
    else:
        vorname, name = "", ""
    client_name_missing = not (name or vorname)

    client = ClientData(
        name=name,
        vorname=vorname,
        geburtsdatum=csv_geburtsdatum,
    )

    # 2. Metadaten (CSV hat Vorrang vor Vorbericht)
    if csv_zeitraum_von:
        von = csv_zeitraum_von
    elif plan_data and plan_data.zeitraum_von:
        von = plan_data.zeitraum_von
    else:
        von = ""

    if csv_zeitraum_bis:
        bis = csv_zeitraum_bis
    elif plan_data and plan_data.zeitraum_bis:
        bis = plan_data.zeitraum_bis
    else:
        bis = ""

    meta = ReportMeta(
        leistungszeitraum_von=von,
        leistungszeitraum_bis=bis,
        leistungsanbieter=csv_leistungsanbieter,
    )

    # 3. Allgemeine Entwicklungen
    developments = ""
    if plan_data and plan_data.raw_summary:
        developments = plan_data.raw_summary

    goals_by_id = {g.section_id: g for g in plan_data.goals} if plan_data else {}

    section_defs = template.section_definitions if template else SECTION_DEFINITIONS
    target_ids = {s.id for s in section_defs}
    is_odw_target = (target_ids == {"1", "2", "3", "4", "5", "6", "7", "8", "9"})

    populated_section_ids: set[str] = set()
    sections: List[ReportSection] = []
    for s_def in section_defs:
        # Direkter Lookup
        goal = goals_by_id.get(s_def.id)
        chat_data = dict(chat_sections.get(s_def.id, {}))

        # Bei Nicht-Übereinstimmung (z.B. Plan hat Hamburg-IDs, Vorlage ist Op-de-Wisch oder umgekehrt)
        if goal is None and is_odw_target:
            # Sammle alle Hamburg-Ziele, die auf diese Op-de-Wisch-ID mappen
            mapped_goals = [
                g for g in (plan_data.goals if plan_data else [])
                if HAMBURG_TO_OP_DE_WISCH_MAP.get(g.section_id) == s_def.id
            ]
            if mapped_goals:
                lz_parts = [g.leitziel.strip() for g in mapped_goals if g.leitziel.strip()]
                sz_parts = [g.smarte_ziele.strip() for g in mapped_goals if g.smarte_ziele.strip()]
                stand_parts = [g.stand_zielerreichung.strip() for g in mapped_goals if g.stand_zielerreichung.strip()]
                vz_parts = [g.vorschlag_ziele.strip() for g in mapped_goals if g.vorschlag_ziele.strip()]
                from .plan_loader import PlanGoalItem

                goal = PlanGoalItem(
                    section_id=s_def.id,
                    section_title=s_def.title,
                    leitziel="\n\n".join(lz_parts),
                    smarte_ziele="\n\n".join(sz_parts),
                    stand_zielerreichung="\n\n".join(stand_parts),
                    vorschlag_ziele="\n\n".join(vz_parts),
                    applies=any(g.applies for g in mapped_goals),
                )
        elif goal is None and not is_odw_target:
            # Hamburg-Ziel, eventuell aus Op-de-Wisch-Plan
            for odw_id, h_ids in OP_DE_WISCH_TO_HAMBURG_MAP.items():
                if s_def.id in h_ids and odw_id in goals_by_id:
                    goal = goals_by_id[odw_id]
                    break

        # Analog für Chat-Daten bei Id-Differenzen
        if not chat_data and is_odw_target:
            # Alle Hamburger Bereiche zusammenführen, die auf diesen Op-de-Wisch-Bereich abbilden
            # (z. B. 1.3 + 1.4 -> 1, 2.1 + 2.2 -> 6), damit kein Dialog-Inhalt still verloren geht.
            merged: Dict[str, str] = {}
            for h_id, odw_id in HAMBURG_TO_OP_DE_WISCH_MAP.items():
                if odw_id == s_def.id and h_id in chat_sections:
                    for field_name, value in chat_sections[h_id].items():
                        if value:
                            prev = merged.get(field_name, "")
                            merged[field_name] = f"{prev}\n\n{value}".strip() if prev else value
            chat_data = merged
        elif not chat_data and not is_odw_target:
            for odw_id, h_ids in OP_DE_WISCH_TO_HAMBURG_MAP.items():
                if s_def.id in h_ids and odw_id in chat_sections:
                    chat_data = dict(chat_sections[odw_id])
                    break

        if bool(
            chat_data.get("bericht_zielerreichung", "").strip()
            or chat_data.get("vorschlag_ziele", "").strip()
            or chat_data.get("vorschlag_massnahmen", "").strip()
        ):
            populated_section_ids.add(s_def.id)

        # Vorbericht-Ziele
        leitziel = goal.leitziel if goal else ""
        smarte_ziele = goal.smarte_ziele if goal else ""

        # Bericht zu Zielerreichung: Chat hat Vorrang, Fallback auf Vorbericht-Stand
        bericht = chat_data.get("bericht_zielerreichung", "").strip()
        if bericht:
            bericht = clean_markdown_for_docx(bericht)
        elif goal and goal.stand_zielerreichung:
            bericht = goal.stand_zielerreichung.strip()

        # Zielvorschläge: Chat hat Vorrang, Fallback auf Vorbericht-Vorschlag
        vorschlag_z = chat_data.get("vorschlag_ziele", "").strip()
        if vorschlag_z:
            vorschlag_z = clean_markdown_for_docx(vorschlag_z)
        elif goal and goal.vorschlag_ziele:
            vorschlag_z = goal.vorschlag_ziele.strip()

        # Maßnahmenvorschlag aus dem Chat
        vorschlag_m = chat_data.get("vorschlag_massnahmen", "").strip()
        if vorschlag_m:
            vorschlag_m = clean_markdown_for_docx(vorschlag_m)

        applies = (goal.applies if goal else False) or bool(
            bericht or leitziel or smarte_ziele or vorschlag_z or vorschlag_m
        )

        sections.append(
            ReportSection(
                id=s_def.id,
                title=s_def.title,
                previous_goals_apply=applies,
                leitziel=leitziel,
                smarte_ziele=smarte_ziele,
                bericht_zielerreichung=bericht,
                vorschlag_ziele=vorschlag_z,
                vorschlag_massnahmen=vorschlag_m,
                no_entries=not bool(bericht or leitziel or vorschlag_z or vorschlag_m),
            )
        )

    draft = ReportDraft(
        client=client,
        meta=meta,
        developments=developments,
        sections=sections,
    )

    # Manuell zu ergänzende Felder ermitteln (Vorlage-spezifisch)
    is_odw = bool(template and template.key == "hamburg_op_de_wisch") or is_odw_target
    manual_fields: List[str] = []

    if is_odw:
        if not client.leistungsart:
            manual_fields.append("Leistungsart")
        if not (meta.leistungszeitraum_von and meta.leistungszeitraum_bis):
            manual_fields.append("Leistungszeitraum")
        if not client.geburtsdatum:
            manual_fields.append("Geburtsdatum")
        if not client.anschrift:
            manual_fields.append("Anschrift")
        manual_fields.append("Gesetzliche Betreuung (Ja/Nein prüfen)")
        if not client.gdb:
            manual_fields.append("GdB")
        if not client.pflegegrad:
            manual_fields.append("Pflegegrad")
        if not meta.abwesenheitszeiten:
            manual_fields.append("Abwesenheitszeiten")
        if not meta.bezugsbetreuer:
            manual_fields.append("Bezugsbegleiter/in")
    else:
        if not client.leistungsart:
            manual_fields.append("Leistungsart")
        if not (meta.leistungszeitraum_von and meta.leistungszeitraum_bis):
            manual_fields.append("Leistung erbracht von/bis")
        if not meta.leistungsanbieter:
            manual_fields.append("Leistungsanbieter")
        if not client.geburtsdatum:
            manual_fields.append("Geburtsdatum")
        if not client.anschrift:
            manual_fields.append("Anschrift")
        manual_fields.append("Gesetzliche Betreuung (Ja/Nein prüfen)")
        if not client.gdb:
            manual_fields.append("GdB")
        if not client.pflegegrad:
            manual_fields.append("Pflegegrad")
        if not meta.abwesenheitszeiten:
            manual_fields.append("Abwesenheitszeiten")
        if not meta.begruegnungsstaette:
            manual_fields.append("Begegnungsstätte")
        if not meta.bezugsbetreuer:
            manual_fields.append("Bezugsbetreuer/in")

    if client_name_missing:
        manual_fields.insert(0, CLIENT_NAME_MANUAL_ITEM)
    if csv_header_issue:
        manual_fields.append(csv_header_issue)

    if zeitraum_derived_from_entries:
        manual_fields.append("Leistungszeitraum aus erstem/letztem Eintrag abgeleitet – bitte prüfen")

    has_plan_goals = False
    if plan_data and plan_data.goals:
        for g in plan_data.goals:
            if (g.leitziel and g.leitziel.strip()) or (g.smarte_ziele and g.smarte_ziele.strip()):
                has_plan_goals = True
                break
    if not has_plan_goals:
        manual_fields.append("Leitziel/Smarte Ziele (Gesamtplan)")

    if not is_odw:
        manual_fields.append("Folgeantrag auf Haushaltshilfen (Auswahlfeld 1.2)")

    warnings: List[str] = [b.label() for b in chat_sections.unmapped_blocks]
    if template:
        diag_warnings = check_unmapped_mentions_in_chat(
            history, template, chat_sections=chat_sections, populated_section_ids=populated_section_ids
        )
        for dw in diag_warnings:
            if dw not in warnings:
                warnings.append(dw)

    if return_unmapped and return_manual:
        return draft, warnings, manual_fields
    if return_unmapped:
        return draft, warnings
    if return_manual:
        return draft, manual_fields
    return draft


#: Kurzbezeichnung der Vorlage im Download-Dateinamen (nur ASCII).
TEMPLATE_FILENAME_SLUGS: Dict[str, str] = {
    "hamburg_soziale_dienste": "hamburg",
    "hamburg_op_de_wisch": "op-de-wisch",
}


def export_filename(template: ReportTemplate, when: Optional[date] = None) -> str:
    """Neutraler, rein ASCII-basierter Download-Dateiname: ``SOB_<Vorlage>_<Datum>.docx``.

    Enthält bewusst keinen Klientennamen (kein Personenbezug im Downloads-Ordner,
    kein UnicodeEncodeError im Content-Disposition-Header).
    """
    when = when or date.today()
    slug = TEMPLATE_FILENAME_SLUGS.get(template.key) or re.sub(r"[^A-Za-z0-9_-]+", "-", template.key).strip("-")
    return f"SOB_{slug or 'vorlage'}_{when:%Y-%m-%d}.docx"


def export_chatbot_to_docx(
    bot: ReflectionChatbot,
    template_name: str,
) -> DocxExportResult:
    """Generiert einen befüllten Word-Bericht (.docx) aus dem Zustand des Chatbots.

    Erzeugt das Dokument rein speicherbasiert via BytesIO (Zero-Disk).

    Args:
        bot: ReflectionChatbot-Instanz mit aktuellem Sitzungszustand.
        template_name: Name oder Alias der Vorlage ('hamburg' oder 'op-de-wisch').

    Returns:
        DocxExportResult(docx_bytes, filename, warnings, manual_fields)
    """
    template = resolve_template(template_name)
    draft, warnings, manual_fields = build_report_draft_from_chatbot(
        bot, template=template, return_unmapped=True, return_manual=True
    )

    # Neutraler Dateiname ohne Klientennamen (DSGVO, #114/#115): SOB_<Vorlage>_<Datum>.docx
    filename = export_filename(template)

    buffer = io.BytesIO()
    renderer = template.renderer_factory(template.docx_path)
    renderer.render(draft, buffer)
    docx_bytes = buffer.getvalue()

    return DocxExportResult(docx_bytes, filename, warnings=warnings, manual_fields=manual_fields)
