"""Opener-Diversität der `bericht_zielerreichung`-Felder (#36).

Reine Domänenlogik, kein I/O und kein LLM: erkennt, welche Abschnitte mit
demselben Satzanfang beginnen, damit der Orchestrator gezielt nur die
Kollidierer neu erzeugen kann (Post-Pass-Uniqueness-Guard).

Hintergrund (#36, Senior-ML-Diagnose): Die 9 Abschnitte entstehen in
unabhängigen Calls bei festem Seed (#35). Mit nur wenigen distinkten Few-Shot-
Eröffnungen kopiert das 8B-Modell die positions-saliente Demonstration quasi-
verbatim; nach Pigeonhole teilen sich dann zwangsläufig ≥3 Abschnitte denselben
Opener. Reine Prompt-Nudges können „≤2 identische 3-Wort-Anfänge" deshalb
strukturell NICHT garantieren — nur ein harter Guard über alle Abschnitte
hinweg kann das. Dieses Modul liefert die Abzähl-Hälfte des Guards; die
Regeneration der erkannten Kollidierer (mit Ban-Direktive im Prompt) liegt im
Orchestrator, der den vollen Abschnitts-Kontext hat.
"""
from __future__ import annotations

import re
from typing import List, Sequence

# Akzeptanzkriterium #36: höchstens 2 Abschnitte mit identischem 3-Wort-Anfang.
DEFAULT_OPENER_WORDS = 3
DEFAULT_MAX_SHARED = 2

# Wort = Folge aus Buchstaben (inkl. deutscher Umlaute/ß) und Ziffern. Alles
# andere (Satzzeichen, typografische Anführungszeichen, Bindestriche) trennt.
_WORD = re.compile(r"[0-9A-Za-zÀ-ÿ]+")


def opener_key(text: str, words: int = DEFAULT_OPENER_WORDS) -> str:
    """Normalisierter Satzanfang: die ersten `words` Wörter, kleingeschrieben.

    Normalisiert über Groß-/Kleinschreibung und Satzzeichen, damit „Herr
    Mustermann hat" und „Herr Mustermann hat," denselben Schlüssel ergeben.
    Leerer/zeichenloser Text → "" (zählt nie als Kollision mit echtem Text).
    """
    tokens = _WORD.findall(text.casefold())
    return " ".join(tokens[:words])


def surplus_indices(
    openers: Sequence[str], max_shared: int = DEFAULT_MAX_SHARED
) -> List[int]:
    """Indizes, die neu erzeugt werden müssen, damit kein Opener-Schlüssel
    häufiger als `max_shared` mal vorkommt.

    Pro Schlüssel bleiben die ersten `max_shared` Vorkommen (in Eingabe-
    reihenfolge, stabil) erhalten; jedes weitere wird als Überhang gemeldet.
    Leere Schlüssel (zeichenloser Bericht) lösen keinen Überhang aus — ein
    leeres Feld ist ein Review-Fall (ADR-3), kein Stil-Kollaps.
    """
    seen: dict[str, int] = {}
    surplus: List[int] = []
    for idx, key in enumerate(openers):
        if not key:
            continue
        count = seen.get(key, 0) + 1
        seen[key] = count
        if count > max_shared:
            surplus.append(idx)
    return surplus
