"""Integritätsprüfung für GGUF-Modellgewichte via SHA-256 (Issue #116).

Sicherheitsinvariante:
Modellgewichte müssen vor der Initialisierung verifiziert werden.
Ohne konfigurierten Hash oder bei Prüfsummenabweichung bricht der Vorgang
mit einem deutlichen Fehler (❌) ab.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Optional, Union


class ModelIntegrityError(ValueError):
    """Signalisiert eine fehlgeschlagene oder fehlende Modell-Integritätsprüfung."""
    pass


def resolve_expected_sha256(
    model_path: Path,
    expected: Optional[str] = None,
) -> Optional[str]:
    """Ermittelt den erwarteten SHA-256-Hash in Prioritätsfolge:
    1. Explizites Argument `expected`
    2. Umgebungsvariable `SOB_MODEL_SHA256`
    3. Sidecar-Datei `<model_path>.sha256`
    """
    if expected and expected.strip():
        return expected.strip().lower()

    env_hash = os.environ.get("SOB_MODEL_SHA256")
    if env_hash and env_hash.strip():
        return env_hash.strip().lower()

    sidecar_path = model_path.with_name(f"{model_path.name}.sha256")
    if sidecar_path.is_file():
        content = sidecar_path.read_text(encoding="utf-8").strip()
        if content:
            # Akzeptiert Formate: "<hash>", "<hash>  <dateiname>", "<hash> *<dateiname>"
            token = content.split()[0].strip().lower()
            if token:
                return token

    return None


def calculate_sha256(path: Path, chunk_size: int = 65536) -> str:
    """Berechnet den SHA-256-Hex-Digest einer Datei in Chunks."""
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest().lower()


def verify_model_sha256(
    path: Union[str, Path],
    expected: Optional[str] = None,
) -> None:
    """Überprüft die SHA-256-Prüfsumme einer Modelldatei.

    Erwartungswert-Priorität:
    1. Argument `expected`
    2. Umgebungsvariable `SOB_MODEL_SHA256`
    3. Sidecar-Datei `<path>.sha256`

    Raises:
        FileNotFoundError: Wenn die Modelldatei nicht existiert (Message beginnt mit ❌).
        ModelIntegrityError: Wenn kein Hash konfiguriert ist oder der berechnete
                             Hash abweicht (Message beginnt mit ❌).
    """
    p = Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"❌ Modelldatei nicht gefunden: {p}")

    expected_hash = resolve_expected_sha256(p, expected)
    if not expected_hash:
        sidecar_path = p.with_name(f"{p.name}.sha256")
        raise ModelIntegrityError(
            f"❌ Kein SHA-256-Hash für Modell '{p.name}' konfiguriert. "
            f"Start/Laden abgebrochen: Integrität kann ohne Prüfsumme nicht gewährleistet werden.\n"
            f"Bitte Umgebungsvariable 'SOB_MODEL_SHA256' setzen oder Sidecar-Datei "
            f"'{sidecar_path.name}' mit Hex-Digest bereitstellen.\n"
            f"Hash erzeugen mit: shasum -a 256 \"{p.name}\" (bzw. sha256sum)"
        )

    actual_hash = calculate_sha256(p)
    if actual_hash != expected_hash:
        raise ModelIntegrityError(
            f"❌ Modell-Hash-Abweichung für '{p.name}': "
            f"Erwartet: {expected_hash}, berechnet: {actual_hash}. "
            f"Integrität der Modellgewichte nicht gewährleistet!"
        )
