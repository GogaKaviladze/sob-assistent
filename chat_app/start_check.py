"""macOS-Start-Check für FileVault, Cloud-Sync und Time Machine.

Sicherheitsprüfung vor Serverstart: Stellt sicher, dass das System verschlüsselt ist,
der App-Ordner nicht in Cloud-Speichern synchronisiert wird und Time-Machine-Backups
entweder verschlüsselt sind oder den App-Ordner ausschließen.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class CheckResult:
    """Ergebnis einer einzelnen Start-Check-Prüfung."""
    name: str
    ok: bool
    reason_de: str
    fix_de: str
    skipped: bool = False


def check_filevault(run: Callable = subprocess.run) -> CheckResult:
    """Prüft, ob macOS FileVault aktiviert ist."""
    try:
        res = run(["fdesetup", "status"], capture_output=True, text=True, timeout=5, check=False)
        if res.returncode != 0:
            return CheckResult(
                name="FileVault",
                ok=False,
                reason_de=f"fdesetup meldete Fehlercode {res.returncode}.",
                fix_de="FileVault ist aus. Bitte in Systemeinstellungen → Datenschutz & Sicherheit → FileVault aktivieren.",
            )
        stdout = res.stdout or ""
        if re.search(r"^FileVault is On\b", stdout, re.MULTILINE):
            return CheckResult(
                name="FileVault",
                ok=True,
                reason_de="FileVault ist aktiviert.",
                fix_de="",
            )
        return CheckResult(
            name="FileVault",
            ok=False,
            reason_de="FileVault ist deaktiviert.",
            fix_de="FileVault ist aus. Bitte in Systemeinstellungen → Datenschutz & Sicherheit → FileVault aktivieren.",
        )
    except FileNotFoundError:
        return CheckResult(
            name="FileVault",
            ok=False,
            reason_de="Befehl 'fdesetup' nicht gefunden.",
            fix_de="Bitte auf macOS ausführen oder Systemintegrität prüfen.",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name="FileVault",
            ok=False,
            reason_de="Timeout (5s) bei 'fdesetup status'.",
            fix_de="Bitte Systemintegrität prüfen.",
        )
    except Exception as exc:
        return CheckResult(
            name="FileVault",
            ok=False,
            reason_de=f"Fehler bei FileVault-Prüfung ({type(exc).__name__}).",
            fix_de="Bitte Systemintegrität prüfen.",
        )


class CloudScanError(Exception):
    """Signalisiert einen Zugriffs- oder I/O-Fehler beim Scannen von Cloud-Sync-Verzeichnissen."""
    pass


def _sanitize_path_str(text: str, home: Optional[Path] = None) -> str:
    """Entfernt absolute Benutzerpfade aus Fehlerbeschreibungen (DSGVO/PII-Schutz)."""
    if not text:
        return ""
    if home is not None:
        try:
            text = text.replace(str(home.resolve()), "~")
        except Exception:
            pass
        text = text.replace(str(home), "~")
    text = re.sub(r"/Users/[^/\n\r'\"]+?(?=/|$)", "~", text)
    text = re.sub(r"/home/[^/\s'\"]+", "~", text)
    text = re.sub(r"/private/var/[^\s'\"]+", "<temp>", text)
    return text


def _is_subpath(target: Path, base: Path) -> bool:
    """Gibt True zurück, wenn target gleich oder ein Unterordner von base ist."""
    try:
        target.relative_to(base)
        return True
    except ValueError:
        return False


def _match_known_provider(folder_name: str) -> Optional[str]:
    """Erkennt bekannte Cloud-Provider auch bei Abweichungen in Leerzeichen/Schreibweise."""
    known = ["Dropbox", "OneDrive", "Google Drive", "GoogleDrive", "Box"]
    f_clean = folder_name.replace(" ", "").replace("-", "").lower()
    for provider in known:
        p_clean = provider.replace(" ", "").replace("-", "").lower()
        if p_clean == "box":
            # 'box' nur als ganzes Wort oder Präfix (z. B. 'Box-Personal'), nicht in 'sandbox' oder 'toolbox'
            if re.search(r"(?:^|[^a-z0-9])box(?:[^a-z0-9]|$)", folder_name, re.IGNORECASE):
                return "Box"
        elif p_clean in f_clean:
            return "Google Drive" if "google" in p_clean else provider
    return None


def _clean_provider_name(name: str) -> str:
    """Extrahiert einen sicheren, anonymisierten Providernamen (ohne Account-/Mail-Anhänge)."""
    matched = _match_known_provider(name)
    if matched:
        return matched
    base = name.split("-")[0].split("@")[0].strip()
    return base if base else "CloudStorage"


def _check_single_path_cloud_sync(target_path: Path, resolved_home: Path) -> Optional[str]:
    """Prüft einen einzelnen absoluten Pfad auf bekannte Cloud-Synchronisationsordner.

    Wirft CloudScanError bei Berechtigungs- oder Dateisystemfehlern (Fail-Closed).
    """
    icloud_drive = (resolved_home / "Library" / "Mobile Documents").resolve()
    cloud_storage = (resolved_home / "Library" / "CloudStorage").resolve()
    dropbox = (resolved_home / "Dropbox").resolve()
    desktop = (resolved_home / "Desktop").resolve()
    documents = (resolved_home / "Documents").resolve()

    icloud_desktop_marker = (
        resolved_home / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "Desktop"
    ).resolve()
    icloud_documents_marker = (
        resolved_home / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "Documents"
    ).resolve()

    if _is_subpath(target_path, icloud_drive):
        return "iCloud Drive"

    if _is_subpath(target_path, cloud_storage):
        try:
            rel = target_path.relative_to(cloud_storage)
            folder_name = rel.parts[0] if rel.parts else "CloudStorage"
            return _clean_provider_name(folder_name)
        except ValueError:
            return "CloudStorage"

    # Zusätzlich: Aufgelöste Anbieterwurzeln innerhalb von CloudStorage vergleichen (Symlink-Schutz)
    raw_cloud_storage = resolved_home / "Library" / "CloudStorage"
    if raw_cloud_storage.is_dir():
        try:
            cs_entries = list(raw_cloud_storage.iterdir())
        except (PermissionError, OSError) as err:
            raise CloudScanError(f"CloudStorage-Verzeichnis nicht lesbar: {err.__class__.__name__}") from err

        for entry in cs_entries:
            try:
                resolved_entry = entry.resolve()
                if _is_subpath(target_path, resolved_entry):
                    return _clean_provider_name(entry.name)
            except (PermissionError, OSError):
                # Fremde, unauflösbare Symlinks oder ungemountete Netzlaufwerke in CloudStorage
                # können target_path (der existiert) nicht enthalten und dürfen keinen Fehlalarm auslösen
                continue

    if _is_subpath(target_path, dropbox):
        return "Dropbox"
    if _is_subpath(target_path, desktop) and icloud_desktop_marker.exists():
        return "iCloud Drive (Schreibtisch)"
    if _is_subpath(target_path, documents) and icloud_documents_marker.exists():
        return "iCloud Drive (Dokumente)"

    return None


def check_cloud_sync(app_dir: Path, home: Path) -> CheckResult:
    """Prüft, ob der App-Ordner oder das Log-Ziel in einem Cloud-Sync-Verzeichnis liegt."""
    try:
        resolved_app = app_dir.resolve()
        resolved_home = home.resolve()

        # Ausgelagerte Log-Ziele via Symlink prüfen
        logs_dir = app_dir / "logs"
        log_file = logs_dir / "chat_app.log"

        if logs_dir.is_symlink() and not _is_subpath(logs_dir.resolve(), resolved_app):
            return CheckResult(
                name="Cloud-Sync",
                ok=False,
                reason_de="Ausgelagertes Log-Verzeichnis per Symlink außerhalb des App-Ordners erkannt.",
                fix_de="Symlink unter logs/ entfernen und Logs im App-Verzeichnis belassen.",
            )
        if log_file.is_symlink() and not _is_subpath(log_file.resolve(), resolved_app):
            return CheckResult(
                name="Cloud-Sync",
                ok=False,
                reason_de="Ausgelagerte Log-Datei per Symlink außerhalb des App-Ordners erkannt.",
                fix_de="Symlink unter logs/chat_app.log entfernen.",
            )

        # Alle relevanten Schreib-/App-Pfade prüfen
        targets_to_check: List[Tuple[str, Path]] = [("App-Ordner", resolved_app)]
        if logs_dir.exists() or logs_dir.is_symlink():
            r_logs = logs_dir.resolve()
            if r_logs not in [t[1] for t in targets_to_check]:
                targets_to_check.append(("Log-Verzeichnis", r_logs))
        if log_file.exists() or log_file.is_symlink():
            r_file = log_file.resolve()
            if r_file not in [t[1] for t in targets_to_check]:
                targets_to_check.append(("Log-Datei", r_file))

        for label, target_path in targets_to_check:
            provider = _check_single_path_cloud_sync(target_path, resolved_home)
            if provider:
                return CheckResult(
                    name="Cloud-Sync",
                    ok=False,
                    reason_de=f"{label} wird mit einer Cloud synchronisiert ({provider}).",
                    fix_de="Bitte Ordner nach z. B. ~/SOB verschieben.",
                )

        return CheckResult(
            name="Cloud-Sync",
            ok=True,
            reason_de="App-Ordner und Log-Ziele liegen nicht in einem Cloud-Sync-Verzeichnis.",
            fix_de="",
        )
    except CloudScanError as err:
        return CheckResult(
            name="Cloud-Sync",
            ok=False,
            reason_de=f"Cloud-Synchronisationsprüfung fehlgeschlagen ({_sanitize_path_str(str(err), home)}).",
            fix_de="Bitte Zugriffsrechte für CloudStorage prüfen oder Ordner nach z. B. ~/SOB verschieben.",
        )
    except Exception as exc:
        return CheckResult(
            name="Cloud-Sync",
            ok=False,
            reason_de=f"Fehler bei Cloud-Sync-Prüfung ({type(exc).__name__}: {_sanitize_path_str(str(exc), home)}).",
            fix_de="Bitte Pfadrechte und Dateisystemintegrität prüfen.",
        )


def _is_no_destinations_diagnostic(stdout: str, stderr: str) -> bool:
    """Prüft, ob die vollständige Ausgabe der tmutil-Diagnose 'No destinations configured' entspricht."""
    pattern = r"^(?:tmutil:\s*)?no destinations configured\.?$"
    s_out = stdout.strip()
    s_err = stderr.strip()
    if s_out and re.fullmatch(pattern, s_out, re.IGNORECASE):
        return True
    if s_err and re.fullmatch(pattern, s_err, re.IGNORECASE):
        return True
    return False


def check_time_machine(app_dir: Path, run: Callable = subprocess.run) -> CheckResult:
    """Prüft, ob Time Machine ausgeschlossen oder verschlüsselt ist."""
    resolved_app = app_dir.resolve()

    # Ausgelagerte Log-Ziele via Symlink prüfen
    logs_dir = app_dir / "logs"
    log_file = logs_dir / "chat_app.log"
    if logs_dir.is_symlink() and not _is_subpath(logs_dir.resolve(), resolved_app):
        return CheckResult(
            name="Time Machine",
            ok=False,
            reason_de="Ausgelagertes Log-Verzeichnis per Symlink außerhalb des App-Ordners erkannt.",
            fix_de="Symlink unter logs/ entfernen und Logs im App-Verzeichnis belassen.",
        )
    if log_file.is_symlink() and not _is_subpath(log_file.resolve(), resolved_app):
        return CheckResult(
            name="Time Machine",
            ok=False,
            reason_de="Ausgelagerte Log-Datei per Symlink außerhalb des App-Ordners erkannt.",
            fix_de="Symlink unter logs/chat_app.log entfernen.",
        )

    # Zu verifizierende Pfade (App-Ordner + ggf. Log-Ziele)
    paths_to_verify: List[Path] = [resolved_app]
    if logs_dir.exists() or logs_dir.is_symlink():
        r_logs = logs_dir.resolve()
        if r_logs not in paths_to_verify:
            paths_to_verify.append(r_logs)
    if log_file.exists() or log_file.is_symlink():
        r_file = log_file.resolve()
        if r_file not in paths_to_verify:
            paths_to_verify.append(r_file)

    try:
        res = run(["tmutil", "destinationinfo"], capture_output=True, text=True, timeout=5, check=False)
        stdout = res.stdout or ""
        stderr = res.stderr or ""

        # Wenn keine Ziele konfiguriert sind (vollständige Diagnosemeldung abgleichen)
        if _is_no_destinations_diagnostic(stdout, stderr):
            return CheckResult(
                name="Time Machine",
                ok=True,
                reason_de="Kein Time-Machine-Ziel konfiguriert.",
                fix_de="",
            )

        if res.returncode != 0:
            return CheckResult(
                name="Time Machine",
                ok=False,
                reason_de=f"tmutil destinationinfo fehlgeschlagen (Exit-Code {res.returncode}).",
                fix_de="Ordner ausschließen: tmutil addexclusion . oder Backup konfigurieren.",
            )

        if not stdout.strip():
            return CheckResult(
                name="Time Machine",
                ok=False,
                reason_de="Time-Machine-Status nicht feststellbar (keine Zielausgabe erhalten).",
                fix_de="Ordner ausschließen: tmutil addexclusion . oder Backup konfigurieren.",
            )

        # Prüfe, ob alle zu sichernden Pfade ausgeschlossen sind (nur Statuspräfix auswerten)
        all_paths_excluded = True
        for path_item in paths_to_verify:
            res_ex = run(["tmutil", "isexcluded", str(path_item)], capture_output=True, text=True, timeout=5, check=False)
            if res_ex.returncode != 0:
                return CheckResult(
                    name="Time Machine",
                    ok=False,
                    reason_de=f"tmutil isexcluded fehlgeschlagen für '{path_item.name}' (Exit-Code {res_ex.returncode}).",
                    fix_de="Ordner ausschließen: tmutil addexclusion . oder Backup verschlüsseln.",
                )

            stdout_ex = (res_ex.stdout or "").strip()
            # Statuspräfix am Zeilenanfang prüfen: verhindert falsch-positive Treffer bei Ordnernamen mit '[Excluded]'
            if not re.match(r"^\[excluded\](?:\s|$)", stdout_ex, re.IGNORECASE):
                all_paths_excluded = False
                break

        if all_paths_excluded:
            return CheckResult(
                name="Time Machine",
                ok=True,
                reason_de="App-Ordner ist von Time-Machine-Backups ausgeschlossen ([Excluded]).",
                fix_de="",
            )

        # Zielweise Auswertung: alle Ziele müssen verifizierbar und verschlüsselt sein
        destinations: List[dict] = []
        current_dest: dict = {}
        for line in stdout.splitlines():
            line_clean = line.strip()
            if line_clean.startswith("="):
                if current_dest:
                    destinations.append(current_dest)
                    current_dest = {}
                continue
            if ":" in line_clean:
                k, v = line_clean.split(":", 1)
                k_norm = k.strip().lower()
                if k_norm == "name" and "name" in current_dest:
                    destinations.append(current_dest)
                    current_dest = {}
                current_dest[k_norm] = v.strip()
        if current_dest:
            destinations.append(current_dest)

        if not destinations:
            mount_points = [
                line.split(":", 1)[1].strip()
                for line in stdout.splitlines()
                if line.strip().lower().startswith("mount point") and ":" in line
            ]
            for mp in mount_points:
                destinations.append({"mount point": mp, "name": mp})

        if not destinations:
            return CheckResult(
                name="Time Machine",
                ok=False,
                reason_de="Time-Machine-Verschlüsselung nicht feststellbar (keine Ziele erkannt).",
                fix_de="Ordner ausschließen: tmutil addexclusion . oder Backup verschlüsseln.",
            )

        for dest in destinations:
            dest_name = dest.get("name", "Unbekannt")
            mp = dest.get("mount point")
            if not mp or mp.strip().lower() in ("(null)", "none", ""):
                return CheckResult(
                    name="Time Machine",
                    ok=False,
                    reason_de=f"Time-Machine-Ziel '{dest_name}' kann nicht verifiziert werden (kein lokaler Mount Point).",
                    fix_de="Ordner ausschließen: tmutil addexclusion . oder Backup verschlüsseln.",
                )

            res_disk = run(["diskutil", "info", mp], capture_output=True, text=True, timeout=5, check=False)
            if res_disk.returncode != 0:
                return CheckResult(
                    name="Time Machine",
                    ok=False,
                    reason_de=f"diskutil info für Time-Machine-Ziel '{dest_name}' fehlgeschlagen (Exit-Code {res_disk.returncode}).",
                    fix_de="Ordner ausschließen: tmutil addexclusion . oder Backup verschlüsseln.",
                )

            d_out = res_disk.stdout or ""
            # Feldnamen am Zeilenanfang verankern und 'Yes' als ganzes Wort prüfen
            has_fv = bool(re.search(r"^\s*FileVault:\s+Yes\b", d_out, re.IGNORECASE | re.MULTILINE))
            has_enc = bool(re.search(r"^\s*Encrypted:\s+Yes\b", d_out, re.IGNORECASE | re.MULTILINE))
            if not (has_fv or has_enc):
                return CheckResult(
                    name="Time Machine",
                    ok=False,
                    reason_de=f"Time-Machine-Ziel '{dest_name}' ist unverschlüsselt.",
                    fix_de="Ordner ausschließen: tmutil addexclusion . oder Backup verschlüsseln.",
                )

        return CheckResult(
            name="Time Machine",
            ok=True,
            reason_de="Alle Time-Machine-Ziele sind verschlüsselt.",
            fix_de="",
        )

    except FileNotFoundError as exc:
        missing_cmd = getattr(exc, "filename", None) or "tmutil"
        return CheckResult(
            name="Time Machine",
            ok=False,
            reason_de=f"Befehl '{missing_cmd}' nicht gefunden.",
            fix_de="Ordner ausschließen: tmutil addexclusion . oder Backup konfigurieren.",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name="Time Machine",
            ok=False,
            reason_de="Timeout (5s) bei Time-Machine-Prüfung.",
            fix_de="Ordner ausschließen: tmutil addexclusion . oder Backup verschlüsseln.",
        )
    except Exception as exc:
        return CheckResult(
            name="Time Machine",
            ok=False,
            reason_de=f"Fehler bei Time-Machine-Prüfung ({type(exc).__name__}).",
            fix_de="Ordner ausschließen: tmutil addexclusion . oder Backup verschlüsseln.",
        )


def run_start_check(
    app_dir: Path,
    run: Callable = subprocess.run,
    home: Path = Path.home(),
    platform: str = sys.platform,
) -> List[CheckResult]:
    """Führt alle Sicherheitsprüfungen durch.
    
    Alle I/O-Parameter sind für automatisierte Tests injizierbar.
    """
    if platform != "darwin":
        return [
            CheckResult(
                name="Plattform",
                ok=True,
                reason_de="ℹ️ Start-Check übersprungen (kein macOS)",
                fix_de="",
                skipped=True,
            )
        ]

    results: List[CheckResult] = [
        check_filevault(run=run),
        check_cloud_sync(app_dir=app_dir, home=home),
        check_time_machine(app_dir=app_dir, run=run),
    ]

    return results


def main(
    results_override: Optional[List[CheckResult]] = None,
    app_dir: Optional[Path] = None,
) -> int:
    """CLI-Einstiegspunkt für den Start-Check."""
    # Entwickler-Override
    if os.getenv("SOB_SKIP_START_CHECK") == "1":
        print("\033[33m⚠️ Start-Check übersprungen – nur synthetische Daten verwenden\033[0m")
        return 0

    target_app_dir = app_dir if app_dir is not None else REPO_ROOT
    results = results_override if results_override is not None else run_start_check(app_dir=target_app_dir)

    if len(results) == 1 and results[0].skipped:
        print(results[0].reason_de)
        return 0

    all_ok = True
    print("\n=======================================================")
    print("🔒 macOS-Start-Check (Datenschutz & Integrität)")
    print("=======================================================")

    for r in results:
        if r.ok:
            print(f"✅ {r.name}: {r.reason_de}")
        else:
            all_ok = False
            print(f"\033[31m❌ {r.name}: {r.reason_de}\033[0m")
            if r.fix_de:
                print(f"\033[31m   👉 Behebung: {r.fix_de}\033[0m")

    print("=======================================================\n")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
