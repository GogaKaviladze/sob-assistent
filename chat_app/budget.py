"""Token-Budget-Manager mit Preflight-Prüfung und sichtbaren Kürzungshinweisen (Issue #17)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

BASE_CTX: int = 16384

BUDGET_SHARES: Dict[str, float] = {
    "system": 900 / BASE_CTX,
    "fall_kopf": 500 / BASE_CTX,
    "vorbericht": 2000 / BASE_CTX,
    "verlauf_doku": 5500 / BASE_CTX,
    "history_and_question": 4900 / BASE_CTX,
    "answer": 2048 / BASE_CTX,
    "reserve": 536 / BASE_CTX,
}

_SENTENCE_SPLIT_REGEX = re.compile(r'(?<=[.!?])\s+(?=[A-ZÄÖÜ0-9"\'\„])')


def calculate_budget_table(n_ctx: int) -> Dict[str, int]:
    """Berechnet die Token-Budget-Tabelle skaliert für das gegebene n_ctx."""
    system = int(round(n_ctx * BUDGET_SHARES["system"]))
    fall_kopf = int(round(n_ctx * BUDGET_SHARES["fall_kopf"]))
    vorbericht = int(round(n_ctx * BUDGET_SHARES["vorbericht"]))
    verlauf_doku = int(round(n_ctx * BUDGET_SHARES["verlauf_doku"]))
    history_and_question = int(round(n_ctx * BUDGET_SHARES["history_and_question"]))
    answer = int(round(n_ctx * BUDGET_SHARES["answer"]))
    reserve = n_ctx - (system + fall_kopf + vorbericht + verlauf_doku + history_and_question + answer)

    return {
        "system": system,
        "fall_kopf": fall_kopf,
        "vorbericht": vorbericht,
        "verlauf_doku": verlauf_doku,
        "history_and_question": history_and_question,
        "answer": answer,
        "reserve": reserve,
    }


class PromptTooLongError(Exception):
    """Wird vor dem Modellaufruf geworfen, wenn die Eingabe das Kontextfenster sprengt."""

    def __init__(self, tokens: int, free: int):
        self.tokens = tokens
        self.free = free
        super().__init__(
            f"❌ Nachricht zu lang für das Kontextfenster ({tokens} Tokens, frei {free}). Bitte kürzen."
        )


@dataclass
class ContextStatus:
    """Status des Token-Budgets und der Kontext-Zusammensetzung für UI und Session."""

    bereich_id: Optional[str]
    vorbericht_complete: bool
    sentences_kept: int
    sentences_total: int
    entries_kept: int
    entries_total: int
    unassigned_kept: int
    dropped_messages: int
    tokens_used: int
    n_ctx: int
    oldest_included_date: Optional[str] = None
    warning_notices: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bereich_id": self.bereich_id,
            "vorbericht_complete": self.vorbericht_complete,
            "sentences_kept": self.sentences_kept,
            "sentences_total": self.sentences_total,
            "entries_kept": self.entries_kept,
            "entries_total": self.entries_total,
            "unassigned_kept": self.unassigned_kept,
            "dropped_messages": self.dropped_messages,
            "tokens_used": self.tokens_used,
            "n_ctx": self.n_ctx,
            "oldest_included_date": self.oldest_included_date,
            "warning_notices": list(self.warning_notices),
        }


def split_sentences(text: str) -> List[str]:
    """Teilt Fließtext deterministisch an Satzgrenzen auf."""
    clean = text.strip()
    if not clean:
        return []
    raw = _SENTENCE_SPLIT_REGEX.split(clean)
    return [s.strip() for s in raw if s.strip()]


class BudgetManager:
    """Verteilt das Kontextfenster nach fester Tabelle und setzt den Prompt zusammen."""

    @classmethod
    def fit(
        cls,
        count_tokens: Callable[[str], int],
        n_ctx: int,
        max_tokens: int,
        system_prompt: str,
        built_context: Optional[Any],
        area_history: List[Dict[str, str]],
        question: str,
        bereich_id: Optional[str] = None,
    ) -> Tuple[List[Dict[str, str]], ContextStatus]:
        """Baut den Prompt mit Token-Budget-Management zusammen."""
        table = calculate_budget_table(n_ctx)
        max_prompt_capacity = max(0, n_ctx - max_tokens)

        # -------------------------------------------------------------
        # 1. Preflight-Check für Pflicht-Komponenten
        # -------------------------------------------------------------
        system_tokens = count_tokens(system_prompt)
        question_tokens = count_tokens(question)

        fall_kopf_text = ""
        vorbericht_mandatory_text = ""

        if built_context is not None:
            fall_kopf_text = getattr(built_context, "fall_kopf", "") or ""
            vorbericht_mandatory_text = getattr(built_context, "vorbericht_mandatory", "") or ""

        fall_kopf_tokens = count_tokens(fall_kopf_text) if fall_kopf_text else 0
        vorbericht_mandatory_tokens = count_tokens(vorbericht_mandatory_text) if vorbericht_mandatory_text else 0

        min_required_tokens = system_tokens + fall_kopf_tokens + vorbericht_mandatory_tokens
        free_for_question = max(0, max_prompt_capacity - min_required_tokens)

        if min_required_tokens + question_tokens > max_prompt_capacity:
            raise PromptTooLongError(tokens=question_tokens, free=free_for_question)

        # -------------------------------------------------------------
        # 2. Ohne Bereichsauswahl (System + History + Frage)
        # -------------------------------------------------------------
        if built_context is None or not bereich_id:
            available_for_history = max(0, max_prompt_capacity - system_tokens - question_tokens)
            kept_history, dropped_count = cls._fit_history_pairs(
                area_history, available_for_history, count_tokens
            )
            messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
            messages.extend(kept_history)
            if question.strip():
                messages.append({"role": "user", "content": question})

            total_tokens = sum(count_tokens(m["content"]) for m in messages)
            warning_notices = []
            if dropped_count > 0:
                warning_notices.append(
                    f"{dropped_count} ältere Nachrichten nicht mehr im Modellkontext (bleiben im Export)"
                )

            status = ContextStatus(
                bereich_id=None,
                vorbericht_complete=True,
                sentences_kept=0,
                sentences_total=0,
                entries_kept=0,
                entries_total=0,
                unassigned_kept=0,
                dropped_messages=dropped_count,
                tokens_used=total_tokens,
                n_ctx=n_ctx,
                oldest_included_date=None,
                warning_notices=warning_notices,
            )
            return messages, status

        # -------------------------------------------------------------
        # 3. Vorbericht-Budgetierung
        # -------------------------------------------------------------
        vorbericht_budget = table["vorbericht"]
        raw_sentences = getattr(built_context, "vorbericht_sentences", []) or []
        sentences_total = len(raw_sentences)

        if sentences_total == 0:
            vorbericht_complete = True
            sentences_kept = 0
            if vorbericht_mandatory_text:
                vorbericht_text = vorbericht_mandatory_text
            else:
                vorbericht_text = getattr(built_context, "vorbericht_bereich", "") or "Keine Vorbericht-Ziele für diesen Lebensbereich erfasst."
            vorbericht_tokens = count_tokens(vorbericht_text)
        else:
            # Prüfen ob alle Sätze zusammen mit dem Pflichtteil hineinpassen
            full_freitext = " ".join(raw_sentences)
            candidate_full = f"{vorbericht_mandatory_text}\n{full_freitext}".strip() if vorbericht_mandatory_text else full_freitext
            candidate_tokens = count_tokens(candidate_full)

            if candidate_tokens <= vorbericht_budget:
                vorbericht_complete = True
                sentences_kept = sentences_total
                vorbericht_text = candidate_full
                vorbericht_tokens = candidate_tokens
            else:
                vorbericht_complete = False
                # Anfang und Ende behalten, inline "[… gekürzt …]"
                truncation_marker = "\n[… gekürzt …]\n"
                left = 1
                right = 1
                best_text = f"{raw_sentences[0]}{truncation_marker}{raw_sentences[-1]}"
                if vorbericht_mandatory_text:
                    best_text = f"{vorbericht_mandatory_text}\n{best_text}"

                # Sukzessive Sätze von vorne und hinten hinzufügen solange Budget reicht
                while left + right < sentences_total:
                    # Nächsten Schritt testen (abwechselnd vorne / hinten)
                    try_left = left + (1 if left <= right else 0)
                    try_right = right + (1 if left > right else 0)

                    part_start = " ".join(raw_sentences[:try_left])
                    part_end = " ".join(raw_sentences[sentences_total - try_right:])
                    candidate = f"{part_start}{truncation_marker}{part_end}"
                    if vorbericht_mandatory_text:
                        candidate = f"{vorbericht_mandatory_text}\n{candidate}"

                    if count_tokens(candidate) <= vorbericht_budget:
                        left = try_left
                        right = try_right
                        best_text = candidate
                    else:
                        break

                sentences_kept = left + right
                vorbericht_text = best_text
                vorbericht_tokens = count_tokens(vorbericht_text)

        # -------------------------------------------------------------
        # 4. Verlaufsdoku-Budgetierung
        # -------------------------------------------------------------
        doku_budget = table["verlauf_doku"]
        doku_items = list(getattr(built_context, "doku_entries", []) or [])
        entries_total = len(doku_items)

        doku_tokens_total = sum(count_tokens(item.formatted_text) for item in doku_items)

        oldest_included_date: Optional[str] = None
        if doku_tokens_total <= doku_budget:
            kept_doku = doku_items
            entries_kept = entries_total
            unassigned_kept = sum(1 for e in kept_doku if not getattr(e, "is_assigned", True))
            if kept_doku:
                oldest_included_date = getattr(kept_doku[0], "datum", None)
            verlauf_tokens = doku_tokens_total
        else:
            # Drop older entries, prefer assigned over unassigned when dropping
            remaining = list(doku_items)
            while remaining and sum(count_tokens(e.formatted_text) for e in remaining) > doku_budget:
                # 1. Wenn es noch unzugeordnete Einträge gibt, den ältesten unzugeordneten entfernen
                unassigned_indices = [i for i, e in enumerate(remaining) if not getattr(e, "is_assigned", True)]
                if unassigned_indices:
                    del remaining[unassigned_indices[0]]
                else:
                    # 2. Sonst den ältesten zugeordneten Eintrag entfernen
                    del remaining[0]

            kept_doku = remaining
            entries_kept = len(kept_doku)
            unassigned_kept = sum(1 for e in kept_doku if not getattr(e, "is_assigned", True))
            if kept_doku:
                oldest_included_date = getattr(kept_doku[0], "datum", None)
            verlauf_tokens = sum(count_tokens(e.formatted_text) for e in kept_doku)

        if not kept_doku:
            verlauf_text = "Keine Verlaufsdokumentation für diesen Bereich vorhanden."
        else:
            verlauf_text = "\n".join(e.formatted_text for e in kept_doku)

        # -------------------------------------------------------------
        # 5. Datenblock zusammensetzen & Restbudget für Verlauf
        # -------------------------------------------------------------
        prompt_user_data_msg = (
            f"<fall_kopf>\n{fall_kopf_text}\n</fall_kopf>\n\n"
            f'<vorbericht_bereich id="{bereich_id}">\n{vorbericht_text}\n</vorbericht_bereich>\n\n'
            f"<verlauf_eintraege>\n{verlauf_text}\n</verlauf_eintraege>"
        )
        data_block_tokens = count_tokens(prompt_user_data_msg)

        # Nicht genutztes Budget fließt in den Gesprächsverlauf
        # Prompt-Budget gesamt = n_ctx - answer - reserve
        total_prompt_budget = max(0, n_ctx - table["answer"] - table["reserve"])
        used_for_structure = system_tokens + data_block_tokens + question_tokens
        available_for_history = max(0, total_prompt_budget - used_for_structure)

        # -------------------------------------------------------------
        # 6. Gesprächsverlauf-Budgetierung (Sliding Window ganzer Paare)
        # -------------------------------------------------------------
        kept_history, dropped_count = cls._fit_history_pairs(
            area_history, available_for_history, count_tokens
        )

        # -------------------------------------------------------------
        # 7. Finalen Payload bauen
        # -------------------------------------------------------------
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt_user_data_msg},
        ]
        messages.extend(kept_history)
        if question.strip():
            messages.append({"role": "user", "content": question})

        total_tokens_used = sum(count_tokens(m["content"]) for m in messages)

        # -------------------------------------------------------------
        # 8. Sichtbare Warn-Hinweise (⚠️ notices)
        # -------------------------------------------------------------
        warning_notices = []
        if not vorbericht_complete and sentences_total > 0:
            warning_notices.append(
                f"Bericht Bereich {bereich_id} gekürzt: {sentences_kept} von {sentences_total} Sätzen im Kontext"
            )
        if entries_kept < entries_total:
            date_info = f" (ältere bis {oldest_included_date} nicht)" if oldest_included_date else ""
            warning_notices.append(
                f"Verlaufsdoku Bereich {bereich_id}: {entries_kept} von {entries_total} Einträgen im Kontext{date_info}"
            )
        if dropped_count > 0:
            warning_notices.append(
                f"{dropped_count} ältere Nachrichten nicht mehr im Modellkontext (bleiben im Export)"
            )

        status = ContextStatus(
            bereich_id=bereich_id,
            vorbericht_complete=vorbericht_complete,
            sentences_kept=sentences_kept,
            sentences_total=sentences_total,
            entries_kept=entries_kept,
            entries_total=entries_total,
            unassigned_kept=unassigned_kept,
            dropped_messages=dropped_count,
            tokens_used=total_tokens_used,
            n_ctx=n_ctx,
            oldest_included_date=oldest_included_date,
            warning_notices=warning_notices,
        )

        return messages, status

    @classmethod
    def _fit_history_pairs(
        cls,
        history: List[Dict[str, str]],
        max_budget: int,
        count_tokens: Callable[[str], int],
    ) -> Tuple[List[Dict[str, str]], int]:
        """Behält von hinten nach vorne ganze user/assistant-Paare, die in max_budget passen."""
        if not history:
            return [], 0

        # Wir gruppieren in Paare (user + assistant)
        pairs: List[List[Dict[str, str]]] = []
        i = 0
        while i < len(history):
            if i + 1 < len(history) and history[i].get("role") == "user" and history[i + 1].get("role") == "assistant":
                pairs.append([history[i], history[i + 1]])
                i += 2
            else:
                pairs.append([history[i]])
                i += 1

        kept_pairs: List[List[Dict[str, str]]] = []
        current_tokens = 0

        for p in reversed(pairs):
            p_tokens = sum(count_tokens(m.get("content", "")) for m in p)
            if current_tokens + p_tokens <= max_budget:
                kept_pairs.append(p)
                current_tokens += p_tokens
            else:
                break

        kept_pairs.reverse()
        kept_messages = [m for p in kept_pairs for m in p]
        dropped_count = len(history) - len(kept_messages)
        return kept_messages, dropped_count
