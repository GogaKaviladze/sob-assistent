from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from chat_app.app import create_app
from chat_app.start_check import (
    CheckResult,
    check_cloud_sync,
    main as start_check_main,
    run_start_check,
)


def make_completed_process(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# Platform Check (Nicht-macOS)
# ---------------------------------------------------------------------------

def test_start_check_non_darwin_skips(tmp_path: Path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    app_dir = fake_home / "sob-generator"
    app_dir.mkdir()

    results = run_start_check(app_dir=app_dir, home=fake_home, platform="linux")
    assert len(results) == 1
    assert results[0].ok is True
    assert results[0].skipped is True
    assert "Start-Check übersprungen (kein macOS)" in results[0].reason_de


# ---------------------------------------------------------------------------
# FileVault Check
# ---------------------------------------------------------------------------

def test_filevault_on(tmp_path: Path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    app_dir = fake_home / "SOB"
    app_dir.mkdir()

    def mock_run(cmd, *args, **kwargs):
        if cmd == ["fdesetup", "status"]:
            return make_completed_process(cmd, returncode=0, stdout="FileVault is On.\n")
        if cmd == ["tmutil", "destinationinfo"]:
            return make_completed_process(cmd, returncode=0, stdout="tmutil: No destinations configured.\n")
        raise AssertionError(f"Unerwarteter Befehl: {cmd}")

    results = run_start_check(app_dir=app_dir, run=mock_run, home=fake_home, platform="darwin")
    fv = next(r for r in results if r.name == "FileVault")
    assert fv.ok is True
    assert "aktiviert" in fv.reason_de


def test_filevault_off(tmp_path: Path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    app_dir = fake_home / "SOB"
    app_dir.mkdir()

    def mock_run(cmd, *args, **kwargs):
        if cmd == ["fdesetup", "status"]:
            return make_completed_process(cmd, returncode=0, stdout="FileVault is Off.\n")
        if cmd == ["tmutil", "destinationinfo"]:
            return make_completed_process(cmd, returncode=0, stdout="tmutil: No destinations configured.\n")
        raise AssertionError(f"Unerwarteter Befehl: {cmd}")

    results = run_start_check(app_dir=app_dir, run=mock_run, home=fake_home, platform="darwin")
    fv = next(r for r in results if r.name == "FileVault")
    assert fv.ok is False
    assert "FileVault ist aus" in fv.fix_de
    assert "Systemeinstellungen" in fv.fix_de


def test_filevault_missing_binary(tmp_path: Path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    app_dir = fake_home / "SOB"
    app_dir.mkdir()

    def mock_run(cmd, *args, **kwargs):
        if cmd == ["fdesetup", "status"]:
            raise FileNotFoundError("fdesetup nicht gefunden")
        if cmd == ["tmutil", "destinationinfo"]:
            return make_completed_process(cmd, returncode=0, stdout="tmutil: No destinations configured.\n")
        raise AssertionError(f"Unerwarteter Befehl: {cmd}")

    results = run_start_check(app_dir=app_dir, run=mock_run, home=fake_home, platform="darwin")
    fv = next(r for r in results if r.name == "FileVault")
    assert fv.ok is False
    assert "nicht gefunden" in fv.reason_de


def test_filevault_timeout(tmp_path: Path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    app_dir = fake_home / "SOB"
    app_dir.mkdir()

    def mock_run(cmd, *args, **kwargs):
        if cmd == ["fdesetup", "status"]:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=5)
        if cmd == ["tmutil", "destinationinfo"]:
            return make_completed_process(cmd, returncode=0, stdout="tmutil: No destinations configured.\n")
        raise AssertionError(f"Unerwarteter Befehl: {cmd}")

    results = run_start_check(app_dir=app_dir, run=mock_run, home=fake_home, platform="darwin")
    fv = next(r for r in results if r.name == "FileVault")
    assert fv.ok is False
    assert "Timeout" in fv.reason_de


# ---------------------------------------------------------------------------
# Cloud Sync Check
# ---------------------------------------------------------------------------

def test_cloud_sync_icloud_drive(tmp_path: Path):
    fake_home = tmp_path / "home"
    app_dir = fake_home / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "sob-app"
    app_dir.mkdir(parents=True)

    def mock_run(cmd, *args, **kwargs):
        if cmd == ["fdesetup", "status"]:
            return make_completed_process(cmd, returncode=0, stdout="FileVault is On.\n")
        if cmd == ["tmutil", "destinationinfo"]:
            return make_completed_process(cmd, returncode=0, stdout="tmutil: No destinations configured.\n")
        raise AssertionError(f"Unerwarteter Befehl: {cmd}")

    results = run_start_check(app_dir=app_dir, run=mock_run, home=fake_home, platform="darwin")
    cs = next(r for r in results if r.name == "Cloud-Sync")
    assert cs.ok is False
    assert "iCloud" in cs.reason_de
    assert "~/SOB" in cs.fix_de


def test_cloud_sync_cloudstorage_onedrive(tmp_path: Path):
    fake_home = tmp_path / "home"
    app_dir = fake_home / "Library" / "CloudStorage" / "OneDrive-Firma" / "sob-app"
    app_dir.mkdir(parents=True)

    def mock_run(cmd, *args, **kwargs):
        if cmd == ["fdesetup", "status"]:
            return make_completed_process(cmd, returncode=0, stdout="FileVault is On.\n")
        if cmd == ["tmutil", "destinationinfo"]:
            return make_completed_process(cmd, returncode=0, stdout="tmutil: No destinations configured.\n")
        raise AssertionError(f"Unerwarteter Befehl: {cmd}")

    results = run_start_check(app_dir=app_dir, run=mock_run, home=fake_home, platform="darwin")
    cs = next(r for r in results if r.name == "Cloud-Sync")
    assert cs.ok is False
    assert "OneDrive" in cs.reason_de
    assert "~/SOB" in cs.fix_de


def test_cloud_sync_dropbox(tmp_path: Path):
    fake_home = tmp_path / "home"
    app_dir = fake_home / "Dropbox" / "sob-app"
    app_dir.mkdir(parents=True)

    def mock_run(cmd, *args, **kwargs):
        if cmd == ["fdesetup", "status"]:
            return make_completed_process(cmd, returncode=0, stdout="FileVault is On.\n")
        if cmd == ["tmutil", "destinationinfo"]:
            return make_completed_process(cmd, returncode=0, stdout="tmutil: No destinations configured.\n")
        raise AssertionError(f"Unerwarteter Befehl: {cmd}")

    results = run_start_check(app_dir=app_dir, run=mock_run, home=fake_home, platform="darwin")
    cs = next(r for r in results if r.name == "Cloud-Sync")
    assert cs.ok is False
    assert "Dropbox" in cs.reason_de


def test_cloud_sync_documents_with_icloud_sync(tmp_path: Path):
    fake_home = tmp_path / "home"
    app_dir = fake_home / "Documents" / "sob-app"
    app_dir.mkdir(parents=True)

    # Marker erstellen für iCloud Schreibtisch & Dokumente
    marker = fake_home / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "Documents"
    marker.mkdir(parents=True)

    def mock_run(cmd, *args, **kwargs):
        if cmd == ["fdesetup", "status"]:
            return make_completed_process(cmd, returncode=0, stdout="FileVault is On.\n")
        if cmd == ["tmutil", "destinationinfo"]:
            return make_completed_process(cmd, returncode=0, stdout="tmutil: No destinations configured.\n")
        raise AssertionError(f"Unerwarteter Befehl: {cmd}")

    results = run_start_check(app_dir=app_dir, run=mock_run, home=fake_home, platform="darwin")
    cs = next(r for r in results if r.name == "Cloud-Sync")
    assert cs.ok is False
    assert "iCloud" in cs.reason_de


def test_cloud_sync_documents_without_icloud_sync(tmp_path: Path):
    fake_home = tmp_path / "home"
    app_dir = fake_home / "Documents" / "sob-app"
    app_dir.mkdir(parents=True)
    # Kein Marker-Verzeichnis vorhanden!

    def mock_run(cmd, *args, **kwargs):
        if cmd == ["fdesetup", "status"]:
            return make_completed_process(cmd, returncode=0, stdout="FileVault is On.\n")
        if cmd == ["tmutil", "destinationinfo"]:
            return make_completed_process(cmd, returncode=0, stdout="tmutil: No destinations configured.\n")
        raise AssertionError(f"Unerwarteter Befehl: {cmd}")

    results = run_start_check(app_dir=app_dir, run=mock_run, home=fake_home, platform="darwin")
    cs = next(r for r in results if r.name == "Cloud-Sync")
    assert cs.ok is True


def test_cloud_sync_safe_dir(tmp_path: Path):
    fake_home = tmp_path / "home"
    app_dir = fake_home / "SOB"
    app_dir.mkdir(parents=True)

    def mock_run(cmd, *args, **kwargs):
        if cmd == ["fdesetup", "status"]:
            return make_completed_process(cmd, returncode=0, stdout="FileVault is On.\n")
        if cmd == ["tmutil", "destinationinfo"]:
            return make_completed_process(cmd, returncode=0, stdout="tmutil: No destinations configured.\n")
        raise AssertionError(f"Unerwarteter Befehl: {cmd}")

    results = run_start_check(app_dir=app_dir, run=mock_run, home=fake_home, platform="darwin")
    cs = next(r for r in results if r.name == "Cloud-Sync")
    assert cs.ok is True


# ---------------------------------------------------------------------------
# Time Machine Check
# ---------------------------------------------------------------------------

def test_time_machine_no_destination(tmp_path: Path):
    fake_home = tmp_path / "home"
    app_dir = fake_home / "SOB"
    app_dir.mkdir(parents=True)

    def mock_run(cmd, *args, **kwargs):
        if cmd == ["fdesetup", "status"]:
            return make_completed_process(cmd, returncode=0, stdout="FileVault is On.\n")
        if cmd == ["tmutil", "destinationinfo"]:
            return make_completed_process(cmd, returncode=0, stdout="tmutil: No destinations configured.\n")
        raise AssertionError(f"Unerwarteter Befehl: {cmd}")

    results = run_start_check(app_dir=app_dir, run=mock_run, home=fake_home, platform="darwin")
    tm = next(r for r in results if r.name == "Time Machine")
    assert tm.ok is True
    assert "Kein Time-Machine-Ziel" in tm.reason_de or "nicht eingerichtet" in tm.reason_de or "Kein Ziel" in tm.reason_de


def test_time_machine_excluded(tmp_path: Path):
    fake_home = tmp_path / "home"
    app_dir = fake_home / "SOB"
    app_dir.mkdir(parents=True)

    def mock_run(cmd, *args, **kwargs):
        if cmd == ["fdesetup", "status"]:
            return make_completed_process(cmd, returncode=0, stdout="FileVault is On.\n")
        if cmd == ["tmutil", "destinationinfo"]:
            return make_completed_process(cmd, returncode=0, stdout="Name : Backup\nMount Point : /Volumes/Backup\n")
        if cmd == ["tmutil", "isexcluded", str(app_dir.resolve())]:
            return make_completed_process(cmd, returncode=0, stdout=f"[Excluded]  {app_dir.resolve()}\n")
        raise AssertionError(f"Unerwarteter Befehl: {cmd}")

    results = run_start_check(app_dir=app_dir, run=mock_run, home=fake_home, platform="darwin")
    tm = next(r for r in results if r.name == "Time Machine")
    assert tm.ok is True
    assert "ausgeschlossen" in tm.reason_de or "Excluded" in tm.reason_de


def test_time_machine_not_excluded_but_encrypted(tmp_path: Path):
    fake_home = tmp_path / "home"
    app_dir = fake_home / "SOB"
    app_dir.mkdir(parents=True)

    def mock_run(cmd, *args, **kwargs):
        if cmd == ["fdesetup", "status"]:
            return make_completed_process(cmd, returncode=0, stdout="FileVault is On.\n")
        if cmd == ["tmutil", "destinationinfo"]:
            return make_completed_process(cmd, returncode=0, stdout="Name : Backup\nMount Point : /Volumes/Backup\n")
        if cmd == ["tmutil", "isexcluded", str(app_dir.resolve())]:
            return make_completed_process(cmd, returncode=0, stdout=f"[Included]  {app_dir.resolve()}\n")
        if cmd == ["diskutil", "info", "/Volumes/Backup"]:
            return make_completed_process(cmd, returncode=0, stdout="   FileVault:               Yes\n   Encrypted:               Yes\n")
        raise AssertionError(f"Unerwarteter Befehl: {cmd}")

    results = run_start_check(app_dir=app_dir, run=mock_run, home=fake_home, platform="darwin")
    tm = next(r for r in results if r.name == "Time Machine")
    assert tm.ok is True
    assert "verschlüsselt" in tm.reason_de


def test_time_machine_not_excluded_unencrypted_or_unknown(tmp_path: Path):
    fake_home = tmp_path / "home"
    app_dir = fake_home / "SOB"
    app_dir.mkdir(parents=True)

    def mock_run(cmd, *args, **kwargs):
        if cmd == ["fdesetup", "status"]:
            return make_completed_process(cmd, returncode=0, stdout="FileVault is On.\n")
        if cmd == ["tmutil", "destinationinfo"]:
            return make_completed_process(cmd, returncode=0, stdout="Name : Backup\nMount Point : /Volumes/Backup\n")
        if cmd == ["tmutil", "isexcluded", str(app_dir.resolve())]:
            return make_completed_process(cmd, returncode=0, stdout=f"[Included]  {app_dir.resolve()}\n")
        if cmd == ["diskutil", "info", "/Volumes/Backup"]:
            return make_completed_process(cmd, returncode=0, stdout="   FileVault:               No\n   Encrypted:               No\n")
        raise AssertionError(f"Unerwarteter Befehl: {cmd}")

    results = run_start_check(app_dir=app_dir, run=mock_run, home=fake_home, platform="darwin")
    tm = next(r for r in results if r.name == "Time Machine")
    assert tm.ok is False
    assert "nicht feststellbar" in tm.reason_de or "unverschlüsselt" in tm.reason_de
    assert "tmutil addexclusion" in tm.fix_de


def test_time_machine_missing_binary(tmp_path: Path):
    fake_home = tmp_path / "home"
    app_dir = fake_home / "SOB"
    app_dir.mkdir(parents=True)

    def mock_run(cmd, *args, **kwargs):
        if cmd == ["fdesetup", "status"]:
            return make_completed_process(cmd, returncode=0, stdout="FileVault is On.\n")
        if cmd == ["tmutil", "destinationinfo"]:
            raise FileNotFoundError("tmutil nicht gefunden")
        raise AssertionError(f"Unerwarteter Befehl: {cmd}")

    results = run_start_check(app_dir=app_dir, run=mock_run, home=fake_home, platform="darwin")
    tm = next(r for r in results if r.name == "Time Machine")
    assert tm.ok is False
    assert "nicht gefunden" in tm.reason_de


# ---------------------------------------------------------------------------
# CLI Entry Point & Exit Codes
# ---------------------------------------------------------------------------

def test_cli_all_pass(monkeypatch, capsys):
    monkeypatch.setenv("SOB_SKIP_START_CHECK", "0")
    dummy_results = [
        CheckResult(name="FileVault", ok=True, reason_de="FileVault ist aktiviert.", fix_de=""),
        CheckResult(name="Cloud-Sync", ok=True, reason_de="App-Ordner liegt sicher.", fix_de=""),
        CheckResult(name="Time Machine", ok=True, reason_de="Kein Backup-Ziel konfiguriert.", fix_de=""),
    ]
    code = start_check_main(results_override=dummy_results)
    assert code == 0
    captured = capsys.readouterr()
    assert "✅ FileVault" in captured.out
    assert "✅ Cloud-Sync" in captured.out
    assert "✅ Time Machine" in captured.out


def test_cli_one_fail(monkeypatch, capsys):
    monkeypatch.setenv("SOB_SKIP_START_CHECK", "0")
    dummy_results = [
        CheckResult(name="FileVault", ok=True, reason_de="FileVault ist aktiviert.", fix_de=""),
        CheckResult(name="Cloud-Sync", ok=False, reason_de="App-Ordner wird mit iCloud synchronisiert.", fix_de="Bitte Ordner nach ~/SOB verschieben."),
    ]
    code = start_check_main(results_override=dummy_results)
    assert code == 1
    captured = capsys.readouterr()
    assert "❌ Cloud-Sync" in captured.out
    assert "Bitte Ordner nach ~/SOB verschieben" in captured.out


def test_cli_skip_env_override(monkeypatch, capsys):
    monkeypatch.setenv("SOB_SKIP_START_CHECK", "1")
    code = start_check_main()
    assert code == 0
    captured = capsys.readouterr()
    assert "⚠️ Start-Check übersprungen – nur synthetische Daten verwenden" in captured.out


def test_cli_linux_skipped(monkeypatch, capsys):
    monkeypatch.delenv("SOB_SKIP_START_CHECK", raising=False)
    dummy_results = [
        CheckResult(name="Plattform", ok=True, reason_de="ℹ️ Start-Check übersprungen (kein macOS)", fix_de="", skipped=True)
    ]
    code = start_check_main(results_override=dummy_results)
    assert code == 0
    captured = capsys.readouterr()
    assert "ℹ️ Start-Check übersprungen (kein macOS)" in captured.out


# ---------------------------------------------------------------------------
# API Session endpoint start_check_skipped flag
# ---------------------------------------------------------------------------

def test_api_session_reports_start_check_skipped(monkeypatch):
    monkeypatch.setenv("SOB_SKIP_START_CHECK", "1")
    app = create_app()
    client = TestClient(app)
    res = client.get("/api/session")
    assert res.status_code == 200
    data = res.json()
    assert data.get("start_check_skipped") is True


def test_api_session_reports_start_check_not_skipped(monkeypatch):
    monkeypatch.delenv("SOB_SKIP_START_CHECK", raising=False)
    app = create_app()
    client = TestClient(app)
    res = client.get("/api/session")
    assert res.status_code == 200
    data = res.json()
    assert data.get("start_check_skipped") is False


# ---------------------------------------------------------------------------
# Logging & Terminal Compliance: Keine Pfade oder Benutzernamen im Terminal
# ---------------------------------------------------------------------------

def test_start_check_terminal_no_paths_or_usernames(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.delenv("SOB_SKIP_START_CHECK", raising=False)
    fake_home = tmp_path / "secret_user_name_12345"
    app_dir = fake_home / "SOB"
    app_dir.mkdir(parents=True)

    def mock_run(cmd, *args, **kwargs):
        if cmd == ["fdesetup", "status"]:
            return make_completed_process(cmd, returncode=0, stdout="FileVault is On.\n")
        if cmd == ["tmutil", "destinationinfo"]:
            return make_completed_process(cmd, returncode=0, stdout="tmutil: No destinations configured.\n")
        raise AssertionError(f"Unerwarteter Befehl: {cmd}")

    results = run_start_check(app_dir=app_dir, run=mock_run, home=fake_home, platform="darwin")
    assert len(results) == 3
    for r in results:
        assert "secret_user_name_12345" not in r.reason_de
        assert "secret_user_name_12345" not in r.fix_de

    # Auch im CLI-Output prüfen
    exit_code = start_check_main(results_override=results, app_dir=app_dir)
    assert exit_code == 0
    captured = capsys.readouterr().out
    assert "secret_user_name_12345" not in captured
    assert str(fake_home) not in captured
    assert str(app_dir) not in captured


def test_file_logger_deferred_until_init_file_logger(tmp_path: Path, monkeypatch):
    """File-Logger darf beim Modul-Import keine Datei anlegen oder leeren."""
    import logging
    import chat_app.logger as app_logger

    test_log_file = tmp_path / "chat_app.log"
    monkeypatch.setattr(app_logger, "LOG_DIR", tmp_path)
    monkeypatch.setattr(app_logger, "LOG_FILE", test_log_file)

    lg = logging.getLogger("chat_app")
    old_fhs = [h for h in lg.handlers if isinstance(h, logging.FileHandler)]
    for h in old_fhs:
        lg.removeHandler(h)
        h.close()

    # Vor init_file_logger() existiert die Datei noch NICHT
    assert not test_log_file.exists()

    # Erst init_file_logger() legt sie an
    app_logger.init_file_logger()
    assert test_log_file.exists()
    content = test_log_file.read_text(encoding="utf-8")
    assert "Log gelöscht: anlass=start" in content


# ---------------------------------------------------------------------------
# Copilot Review Invarianten & Edge Cases
# ---------------------------------------------------------------------------

def test_cloud_sync_symlinked_root(tmp_path: Path):
    """Prüft, dass Symlinks auf Cloud-Ordner ebenfalls aufgelöst und erkannt werden."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    real_dropbox = tmp_path / "external_disk" / "DropboxTarget"
    real_dropbox.mkdir(parents=True)
    # Symlink ~/Dropbox -> real_dropbox
    symlink_dropbox = fake_home / "Dropbox"
    symlink_dropbox.symlink_to(real_dropbox)

    app_dir = real_dropbox / "sob-app"
    app_dir.mkdir()

    def mock_run(cmd, *args, **kwargs):
        if cmd == ["fdesetup", "status"]:
            return make_completed_process(cmd, returncode=0, stdout="FileVault is On.\n")
        if cmd == ["tmutil", "destinationinfo"]:
            return make_completed_process(cmd, returncode=0, stdout="tmutil: No destinations configured.\n")
        raise AssertionError(f"Unerwarteter Befehl: {cmd}")

    results = run_start_check(app_dir=app_dir, run=mock_run, home=fake_home, platform="darwin")
    cs = next(r for r in results if r.name == "Cloud-Sync")
    assert cs.ok is False
    assert "Dropbox" in cs.reason_de


def test_time_machine_destinationinfo_partial_failure(tmp_path: Path):
    """Teilausgabe mit Exit-Code 1 bei destinationinfo darf nicht als OK gewertet werden."""
    fake_home = tmp_path / "home"
    app_dir = fake_home / "SOB"
    app_dir.mkdir(parents=True)

    def mock_run(cmd, *args, **kwargs):
        if cmd == ["fdesetup", "status"]:
            return make_completed_process(cmd, returncode=0, stdout="FileVault is On.\n")
        if cmd == ["tmutil", "destinationinfo"]:
            # Teil-Ausgabe aber Returncode 1!
            return make_completed_process(cmd, returncode=1, stdout="Name : Partial Backup\n")
        raise AssertionError(f"Unerwarteter Befehl: {cmd}")

    results = run_start_check(app_dir=app_dir, run=mock_run, home=fake_home, platform="darwin")
    tm = next(r for r in results if r.name == "Time Machine")
    assert tm.ok is False
    assert "fehlgeschlagen" in tm.reason_de


def test_time_machine_isexcluded_failure_fails_check(tmp_path: Path):
    """Fehler bei tmutil isexcluded darf nicht still in die Verschlüsselung durchfallen."""
    fake_home = tmp_path / "home"
    app_dir = fake_home / "SOB"
    app_dir.mkdir(parents=True)

    def mock_run(cmd, *args, **kwargs):
        if cmd == ["fdesetup", "status"]:
            return make_completed_process(cmd, returncode=0, stdout="FileVault is On.\n")
        if cmd == ["tmutil", "destinationinfo"]:
            return make_completed_process(cmd, returncode=0, stdout="Name : Backup\nMount Point : /Volumes/Backup\n")
        if cmd == ["tmutil", "isexcluded", str(app_dir.resolve())]:
            # isexcluded wirft Fehler (Exit 1)
            return make_completed_process(cmd, returncode=1, stderr="Error checking exclusion\n")
        raise AssertionError(f"Unerwarteter Befehl: {cmd}")

    results = run_start_check(app_dir=app_dir, run=mock_run, home=fake_home, platform="darwin")
    tm = next(r for r in results if r.name == "Time Machine")
    assert tm.ok is False
    assert "tmutil isexcluded fehlgeschlagen" in tm.reason_de


def test_time_machine_unverified_network_destination_fails(tmp_path: Path):
    """Netzwerkziel ohne Mount Point darf nicht ignoriert werden."""
    fake_home = tmp_path / "home"
    app_dir = fake_home / "SOB"
    app_dir.mkdir(parents=True)

    destinationinfo_output = """====================================================
Name          : LocalBackup
Kind          : Local
Mount Point   : /Volumes/LocalBackup
ID            : 11111111-1111-1111-1111-111111111111
====================================================
Name          : NetworkBackup
Kind          : Network
URL           : smb://server.local/backups
ID            : 22222222-2222-2222-2222-222222222222
"""

    def mock_run(cmd, *args, **kwargs):
        if cmd == ["fdesetup", "status"]:
            return make_completed_process(cmd, returncode=0, stdout="FileVault is On.\n")
        if cmd == ["tmutil", "destinationinfo"]:
            return make_completed_process(cmd, returncode=0, stdout=destinationinfo_output)
        if cmd == ["tmutil", "isexcluded", str(app_dir.resolve())]:
            return make_completed_process(cmd, returncode=0, stdout=f"[Included]  {app_dir.resolve()}\n")
        if cmd == ["diskutil", "info", "/Volumes/LocalBackup"]:
            return make_completed_process(cmd, returncode=0, stdout="   FileVault:               Yes\n   Encrypted:               Yes\n")
        raise AssertionError(f"Unerwarteter Befehl: {cmd}")

    results = run_start_check(app_dir=app_dir, run=mock_run, home=fake_home, platform="darwin")
    tm = next(r for r in results if r.name == "Time Machine")
    assert tm.ok is False
    assert "NetworkBackup" in tm.reason_de or "nicht verifiziert" in tm.reason_de


def test_time_machine_diskutil_failure_with_partial_stdout_fails(tmp_path: Path):
    """diskutil info mit Exit-Code 1 darf trotz Teilausgabe nicht als verschlüsselt gelten."""
    fake_home = tmp_path / "home"
    app_dir = fake_home / "SOB"
    app_dir.mkdir(parents=True)

    def mock_run(cmd, *args, **kwargs):
        if cmd == ["fdesetup", "status"]:
            return make_completed_process(cmd, returncode=0, stdout="FileVault is On.\n")
        if cmd == ["tmutil", "destinationinfo"]:
            return make_completed_process(cmd, returncode=0, stdout="Name : Backup\nMount Point : /Volumes/Backup\n")
        if cmd == ["tmutil", "isexcluded", str(app_dir.resolve())]:
            return make_completed_process(cmd, returncode=0, stdout=f"[Included]  {app_dir.resolve()}\n")
        if cmd == ["diskutil", "info", "/Volumes/Backup"]:
            # Exit 1 trotz Teil-Ausgabe!
            return make_completed_process(cmd, returncode=1, stdout="FileVault: Yes\nError reading partition table\n")
        raise AssertionError(f"Unerwarteter Befehl: {cmd}")

    results = run_start_check(app_dir=app_dir, run=mock_run, home=fake_home, platform="darwin")
    tm = next(r for r in results if r.name == "Time Machine")
    assert tm.ok is False
    assert "diskutil info" in tm.reason_de
    assert "fehlgeschlagen" in tm.reason_de


def test_chat_app_main_entrypoint_aborts_on_failed_start_check(monkeypatch):
    """Prüft, dass der Python-Einstiegspunkt python -m chat_app bei fehlgeschlagenem Check mit Exit 1 abbricht."""
    import chat_app.__main__ as chat_main

    monkeypatch.delenv("SOB_SKIP_START_CHECK", raising=False)
    # Mock run_start_check_cli liefert Fehlercode 1
    monkeypatch.setattr(chat_main, "run_start_check_cli", lambda: 1)

    with pytest.raises(SystemExit) as exc_info:
        chat_main.main()
    assert exc_info.value.code == 1


def test_time_machine_destination_named_no_destinations_configured_fails_if_unencrypted(tmp_path: Path):
    """Teilstring 'no destinations configured' in einem Zielnamen darf nicht als leeres Ziel gewertet werden."""
    fake_home = tmp_path / "home"
    app_dir = fake_home / "SOB"
    app_dir.mkdir(parents=True)

    dest_output = """====================================================
Name          : No destinations configured Backup
Kind          : Local
Mount Point   : /Volumes/NoDestBackup
ID            : 33333333-3333-3333-3333-333333333333
"""

    def mock_run(cmd, *args, **kwargs):
        if cmd == ["fdesetup", "status"]:
            return make_completed_process(cmd, returncode=0, stdout="FileVault is On.\n")
        if cmd == ["tmutil", "destinationinfo"]:
            return make_completed_process(cmd, returncode=0, stdout=dest_output)
        if cmd == ["tmutil", "isexcluded", str(app_dir.resolve())]:
            return make_completed_process(cmd, returncode=0, stdout=f"[Included]  {app_dir.resolve()}\n")
        if cmd == ["diskutil", "info", "/Volumes/NoDestBackup"]:
            return make_completed_process(cmd, returncode=0, stdout="   FileVault:               No\n   Encrypted:               No\n")
        raise AssertionError(f"Unerwarteter Befehl: {cmd}")

    results = run_start_check(app_dir=app_dir, run=mock_run, home=fake_home, platform="darwin")
    tm = next(r for r in results if r.name == "Time Machine")
    assert tm.ok is False
    assert "unverschlüsselt" in tm.reason_de


def test_time_machine_isexcluded_folder_name_contains_excluded(tmp_path: Path):
    """Status '[Included]' bei einem Pfad, der '[Excluded]' im Namen trägt, darf nicht als Ausschluss gewertet werden."""
    fake_home = tmp_path / "home"
    app_dir = fake_home / "SOB [Excluded]"
    app_dir.mkdir(parents=True)

    def mock_run(cmd, *args, **kwargs):
        if cmd == ["fdesetup", "status"]:
            return make_completed_process(cmd, returncode=0, stdout="FileVault is On.\n")
        if cmd == ["tmutil", "destinationinfo"]:
            return make_completed_process(cmd, returncode=0, stdout="Name : Backup\nMount Point : /Volumes/Backup\n")
        if cmd == ["tmutil", "isexcluded", str(app_dir.resolve())]:
            # Pfadname enthält [Excluded], aber Status ist [Included]!
            return make_completed_process(cmd, returncode=0, stdout=f"[Included]  {app_dir.resolve()}\n")
        if cmd == ["diskutil", "info", "/Volumes/Backup"]:
            return make_completed_process(cmd, returncode=0, stdout="   FileVault:               No\n   Encrypted:               No\n")
        raise AssertionError(f"Unerwarteter Befehl: {cmd}")

    results = run_start_check(app_dir=app_dir, run=mock_run, home=fake_home, platform="darwin")
    tm = next(r for r in results if r.name == "Time Machine")
    assert tm.ok is False
    assert "unverschlüsselt" in tm.reason_de


def test_time_machine_diskutil_volume_name_spoof_fails(tmp_path: Path):
    """Eine Zeile 'Volume Name: Encrypted: Yes' darf nicht als Verschlüsselung akzeptiert werden."""
    fake_home = tmp_path / "home"
    app_dir = fake_home / "SOB"
    app_dir.mkdir(parents=True)

    spoofed_diskutil_output = """   Device Identifier:        disk3s1
   Volume Name:              Encrypted: Yes
   FileVault:                No
   Encrypted:                No
"""

    def mock_run(cmd, *args, **kwargs):
        if cmd == ["fdesetup", "status"]:
            return make_completed_process(cmd, returncode=0, stdout="FileVault is On.\n")
        if cmd == ["tmutil", "destinationinfo"]:
            return make_completed_process(cmd, returncode=0, stdout="Name : Backup\nMount Point : /Volumes/Backup\n")
        if cmd == ["tmutil", "isexcluded", str(app_dir.resolve())]:
            return make_completed_process(cmd, returncode=0, stdout=f"[Included]  {app_dir.resolve()}\n")
        if cmd == ["diskutil", "info", "/Volumes/Backup"]:
            return make_completed_process(cmd, returncode=0, stdout=spoofed_diskutil_output)
        raise AssertionError(f"Unerwarteter Befehl: {cmd}")

    results = run_start_check(app_dir=app_dir, run=mock_run, home=fake_home, platform="darwin")
    tm = next(r for r in results if r.name == "Time Machine")
    assert tm.ok is False
    assert "unverschlüsselt" in tm.reason_de


def test_time_machine_diskutil_missing_reports_diskutil_filename(tmp_path: Path):
    """Wenn diskutil fehlt, muss die Fehlermeldung 'diskutil' und nicht 'tmutil' nennen."""
    fake_home = tmp_path / "home"
    app_dir = fake_home / "SOB"
    app_dir.mkdir(parents=True)

    def mock_run(cmd, *args, **kwargs):
        if cmd == ["fdesetup", "status"]:
            return make_completed_process(cmd, returncode=0, stdout="FileVault is On.\n")
        if cmd == ["tmutil", "destinationinfo"]:
            return make_completed_process(cmd, returncode=0, stdout="Name : Backup\nMount Point : /Volumes/Backup\n")
        if cmd == ["tmutil", "isexcluded", str(app_dir.resolve())]:
            return make_completed_process(cmd, returncode=0, stdout=f"[Included]  {app_dir.resolve()}\n")
        if cmd == ["diskutil", "info", "/Volumes/Backup"]:
            raise FileNotFoundError(2, "No such file or directory", "diskutil")
        raise AssertionError(f"Unerwarteter Befehl: {cmd}")

    results = run_start_check(app_dir=app_dir, run=mock_run, home=fake_home, platform="darwin")
    tm = next(r for r in results if r.name == "Time Machine")
    assert tm.ok is False
    assert "Befehl 'diskutil' nicht gefunden" in tm.reason_de


def test_cloud_sync_and_time_machine_symlinked_logs_outside_app_fails(tmp_path: Path):
    """Ausgelagerte Log-Ziele per Symlink außerhalb des App-Ordners werden sofort abgewiesen."""
    fake_home = tmp_path / "home"
    app_dir = fake_home / "SOB"
    app_dir.mkdir(parents=True)

    external_logs = fake_home / "external_logs"
    external_logs.mkdir(parents=True)

    symlink_logs = app_dir / "logs"
    symlink_logs.symlink_to(external_logs)

    def mock_run(cmd, *args, **kwargs):
        if cmd == ["fdesetup", "status"]:
            return make_completed_process(cmd, returncode=0, stdout="FileVault is On.\n")
        if cmd == ["tmutil", "destinationinfo"]:
            return make_completed_process(cmd, returncode=0, stdout="tmutil: No destinations configured.\n")
        raise AssertionError(f"Unerwarteter Befehl: {cmd}")

    results = run_start_check(app_dir=app_dir, run=mock_run, home=fake_home, platform="darwin")
    cs = next(r for r in results if r.name == "Cloud-Sync")
    assert cs.ok is False
    assert "Ausgelagertes Log-Verzeichnis per Symlink" in cs.reason_de


def test_cloud_sync_cloudstorage_provider_symlink_resolved(tmp_path: Path):
    """Prüft, dass Symlinks auf Provider-Wurzeln unter ~/Library/CloudStorage/ aufgelöst und erkannt werden."""
    fake_home = tmp_path / "home"
    fake_cloud_storage = fake_home / "Library" / "CloudStorage"
    fake_cloud_storage.mkdir(parents=True)

    # Reales Ziel außerhalb des Home-Ordners
    real_onedrive = tmp_path / "external_drive" / "OneDrive-Firma"
    real_onedrive.mkdir(parents=True)

    # Symlink ~/Library/CloudStorage/OneDrive-Firma -> real_onedrive
    symlink_provider = fake_cloud_storage / "OneDrive-Firma"
    symlink_provider.symlink_to(real_onedrive)

    app_dir = real_onedrive / "sob-app"
    app_dir.mkdir()

    def mock_run(cmd, *args, **kwargs):
        if cmd == ["fdesetup", "status"]:
            return make_completed_process(cmd, returncode=0, stdout="FileVault is On.\n")
        if cmd == ["tmutil", "destinationinfo"]:
            return make_completed_process(cmd, returncode=0, stdout="tmutil: No destinations configured.\n")
        raise AssertionError(f"Unerwarteter Befehl: {cmd}")

    results = run_start_check(app_dir=app_dir, run=mock_run, home=fake_home, platform="darwin")
    cs = next(r for r in results if r.name == "Cloud-Sync")
    assert cs.ok is False
    assert "OneDrive" in cs.reason_de


def test_time_machine_unrecognized_empty_stdout_fails(tmp_path: Path):
    """Leeres stdout ohne explizite 'No destinations configured'-Meldung muss fail-closed abgelehnt werden."""
    fake_home = tmp_path / "home"
    app_dir = fake_home / "SOB"
    app_dir.mkdir(parents=True)

    def mock_run(cmd, *args, **kwargs):
        if cmd == ["fdesetup", "status"]:
            return make_completed_process(cmd, returncode=0, stdout="FileVault is On.\n")
        if cmd == ["tmutil", "destinationinfo"]:
            # Exit-Code 0, aber leere Ausgabe ohne die bekannte Diagnosemeldung
            return make_completed_process(cmd, returncode=0, stdout="   \n", stderr="")
        raise AssertionError(f"Unerwarteter Befehl: {cmd}")

    results = run_start_check(app_dir=app_dir, run=mock_run, home=fake_home, platform="darwin")
    tm = next(r for r in results if r.name == "Time Machine")
    assert tm.ok is False
    assert "nicht feststellbar" in tm.reason_de


def test_cloud_sync_scan_permission_error_fails_closed(tmp_path: Path, monkeypatch):
    """Zugriffsfehler (z. B. fehlende TCC-Rechte) beim Scannen von CloudStorage müssen fail-closed abbrechen."""
    fake_home = tmp_path / "home"
    fake_cloud_storage = fake_home / "Library" / "CloudStorage"
    fake_cloud_storage.mkdir(parents=True)
    app_dir = fake_home / "SOB"
    app_dir.mkdir(parents=True)

    original_iterdir = Path.iterdir

    def mock_iterdir(self):
        if self == fake_cloud_storage:
            raise PermissionError("Operation not permitted (TCC)")
        return original_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", mock_iterdir)

    def mock_run(cmd, *args, **kwargs):
        if cmd == ["fdesetup", "status"]:
            return make_completed_process(cmd, returncode=0, stdout="FileVault is On.\n")
        if cmd == ["tmutil", "destinationinfo"]:
            return make_completed_process(cmd, returncode=0, stdout="tmutil: No destinations configured.\n")
        raise AssertionError(f"Unerwarteter Befehl: {cmd}")

    results = run_start_check(app_dir=app_dir, run=mock_run, home=fake_home, platform="darwin")
    cs = next(r for r in results if r.name == "Cloud-Sync")
    assert cs.ok is False
    assert "fehlgeschlagen" in cs.reason_de
    assert "nicht lesbar" in cs.reason_de


def test_cloud_sync_unrelated_broken_entry_in_cloud_storage_does_not_block_sob(tmp_path: Path, monkeypatch):
    """Fehler beim Auflösen eines fremden CloudStorage-Eintrags dürfen eine unbeteiligte App in ~/SOB nicht blockieren."""
    fake_home = tmp_path / "home"
    fake_cloud_storage = fake_home / "Library" / "CloudStorage"
    fake_cloud_storage.mkdir(parents=True)
    bad_entry = fake_cloud_storage / "CorruptProvider"
    bad_entry.mkdir()
    app_dir = fake_home / "SOB"
    app_dir.mkdir(parents=True)

    original_resolve = Path.resolve

    def mock_resolve(self, *args, **kwargs):
        if self == bad_entry:
            raise OSError("Stale NFS file handle or broken symlink")
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", mock_resolve)

    def mock_run(cmd, *args, **kwargs):
        if cmd == ["fdesetup", "status"]:
            return make_completed_process(cmd, returncode=0, stdout="FileVault is On.\n")
        if cmd == ["tmutil", "destinationinfo"]:
            return make_completed_process(cmd, returncode=0, stdout="tmutil: No destinations configured.\n")
        raise AssertionError(f"Unerwarteter Befehl: {cmd}")

    results = run_start_check(app_dir=app_dir, run=mock_run, home=fake_home, platform="darwin")
    cs = next(r for r in results if r.name == "Cloud-Sync")
    assert cs.ok is True
    assert "liegen nicht in einem Cloud-Sync-Verzeichnis" in cs.reason_de


def test_cloud_sync_google_drive_with_spaces_detected(tmp_path: Path):
    """CloudStorage-Ordner mit Leerzeichen ('Google Drive - user@gmail.com') werden erkannt."""
    fake_home = tmp_path / "home"
    gdrive_dir = fake_home / "Library" / "CloudStorage" / "Google Drive - max@example.com"
    app_dir = gdrive_dir / "sob-app"
    app_dir.mkdir(parents=True)

    def mock_run(cmd, *args, **kwargs):
        if cmd == ["fdesetup", "status"]:
            return make_completed_process(cmd, returncode=0, stdout="FileVault is On.\n")
        if cmd == ["tmutil", "destinationinfo"]:
            return make_completed_process(cmd, returncode=0, stdout="tmutil: No destinations configured.\n")
        raise AssertionError(f"Unerwarteter Befehl: {cmd}")

    results = run_start_check(app_dir=app_dir, run=mock_run, home=fake_home, platform="darwin")
    cs = next(r for r in results if r.name == "Cloud-Sync")
    assert cs.ok is False
    assert "Google Drive" in cs.reason_de


def test_cloud_sync_top_level_exception_fails_gracefully(tmp_path: Path, monkeypatch):
    """Unerwartete Fehler in check_cloud_sync führen zu sicherem Fehlerstatus ohne Crash."""
    fake_home = tmp_path / "home"
    app_dir = fake_home / "SOB"

    original_resolve = Path.resolve

    def mock_resolve(self, *args, **kwargs):
        if self == app_dir:
            raise RuntimeError("Fatal filesystem exception")
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", mock_resolve)

    result = check_cloud_sync(app_dir=app_dir, home=fake_home)
    assert result.ok is False
    assert "Fehler bei Cloud-Sync-Prüfung" in result.reason_de
    assert "Fatal filesystem exception" in result.reason_de


# ---------------------------------------------------------------------------
# Regressionstests für PR #146 Review (Runde 1)
# ---------------------------------------------------------------------------

def test_cloud_sync_mobile_documents_no_tcc_permission_error(tmp_path: Path, monkeypatch):
    """iCloud-Ordner Mobile Documents darf nicht per iterdir() gescannt werden (TCC-Schutz)."""
    fake_home = tmp_path / "home"
    fake_icloud = fake_home / "Library" / "Mobile Documents"
    fake_icloud.mkdir(parents=True)
    app_dir = fake_home / "SOB"
    app_dir.mkdir(parents=True)

    original_iterdir = Path.iterdir

    def mock_iterdir(self):
        if self == fake_icloud:
            raise PermissionError("[Errno 1] Operation not permitted (macOS TCC)")
        return original_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", mock_iterdir)

    result = check_cloud_sync(app_dir=app_dir, home=fake_home)
    assert result.ok is True
    assert "liegen nicht in einem Cloud-Sync-Verzeichnis" in result.reason_de


def test_start_check_no_paths_or_usernames_on_failures(tmp_path: Path, monkeypatch, capsys):
    """Weder reason_de, fix_de noch Terminal-Output dürfen bei Fehlern Benutzernamen oder Pfade leaken."""
    secret_user = "secret_user_goga_42"
    fake_home = tmp_path / secret_user
    app_dir = fake_home / "GeheimesProjekt"
    app_dir.mkdir(parents=True)

    # 1. Time Machine Fehler: unverschlüsseltes Ziel
    def mock_run_tm_unencrypted(cmd, *args, **kwargs):
        if cmd == ["fdesetup", "status"]:
            return make_completed_process(cmd, returncode=0, stdout="FileVault is On.\n")
        if cmd == ["tmutil", "destinationinfo"]:
            return make_completed_process(cmd, returncode=0, stdout="Name: BackupDrive\nMount Point: /Volumes/Backup\n")
        if cmd[:2] == ["tmutil", "isexcluded"]:
            return make_completed_process(cmd, returncode=0, stdout="[Included]\n")
        if cmd[:2] == ["diskutil", "info"]:
            return make_completed_process(cmd, returncode=0, stdout="FileVault: No\nEncrypted: No\n")
        raise AssertionError(f"Unerwarteter Befehl: {cmd}")

    results = run_start_check(app_dir=app_dir, run=mock_run_tm_unencrypted, home=fake_home, platform="darwin")
    tm = next(r for r in results if r.name == "Time Machine")
    assert tm.ok is False
    assert secret_user not in tm.reason_de
    assert secret_user not in tm.fix_de
    assert str(app_dir) not in tm.fix_de
    assert "tmutil addexclusion ." in tm.fix_de

    # 2. Time Machine Fehler: destinationinfo schlägt fehl
    def mock_run_tm_err(cmd, *args, **kwargs):
        if cmd == ["fdesetup", "status"]:
            return make_completed_process(cmd, returncode=0, stdout="FileVault is On.\n")
        if cmd == ["tmutil", "destinationinfo"]:
            return make_completed_process(cmd, returncode=1, stdout="", stderr="Error connecting to daemon")
        raise AssertionError(f"Unerwarteter Befehl: {cmd}")

    results_err = run_start_check(app_dir=app_dir, run=mock_run_tm_err, home=fake_home, platform="darwin")
    tm_err = next(r for r in results_err if r.name == "Time Machine")
    assert tm_err.ok is False
    assert secret_user not in tm_err.fix_de
    assert str(app_dir) not in tm_err.fix_de

    # 3. CLI Terminal-Ausgabe prüfen
    start_check_main(results_override=results, app_dir=app_dir)
    captured = capsys.readouterr().out
    assert secret_user not in captured
    assert str(app_dir) not in captured


def test_cloud_sync_provider_matching_box_precision(tmp_path: Path):
    """Ordner wie 'sandbox' oder 'toolbox' unter CloudStorage dürfen nicht als 'Box' erkannt werden."""
    fake_home = tmp_path / "home"
    sandbox_dir = fake_home / "Library" / "CloudStorage" / "sandbox-testing"
    app_dir = sandbox_dir / "my-app"
    app_dir.mkdir(parents=True)

    result = check_cloud_sync(app_dir=app_dir, home=fake_home)
    assert result.ok is False
    # Muss als "sandbox-testing" oder CloudStorage erkannt werden, NICHT als Provider "Box"
    assert "Box" not in result.reason_de


def test_filevault_line_anchored_regex():
    """FileVault muss am Zeilenanfang stehen; Substrings mitten in Zeilen dürfen nicht matchen."""
    from chat_app.start_check import check_filevault

    # Negativer Fall: 'FileVault is On' ist nur ein Teilstring mitten im Satz
    def mock_run_invalid(cmd, *args, **kwargs):
        return make_completed_process(cmd, returncode=0, stdout="Notice: Volume 2 FileVault is On, but main volume is not.\n")

    res = check_filevault(run=mock_run_invalid)
    assert res.ok is False
    assert "deaktiviert" in res.reason_de

    # Positiver Fall: 'FileVault is On.' am Zeilenanfang
    def mock_run_valid(cmd, *args, **kwargs):
        return make_completed_process(cmd, returncode=0, stdout="Some header\nFileVault is On.\n")

    res_ok = check_filevault(run=mock_run_valid)
    assert res_ok.ok is True


def test_cloud_sync_provider_name_masks_email_and_account(tmp_path: Path):
    """E-Mail-Adressen in FileProvider-Ordnern (z. B. Nextcloud-user@domain.com) werden abgetrennt."""
    fake_home = tmp_path / "home"
    nextcloud_dir = fake_home / "Library" / "CloudStorage" / "Nextcloud-max.mustermann@firma.de"
    app_dir = nextcloud_dir / "sob-app"
    app_dir.mkdir(parents=True)

    result = check_cloud_sync(app_dir=app_dir, home=fake_home)
    assert result.ok is False
    assert "max.mustermann@firma.de" not in result.reason_de
    assert "Nextcloud" in result.reason_de


def test_time_machine_null_mount_point_fails_cleanly(tmp_path: Path):
    """Ungemountete Netzwerk-Backup-Ziele mit Mount Point '(null)' melden sauberen Fehler."""
    fake_home = tmp_path / "home"
    app_dir = fake_home / "SOB"
    app_dir.mkdir(parents=True)

    def mock_run(cmd, *args, **kwargs):
        if cmd == ["fdesetup", "status"]:
            return make_completed_process(cmd, returncode=0, stdout="FileVault is On.\n")
        if cmd == ["tmutil", "destinationinfo"]:
            return make_completed_process(
                cmd,
                returncode=0,
                stdout="Name : NetworkBackup\nMount Point : (null)\n",
            )
        if cmd[:2] == ["tmutil", "isexcluded"]:
            return make_completed_process(cmd, returncode=0, stdout="[Included]\n")
        raise AssertionError(f"Unerwarteter Befehl: {cmd}")

    results = run_start_check(app_dir=app_dir, run=mock_run, home=fake_home, platform="darwin")
    tm = next(r for r in results if r.name == "Time Machine")
    assert tm.ok is False
    assert "kein lokaler Mount Point" in tm.reason_de


def test_sanitize_path_str_username_with_spaces():
    """Benutzernamen mit Leerzeichen (z. B. /Users/Max Mustermann/) werden zuverlässig anonymisiert."""
    from chat_app.start_check import _sanitize_path_str

    raw = "Fehler in /Users/Max Mustermann/Projects/SOB/file.txt gefunden."
    sanitized = _sanitize_path_str(raw)
    assert "Max Mustermann" not in sanitized
    assert "~/Projects/SOB/file.txt" in sanitized







