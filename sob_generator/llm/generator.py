"""LLMDraftGenerator (ADR-2, ADR-6, ADR-11).

Ein Aufruf erzeugt entweder die 4 LLM-Felder eines Abschnitts
(`SectionLLMOutput`) oder das Top-Level-Feld `developments`
(`DevelopmentsLLMOutput`). Der Output wird per GBNF-Grammar (grammar.py)
auf gültiges JSON im Schema des jeweiligen Pydantic-Modells erzwungen und
anschließend validiert (ADR-11).

Fehlerverhalten (ADR-3, Human-in-the-Loop als Sicherheitsnetz):
- 1. Versuch schlägt fehl (kein valides JSON / Pydantic-Validierungsfehler)
  → 1 Retry mit verschärfter Erinnerung im Prompt.
- 2. Versuch schlägt ebenfalls fehl → leere Felder + `needs_review=True`.
  Die Fachkraft sieht das Feld leer und mit Markierung im Review (ADR-3),
  der Lauf bricht NICHT ab.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError

from sob_generator.previous_sob import HAMBURG_TO_OP_DE_WISCH_GOALS
from .grammar import build_grammar
from .prompts import (
    build_batch_classification_prompt,
    build_classification_prompt,
    build_developments_prompt,
    build_field_goal_clarification_prompt,
    build_field_questions_prompt,
    build_gap_analysis_prompt,
    build_refine_section_prompt,
    build_revise_field_goals_prompt,
    build_revise_section_prompt,
    build_screening_turn_prompt,
    build_section_prompt,
)

if TYPE_CHECKING:
    from sob_generator.models import ClientData
from .schemas import (
    BatchEntryClassification,
    DevelopmentsLLMOutput,
    DocumentationEntry,
    EntryClassification,
    GapAnalysisOutput,
    ScreeningTurnOutput,
    SectionLLMOutput,
)
from .sections import SectionDefinition

ModelT = TypeVar("ModelT", bound=BaseModel)

logger = logging.getLogger(__name__)


def _count_sentences(text: str) -> int:
    """Zählt Sätze anhand Satzendzeichen — konsistent mit dem DOCX-Audit."""
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    return len([s for s in parts if len(s.strip()) > 3])


def sanitize_clinical_terminology(text: str) -> str:
    """Bereinigt verharmlosende Floskeln bei psychiatrischen Krisen (Issue #71)."""
    if not text:
        return text
    # "psychologischen Phase" -> "psychiatrischen Krise"
    cleaned = re.sub(
        r"\bpsychologischen\s+Phase\b",
        "psychiatrischen Krise",
        text,
        flags=re.IGNORECASE,
    )
    # "psychologische Phase" -> "psychiatrische Krise"
    cleaned = re.sub(
        r"\bpsychologische\s+Phase\b",
        "psychiatrische Krise",
        cleaned,
        flags=re.IGNORECASE,
    )
    # "Down-Phase" -> "depressive Phase"
    cleaned = re.sub(
        r"\bDown-Phase(n)?\b",
        r"depressive Phase\1",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned


def clean_dialogue_response(text: str) -> str:
    """Bereinigt Chat-Antworten von Brief-Anreden, Grußformeln, Platzhaltern, SMART-Klammern und Meta-Kommentaren."""
    cleaned = text.strip()

    # Brief-Anreden am Anfang entfernen (z. B. "Liebe Kollegin,", "Hallo Kollege,")
    cleaned = re.sub(
        r"^(?:(?:Liebe[rs]?|Hallo|Guten Tag)\s+(?:Kollegin|Kollege|Team|Fachkraft|[A-ZÄÖÜ][a-zäöüß]+)[,:\s]*)+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()

    # Grußformeln und Signaturen am Ende entfernen (z. B. "Liebe Grüße\n[Dein Name]")
    cleaned = re.sub(
        r"(?:\n+|^)(?:(?:Liebe|Viele|Herzliche|Beste|Schöne)\s+Grüße|Mit\s+kollegialen\s+Grüßen|Dein[e]?\s+Assistent[in]?)[^\n]*(?:\n+\[?[^\]\n]+\]?)?\s*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()

    # Platzhalter wie [Dein Name], [Name], [Unterschrift] entfernen
    cleaned = re.sub(r"\[(?:Dein\s+Name|Name|Unterschrift|Absender)\]", "", cleaned, flags=re.IGNORECASE).strip()

    # Didaktische SMART-Klammern wie (Spezifisch: ..., Messbar: ..., ...) entfernen
    cleaned = re.sub(
        r"\s*\((?:Spezifisch|Messbar|Akzeptabel|Attraktiv|Realistisch|Terminiert|Zeitbegrenzt):[^)]+\)",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()

    # Meta-Kommentare und Nachbemerkungen am Ende entfernen (z. B. "**Kommentar:** ...", "Die Reflexionsfragen zielen darauf ab...")
    cleaned = re.sub(
        r"(?:\n+|^)(?:\*{0,2}(?:Kommentar|Hinweis|Anmerkung):?\*{0,2}|Die Reflexionsfragen zielen darauf ab).*$",
        "",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    ).strip()

    # Klinische Terminologie bereinigen (#71)
    cleaned = sanitize_clinical_terminology(cleaned)

    return cleaned


def extract_single_question(text: str, prefer_crisis: bool = False) -> str:
    """Extrahiert strikt genau EINE fokussierte Frage aus einer LLM-Antwort.

    Verhindert 'ganze Texte statt Dialog' und schneidet Fragenbatterien (1., 2., 3., 4.)
    sowie vor- und nachgelagertes Beiwerk rigoros auf eine einzelne, dialogische Frage ab.
    """
    if not text or not text.strip():
        return text
    cleaned = clean_dialogue_response(text).strip()

    # Meta-Header entfernen: "Vertiefungsfrage zur Konkretisierung:", "Vertiefungsfragen:", "Fragen an dich:" etc.
    cleaned = re.sub(
        r"^(?:\*{0,2}(?:Vertiefungsfrage(?:n)?|Frage(?:n)?|Frage an dich|Rückfrage(?:n)?)(?:\s*(?:zur|an)[^:\n]*)?:?\*{0,2}\s*\n*)+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()

    # Nach nummerierten Items splitten (z. B. "1. ... \n2. ...", "1) ...", "- ...", "• ...")
    item_pattern = re.compile(r"(?:^|\n+)\s*(?:[1-9]\d*[\.\)]|•|-)\s*")
    raw_parts = [p.strip() for p in item_pattern.split(cleaned) if p.strip()]

    selected = ""
    if len(raw_parts) > 1:
        if prefer_crisis:
            # 1. Priorisiere spezifische Akutkrisen-Keywords (Notfall, Krise, Suizid, Psychiatrie)
            acute_kw = ("notfall", "krise", "suizid", "psychiatr")
            for p in raw_parts:
                if any(kw in p.lower() for kw in acute_kw) and "?" in p:
                    selected = p
                    break
            # 2. Falls nicht gefunden, sekundäre Schutz-Keywords
            if not selected:
                general_kw = ("deeskalation", "überforderung", "schutz", "stabilisier")
                for p in raw_parts:
                    if any(kw in p.lower() for kw in general_kw) and "?" in p:
                        selected = p
                        break
        if not selected:
            selected = raw_parts[0]
    elif raw_parts:
        selected = raw_parts[0]
    else:
        selected = cleaned

    # Falls das Item führende Nummerierung hat (z. B. falls split am Anfang stand):
    selected = re.sub(r"^(?:[1-9]\d*[\.\)]|•|-)\s*", "", selected).strip()

    # Falls das ausgewählte Item immer noch mehrere Fragesätze enthält (getrennt durch '?'):
    # z. B. "Frage 1? Welche weiteren Hilfen...?"
    q_matches = [q.strip() + "?" for q in selected.split("?") if q.strip()]
    if len(q_matches) > 1:
        if prefer_crisis:
            acute_kw = ("notfall", "krise", "suizid", "psychiatr")
            for qm in q_matches:
                if any(kw in qm.lower() for kw in acute_kw):
                    selected = qm
                    break
            else:
                general_kw = ("deeskalation", "überforderung", "schutz", "stabilisier")
                for qm in q_matches:
                    if any(kw in qm.lower() for kw in general_kw):
                        selected = qm
                        break
                else:
                    selected = q_matches[0]
        else:
            selected = q_matches[0]
    elif q_matches:
        selected = q_matches[0]

    # Bereinige mehrfache Satzzeichen
    selected = re.sub(r"\?+", "?", selected).strip()

    # Falls kein Fragezeichen vorhanden ist, aber es eine Frage ist:
    if selected and not selected.endswith("?") and any(selected.lower().startswith(w) for w in ("wie", "was", "welch", "wo", "inwiefern", "gibt", "kann", "können")):
        selected += "?"

    return selected


RETRY_REMINDER = (
    "\n\nDeine letzte Antwort war kein gültiges JSON oder enthielt leere Pflichtfelder. "
    "Antworte jetzt ausschließlich mit einem einzigen JSON-Objekt. "
    "Das Feld \"bericht_zielerreichung\" MUSS einen nicht-leeren Fließtext enthalten, "
    "der die Dokumentationseinträge zusammenfasst. Auch \"vorschlag_ziele\" und "
    "\"vorschlag_massnahmen\" MÜSSEN nicht-leer sein: konkrete Ziele bzw. "
    "Maßnahmen für den nächsten Bewilligungszeitraum."
)

# Budget für ALLE 9 Abschnitte + developments zusammen: 1-3 Min. (ADR-2).
# 1536 = ~384 Tokens je Feld * 4 Felder (#47): mit 1024 brach der 14B-Lauf
# (2026-06-30) das JSON regelmäßig VOR vorschlag_ziele/-massnahmen ab — beide
# Felder blieben in allen 9 Abschnitten leer (20 leere Pflichtfelder), weil
# das ausführlichere bericht_zielerreichung das Budget allein aufbrauchte.
# Kostenpunkt: max_tokens ist nur eine Obergrenze; kurze, vollständige
# Antworten stoppen am Grammatik-Ende und werden nicht langsamer.
SECTION_MAX_TOKENS = 1536
# Pro Eintrag ~150 Token Spielraum; Minimum bleibt SECTION_MAX_TOKENS.
_TOKENS_PER_ENTRY = 250
# developments fasst ALLE Einträge des Berichtszeitraums zusammen (#50/K5) —
# 512 konnte das JSON bei langen Zeiträumen vor dem schließenden Objekt
# abschneiden (→ Validierungsfehler trotz inhaltlich guter Antwort). Wie bei
# SECTION_MAX_TOKENS ist das nur eine Obergrenze, keine Ziel-Länge.
DEVELOPMENTS_MAX_TOKENS = 768

# Tight per-turn max_tokens für interaktive Dialogschritte (#55, #60 Follow-Up):
# 600 Tokens bietet Puffer gegen vorzeitiges Abschneiden (Truncation).
CHAT_TURN_MAX_TOKENS = 600

# Laufzeit-Defaults für llama.cpp — EINE Quelle der Wahrheit für CLI
# (sob_generator.__main__) und UI (app.main). Ohne sie fällt llama.cpp auf
# n_ctx=512 (zu klein für die Abschnitts-Prompts → Lauf bricht ab) und CPU-only
# zurück. n_gpu_layers=-1 ist der Performance-Hebel auf Apple Silicon (ADR-2/8).
DEFAULT_N_CTX = 8192
DEFAULT_N_THREADS = 4
DEFAULT_N_GPU_LAYERS = -1

# Generierungs-Parameter gegen Wiederholungsschleifen (#19): im 8B-Lauf (#13)
# liefen Calls in Endlosschleifen bis ins Token-Limit. repeat_penalty bestraft
# das Wiederholen kürzlich erzeugter Tokens. ~1.15–1.3 bricht die Schleifen,
# ohne die deutsche Morphologie/Flüssigkeit spürbar zu beschädigen; 1.15 ist der
# konservative Einstieg. WICHTIG: ein Generierungs-Parameter — gehört an den
# self.llm(...)-Call in _complete, NICHT an den Llama(...)-Konstruktor
# (llama_kwargs), sonst wirkungslos.
DEFAULT_REPEAT_PENALTY = 1.15

# Fester Seed für reproduzierbare A/B- und Regressionsläufe (#35/#38).
# Übergabe an den Llama(...)-Konstruktor ist der zuverlässigste Weg in
# llama.cpp (vs. Übergabe je Call). -1 = llama.cpp-Default (zufällig).
DEFAULT_SEED = 42

# Sampling-Temperatur (#30, Few-Shot-Kollaps). Die 9 Abschnitte werden in
# unabhängigen Aufrufen erzeugt; bei nahezu greedy-Decoding (0.2) fällt jeder
# Aufruf auf dieselbe wahrscheinlichste Eröffnungsfloskel zurück — alle
# Abschnitte beginnen identisch ("In der Betreuung lag ein Schwerpunkt …",
# "Die Assistenz reflektiert in Einzelgesprächen …"). Weder Few-Shot-Vielfalt
# noch die Anti-Schablonen-Anweisung greifen gegen diesen Kollaps, solange das
# Decoding quasi-deterministisch ist. 0.7 löst die Abschnitte voneinander,
# ohne die Faktentreue zu gefährden: die GBNF-Grammar erzwingt weiterhin
# gültiges JSON (ADR-11), die Temperatur betrifft nur die Formulierung.
GENERATION_TEMPERATURE = 0.7

# Chat-Format-Wrapping (#49): Qwen2.5 ist auf ChatML trainiert — ohne die
# <|im_start|>-Marker sieht das Modell den Prompt als Text-Continuation statt
# als Instruktion. WICHTIG: `chat_format` im Llama(...)-Konstruktor wirkt NUR
# auf create_chat_completion; dieser Generator nutzt raw completion
# (self.llm(prompt), wegen GBNF-Grammar) — der Wrapper muss deshalb manuell
# um den Prompt gelegt werden. "auto" erkennt Qwen am Modell-Dateinamen;
# Llama-3.1/Phi-4 haben eigene Templates und bleiben bei raw completion.
CHAT_FORMAT_AUTO = "auto"
CHAT_FORMAT_NONE = "none"
CHAT_FORMAT_CHATML = "chatml"
CHAT_FORMATS = (CHAT_FORMAT_AUTO, CHAT_FORMAT_NONE, CHAT_FORMAT_CHATML)


def _wrap_chatml(prompt: str) -> str:
    """Legt die ChatML-Marker um den Prompt (User-Turn → Assistant-Antwort)."""
    return f"<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n"


def resolve_chat_format(chat_format: str, model_path: str) -> str:
    """Löst "auto" anhand des Modell-Dateinamens auf ("qwen" → chatml)."""
    if chat_format not in CHAT_FORMATS:
        raise ValueError(
            f"Unbekanntes chat_format {chat_format!r} — erlaubt: {CHAT_FORMATS}"
        )
    if chat_format != CHAT_FORMAT_AUTO:
        return chat_format
    from pathlib import Path

    name = Path(model_path).name.lower()
    return CHAT_FORMAT_CHATML if "qwen" in name else CHAT_FORMAT_NONE


@dataclass
class GenerationResult(Generic[ModelT]):
    value: ModelT
    needs_review: bool
    raw_output: str
    error: str | None = None


def _empty_instance(model_cls: Type[ModelT]) -> ModelT:
    # model_construct umgeht Validatoren bewusst — der leere Fallback darf nicht
    # durch den bericht_zielerreichung-Validator blockiert werden.
    return model_cls.model_construct(**{name: "" for name in model_cls.model_fields})


def _validate(raw: str, model_cls: Type[ModelT]) -> ModelT:
    """Wirft ValueError, wenn `raw` kein gültiges JSON im Schema von model_cls ist."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"kein gültiges JSON: {exc}") from exc

    try:
        return model_cls.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"Pydantic-Validierung fehlgeschlagen: {exc}") from exc





class LLMDraftGenerator:
    """Wrapper um llama-cpp-python für grammar-erzwungene Abschnitts-Generierung."""

    def __init__(
        self,
        model_path: str,
        repeat_penalty: float = DEFAULT_REPEAT_PENALTY,
        seed: int = DEFAULT_SEED,
        chat_format: str = CHAT_FORMAT_AUTO,
        **llama_kwargs,
    ):
        self._model_path = model_path
        # #49: "auto" → chatml für Qwen, sonst raw completion. Einmal beim
        # Konstruieren auflösen, damit jeder _complete-Call denselben Modus hat.
        self._chat_format = resolve_chat_format(chat_format, model_path)
        # Sampling-Parameter gegen Wiederholungsschleifen (#19). Konfigurierbar,
        # damit CLI/UI ihn bei Bedarf nachschärfen können (Spike → voller Lauf).
        self._repeat_penalty = repeat_penalty
        # Produktionsdefaults setzen; explizit übergebene Werte (z.B. von der
        # CLI über --n-ctx/--n-gpu-layers) überschreiben sie.
        self._llama_kwargs = {
            "n_ctx": DEFAULT_N_CTX,
            "n_threads": DEFAULT_N_THREADS,
            "n_gpu_layers": DEFAULT_N_GPU_LAYERS,
            "seed": seed,
            **llama_kwargs,
        }
        self._llm = None
        self._lock = threading.Lock()

    @property
    def llm(self):
        if self._llm is None:
            with self._lock:
                if self._llm is None:
                    from sob_generator.model_integrity import verify_model_sha256

                    verify_model_sha256(self._model_path)
                    from llama_cpp import Llama

                    self._llm = Llama(model_path=self._model_path, **self._llama_kwargs)
        return self._llm

    def _complete(self, prompt: str, model_cls: Optional[Type[BaseModel]], max_tokens: int) -> str:
        grammar = build_grammar(model_cls) if model_cls is not None else None
        if self._chat_format == CHAT_FORMAT_CHATML:
            prompt = _wrap_chatml(prompt)
        # Guard nach dem Wrapping: gezählt wird der Prompt, der wirklich rausgeht.
        self._warn_if_context_overflow(prompt, max_tokens)
        kwargs = {
            "max_tokens": max_tokens,
            "temperature": GENERATION_TEMPERATURE,
            "repeat_penalty": self._repeat_penalty,
        }
        # Dialogue must be able to repeat evidence verbatim, without rewarding novelty.
        from .schemas import DialogueTurnOutput, GoalProposalOutput, GroundingCheckOutput
        if model_cls is not None and issubclass(model_cls, (DialogueTurnOutput, GoalProposalOutput, GroundingCheckOutput)):
            kwargs.update(temperature=0.2, repeat_penalty=1.0)
        if grammar is not None:
            kwargs["grammar"] = grammar
        result = self.llm(
            prompt,
            **kwargs,
        )
        return result["choices"][0]["text"]

    def _warn_if_context_overflow(self, prompt: str, max_tokens: int) -> None:
        """Loggt eine Warnung, wenn Prompt + Antwortbudget n_ctx sprengen (#50/K5).

        llama.cpp schneidet in dem Fall still ab bzw. bricht ab — der Lauf
        scheitert dann mit einem scheinbaren Validierungsfehler, ohne dass die
        eigentliche Ursache (Kontextüberlauf, z.B. sehr viele CSV-Einträge in
        einem Abschnitt) sichtbar wird. Kein Abbruch: needs_review fängt das
        Ergebnis ohnehin ab, die Warnung macht die Ursache diagnostizierbar.
        """
        n_prompt = len(self.llm.tokenize(prompt.encode("utf-8")))
        n_ctx = self.llm.n_ctx()
        if n_prompt + max_tokens > n_ctx:
            logger.warning(
                "Kontextfenster wahrscheinlich zu klein: %d Prompt-Tokens + "
                "%d max_tokens > n_ctx=%d — Antwort kann abgeschnitten werden "
                "(--n-ctx erhöhen oder Eintragsmenge reduzieren).",
                n_prompt,
                max_tokens,
                n_ctx,
            )

    def _generate(
        self, prompt: str, model_cls: Type[ModelT], max_tokens: int
    ) -> GenerationResult[ModelT]:
        raw = self._complete(prompt, model_cls, max_tokens)
        try:
            value = _validate(raw, model_cls)
            return GenerationResult(value=value, needs_review=False, raw_output=raw)
        except ValueError as first_error:
            raw_retry = self._complete(prompt + RETRY_REMINDER, model_cls, max_tokens)
            try:
                value = _validate(raw_retry, model_cls)
                return GenerationResult(
                    value=value, needs_review=False, raw_output=raw_retry
                )
            except ValueError as second_error:
                return GenerationResult(
                    value=_empty_instance(model_cls),
                    needs_review=True,
                    raw_output=raw_retry,
                    error=f"1. Versuch: {first_error} / 2. Versuch: {second_error}",
                )


    def classify_entry(
        self,
        entry: DocumentationEntry,
        sections: list[SectionDefinition],
    ) -> str:
        """Pass 0: klassifiziert einen Eintrag per LLM → Abschnitts-ID oder 'unklar'."""
        prompt = build_classification_prompt(entry, sections)
        raw = self._complete(prompt, EntryClassification, max_tokens=30)
        try:
            sid = EntryClassification.model_validate_json(raw).section_id
            valid_ids = {s.id for s in sections}
            if sid in HAMBURG_TO_OP_DE_WISCH_GOALS and sid not in valid_ids:
                sid = HAMBURG_TO_OP_DE_WISCH_GOALS[sid]
            return sid
        except Exception:
            return "unklar"

    def classify_entries_batch(
        self,
        entries: list[DocumentationEntry],
        sections: list[SectionDefinition],
        batch_size: int = 15,
    ) -> dict[str, list[DocumentationEntry]]:
        """Pass 0: Semantische Klassifikation von Einträgen im Batch via LLM."""
        result: dict[str, list[DocumentationEntry]] = {s.id: [] for s in sections}
        result["unklar"] = []
        valid_ids = {s.id for s in sections}

        if not entries:
            return result

        for i in range(0, len(entries), batch_size):
            chunk = entries[i : i + batch_size]
            prompt = build_batch_classification_prompt(chunk, sections)
            max_tokens = max(256, len(chunk) * 40)
            mapping: dict[int, str] = {}
            try:
                raw = self._complete(prompt, BatchEntryClassification, max_tokens=max_tokens)
                parsed = BatchEntryClassification.model_validate_json(raw)
                mapping = {item.id: item.section_id for item in parsed.classifications}
            except Exception as exc:
                logger.warning("Batch classification failed: %s. Falling back to single classify_entry.", exc)

            for idx, entry in enumerate(chunk):
                sid = mapping.get(idx)
                if sid in HAMBURG_TO_OP_DE_WISCH_GOALS and sid not in valid_ids:
                    sid = HAMBURG_TO_OP_DE_WISCH_GOALS[sid]
                if not sid or (sid not in valid_ids and sid != "unklar"):
                    try:
                        sid = self.classify_entry(entry, sections)
                    except Exception:
                        sid = "unklar"

                if sid in valid_ids:
                    result[sid].append(entry)
                else:
                    result["unklar"].append(entry)

        return result

    def generate_section(
        self,
        section: SectionDefinition,
        entries: list[DocumentationEntry],
        leitziel: str = "",
        smarte_ziele: str = "",
        previous_goals_apply: bool = True,
        freitext: str = "",
        anrede: str = "",
        nachname: str = "",
        vorname: str = "",
        forbidden_openers: tuple[str, ...] = (),
    ) -> GenerationResult[SectionLLMOutput]:
        prompt = build_section_prompt(
            section=section,
            entries=entries,
            leitziel=leitziel,
            smarte_ziele=smarte_ziele,
            previous_goals_apply=previous_goals_apply,
            freitext=freitext,
            anrede=anrede,
            nachname=nachname,
            vorname=vorname,
            forbidden_openers=forbidden_openers,
        )
        max_tokens = max(SECTION_MAX_TOKENS, len(entries) * _TOKENS_PER_ENTRY)
        result = self._generate(prompt, SectionLLMOutput, max_tokens)

        # Mindestlängen-Check: Falls < 2 Sätze, 1× nachschärfen.
        if not result.needs_review and entries:
            n_sätze = _count_sentences(result.value.bericht_zielerreichung)
            if n_sätze < 2:
                coverage_reminder = (
                    "\n\nACHTUNG: bericht_zielerreichung ist zu kurz. "
                    "PFLICHT: Schreibe 2 bis 5 vollständige Sätze über die "
                    "wichtigsten Erkenntnisse und Entwicklungen des Bewilligungszeitraums. "
                    "Generiere das komplette JSON neu."
                )
                result = self._generate(prompt + coverage_reminder, SectionLLMOutput, max_tokens)

        if not result.needs_review and result.value:
            result.value.bericht_zielerreichung = sanitize_clinical_terminology(result.value.bericht_zielerreichung)
            result.value.vorschlag_ziele = sanitize_clinical_terminology(result.value.vorschlag_ziele)
            result.value.vorschlag_massnahmen = sanitize_clinical_terminology(result.value.vorschlag_massnahmen)

        return result

    def refine_section(
        self,
        section: SectionDefinition,
        entries: list[DocumentationEntry],
        leitziel: str = "",
        smarte_ziele: str = "",
        previous_goals_apply: bool = True,
        freitext: str = "",
        user_notes: list[str] = (),
        anrede: str = "",
        nachname: str = "",
        vorname: str = "",
        forbidden_openers: tuple[str, ...] = (),
        developments: str = "",
    ) -> GenerationResult[SectionLLMOutput]:
        prompt = build_refine_section_prompt(
            section=section,
            entries=entries,
            leitziel=leitziel,
            smarte_ziele=smarte_ziele,
            previous_goals_apply=previous_goals_apply,
            freitext=freitext,
            user_notes=user_notes,
            anrede=anrede,
            nachname=nachname,
            vorname=vorname,
            forbidden_openers=forbidden_openers,
            developments=developments,
        )
        max_tokens = max(SECTION_MAX_TOKENS, len(entries) * _TOKENS_PER_ENTRY)
        result = self._generate(prompt, SectionLLMOutput, max_tokens)

        return self._check_grounding(result, section=section.title,
            entries=[entry.model_dump() for entry in entries], notes=list(user_notes),
            current_context=freitext, historical_goals=smarte_ziele, developments=developments)

    def _check_grounding(self, result, **sources):
        from .prompts import build_grounding_check_prompt
        from .schemas import GroundingCheckOutput
        if result.needs_review or result.error:
            return result
        try:
            check = self._generate(build_grounding_check_prompt(sources, result.value),
                GroundingCheckOutput, CHAT_TURN_MAX_TOKENS)
        except Exception:
            result.needs_review = True
            result.error = "Die Quellenprüfung ist fehlgeschlagen."
            return result
        if check.error or check.needs_review or not check.value.supported or check.value.unsupported_claims:
            result.needs_review = True
            result.error = "Die Quellenprüfung konnte den Entwurf nicht bestätigen."
        return result

    def revise_section(
        self,
        section: SectionDefinition,
        current_draft: SectionLLMOutput,
        critique: str,
        entries: list[DocumentationEntry],
        leitziel: str = "",
        smarte_ziele: str = "",
        previous_goals_apply: bool = True,
        freitext: str = "",
        user_notes: list[str] = (),
        anrede: str = "",
        nachname: str = "",
        vorname: str = "",
        forbidden_openers: tuple[str, ...] = (),
        developments: str = "",
    ) -> GenerationResult[SectionLLMOutput]:
        """Überarbeitet einen vorhandenen Entwurf; Fehler erhalten den bisherigen Text."""
        if not critique.strip():
            return GenerationResult(
                value=current_draft.model_copy(deep=True),
                needs_review=True,
                raw_output="",
                error="Kein Änderungsauftrag für die Überarbeitung angegeben.",
            )
        prompt = build_revise_section_prompt(
            section=section,
            current_draft=current_draft,
            critique=critique,
            entries=entries,
            leitziel=leitziel,
            smarte_ziele=smarte_ziele,
            previous_goals_apply=previous_goals_apply,
            freitext=freitext,
            user_notes=user_notes,
            anrede=anrede,
            nachname=nachname,
            vorname=vorname,
            forbidden_openers=forbidden_openers,
            developments=developments,
        )
        max_tokens = max(SECTION_MAX_TOKENS, len(entries) * _TOKENS_PER_ENTRY)
        try:
            result = self._generate(prompt, SectionLLMOutput, max_tokens)
        except Exception as exc:
            logger.warning("Abschnittsüberarbeitung fehlgeschlagen: %s", exc)
            return GenerationResult(
                value=current_draft.model_copy(deep=True),
                needs_review=True,
                raw_output="",
                error=f"Abschnittsüberarbeitung fehlgeschlagen: {exc}",
            )
        if result.needs_review:
            result.value = current_draft.model_copy(deep=True)
            return result
        if result.value == current_draft:
            result.needs_review = True
            result.error = "Die Überarbeitung hat den vorhandenen Entwurf nicht verändert."
        result = self._check_grounding(result, section=section.title,
            entries=[entry.model_dump() for entry in entries], notes=list(user_notes),
            current_context=freitext, historical_goals=smarte_ziele,
            developments=developments, revision_request=critique,
            previous_draft=current_draft.model_dump())
        if result.needs_review:
            result.value = current_draft.model_copy(deep=True)
        return result

    def analyze_section_gaps(
        self,
        section: SectionDefinition,
        entries: list[DocumentationEntry],
        leitziel: str = "",
        smarte_ziele: str = "",
        previous_goals_apply: bool = True,
        freitext: str = "",
        user_notes: list[str] = (),
        anrede: str = "",
        nachname: str = "",
        vorname: str = "",
    ) -> GenerationResult[GapAnalysisOutput]:
        prompt = build_gap_analysis_prompt(
            section=section,
            entries=entries,
            leitziel=leitziel,
            smarte_ziele=smarte_ziele,
            previous_goals_apply=previous_goals_apply,
            freitext=freitext,
            user_notes=user_notes,
            anrede=anrede,
            nachname=nachname,
            vorname=vorname,
        )
        return self._generate(prompt, GapAnalysisOutput, CHAT_TURN_MAX_TOKENS)

    def generate_developments(
        self,
        entries: list[DocumentationEntry],
        previous_goals_summary: str = "",
        freitext: str = "",
        anrede: str = "",
        nachname: str = "",
        vorname: str = "",
    ) -> GenerationResult[DevelopmentsLLMOutput]:
        prompt = build_developments_prompt(
            entries=entries,
            previous_goals_summary=previous_goals_summary,
            freitext=freitext,
            anrede=anrede,
            nachname=nachname,
            vorname=vorname,
        )
        return self._generate(prompt, DevelopmentsLLMOutput, DEVELOPMENTS_MAX_TOKENS)

    def generate_field_goals(self, section, entries, leitziel="", smarte_ziele="", client=None, developments=""):
        from .prompts import build_goal_proposal_prompt
        from .schemas import GoalProposalOutput
        return self._generate(build_goal_proposal_prompt({
            "field": section.title, "entries": [e.model_dump() for e in entries],
            "previous_leitziel": leitziel, "previous_goals": smarte_ziele,
            "client": client.model_dump() if client else {}, "developments": developments,
        }), GoalProposalOutput, CHAT_TURN_MAX_TOKENS)

    def revise_field_goals(self, section, current_goals, critique, entries, client=None):
        from .prompts import build_goal_proposal_prompt
        from .schemas import GoalProposalOutput
        return self._generate(build_goal_proposal_prompt({
            "field": section.title, "current_goals": current_goals, "critique": critique,
            "entries": [e.model_dump() for e in entries],
            "client": client.model_dump() if client else {},
        }), GoalProposalOutput, CHAT_TURN_MAX_TOKENS)

    def converse(self, context: dict):
        from .prompts import build_dialogue_turn_prompt
        from .schemas import dialogue_output_contract
        return self._generate(build_dialogue_turn_prompt(context), dialogue_output_contract(context["message"]), CHAT_TURN_MAX_TOKENS)

    def generate_field_questions(
        self,
        section: SectionDefinition,
        entries: list[DocumentationEntry],
        confirmed_goals: str = "",
        client: Optional[ClientData] = None,
        user_notes: list[str] = (),
        is_living_draft: bool = False,
    ) -> str:
        notes = list(user_notes)
        if confirmed_goals:
            notes.insert(0, f"Bestätigte Ziele: {confirmed_goals}")
        prompt = build_field_questions_prompt(
            section=section,
            entries=entries,
            user_notes=notes,
            anrede=client.anrede if client else "",
            nachname=client.name if client else "",
            vorname=client.vorname if client else "",
            is_living_draft=is_living_draft,
        )
        questions_body = self._complete(prompt, None, CHAT_TURN_MAX_TOKENS).strip()
        cleaned = clean_dialogue_response(questions_body)
        if is_living_draft:
            prefer_crisis = any(
                any(kw in n.lower() for kw in ("krise", "suizid", "notfall", "stationär", "psychiatri", "schutz"))
                for n in notes
            )
            return extract_single_question(cleaned, prefer_crisis=prefer_crisis)
        return (
            f"Gut — dann kommen jetzt meine gezielten Rückfragen zu **Lebensfeld {section.id} ({section.title})**, bevor ich den Textentwurf formuliere:\n\n"
            f"{cleaned}\n\n"
            "Du kannst fachliche Fragen und Unsicherheiten normal formulieren; sie werden nicht automatisch als Berichtstatsachen übernommen."
        )

    def classify_intent(self, text: str, phase: DialoguePhase) -> Optional[DialogueIntent]:
        """Schlanker LLM-Intent-Klassifizierer mit nativer Negationsbeachtung (Issue #60 Follow-Up)."""
        from sob_generator.dialogue_router import DialogueIntent
        from sob_generator.llm.prompts import build_intent_classification_prompt

        prompt = build_intent_classification_prompt(text, phase.value)
        try:
            raw = self._complete(prompt, None, max_tokens=30).strip().lower()
            raw = re.sub(r"[^a-z_]", "", raw)
            for intent in DialogueIntent:
                if intent.value == raw:
                    return intent
        except Exception as e:
            logger.warning("LLM intent classification failed: %s", e)
        return None

    def process_screening_turn(
        self,
        message: str,
        sections: list[SectionDefinition],
        active_section_ids: list[str] | set[str],
        client: Optional[ClientData] = None,
    ) -> ScreeningTurnOutput:
        prompt = build_screening_turn_prompt(
            message=message,
            sections=sections,
            active_section_ids=active_section_ids,
            anrede=client.anrede if client else "",
            nachname=client.name if client else "",
            vorname=client.vorname if client else "",
        )
        res = self._generate(prompt, ScreeningTurnOutput, CHAT_TURN_MAX_TOKENS)
        output = res.value

        # Defensive Validierung der IDs gegen die aktuellen Vorlagen-Abschnitte
        valid_ids = {s.id for s in sections}
        output.to_activate = [sid for sid in output.to_activate if sid in valid_ids]
        output.to_deactivate = [sid for sid in output.to_deactivate if sid in valid_ids]
        if output.first_active_id and output.first_active_id not in valid_ids:
            output.first_active_id = None

        if not output.reply or not output.reply.strip():
            output.reply = (
                "Alles klar, ich habe deine Rückmeldung notiert. "
                "Passt die Aufteilung der Lebensfelder ansonsten so für dich?"
            )
        else:
            output.reply = clean_dialogue_response(output.reply)

        return output
