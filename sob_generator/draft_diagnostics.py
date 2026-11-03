"""Abnahme-Diagnostik für einen ReportDraft — objektive, entscheidbare Signale.

Misst die maschinell prüfbaren Akzeptanzkriterien aus #36 (Opener-Kollaps) und
#37 (Stichpunktlisten im Prosa-Feld, nackter Nachname mitten im Satz), damit ein
Modellwechsel (Llama-3.1-8B vs. phi-4) an denselben synthetischen Fixtures
objektiv verglichen werden kann, statt nach Bauchgefühl.

Bewusst NICHT automatisiert sind die semantischen Kriterien aus #37 — die
Themen-Verschmierung zwischen Abschnitten und die Grammatik-/Deklinationsfehler.
Beide sind nicht zuverlässig regelbasiert entscheidbar; ein vorgetäuschter
Detektor würde falsche Sicherheit erzeugen. Sie bleiben der manuellen Abnahme
vorbehalten — der Bericht liefert dafür je Abschnitt Opener und ersten Satz.

Reine Logik: keine I/O, kein LLM, keine docx-Abhängigkeit. Die Diagnose hängt
nur am internen Datenmodell (ReportDraft).
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from .models import ReportDraft, ReportSection
from .opener_diversity import DEFAULT_MAX_SHARED, opener_key

# Zeilenanfänge, die eine Stichpunktliste markieren: „- ", „* ", „• ", „1. ",
# „2) ". `bericht_zielerreichung` soll Fließtext sein (#37, Kriterium 2).
_BULLET_RE = re.compile(r"(?m)^[ \t]*(?:[-*•]|\d+[.)])[ \t]+")

# Wörter, nach denen der Nachname als korrekte Anrede gilt (#27/#36).
_ANREDE_WORDS = frozenset({"herr", "herrn", "frau"})
_WORD_RE = re.compile(r"[0-9A-Za-zÀ-ÿ]+")
_SENTENCE_END = (".", "!", "?", ":")

# Stilqualität (#34/#38): „Beispiel" als ganzes Wort im Bericht ist unpassend
# für SOB-Amtsstil (umgangssprachlich „zum Beispiel") und war früher auch ein
# Platzhalter-Leak-Indikator (Few-Shot-Name war „Herr Beispiel", jetzt „Herr Muster").
_PLACEHOLDER_RE = re.compile(r"\bBeispiel\b")

# Genitiv-Apostroph (#37/#38): falscher dt. Genitiv „Mustermann’" oder
# „Mustermann’s". Drei Apostrophvarianten (ASCII, typografisch).
_GENITIVE_APOSTROPHE_RE = re.compile(r"\b([A-ZÄÖÜ][A-Za-zäöüÄÖÜß]+)[‘’’]s?\b")

# JSON-in-Feld-Erkennung: Feldwert beginnt nach optionalem Whitespace mit
# `[` oder `{` — Zeichen für ein rohes JSON-Array/-Objekt statt Fließtext.
# Tritt auf wenn das Modell den Grammar-Zwang missversteht oder die
# Kontextgrenze überschritten wurde (#40-Diagnose).
_JSON_IN_FIELD_RE = re.compile(r"^\s*[\[{]")

# Pflichtfelder: müssen vom LLM befüllt werden. `realisierung_andere` ist
# ausgenommen — leerer String ist dort ein gültiger Wert (Schema-Kommentar).
_REQUIRED_LLM_FIELDS = ("bericht_zielerreichung", "vorschlag_ziele", "vorschlag_massnahmen")


def empty_llm_fields(section: ReportSection) -> tuple[str, ...]:
    """Namen der Pflicht-LLM-Felder, die leer oder nur Whitespace sind.

    `realisierung_andere` wird ausgelassen — dort ist ein leerer String
    semantisch korrekt (nicht zutreffend).
    """
    return tuple(
        name
        for name in _REQUIRED_LLM_FIELDS
        if not getattr(section, name, "").strip()
    )


def json_in_fields(section: ReportSection) -> tuple[str, ...]:
    """Namen aller vier LLM-Felder, deren Wert mit `[` oder `{` beginnt.

    Zeigt an, dass das Modell ein JSON-Objekt/-Array statt Fließtext erzeugt
    hat — ein kritischer Fehler, der im DOCX als Rohtext landet.
    """
    all_fields = _REQUIRED_LLM_FIELDS + ("realisierung_andere",)
    return tuple(
        name
        for name in all_fields
        if _JSON_IN_FIELD_RE.match(getattr(section, name, ""))
    )


def has_bullets(text: str) -> bool:
    """True, wenn `text` mindestens eine Stichpunkt-Zeile enthält."""
    return _BULLET_RE.search(text) is not None


def bare_surname_hits(text: str, surname: str) -> int:
    """Zählt nackte Nachnamen mitten im Satz — ohne „Herr/Frau" davor (#37).

    Ein Vorkommen am Satzanfang oder direkt nach einer Anrede (Herr/Herrn/Frau)
    gilt als korrekt und wird nicht gezählt. Heuristik: bewertet nur das direkt
    vorangehende Wort und das Satzende-Zeichen, keine Syntaxanalyse.
    """
    if not surname:
        return 0
    hits = 0
    for match in re.finditer(rf"\b{re.escape(surname)}\b", text):
        prefix = text[: match.start()]
        preceding_words = _WORD_RE.findall(prefix)
        preceded_by_anrede = bool(preceding_words) and preceding_words[-1].casefold() in _ANREDE_WORDS
        stripped = prefix.rstrip()
        at_sentence_start = not stripped or stripped.endswith(_SENTENCE_END)
        if not preceded_by_anrede and not at_sentence_start:
            hits += 1
    return hits


def placeholder_leaks(text: str) -> int:
    """Zählt Vorkommen des Few-Shot-Platzhalters „Beispiel" im Text (#34/#38)."""
    return len(_PLACEHOLDER_RE.findall(text))


def genitive_apostrophes(text: str, surname: str) -> int:
    """Zählt falsche dt. Genitiv-Apostrophe für den Nachnamen (#37/#38).

    Gesucht: „{Nachname}'" oder „{Nachname}'s" (ASCII- und typografische
    Apostrophvarianten U+0027/U+2018/U+2019). Leerer Nachname → immer 0.

    Trailing \\b vermieden: nach einem Apostroph ist kein Wortgrenzen-Anker
    möglich (Apostroph ist kein Wortzeichen). Stattdessen negativer Lookahead
    `(?!\\w)`, um Treffer wie „Mustermann'sche" auszuschließen.
    """
    if not surname:
        return 0
    pattern = re.compile(
        rf"\b{re.escape(surname)}(?:'|‘|’)s?(?!\w)"
    )
    return len(pattern.findall(text))


# Bracket/Stub-Platzhalter (#55 Quality Gate): erkennt Reste wie [STUB],
# [TODO], [...], [Platzhalter], [Name], etc.
_BRACKET_PLACEHOLDER_RE = re.compile(
    r"\[(?:STUB|TODO|\.\.\.|\s*|Platzhalter|Datum|Name|X)\]", re.IGNORECASE
)


def vorname_leaks(text: str, vorname: str) -> int:
    """Zählt Leaks des Vornamens im Fließtext (#32/#55 GDPR Check)."""
    if not vorname or not text:
        return 0
    teile = {vorname.strip(), *vorname.split()}
    namen = sorted((t for t in teile if len(t) >= 2), key=len, reverse=True)
    if not namen:
        return 0
    pattern = re.compile(r"\b(?:" + "|".join(re.escape(n) for n in namen) + r")\b", re.IGNORECASE)
    return len(pattern.findall(text))


def bracket_placeholders(text: str) -> int:
    """Zählt verbliebene Stub- und Klammer-Platzhalter im Fließtext (#55)."""
    if not text:
        return 0
    return len(_BRACKET_PLACEHOLDER_RE.findall(text))


def first_sentence(text: str) -> str:
    """Erster Satz (bis zum ersten Satzende-Zeichen) — für die manuelle Abnahme."""
    stripped = text.strip()
    for idx, char in enumerate(stripped):
        if char in (".", "!", "?"):
            return stripped[: idx + 1]
    return stripped


@dataclass(frozen=True)
class SectionDiagnostics:
    """Maschinell prüfbare Signale eines einzelnen Abschnitts."""

    id: str
    opener: str
    has_bullets: bool
    bare_surname_hits: int
    placeholder_leaks: int
    genitive_apostrophes: int
    first_sentence: str
    needs_review: bool
    empty_fields: tuple[str, ...]
    json_fields: tuple[str, ...]
    # Guard-Fall (#50/K1): Pass 0 hat keine Einträge zugeordnet — Datenlage,
    # kein LLM-Defekt. Solche Abschnitte zählen nicht in die kritischen
    # LLM-Qualitätsmetriken (empty_fields/needs_review), sondern werden
    # separat als `no_entries_sections` ausgewiesen.
    no_entries: bool = False
    vorname_leaks: int = 0
    bracket_placeholders: int = 0


@dataclass(frozen=True)
class DraftDiagnostics:
    """Aggregierte Abnahme-Signale über alle Abschnitte eines Berichts."""

    sections: tuple[SectionDiagnostics, ...]
    max_shared_opener: int
    colliding_openers: tuple[str, ...]

    @property
    def opener_ok(self) -> bool:
        """#36: höchstens DEFAULT_MAX_SHARED Abschnitte je 3-Wort-Anfang."""
        return self.max_shared_opener <= DEFAULT_MAX_SHARED

    @property
    def bullet_sections(self) -> tuple[str, ...]:
        """IDs der Abschnitte mit Stichpunktliste im Prosa-Feld (#37)."""
        return tuple(s.id for s in self.sections if s.has_bullets)

    @property
    def prose_ok(self) -> bool:
        """#37: kein bericht_zielerreichung enthält eine Stichpunktliste."""
        return not self.bullet_sections

    @property
    def total_bare_surname_hits(self) -> int:
        return sum(s.bare_surname_hits for s in self.sections)

    @property
    def total_placeholder_leaks(self) -> int:
        return sum(s.placeholder_leaks for s in self.sections)

    @property
    def total_genitive_apostrophes(self) -> int:
        return sum(s.genitive_apostrophes for s in self.sections)

    @property
    def total_vorname_leaks(self) -> int:
        return sum(s.vorname_leaks for s in self.sections)

    @property
    def total_bracket_placeholders(self) -> int:
        return sum(s.bracket_placeholders for s in self.sections)

    @property
    def no_entries_sections(self) -> tuple[str, ...]:
        """IDs der Abschnitte ohne zugeordnete Einträge (#50/K1) — Datenlage."""
        return tuple(s.id for s in self.sections if s.no_entries)

    @property
    def total_needs_review(self) -> int:
        """Abschnitte, bei denen die LLM-Generierung zweimal fehlschlug (#18).

        Guard-Abschnitte ohne Einträge (#50/K1) zählen nicht mit — dort lief
        gar kein LLM-Call; sie stehen in `no_entries_sections`.
        """
        return sum(1 for s in self.sections if s.needs_review and not s.no_entries)

    @property
    def total_empty_fields(self) -> int:
        """Summe leerer Pflicht-LLM-Felder über alle Abschnitte.

        Guard-Abschnitte ohne Einträge (#50/K1) sind erwartbar leer und
        zählen nicht mit — sonst würde die Datenlage als LLM-Defekt gewertet.
        """
        return sum(len(s.empty_fields) for s in self.sections if not s.no_entries)

    @property
    def total_json_fields(self) -> int:
        """Summe der Felder mit rohem JSON-Inhalt statt Fließtext."""
        return sum(len(s.json_fields) for s in self.sections)

    @property
    def critical_ok(self) -> bool:
        """True, wenn keine leeren Felder, kein JSON in Feldern und kein needs_review."""
        return self.total_empty_fields == 0 and self.total_json_fields == 0 and self.total_needs_review == 0

    @property
    def gdpr_ok(self) -> bool:
        """#55: Keine Vornamen-Leaks, keine nackten Nachnamen."""
        return (
            self.total_vorname_leaks == 0
            and self.total_bare_surname_hits == 0
        )

    @property
    def completeness_ok(self) -> bool:
        """#55: Alle Pflichtfelder befüllt, kein rohes JSON, keine Review-Fehler."""
        return self.critical_ok

    @property
    def placeholders_ok(self) -> bool:
        """#55: Keine verbliebenen [STUB]- oder Beispiel-Platzhalter."""
        return self.total_bracket_placeholders == 0 and self.total_placeholder_leaks == 0

    @property
    def all_passed(self) -> bool:
        """Gesamtes Pre-Export Quality Gate bestanden (#55)."""
        return self.gdpr_ok and self.completeness_ok and self.placeholders_ok and self.prose_ok and self.opener_ok

    @property
    def quality_badges(self) -> tuple[dict, ...]:
        """Strukturierte Status-Badges für das Pre-Export Quality Gate im UI-Review (#55)."""
        return (
            {
                "id": "gdpr",
                "label": "DSGVO & Anrede",
                "icon": "🛡️",
                "passed": self.gdpr_ok,
                "detail": (
                    "Sauber"
                    if self.gdpr_ok
                    else f"Hinweis: {self.total_vorname_leaks} Vorname(n), {self.total_bare_surname_hits} nackte Nachname(n)"
                ),
            },
            {
                "id": "completeness",
                "label": "Vollständigkeit",
                "icon": "🔍",
                "passed": self.completeness_ok,
                "detail": (
                    "Vollständig"
                    if self.completeness_ok
                    else f"Hinweis: {self.total_empty_fields} leere Pflichtfelder, {self.total_json_fields} Formatfehler"
                ),
            },
            {
                "id": "placeholders",
                "label": "Platzhalter-Guard",
                "icon": "🚫",
                "passed": self.placeholders_ok,
                "detail": (
                    "Keine Reste"
                    if self.placeholders_ok
                    else f"Hinweis: {self.total_bracket_placeholders} Stub-Klammern, {self.total_placeholder_leaks} Platzhalter-Namen"
                ),
            },
            {
                "id": "prose",
                "label": "Behördenprosa-Stil",
                "icon": "✍️",
                "passed": self.prose_ok and self.opener_ok,
                "detail": (
                    "Guter Stil"
                    if (self.prose_ok and self.opener_ok)
                    else f"Hinweis: {len(self.bullet_sections)} Listen im Text, Eröffnungskollision: {self.max_shared_opener}"
                ),
            },
        )


@dataclass(frozen=True)
class GateCriterion:
    """Ergebnis eines einzelnen Gate-Kriteriums."""

    name: str
    actual: int
    limit: int

    @property
    def passed(self) -> bool:
        return self.actual <= self.limit


@dataclass(frozen=True)
class GateResult:
    """Gesamtergebnis des Baseline-Vergleichs."""

    criteria: tuple[GateCriterion, ...]

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.criteria)

    @property
    def failures(self) -> tuple[GateCriterion, ...]:
        return tuple(c for c in self.criteria if not c.passed)


def compare_to_baseline(diag: DraftDiagnostics, baseline: dict) -> GateResult:
    """Vergleicht Diagnostik-Werte gegen eine eingefrorene Baseline (#38).

    `baseline` ist ein Dict mit optionalen Schlüsseln:
      max_shared_opener, bullet_sections, placeholder_leaks,
      genitive_apostrophes, bare_surname_hits,
      empty_fields, json_fields, needs_review.
    Fehlende Schlüssel werden übersprungen (kalibrierbar).
    """
    checks = [
        ("max_shared_opener", diag.max_shared_opener),
        ("bullet_sections", len(diag.bullet_sections)),
        ("placeholder_leaks", diag.total_placeholder_leaks),
        ("genitive_apostrophes", diag.total_genitive_apostrophes),
        ("bare_surname_hits", diag.total_bare_surname_hits),
        ("empty_fields", diag.total_empty_fields),
        ("json_fields", diag.total_json_fields),
        ("needs_review", diag.total_needs_review),
    ]
    criteria = tuple(
        GateCriterion(name=name, actual=actual, limit=baseline[name])
        for name, actual in checks
        if name in baseline
    )
    return GateResult(criteria=criteria)


def diagnose(draft: ReportDraft) -> DraftDiagnostics:
    """Erhebt alle objektiven Abnahme-Signale aus einem fertigen ReportDraft."""
    surname = draft.client.name
    vorname = draft.client.vorname
    sections = []
    for section in draft.sections:
        full_text = f"{section.bericht_zielerreichung}\n{section.vorschlag_ziele}\n{section.vorschlag_massnahmen}\n{section.realisierung_andere}"
        sections.append(
            SectionDiagnostics(
                id=section.id,
                opener=opener_key(section.bericht_zielerreichung),
                has_bullets=has_bullets(section.bericht_zielerreichung),
                bare_surname_hits=bare_surname_hits(section.bericht_zielerreichung, surname),
                placeholder_leaks=placeholder_leaks(full_text),
                genitive_apostrophes=genitive_apostrophes(section.bericht_zielerreichung, surname),
                first_sentence=first_sentence(section.bericht_zielerreichung),
                needs_review=section.needs_review,
                empty_fields=empty_llm_fields(section),
                json_fields=json_in_fields(section),
                no_entries=section.no_entries,
                vorname_leaks=vorname_leaks(full_text, vorname),
                bracket_placeholders=bracket_placeholders(full_text),
            )
        )

    sections_tuple = tuple(sections)
    opener_counts = Counter(s.opener for s in sections_tuple if s.opener)
    max_shared = max(opener_counts.values(), default=0)
    colliding = tuple(
        sorted(key for key, count in opener_counts.items() if count > DEFAULT_MAX_SHARED)
    )
    return DraftDiagnostics(
        sections=sections_tuple,
        max_shared_opener=max_shared,
        colliding_openers=colliding,
    )
