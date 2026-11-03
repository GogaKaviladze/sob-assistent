"""Upload-Größenbegrenzung und Zero-Disk-Garantie für die Chat-App.

Stellt sicher, dass Dateiuploads vor und während des Empfangs auf ein
konfigurierbares Limit beschränkt werden und Starlette's MultiPartParser
niemals temporäre Dateien auf die Festplatte auslagert (SpooledTemporaryFile Rollover).
"""
from __future__ import annotations

import json
import os
from typing import Optional

from fastapi import HTTPException
import starlette.formparsers
from .logger import logger

DEFAULT_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
HARD_CEILING_MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB
UPLOAD_PATHS = ("/api/upload", "/api/upload-plan")


def get_max_upload_bytes() -> int:
    """Ermittelt das konfigurierte Upload-Limit aus SOB_MAX_UPLOAD_BYTES mit Validierung und Clamping."""
    raw = os.getenv("SOB_MAX_UPLOAD_BYTES", "").strip()
    if not raw:
        return DEFAULT_MAX_UPLOAD_BYTES
    try:
        val = int(raw)
        if val <= 0:
            logger.warning(
                "Ungültiger Wert für SOB_MAX_UPLOAD_BYTES (<= 0). Fallback auf Default (%d Bytes).",
                DEFAULT_MAX_UPLOAD_BYTES,
            )
            return DEFAULT_MAX_UPLOAD_BYTES
        if val > HARD_CEILING_MAX_UPLOAD_BYTES:
            logger.warning(
                "SOB_MAX_UPLOAD_BYTES (%d) überschreitet harte Obergrenze (%d). Begrenzt auf %d Bytes.",
                val,
                HARD_CEILING_MAX_UPLOAD_BYTES,
                HARD_CEILING_MAX_UPLOAD_BYTES,
            )
            return HARD_CEILING_MAX_UPLOAD_BYTES
        return val
    except ValueError:
        logger.warning(
            "Nicht-ganzzahliger Wert für SOB_MAX_UPLOAD_BYTES. Fallback auf Default (%d Bytes).",
            DEFAULT_MAX_UPLOAD_BYTES,
        )
        return DEFAULT_MAX_UPLOAD_BYTES


def format_limit_mb(num_bytes: int) -> str:
    """Formatiert Byte-Anzahl lesbar als MB (z. B. '10 MB' oder '2.5 MB')."""
    mb = num_bytes / (1024 * 1024)
    if mb.is_integer():
        return f"{int(mb)} MB"
    return f"{mb:.1f} MB"


def configure_spool_threshold(max_bytes: int) -> int:
    """Setzt Starlettes Spool-Schwelle mindestens auf HARD_CEILING_MAX_UPLOAD_BYTES und das übergebene Limit.

    Die Schwelle wird niemals verringert, sodass auch bei mehreren Apps oder unterschiedlichen
    Limits die Spool-Schwelle stets >= jedem konfigurierten Limit bleibt.
    """
    current = getattr(starlette.formparsers.MultiPartParser, "spool_max_size", 0)
    target = max(current, max_bytes, HARD_CEILING_MAX_UPLOAD_BYTES)
    starlette.formparsers.MultiPartParser.spool_max_size = target
    if starlette.formparsers.MultiPartParser.spool_max_size < max_bytes:
        raise RuntimeError(
            f"Invariante verletzt: Limit ({max_bytes}) > Spool-Schwelle ({starlette.formparsers.MultiPartParser.spool_max_size})"
        )
    return target


class LimitUploadSizeMiddleware:
    """ASGI-Middleware zur Durchsetzung des Upload-Limits vor und während des Streamings."""

    def __init__(self, app, max_bytes: Optional[int] = None):
        self.app = app
        self._configured_max_bytes = max_bytes

    @property
    def max_bytes(self) -> int:
        if self._configured_max_bytes is not None:
            return self._configured_max_bytes
        return get_max_upload_bytes()

    def _get_detail_msg(self) -> str:
        return f"❌ Datei zu groß (max. {format_limit_mb(self.max_bytes)})."

    async def _send_json_response(self, send, status_code: int, detail: str) -> None:
        body = json.dumps({"detail": detail}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({
            "type": "http.response.body",
            "body": body,
        })

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["path"].rstrip("/") not in UPLOAD_PATHS:
            await self.app(scope, receive, send)
            return

        limit = self.max_bytes
        detail_msg = self._get_detail_msg()

        # 1. Vorabprüfung via Content-Length Header (sofern vorhanden)
        headers = dict(scope.get("headers", []))
        cl_raw = headers.get(b"content-length")
        if cl_raw is not None:
            try:
                cl_val = int(cl_raw.decode("ascii").strip())
                if cl_val < 0:
                    logger.warning("Upload abgelehnt: Negativer Content-Length-Header (%d).", cl_val)
                    await self._send_json_response(send, 400, "❌ Ungültige Anfrage.")
                    return
                if cl_val > limit:
                    logger.warning(
                        "Upload abgelehnt: Content-Length (%d Bytes) überschreitet Limit (%d Bytes).",
                        cl_val,
                        limit,
                    )
                    await self._send_json_response(send, 413, detail_msg)
                    return
            except (ValueError, UnicodeDecodeError) as exc:
                logger.warning(
                    "Upload abgelehnt: Ungültiger Content-Length-Header (%s).",
                    type(exc).__name__,
                )
                await self._send_json_response(send, 400, "❌ Ungültige Anfrage.")
                return

        # 2. Mitlaufende Zählung des Request-Body-Streams (für chunked oder gefälschte Content-Length)
        received_bytes = 0
        response_started = False

        async def custom_receive():
            nonlocal received_bytes
            message = await receive()
            if message.get("type") == "http.request":
                body = message.get("body", b"")
                received_bytes += len(body)
                if received_bytes > limit:
                    logger.warning(
                        "Upload abgelehnt: Empfangene Daten (%d Bytes) überschreiten Limit (%d Bytes).",
                        received_bytes,
                        limit,
                    )
                    raise HTTPException(status_code=413, detail=detail_msg)
            return message

        async def custom_send(message):
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, custom_receive, custom_send)
        except HTTPException as exc:
            if exc.status_code == 413 and not response_started:
                await self._send_json_response(send, 413, detail_msg)
            else:
                raise
