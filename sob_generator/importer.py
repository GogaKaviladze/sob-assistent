"""Importer (ADR-4): Vivendi-PD-CSV → Klient + Dokumentationseinträge.

Struktur der Vivendi-CSV (verifiziert an templates/Vivendi/Vivendi CSV Vorlage.csv):
- Encoding utf-8-sig (BOM), Zeilenenden CRLF, Separator `;`, 19 Spalten.
- Z0/Sp16: Leistungszeitraum „DD.MM.JJJJ HH:MM - DD.MM.JJJJ HH:MM"
- Z1/Sp0:  Name der Einrichtung (Leistungsanbieter)
- Z3/Sp0:  „Nachname, Vorname (*TT.MM.JJJJ)"
- Z5+:     paarweise — Metazeile (Sp4=Datum, Sp10=Kategorie) + Textzeile (Sp4)

Edge-Case-Hunting ist bewusst NICHT Teil dieses Durchstichs (das ist #10).
"""
from __future__ import annotations

import csv
import io
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Sequence

from sob_generator.llm.schemas import DocumentationEntry

from .models import ClientData, ReportMeta

# Zeilen-/Spaltenindizes (0-basiert, ADR-4).
ROW_ZEITRAUM = 0
COL_ZEITRAUM = 16
ROW_EINRICHTUNG = 1
COL_EINRICHTUNG = 0
ROW_KLIENT = 3
COL_KLIENT = 0
FIRST_ENTRY_ROW = 5
COL_META_DATUM = 4
COL_META_UHRZEIT = 6
COL_META_SCHICHT = 8
COL_META_KATEGORIE = 10
COL_TEXT = 4

_KLIENT_RE = re.compile(
    r"^\s*(?P<name>[^,]+),\s*(?P<vorname>.+?)\s*\(\*(?P<geburtsdatum>\d{2}\.\d{2}\.\d{4})\)"
)


@dataclass
class VivendiImport:
    """Ergebnis von `VivendiPDImporter.load()` (ADR-4)."""

    client: ClientData
    meta: ReportMeta
    entries: List[DocumentationEntry]


def _cell(row: Sequence[str], index: int) -> str:
    return row[index].strip() if index < len(row) else ""


def parse_vivendi_text(text: str, strict: bool = False) -> VivendiImport:
    """Parst den Textinhalt einer Vivendi-PD-CSV speicherbasiert (ADR-4/Zero-Disk)."""
    clean_text = text.lstrip("\ufeff")
    rows = list(csv.reader(io.StringIO(clean_text), delimiter=";"))
    importer = VivendiPDImporter.__new__(VivendiPDImporter)
    return VivendiImport(
        client=importer._parse_client(rows, strict=strict),
        meta=importer._parse_meta(rows),
        entries=list(importer._parse_entries(rows)),
    )


class BaseImporter(ABC):
    """Gemeinsame Basis der beiden Importer (ADR-4)."""

    @abstractmethod
    def load(self):  # pragma: no cover - Schnittstelle
        ...


class VivendiPDImporter(BaseImporter):
    """Liest einen Vivendi-PD-CSV-Export (ADR-4)."""

    def __init__(self, csv_path):
        self._path = Path(csv_path)

    def load(self) -> VivendiImport:
        with open(self._path, encoding="utf-8-sig", newline="") as f:
            return parse_vivendi_text(f.read(), strict=True)

    def _read_rows(self) -> List[List[str]]:
        with open(self._path, encoding="utf-8-sig", newline="") as f:
            return list(csv.reader(f, delimiter=";"))

    def _parse_client(self, rows: List[List[str]], strict: bool = True) -> ClientData:
        raw = _cell(rows[ROW_KLIENT], COL_KLIENT) if len(rows) > ROW_KLIENT else ""
        match = _KLIENT_RE.match(raw)
        if not match:
            if strict:
                raise ValueError(
                    # DSGVO: Rohzeile bewusst nicht in die Meldung (landet sonst in Logs/Konsole)
                    f"Klientenzeile (Z{ROW_KLIENT}) nicht parsebar. "
                    "Erwartet 'Nachname, Vorname (*TT.MM.JJJJ)'."
                )
            return ClientData(name="", vorname="", geburtsdatum="")
        return ClientData(
            name=match.group("name").strip(),
            vorname=match.group("vorname").strip(),
            geburtsdatum=match.group("geburtsdatum"),
        )

    def _parse_meta(self, rows: List[List[str]]) -> ReportMeta:
        zeitraum = _cell(rows[ROW_ZEITRAUM], COL_ZEITRAUM) if rows else ""
        von, bis = self._split_zeitraum(zeitraum)
        einrichtung = (
            _cell(rows[ROW_EINRICHTUNG], COL_EINRICHTUNG)
            if len(rows) > ROW_EINRICHTUNG
            else ""
        )
        return ReportMeta(
            leistungszeitraum_von=von,
            leistungszeitraum_bis=bis,
            leistungsanbieter=einrichtung,
        )

    @staticmethod
    def _split_zeitraum(zeitraum: str) -> tuple[str, str]:
        """„DD.MM.JJJJ HH:MM - DD.MM.JJJJ HH:MM" → ('DD.MM.JJJJ', 'DD.MM.JJJJ')."""
        if " - " not in zeitraum:
            return "", ""
        start, end = zeitraum.split(" - ", 1)
        return start.split()[0], end.split()[0]

    def _parse_entries(self, rows: List[List[str]]) -> Iterator[DocumentationEntry]:
        for i in range(FIRST_ENTRY_ROW, len(rows), 2):
            meta_row = rows[i]
            if i + 1 >= len(rows):
                if any(cell.strip() for cell in meta_row):
                    raise ValueError(
                        f"Vivendi-Export unvollständig: Metazeile (Z{i+1}) ohne zugehörige Textzeile am Dateiende."
                    )
                break
            text_row = rows[i + 1]
            datum = _cell(meta_row, COL_META_DATUM)
            uhrzeit = _cell(meta_row, COL_META_UHRZEIT)
            schicht = _cell(meta_row, COL_META_SCHICHT)
            kategorie = _cell(meta_row, COL_META_KATEGORIE)
            text = _cell(text_row, COL_TEXT)
            if not datum and not text:
                continue
            yield DocumentationEntry(
                datum=datum,
                uhrzeit=uhrzeit,
                kategorie=kategorie,
                text=text,
                schicht=schicht,
            )
