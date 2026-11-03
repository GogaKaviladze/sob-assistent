"""Zentrales Logging und Diagnose-System für die Chat-App (Separation of Concerns).

Schreibt strukturierte Diagnose-Logs nach `logs/chat_app.log` und auf die Konsole,
damit Modell-Inferenz, Erkennung und Performance transparent nachvollziehbar sind.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, List

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_FILE = LOG_DIR / "chat_app.log"


def truncate_log_file(reason: str = "start") -> int:
    """Trunkiert die Log-Datei DSGVO-konform (Legal 3) auf eine einzige inhaltsfreie Statuszeile.

    Schreibt ausschließlich: Zeitpunkt/Anlass und gelöschte Zeilenzahl.
    Niemals Fall- oder Klientendaten.
    """
    line_count = 0
    if LOG_FILE.exists():
        try:
            line_count = len(LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines())
        except Exception:
            pass

    app_logger = logging.getLogger("chat_app")
    file_handlers = [h for h in app_logger.handlers if isinstance(h, logging.FileHandler)]
    for h in file_handlers:
        h.flush()
        h.close()

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        LOG_FILE.write_text(f"Log gelöscht: anlass={reason} zeilen={line_count}\n", encoding="utf-8")
    except Exception as err:
        sys.stderr.write(f"Konnte Log-Datei nicht leeren: {err}\n")

    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    for h in file_handlers:
        try:
            new_h = logging.FileHandler(LOG_FILE, encoding="utf-8")
            new_h.setLevel(h.level)
            new_h.setFormatter(formatter)
            app_logger.removeHandler(h)
            app_logger.addHandler(new_h)
        except Exception:
            pass

    if not any(isinstance(h, logging.FileHandler) for h in app_logger.handlers):
        try:
            new_h = logging.FileHandler(LOG_FILE, encoding="utf-8")
            new_h.setLevel(logging.INFO)
            new_h.setFormatter(formatter)
            app_logger.addHandler(new_h)
        except Exception as err:
            sys.stderr.write(f"Konnte Log-Datei nicht öffnen: {err}\n")

    return line_count


def init_file_logger() -> None:
    """Aktiviert das Dateilogging nach erfolgreichem Sicherheitscheck (DSGVO-konform).

    Stellt sicher, dass logs/chat_app.log erst nach bestandener Validierung
    angelegt oder überschrieben wird (Schutz gegen externe Symlink-Manipulationen).
    """
    # Serverstart: Logfile auf Ursprungszeile zurücksetzen (Legal 3) und FileHandler einrichten
    truncate_log_file("start")


def setup_logger() -> logging.Logger:
    """Richtet den Konsolen-Logger ein.

    Der File-Logger wird aus Sicherheitsgründen erst nach erfolgreichem
    Start-Check via `init_file_logger()` aktiviert.
    """
    app_logger = logging.getLogger("chat_app")
    if app_logger.handlers:
        return app_logger

    app_logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Console Handler (immer aktiv)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    app_logger.addHandler(console_handler)

    return app_logger


logger = setup_logger()


def log_model_discovery(search_paths: List[Path], found_models: List[str]) -> None:
    """Protokolliert die Modell-Suche."""
    paths_str = ", ".join(str(p) for p in search_paths)
    logger.info(
        "🔍 Modell-Suche durchgeführt. Geprüfte Pfade: [%s]. Gefundene Modelle (%d): %s",
        paths_str, len(found_models), found_models
    )


def log_model_loaded(name: str, path: str, n_ctx: int, duration_sec: float, is_gpu: bool = True) -> None:
    """Protokolliert das Laden eines Modells."""
    gpu_info = "Metal GPU aktiviert" if is_gpu else "CPU-Modus"
    logger.info(
        "🚀 Modell geladen: '%s' aus '%s' (Kontext: %d Tokens, %s, Ladedauer: %.2fs)",
        name, path, n_ctx, gpu_info, duration_sec
    )


def log_generation(model_name: str, prompt_tokens_est: int, generated_tokens: int, duration_sec: float) -> None:
    """Protokolliert Inferenz-Laufzeit und Token-Geschwindigkeit."""
    tok_per_sec = (generated_tokens / duration_sec) if duration_sec > 0 else 0.0
    logger.info(
        "⚡ Inferenz beendet [%s]: %d Tokens generiert in %.2fs (%.1f Tok/s) | Prompt ~%d Tokens",
        model_name, generated_tokens, duration_sec, tok_per_sec, prompt_tokens_est
    )


def log_csv_loaded(
    filename: str = "",
    total_rows: int = 0,
    headers: List[str] | None = None,
    columns_count: int | None = None,
) -> None:
    """Protokolliert CSV-Upload strikt DSGVO-konform ohne Dateinamen oder Klientenbezug."""
    cols = columns_count if columns_count is not None else (len(headers) if headers else 0)
    logger.info(
        "📄 CSV-Falldaten geladen: %d Zeilen, %d Spalten (anonymisiert)",
        total_rows, cols
    )


def log_plan_loaded(
    filename: str = "",
    goals_count: int = 0,
    client_name: str = "",
    **kwargs: Any,
) -> None:
    """Protokolliert Vorbericht/Plan-Upload strikt DSGVO-konform ohne Dateinamen oder Klientenbezug."""
    logger.info(
        "📋 Vorbericht/Gesamtplan geladen: %d Ziele aktiv (anonymisiert)",
        goals_count
    )



def get_recent_logs(max_lines: int = 100) -> str:
    """Liest die letzten N Zeilen aus chat_app.log."""
    if not LOG_FILE.exists():
        return "Keine Log-Datei vorhanden."
    try:
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception as err:
        return f"Fehler beim Lesen der Logs: {err}"
