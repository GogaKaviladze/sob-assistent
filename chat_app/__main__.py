"""Entry-Point: `python -m chat_app` startet die interaktive Chat-App."""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser

from .start_check import main as run_start_check_cli


def find_free_port(host: str = "127.0.0.1", preferred: int = 8000) -> int:
    """Versucht bevorzugten Port oder ermittelt den nächsten freien."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, preferred))
            return preferred
        except OSError:
            pass

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def main() -> None:
    # 1. macOS-Sicherheitsprüfungen (FileVault, Cloud-Sync, Time Machine)
    # Strikter Gatekeeper: Beendet sofort, falls Checks nicht bestanden.
    # Weder File-Logger noch Server-Komponenten werden vorher geladen.
    if run_start_check_cli() != 0:
        sys.exit(1)

    # 2. File-Logging erst NACH bestandenem Start-Check aktivieren (DSGVO & Symlink-Schutz)
    from .logger import init_file_logger, logger
    init_file_logger()

    # 3. Server- und App-Komponenten laden
    import uvicorn
    from .app import create_app

    host = "127.0.0.1"
    port = find_free_port(host, preferred=8000)
    url = f"http://{host}:{port}"
    pid = os.getpid()

    logger.info("🚀 Starte Fachdialog & Reflexion Chat-App auf %s (PID: %d)", url, pid)

    print(f"\n=======================================================")
    print(f"🚀 Fachdialog & Reflexion Chat-App gestartet!")
    print(f"👉 URL im Browser: {url}")
    print(f"📄 Logs werden geschrieben nach: logs/chat_app.log")
    if port != 8000:
        print(f"⚠️ Hinweis: Port 8000 war belegt. Verwende stattdessen: {url}")
    print(f"=======================================================\n")

    def _open_browser():
        time.sleep(0.8)
        webbrowser.open(url)

    threading.Thread(target=_open_browser, daemon=True).start()

    app = create_app()
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
