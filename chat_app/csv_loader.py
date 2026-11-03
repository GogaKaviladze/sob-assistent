"""Einfacher, robuster CSV-Loader für die Chat-App.

Lädt CSV-Dateien, erkennt Vivendi-PD-Formate oder generische CSVs,
rendert Einträge kompakt ohne Metadatenmüll und hält die neuesten Einträge
im Kontextbudget (KISS, SRP, Zero-Persistence).
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Any, List, Tuple

from sob_generator.llm.schemas import DocumentationEntry


def _parse_entry_datetime(datum_str: str, uhrzeit_str: str = "") -> tuple[int, int, int, int, int]:
    """Extrahiert ein vergleichbares DateTime-Tupel (Jahr, Monat, Tag, Stunde, Minute) für die chronologische Sortierung."""
    jahr, monat, tag = 0, 0, 0
    stunde, minute = 0, 0

    if datum_str:
        # 1. Deutsches Format: DD.MM.YYYY oder DD.MM.YY
        m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{2,4})\b", datum_str)
        if m:
            tag = int(m.group(1))
            monat = int(m.group(2))
            jahr_raw = int(m.group(3))
            jahr = jahr_raw if jahr_raw >= 100 else (2000 + jahr_raw if jahr_raw < 50 else 1900 + jahr_raw)
        else:
            # 2. ISO-8601 Format: YYYY-MM-DD
            m_iso = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", datum_str)
            if m_iso:
                jahr = int(m_iso.group(1))
                monat = int(m_iso.group(2))
                tag = int(m_iso.group(3))

        # Falls im Datum-String bereits eine Uhrzeit enthalten ist (z. B. "01.06.2025 14:30")
        if not uhrzeit_str:
            m_t = re.search(r"\b(\d{1,2}):(\d{2})(?::\d{2})?\b", datum_str)
            if m_t:
                stunde = int(m_t.group(1))
                minute = int(m_t.group(2))

    if uhrzeit_str:
        m_u = re.search(r"\b(\d{1,2}):(\d{2})(?::\d{2})?\b", uhrzeit_str)
        if m_u:
            stunde = int(m_u.group(1))
            minute = int(m_u.group(2))

    return (jahr, monat, tag, stunde, minute)


def _parse_entry_date(datum_str: str) -> tuple[int, int, int]:
    """Extrahiert ein vergleichbares Datumstripel (Jahr, Monat, Tag) für die chronologische Sortierung."""
    dt = _parse_entry_datetime(datum_str)
    return (dt[0], dt[1], dt[2])


def _format_vivendi_entry(entry: DocumentationEntry) -> str:
    """Rendert einen Vivendi-Eintrag im Kompaktformat: TT.MM.JJ Schicht[ · Kategorie]: Text."""
    d = entry.datum.strip()
    # Datumsformatierung: DD.MM.YYYY -> DD.MM.YY (bzw. DD.MM.YY beibehalten)
    d_clean = d.split()[0] if d else ""
    m = re.match(r"^(\d{2}\.\d{2}\.)(?:\d{2})?(\d{2})$", d_clean)
    datum_str = f"{m.group(1)}{m.group(2)}" if m else d_clean

    meta_parts = []
    if entry.schicht.strip():
        s_clean = re.sub(r"\s+", " ", entry.schicht).strip()
        if s_clean:
            meta_parts.append(s_clean)
    if entry.kategorie.strip():
        k_clean = re.sub(r"\s+", " ", entry.kategorie).strip()
        if k_clean:
            meta_parts.append(k_clean)

    meta_str = " · ".join(meta_parts)
    prefix = f"{datum_str} {meta_str}".strip() if meta_str else datum_str

    # Zeilenumbrüche und mehrfache Whitespaces zu Leerzeichen normalisieren (Einzeiligkeits-Invariante)
    text_clean = re.sub(r"[\r\n]+", " ", entry.text).strip()
    text_clean = re.sub(r"\s+", " ", text_clean)

    if prefix and text_clean:
        return f"{prefix}: {text_clean}"
    if prefix:
        return prefix
    return text_clean


def _format_generic_row(idx: int, row: List[str], headers: List[str]) -> str:
    """Rendert eine generische CSV-Zeile ohne Dateinamen, Spaltenkopf oder Spalte_N."""
    if headers and len(headers) == len(row):
        entry_parts = []
        for col, val in zip(headers, row):
            v = re.sub(r"[\r\n]+", " ", val).strip()
            v = re.sub(r"\s+", " ", v)
            if not v:
                continue
            c = col.strip()
            if c and not c.startswith("Spalte_"):
                entry_parts.append(f"{c}: {v}")
            else:
                entry_parts.append(v)
        return f"[{idx}] " + " | ".join(entry_parts) if entry_parts else ""
    non_empty = [re.sub(r"\s+", " ", val).strip() for val in row if val.strip()]
    return f"[{idx}] " + " | ".join(non_empty) if non_empty else ""


def _select_newest_within_budget(
    pairs: List[Tuple[Any, str]],
    total: int,
    max_rows: int,
    max_chars: int,
) -> Tuple[int, str]:
    """Wählt die neuesten k Einträge aus pairs, die inkl. Kürzungshinweis in max_chars und max_rows passen."""
    eff_max_rows = min(max_rows, len(pairs)) if max_rows else len(pairs)
    chosen_k = 0
    chosen_notice = ""

    sub_lens = [len(p[1]) for p in pairs]
    all_len = sum(sub_lens[-eff_max_rows:]) + (eff_max_rows - 1)
    if eff_max_rows == total and all_len <= max_chars:
        chosen_k = total
        chosen_notice = ""
    else:
        start_k = eff_max_rows if eff_max_rows < total else total - 1
        running_len = sum(sub_lens[-start_k:]) if start_k > 0 else 0
        for k in range(start_k, 0, -1):
            skipped = total - k
            notice = f"... [{skipped} ältere Einträge wegen Limit übersprungen; zeigt die neuesten {k} Einträge] ..."
            cand_len = len(notice) + 1 + running_len + (k - 1)
            if cand_len <= max_chars:
                chosen_k = k
                chosen_notice = notice
                break
            running_len -= sub_lens[-k]

        if chosen_k == 0 and eff_max_rows > 0:
            newest_len = sub_lens[-1]
            if newest_len <= max_chars:
                chosen_k = 1
                short_notice = f"... [{total - 1} ältere übersprungen; zeigt neuesten Eintrag] ..."
                if len(short_notice) + 1 + newest_len <= max_chars:
                    chosen_notice = short_notice
                else:
                    chosen_notice = ""

    return chosen_k, chosen_notice


@dataclass
class CsvData:
    """Repräsentiert die geparsten Daten einer CSV-Datei."""

    filename: str
    headers: List[str]
    rows: List[List[str]]
    total_rows: int
    raw_text: str = ""
    client_name: str = ""
    entries: List[DocumentationEntry] = field(default_factory=list)
    entries_total: int = 0
    entries_in_context: int = 0
    oldest_included_date: str = ""

    def to_context_text(
        self,
        max_rows: int = 500,
        max_chars: int = 45000,
        update_counters: bool = True,
    ) -> str:
        """Erzeugt eine saubere Textdarstellung für den System-Prompt des Chatbots.

        Behält bei Kürzung stets die NEUESTEN Einträge (chronologisch sortiert).
        Gibt keine Dateinamen, Spaltenköpfe, Handzeichen oder Geburtsdaten aus.
        Garantiert strikt, dass max_chars niemals überschritten wird.
        """
        if max_chars <= 0:
            if update_counters:
                self.entries_in_context = 0
            return ""

        # Pfad A: Strukturierte Vivendi-Einträge vorhanden
        if self.entries:
            total = len(self.entries)
            # Chronologisch stabil aufsteigend sortieren (älteste zuerst, neueste zuletzt)
            sorted_entries = sorted(
                self.entries,
                key=lambda e: _parse_entry_datetime(e.datum, getattr(e, "uhrzeit", "")),
            )
            formatted_lines = [_format_vivendi_entry(e) for e in sorted_entries]

            pairs = [(e, l) for e, l in zip(sorted_entries, formatted_lines) if l]
            if not pairs:
                if update_counters:
                    self.entries_total = total
                    self.entries_in_context = 0
                    self.oldest_included_date = ""
                return ""

            chosen_k, chosen_notice = _select_newest_within_budget(pairs, total, max_rows, max_chars)

            if chosen_k > 0:
                kept_pairs = pairs[-chosen_k:]
                lines = [p[1] for p in kept_pairs]
                oldest_dt = kept_pairs[0][0].datum.strip()
                m_dt = re.search(r"\b(\d{2}\.\d{2}\.\d{2,4})\b", oldest_dt)
                oldest_date = m_dt.group(1) if m_dt else oldest_dt

                if update_counters:
                    self.entries_total = total
                    self.entries_in_context = chosen_k
                    self.oldest_included_date = oldest_date

                if chosen_notice:
                    return f"{chosen_notice}\n" + "\n".join(lines)
                return "\n".join(lines)

            # Nicht mal 1 vollständiger Eintrag passt ins Zeichenbudget:
            if update_counters:
                self.entries_total = total
                self.entries_in_context = 0
                self.oldest_included_date = ""

            return ""

        # Pfad B: Generische CSV
        total = len(self.rows)
        if total == 0:
            if update_counters:
                self.entries_total = 0
                self.entries_in_context = 0
                self.oldest_included_date = ""
            return ""

        # Chronologisch sortieren, falls eine Datumsspalte vorhanden ist
        all_items = list(enumerate(self.rows, 1))
        date_col_idx = -1
        time_col_idx = -1
        if self.headers:
            # 1. Datumsspalte: Verlaufsdatumsspalten bevorzugen, Geburtsdaten strikt ausschließen
            for c_idx, h in enumerate(self.headers):
                h_low = h.lower()
                if any(b in h_low for b in ("geburt", "birth", "dob")):
                    continue
                if any(d in h_low for d in ("datum", "date", "zeitstempel", "timestamp")):
                    if any(v in h_low for v in ("verlauf", "dok", "eintrag")):
                        date_col_idx = c_idx
                        break
            if date_col_idx == -1:
                for c_idx, h in enumerate(self.headers):
                    h_low = h.lower()
                    if any(b in h_low for b in ("geburt", "birth", "dob")):
                        continue
                    if any(d in h_low for d in ("datum", "date", "zeitstempel", "timestamp")):
                        date_col_idx = c_idx
                        break

            # 2. Uhrzeitspalte (sofern separat)
            for c_idx, h in enumerate(self.headers):
                h_low = h.lower()
                if any(t in h_low for t in ("uhrzeit", "zeit", "time")) and c_idx != date_col_idx:
                    time_col_idx = c_idx
                    break

        if date_col_idx != -1:
            def _generic_sort_key(item):
                row = item[1]
                d_val = row[date_col_idx] if date_col_idx < len(row) else ""
                t_val = row[time_col_idx] if (time_col_idx != -1 and time_col_idx < len(row)) else ""
                return _parse_entry_datetime(d_val, t_val)

            sorted_items = sorted(all_items, key=_generic_sort_key)
        else:
            sorted_items = all_items

        formatted = [
            (row, _format_generic_row(idx, row, self.headers))
            for idx, row in sorted_items
        ]
        pairs = [(row, l) for row, l in formatted if l]
        if not pairs:
            if update_counters:
                self.entries_total = total
                self.entries_in_context = 0
                self.oldest_included_date = ""
            return ""

        chosen_k, chosen_notice = _select_newest_within_budget(pairs, total, max_rows, max_chars)

        def _extract_oldest_date(row: List[str]) -> str:
            if date_col_idx != -1 and date_col_idx < len(row):
                val = row[date_col_idx]
                m = re.search(r"\b(\d{2}\.\d{2}\.\d{2,4})\b", val)
                if m:
                    return m.group(1)
                m_iso = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", val)
                if m_iso:
                    return m_iso.group(1)
                return val.strip()
            for c_idx, cell in enumerate(row):
                if self.headers and c_idx < len(self.headers):
                    h_low = self.headers[c_idx].lower()
                    if any(b in h_low for b in ("geburt", "birth", "dob")):
                        continue
                m = re.search(r"\b(\d{2}\.\d{2}\.\d{2,4})\b", cell)
                if m:
                    return m.group(1)
            return ""

        if chosen_k > 0:
            kept_pairs = pairs[-chosen_k:]
            lines = [p[1] for p in kept_pairs]
            oldest_date = _extract_oldest_date(kept_pairs[0][0])

            if update_counters:
                self.entries_total = total
                self.entries_in_context = chosen_k
                self.oldest_included_date = oldest_date

            if chosen_notice:
                return f"{chosen_notice}\n" + "\n".join(lines)
            return "\n".join(lines)

        if update_counters:
            self.entries_total = total
            self.entries_in_context = 0
            self.oldest_included_date = ""

        return ""


def detect_delimiter(sample: str) -> str:
    """Ermittelt das wahrscheinlichste Trennzeichen (, ; oder Tab)."""
    candidates = [";", ",", "\t"]
    counts = {delim: sample.count(delim) for delim in candidates}
    best = max(counts, key=counts.get)
    return best if counts[best] > 0 else ","


def is_vivendi_csv(content: str) -> bool:
    """Erkennt einen Vivendi-PD-Export anhand des vom Importer vorausgesetzten Layouts.

    Erfordert:
    - Semicolon-Delimiter
    - Mindestens 5 Zeilen
    - Zeile 4 (Index 3, COL_KLIENT=0) enthält Klientennamen mit '(*TT.MM.JJJJ)'
    - Zeile 5 (Index 4) enthält die Vivendi-Kopfzeile: Spalten 0-3 sind leer und
      enthält 'Datum', 'Uhrzeit', 'Kategorie'
    """
    if not content or not content.strip():
        return False
    clean = content.lstrip("\ufeff")
    sample_lines = clean.splitlines()[:10]
    if len(sample_lines) < 5:
        return False

    try:
        rows = list(csv.reader(io.StringIO("\n".join(sample_lines)), delimiter=";"))
    except Exception:
        return False

    if len(rows) < 5:
        return False

    # 1. Zeile 4 (Index 3): Klientenzeile mit Geburtsdatum
    row_client = rows[3]
    if not row_client:
        return False
    first_cell = row_client[0].strip()
    if not re.search(r"^\s*[^,]+,\s*.+?\s*\(\*\d{2}\.\d{2}\.\d{4}\)", first_cell):
        return False

    # 2. Zeile 5 (Index 4): Spezifische Vivendi-Kopfzeile
    row_header = rows[4]
    if len(row_header) < 5:
        return False
    if any(row_header[i].strip() for i in range(min(4, len(row_header)))):
        return False

    norm_cells = [c.strip().lower() for c in row_header if c.strip()]
    required = {"datum", "uhrzeit", "kategorie"}
    return required.issubset(set(norm_cells))


def parse_csv_content(content: str, filename: str = "daten.csv", client_name: str = "") -> CsvData:
    """Parst einen CSV-Textstring robust (unterstützt Vivendi-PD und generische Formate)."""
    if not content or not content.strip():
        return CsvData(
            filename=filename,
            headers=[],
            rows=[],
            total_rows=0,
            raw_text="",
            client_name=client_name,
            entries=[],
            entries_total=0,
            entries_in_context=0,
            oldest_included_date="",
        )

    content_clean = content.lstrip("\ufeff")

    # Vivendi-Spezialpfad (RAM-only via Importer, ohne stillen Fallback)
    if is_vivendi_csv(content_clean):
        from sob_generator.importer import parse_vivendi_text

        try:
            vivendi_data = parse_vivendi_text(content_clean, strict=True)
            entries = vivendi_data.entries
            if not entries:
                raise ValueError("Vivendi-Export enthält keine gültigen Verlaufsdokumentationseinträge.")

            det_client = f"{vivendi_data.client.vorname} {vivendi_data.client.name}".strip()
            final_client = det_client if det_client else client_name

            csv_data = CsvData(
                filename=filename,
                headers=[],
                rows=[],
                total_rows=len(entries),
                raw_text=content,
                client_name=final_client,
                entries=entries,
                entries_total=len(entries),
                entries_in_context=0,
                oldest_included_date="",
            )
            csv_data.to_context_text()
            return csv_data
        except Exception as exc:
            if "unvollständig" in str(exc).lower():
                raise
            raise ValueError(
                f"Vivendi-Export fehlerhaft und kann nicht verarbeitet werden: {type(exc).__name__}"
            ) from exc

    # Generischer CSV-Pfad
    sample_lines = [line for line in content_clean.splitlines() if line.strip()][:10]
    sample = "\n".join(sample_lines)
    delimiter = detect_delimiter(sample)

    reader = csv.reader(io.StringIO(content_clean), delimiter=delimiter)
    raw_rows: List[List[str]] = []
    for row in reader:
        cleaned = [cell.strip() for cell in row]
        if any(cleaned):
            raw_rows.append(cleaned)

    if not raw_rows:
        return CsvData(
            filename=filename,
            headers=[],
            rows=[],
            total_rows=0,
            raw_text=content,
            client_name=client_name,
            entries=[],
            entries_total=0,
            entries_in_context=0,
            oldest_included_date="",
        )

    # Header-Erkennung unter den ersten 10 Zeilen
    header_idx = -1
    max_text_cols = 0
    common_header_words = {"datum", "date", "uhrzeit", "zeit", "kategorie", "details", "notiz", "inhalt", "eintrag", "text"}

    for idx, r in enumerate(raw_rows[:10]):
        text_cols = [c for c in r if any(ch.isalpha() for ch in c) and len(c) < 50]
        col_count = len(text_cols)
        has_common = any(c.lower() in common_header_words for c in text_cols)
        score = col_count + (10 if has_common else 0)

        if score > max_text_cols and col_count >= 2:
            max_text_cols = score
            header_idx = idx

    headers: List[str] = []
    if header_idx != -1:
        candidate = raw_rows[header_idx]
        headers = [c if c else f"Spalte_{i+1}" for i, c in enumerate(candidate)]
        rows = raw_rows[header_idx + 1:]
    else:
        rows = raw_rows

    # Fail-closed: Prüfe auf erkennbare Verlaufsdokumentations-Spalten
    doku_text_cols = {
        "notiz", "text", "bericht", "verlauf", "dokumentation", "eintrag",
        "inhalt", "details", "beschreibung", "beobachtung", "bemerkung", "event",
    }
    non_doku_cols = {"mitarbeiter", "personal", "dienst", "pause", "raum", "urlaub", "soll", "ist"}
    norm_headers = {h.strip().lower() for h in headers if h.strip() and not h.startswith("Spalte_")}
    has_doku_col = any(any(d in h for d in doku_text_cols) for h in norm_headers)
    has_non_doku_col = any(any(nd in h for nd in non_doku_cols) for h in norm_headers)

    if not norm_headers or not has_doku_col or (has_non_doku_col and "schicht" in norm_headers):
        raise ValueError(
            "❌ Keine Verlaufsdokumentation erkannt: Die CSV-Datei enthält keine erkennbaren Dokumentationsspalten."
        )

    csv_data = CsvData(
        filename=filename,
        headers=headers,
        rows=rows,
        total_rows=len(rows),
        raw_text=content,
        client_name=client_name,
        entries=[],
        entries_total=len(rows),
        entries_in_context=0,
        oldest_included_date="",
    )
    csv_data.to_context_text()
    return csv_data
