"""Tests für die Opener-Diversitätslogik (#36).

Reine Domänenfunktionen ohne LLM: 3-Wort-Schlüssel + Überhang-Erkennung, die
der Orchestrator-Guard nutzt, um „höchstens 2 Abschnitte mit identischem
3-Wort-Anfang" zu garantieren (Akzeptanzkriterium #36).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sob_generator.opener_diversity import (  # noqa: E402
    opener_key,
    surplus_indices,
)


def test_opener_key_nimmt_erste_drei_woerter_normalisiert():
    assert opener_key("Herr Mustermann hat sich entwickelt.") == "herr mustermann hat"


def test_opener_key_ignoriert_satzzeichen_und_gross_klein():
    # Gleicher Anfang trotz Komma und Großschreibung → gleicher Schlüssel.
    a = opener_key("Im Mittelpunkt des Berichtszeitraums stand …")
    b = opener_key("im mittelpunkt des, anderen satzes")
    assert a == b == "im mittelpunkt des"


def test_opener_key_leerer_text_ist_leerer_schluessel():
    assert opener_key("") == ""
    assert opener_key("   .,;  ") == ""


def test_opener_key_weniger_als_drei_woerter():
    assert opener_key("Nur zwei") == "nur zwei"


def test_surplus_meldet_drittes_und_weiteres_vorkommen():
    openers = ["a b c", "a b c", "a b c", "x y z"]
    # Die ersten zwei „a b c" bleiben, das dritte (Index 2) ist Überhang.
    assert surplus_indices(openers) == [2]


def test_surplus_ist_stabil_in_eingabereihenfolge():
    openers = ["k", "k", "k", "k"]  # 1-Wort-Schlüssel reichen für die Logik
    assert surplus_indices(openers) == [2, 3]


def test_surplus_leer_wenn_alle_verschieden():
    assert surplus_indices(["a", "b", "c", "d"]) == []


def test_surplus_leere_schluessel_loesen_keinen_ueberhang_aus():
    # Drei leere Berichte (Review-Fälle, ADR-3) sind kein Stil-Kollaps.
    assert surplus_indices(["", "", "", "a", "a"]) == []


def test_surplus_max_shared_konfigurierbar():
    openers = ["a", "a", "a"]
    assert surplus_indices(openers, max_shared=1) == [1, 2]


def test_pigeonhole_drei_klassen_ueber_neun_abschnitte():
    # Genau die #36-Ausgangslage: 3 distinkte Opener, je 3× → 3 Überhänger.
    openers = ["a", "b", "c"] * 3
    surplus = surplus_indices(openers)
    assert len(surplus) == 3
    # Nach Entfernen der Überhänger bleibt jeder Schlüssel ≤ 2×.
    rest = [o for i, o in enumerate(openers) if i not in surplus]
    assert all(rest.count(o) <= 2 for o in set(rest))
