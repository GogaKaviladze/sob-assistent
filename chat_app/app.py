"""FastAPI-Webanwendung für den interaktiven Reflexions-Chat (KISS, Separation of Concerns).

Stellt Endpunkte für den CSV-Upload, interaktives Token-Streaming,
Modellauswahl aus models/, Export des Chatverlaufs und Diagnose-Logging bereit.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Literal, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from sob_generator.docx_safety import UnsafeDocxError
from sob_generator.llm.sections import SECTION_DEFINITIONS
from sob_generator.llm.sections_op_de_wisch import OP_DE_WISCH_SECTION_DEFINITIONS
from .chatbot import ReflectionChatbot, ReflectionEngine, get_candidate_model_directories
from .context_builder import ContextBuilder, classify_entry_sections
from .csv_loader import CsvData, parse_csv_content
from .docx_exporter import (
    DocxExportError,
    EmptyExportError,
    UnknownTemplateError,
    UnmappedChatContentError,
    export_chatbot_to_docx,
)
from .logger import get_recent_logs, log_csv_loaded, log_plan_loaded, logger
from .plan_loader import (
    IncompletePlanError,
    PlanData,
    extract_client_name_from_filename,
    parse_plan_docx,
    parse_plan_text,
)
from .upload_limits import (
    LimitUploadSizeMiddleware,
    configure_spool_threshold,
    get_max_upload_bytes,
)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


class ChatMessageRequest(BaseModel):
    message: str


class SelectModelRequest(BaseModel):
    model_name: str


class SetPlanRequest(BaseModel):
    plan_text: str
    filename: Optional[str] = None
    title: Optional[str] = None


# Allowlist der gültigen Lebensbereiche (Hamburg 1.1-3, Op de Wisch 1-9)
ValidAreaId = Literal[
    "1.1", "1.2", "1.3", "1.4", "1.5", "2.1", "2.2", "2.3", "3",
    "1", "2", "4", "5", "6", "7", "8", "9",
]


class SelectAreaRequest(BaseModel):
    bereich_id: ValidAreaId


class SelectTemplateRequest(BaseModel):
    template: str


class AssignEntryRequest(BaseModel):
    entry_id: str
    bereich_id: str


DEFAULT_DOCX_PARSE_TIMEOUT = 10.0

_GENERIC_CLIENT_NAMES = {
    "klient*in (anonymisiert)",
    "klient*in",
    "anonymisiert",
    "synthetischer klient",
    "vorlage",
    "fosvorlage",
    "",
}


def _canonical_client_name(name: str) -> str:
    """Normalisiert Klientennamen ('Nachname, Vorname' -> 'vorname nachname') für den Fallabgleich."""
    raw = (name or "").strip()
    if not raw or raw.lower() in _GENERIC_CLIENT_NAMES:
        return ""
    if any(ph in raw.lower() for ph in ("name, vorname", "klient*in", "klientin")):
        return ""
    if "," in raw:
        parts = [p.strip() for p in raw.split(",", 1)]
        raw = f"{parts[1]} {parts[0]}".strip()
    return " ".join(raw.lower().split())


def _evaluate_client_match(
    existing_name: str,
    existing_from_filename: bool,
    new_name: str,
    new_from_filename: bool,
) -> tuple[bool, str]:
    """Prüft auf Klientenkonflikt zwischen bestehendem Fall und neuem Upload.

    Gibt (client_conflict, warning) zurück:
    - client_conflict=True nur wenn beide Namen sicher erkannt (nicht aus Dateinamen) und abweichend sind.
    - warning (nicht-leer) wenn ein abweichender oder fehlender Name unsicher ist (kein Reset).
    """
    c_exist = _canonical_client_name(existing_name)
    c_new = _canonical_client_name(new_name)
    if not c_exist and not c_new:
        return False, ""
    if not c_exist or not c_new:
        return (
            False,
            "⚠️ Klientenname in einer der Dateien nicht sicher erkannt – kein automatischer Fall-Reset. Bitte Fallzugehörigkeit prüfen.",
        )

    exist_tokens = set(c_exist.split())
    new_tokens = set(c_new.split())
    same_client = (
        c_exist == c_new
        or c_exist in c_new
        or c_new in c_exist
        or (len(exist_tokens) >= 2 and exist_tokens == new_tokens)
    )
    if same_client:
        return False, ""

    exist_confident = not existing_from_filename and len(exist_tokens) >= 2
    new_confident = not new_from_filename and len(new_tokens) >= 2
    if exist_confident and new_confident:
        return True, ""

    return (
        False,
        "⚠️ Klientenname nicht sicher erkannt (nur aus Dateiname abgeleitet oder unvollständig) – kein automatischer Fall-Reset. Bitte Fallzugehörigkeit prüfen.",
    )


def get_docx_parse_timeout() -> float:
    raw = os.getenv("SOB_DOCX_PARSE_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_DOCX_PARSE_TIMEOUT
    try:
        val = float(raw)
        return val if val > 0 else DEFAULT_DOCX_PARSE_TIMEOUT
    except ValueError:
        return DEFAULT_DOCX_PARSE_TIMEOUT


def create_app(
    chatbot: Optional[ReflectionChatbot] = None,
    max_upload_bytes: Optional[int] = None,
) -> FastAPI:
    """Application Factory für flexible Testbarkeit und Instanziierung."""
    limit = max_upload_bytes if max_upload_bytes is not None else get_max_upload_bytes()
    # Starlette Spool-Schwelle setzen (Garantie: keine Upload-Daten auf Festplatte)
    configure_spool_threshold(limit)

    app = FastAPI(
        title="Fachdialog & Reflexion Chatbot",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(LimitUploadSizeMiddleware, max_bytes=limit)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        # M5: Rohwerte niemals in Response Body oder Logs spiegeln
        has_path_area_error = any(
            "bereich_id" in err.get("loc", ()) or "path" in err.get("loc", ())
            for err in exc.errors()
        )
        if has_path_area_error:
            return JSONResponse(
                status_code=422,
                content={"detail": "❌ Ungültiger Lebensbereich."},
            )
        clean_errors = []
        for err in exc.errors():
            clean_err = {k: v for k, v in err.items() if k != "input"}
            clean_errors.append(clean_err)
        return JSONResponse(
            status_code=422,
            content={"detail": clean_errors},
        )

    bot = chatbot or ReflectionChatbot()

    _template_labels = {
        "hamburg": "Hamburg",
        "op-de-wisch": "Op de Wisch",
    }

    def _normalize_template(raw: str) -> str:
        norm = (raw or "").strip().lower()
        if norm in {"hamburg", "hh", "hamburger", "hamburg_svb"}:
            return "hamburg"
        if norm in {"op-de-wisch", "op_de_wisch", "hamburg_op_de_wisch", "odw", "opdewisch"}:
            return "op-de-wisch"
        raise HTTPException(
            status_code=422,
            detail="❌ Ungültige Vorlage – bitte 'Hamburg' oder 'Op de Wisch' wählen.",
        )

    def _check_template_mismatch(detected_template: str) -> Optional[JSONResponse]:
        """Prüft, ob der erkannte Vorbericht-Typ von der durch die Fachkraft gewählten Vorlage abweicht."""
        if getattr(bot.case, "template_selected", False) and detected_template != bot.case.template:
            det_label = _template_labels.get(detected_template, detected_template)
            chosen_label = _template_labels.get(bot.case.template, bot.case.template)
            msg = (
                f"❌ Vorbericht sieht nach {det_label} aus, "
                f"gewählt ist {chosen_label} – bitte Vorlage prüfen"
            )
            logger.warning(
                "Vorbericht-Upload abgelehnt wegen Vorlagen-Abweichung: erkannt=%s, gewaehlt=%s",
                detected_template,
                bot.case.template,
            )
            return JSONResponse(
                status_code=422,
                content={
                    "detail": msg,
                    "error": msg,
                    "template": bot.case.template,
                    "detected_template": detected_template,
                },
            )
        return None

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        """Liefert die interaktive Benutzeroberfläche aus."""
        template_file = TEMPLATES_DIR / "index.html"
        if not template_file.exists():
            raise HTTPException(status_code=500, detail="UI-Template nicht gefunden.")
        return HTMLResponse(content=template_file.read_text(encoding="utf-8"))

    @app.post("/api/upload")
    async def upload_csv(
        file: UploadFile = File(...),
        new_case: bool = Form(False),
    ):
        """Lädt eine CSV-Datei hoch, parst sie und übergibt sie dem Chatbot als Kontext."""
        bot.check_ttl()
        if not file.filename or not file.filename.lower().endswith(".csv"):
            raise HTTPException(status_code=400, detail="Bitte eine gültige .csv-Datei hochladen.")

        content_bytes = await file.read()
        try:
            content_text = content_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                content_text = content_bytes.decode("latin-1")
            except Exception as exc:
                logger.warning("Fehler beim Dekodieren der CSV-Datei: %s", type(exc).__name__)
                raise HTTPException(
                    status_code=400,
                    detail=f"❌ Konnte Datei-Encoding nicht lesen ({type(exc).__name__}).",
                ) from exc

        detected_client = extract_client_name_from_filename(file.filename)
        client_name = detected_client if detected_client else "Klient*in (anonymisiert)"

        try:
            parsed_csv = parse_csv_content(content_text, filename=file.filename, client_name=client_name)
        except ValueError as exc:
            logger.warning("Fehler beim Parsen der CSV-Datei: %s", type(exc).__name__)
            msg = str(exc)
            if msg.startswith("❌"):
                raise HTTPException(status_code=400, detail=msg) from exc
            raise HTTPException(
                status_code=400,
                detail=f"❌ CSV-Datei konnte nicht verarbeitet werden ({type(exc).__name__}).",
            ) from exc
        except Exception as exc:
            logger.warning("Fehler beim Parsen der CSV-Datei: %s", type(exc).__name__)
            raise HTTPException(
                status_code=400,
                detail=f"❌ CSV-Datei konnte nicht verarbeitet werden ({type(exc).__name__}).",
            ) from exc

        # Wenn parsed_csv einen strukturiert erfassten Klientennamen ermittelt hat, bevorzugen
        if parsed_csv.client_name and parsed_csv.client_name != "Klient*in (anonymisiert)":
            client_name = parsed_csv.client_name

        is_vivendi = bool(parsed_csv.entries) or ("vivendi" in file.filename.lower())
        source = "Vivendi PD Verlaufsbericht" if is_vivendi else f"Verlaufsdokumentation ({file.filename})"

        warning = ""
        was_selected = bool(getattr(bot.case, "template_selected", False))
        selected_template = bot.case.template
        if not new_case:
            # Konfliktprüfung für harten Fall-Reset (Issue #133):
            # (a) Dateityp bereits geladen, (b) Chat-Verlauf existiert, (c) sicher erkannter Klientenkonflikt
            has_same_type = bot.has_csv
            has_history = len(bot.messages) > 0
            client_conflict = False
            if bot.plan_data is not None:
                plan_client = getattr(bot.plan_data, "client_name", "")
                plan_from_fn = bool(getattr(bot.plan_data, "client_name_from_filename", False))
                effective_csv_client = client_name if client_name != "Klient*in (anonymisiert)" else detected_client
                csv_from_fn = bool(getattr(parsed_csv, "client_name_from_filename", False))
                client_conflict, warning = _evaluate_client_match(
                    plan_client, plan_from_fn, effective_csv_client, csv_from_fn
                )

            if has_same_type or has_history or client_conflict:
                return JSONResponse(
                    status_code=409,
                    content={
                        "needs_confirmation": True,
                        "detail": "Neuer Fall? Alle Daten dieses Falls werden gelöscht.",
                    },
                )
        else:
            bot.hard_reset(reason="reset")
            if was_selected:
                bot.case.set_template(selected_template, selected=True)

        bot.set_csv(parsed_csv)
        log_csv_loaded(total_rows=parsed_csv.entries_total, columns_count=len(parsed_csv.headers))

        # Erste 3 Zeilen als Vorschau für die UI
        preview_lines = parsed_csv.to_context_text(max_rows=3, update_counters=False)

        resp_payload: Dict[str, Any] = {
            "success": True,
            "filename": parsed_csv.filename,
            "total_rows": parsed_csv.total_rows,
            "entries_total": parsed_csv.entries_total,
            "entries_in_context": parsed_csv.entries_in_context,
            "oldest_included_date": parsed_csv.oldest_included_date,
            "headers": parsed_csv.headers,
            "preview": preview_lines,
            "client_name": client_name,
            "source": source,
        }
        if warning:
            resp_payload["warning"] = warning
        return resp_payload

    @app.post("/api/upload-plan")
    async def upload_plan(
        file: UploadFile = File(...),
        new_case: bool = Form(False),
    ):
        """Lädt einen Vorbericht / Gesamtplan (.docx, .txt oder .md) hoch, parst ihn und übergibt ihn dem Chatbot als Kontext."""
        bot.check_ttl()
        if not file.filename:
            raise HTTPException(status_code=400, detail="Dateiname fehlt.")

        lower_name = file.filename.lower()
        if not (lower_name.endswith(".docx") or lower_name.endswith(".txt") or lower_name.endswith(".md")):
            raise HTTPException(status_code=400, detail="❌ Bitte eine gültige .docx-, .txt- oder .md-Datei hochladen.")

        content_bytes = await file.read()

        if lower_name.endswith(".docx"):
            timeout = get_docx_parse_timeout()
            try:
                parsed_plan = await asyncio.wait_for(
                    run_in_threadpool(parse_plan_docx, content_bytes, filename=file.filename),
                    timeout=timeout,
                )
            except (UnsafeDocxError, asyncio.TimeoutError) as exc:
                reason = "timeout" if isinstance(exc, asyncio.TimeoutError) else getattr(exc, "reason", "unsafe")
                logger.warning("Upload-Plan abgelehnt: %s", reason)
                raise HTTPException(
                    status_code=400,
                    detail="❌ Datei abgelehnt: ungültiges oder zu großes Word-Dokument",
                ) from exc
            except IncompletePlanError as exc:
                logger.warning("Vorbericht unvollständig: %d von %d Bereichen erkannt", exc.found, exc.expected)
                raise HTTPException(
                    status_code=422,
                    detail=f"❌ Vorbericht unvollständig gelesen: {exc.found} von {exc.expected} Bereichen erkannt. Bitte Datei prüfen.",
                ) from exc
            except Exception as exc:
                logger.warning("Fehler beim Parsen der Vorbericht-Datei: %s", type(exc).__name__)
                raise HTTPException(
                    status_code=400,
                    detail=f"❌ Plan konnte nicht gelesen werden ({type(exc).__name__}).",
                ) from exc

            if parsed_plan.goals_count == 0:
                logger.warning("Kein Lebensbereich im Vorbericht erkannt: goals_count=0")
                raise HTTPException(
                    status_code=422,
                    detail="❌ Kein gültiger Vorbericht erkannt: Es konnten keine Lebensbereiche oder Ziele extrahiert werden. Bitte Vorlage prüfen (Hamburger SVB oder Op de Wisch).",
                )

            is_odw = bool(
                getattr(parsed_plan, "template", "") == "op-de-wisch"
                or any(
                    getattr(g, "section_id", "") in {"1", "2", "4", "5", "6", "7", "8", "9"}
                    for g in getattr(parsed_plan, "goals", [])
                )
                or "op_de_wisch" in parsed_plan.filename.lower()
                or "op-de-wisch" in parsed_plan.filename.lower()
                or "fo_svb" in parsed_plan.filename.lower()
            )
            template_name = "op-de-wisch" if is_odw else "hamburg"

            mismatch_resp = _check_template_mismatch(template_name)
            if mismatch_resp is not None:
                return mismatch_resp

            detected_plan_client = extract_client_name_from_filename(file.filename)
            effective_plan_client = parsed_plan.client_name or detected_plan_client
            plan_from_fn = bool(
                getattr(parsed_plan, "client_name_from_filename", False) or not parsed_plan.client_name
            )
            client_conflict = False
            warning = ""
            if bot.csv_data is not None:
                csv_client = getattr(bot.csv_data, "client_name", "")
                csv_from_fn = bool(getattr(bot.csv_data, "client_name_from_filename", False))
                client_conflict, warning = _evaluate_client_match(
                    csv_client, csv_from_fn, effective_plan_client, plan_from_fn
                )

            was_selected = bool(getattr(bot.case, "template_selected", False))
            selected_template = bot.case.template

            if not new_case:
                has_same_type = bot.has_plan
                has_history = len(bot.messages) > 0
                if has_same_type or has_history or client_conflict:
                    return JSONResponse(
                        status_code=409,
                        content={
                            "needs_confirmation": True,
                            "detail": "Neuer Fall? Alle Daten dieses Falls werden gelöscht.",
                        },
                    )
            else:
                bot.hard_reset(reason="reset")
                if was_selected:
                    bot.case.set_template(selected_template, selected=True)

            template_source = "gewaehlt" if was_selected else "erkannt"
            bot.case.set_template(template_name, selected=was_selected)
            bot.set_plan(parsed_plan)
            log_plan_loaded(goals_count=parsed_plan.goals_count)

            area_defs = OP_DE_WISCH_SECTION_DEFINITIONS if is_odw else SECTION_DEFINITIONS
            available_areas = [
                {
                    "id": s.id,
                    "title": s.title,
                    "confirmed": bot.case.is_area_confirmed(s.id),
                }
                for s in area_defs
            ]

            parser_warning = getattr(parsed_plan, "warning", "")
            combined_warning = f"{parser_warning}\n{warning}" if (parser_warning and warning) else (parser_warning or warning)

            plan_resp: Dict[str, Any] = {
                "success": True,
                "has_plan": True,
                "filename": parsed_plan.filename,
                "goals_count": parsed_plan.goals_count,
                "preview": parsed_plan.preview,
                "client_name": parsed_plan.client_name,
                "zeitraum_von": parsed_plan.zeitraum_von,
                "zeitraum_bis": parsed_plan.zeitraum_bis,
                "hidden_removed": getattr(parsed_plan, "hidden_removed", 0),
                "is_fallback": getattr(parsed_plan, "is_fallback", False),
                "warning": combined_warning,
                "parser_source": getattr(parsed_plan, "parser_source", ""),
                "template": template_name,
                "template_source": template_source,
                "available_areas": available_areas,
            }
            return plan_resp
        elif lower_name.endswith(".txt") or lower_name.endswith(".md"):
            try:
                content_text = content_bytes.decode("utf-8-sig")
            except UnicodeDecodeError:
                try:
                    content_text = content_bytes.decode("latin-1")
                except Exception as exc:
                    logger.warning("Fehler beim Dekodieren der Vorbericht-Datei: %s", type(exc).__name__)
                    raise HTTPException(
                        status_code=400,
                        detail=f"❌ Konnte Datei-Encoding nicht lesen ({type(exc).__name__}).",
                    ) from exc

            parsed_text_plan = parse_plan_text(content_text, filename=file.filename)
            if parsed_text_plan.goals_count == 0:
                logger.warning("Kein Lebensbereich im Text-Vorbericht erkannt: goals_count=0")
                raise HTTPException(
                    status_code=422,
                    detail="❌ Kein gültiger Vorbericht erkannt: Es konnten keine Lebensbereiche oder Ziele extrahiert werden.",
                )

            odw_only_ids = {"1", "2", "4", "5", "6", "7", "8", "9"}
            has_hamburg_dotted = any("." in getattr(g, "section_id", "") for g in parsed_text_plan.goals)
            is_odw = bool(
                getattr(parsed_text_plan, "template", "") == "op-de-wisch"
                or (
                    not has_hamburg_dotted
                    and (
                        "op de wisch" in content_text.lower()
                        or "op-de-wisch" in file.filename.lower()
                        or "op_de_wisch" in file.filename.lower()
                        or any(getattr(g, "section_id", "") in odw_only_ids for g in parsed_text_plan.goals)
                    )
                )
            )
            template_name = "op-de-wisch" if is_odw else "hamburg"

            mismatch_resp = _check_template_mismatch(template_name)
            if mismatch_resp is not None:
                return mismatch_resp

            was_selected = bool(getattr(bot.case, "template_selected", False))
            selected_template = bot.case.template

            if not new_case:
                has_same_type = bot.has_plan
                has_history = len(bot.messages) > 0
                if has_same_type or has_history:
                    return JSONResponse(
                        status_code=409,
                        content={
                            "needs_confirmation": True,
                            "detail": "Neuer Fall? Alle Daten dieses Falls werden gelöscht.",
                        },
                    )
            else:
                bot.hard_reset(reason="reset")
                if was_selected:
                    bot.case.set_template(selected_template, selected=True)

            template_source = "gewaehlt" if was_selected else "erkannt"
            bot.case.set_template(template_name, selected=was_selected)
            bot.set_plan(parsed_text_plan)
            bot.set_plan_context(
                content_text,
                plan_info={"filename": file.filename, "goals_count": parsed_text_plan.goals_count},
            )
            bot.plan_data = parsed_text_plan
            log_plan_loaded(goals_count=parsed_text_plan.goals_count)

            area_defs = OP_DE_WISCH_SECTION_DEFINITIONS if is_odw else SECTION_DEFINITIONS
            available_areas = [
                {
                    "id": s.id,
                    "title": s.title,
                    "confirmed": bot.case.is_area_confirmed(s.id),
                }
                for s in area_defs
            ]
            return {
                "success": True,
                "has_plan": True,
                "filename": file.filename,
                "goals_count": parsed_text_plan.goals_count,
                "client_name": parsed_text_plan.client_name,
                "zeitraum_von": parsed_text_plan.zeitraum_von,
                "zeitraum_bis": parsed_text_plan.zeitraum_bis,
                "preview": (bot.plan_context[:200] if bot.plan_context else ""),
                "hidden_removed": getattr(parsed_text_plan, "hidden_removed", 0),
                "is_fallback": getattr(parsed_text_plan, "is_fallback", True),
                "warning": getattr(parsed_text_plan, "warning", ""),
                "parser_source": getattr(parsed_text_plan, "parser_source", "text_plan"),
                "template": template_name,
                "template_source": template_source,
                "available_areas": available_areas,
            }

    def _validate_area_for_template(bereich_id: str) -> None:
        """Prüft bereich_id gegen die Bereiche der geladenen Vorlage."""
        is_odw = ContextBuilder.is_odw_case(bot.case)
        valid_ids = {
            s.id for s in (OP_DE_WISCH_SECTION_DEFINITIONS if is_odw else SECTION_DEFINITIONS)
        }
        if bereich_id not in valid_ids:
            raise HTTPException(status_code=422, detail="❌ Bereich gehört nicht zur Vorlage")

    @app.get("/api/areas")
    async def get_areas(template: Optional[str] = None) -> Dict[str, Any]:
        """Liefert die verfügbaren Lebensbereiche für die aktive oder angefragte Vorlage (nur lesend)."""
        bot.check_ttl()
        if template:
            template_key = _normalize_template(template)
            is_odw = template_key == "op-de-wisch"
            tpl_name = template_key
        else:
            is_odw = ContextBuilder.is_odw_case(bot.case)
            tpl_name = "op-de-wisch" if is_odw else "hamburg"
        area_defs = OP_DE_WISCH_SECTION_DEFINITIONS if is_odw else SECTION_DEFINITIONS
        available_areas = [
            {
                "id": s.id,
                "title": s.title,
                "confirmed": bot.case.is_area_confirmed(s.id),
            }
            for s in area_defs
        ]
        return {
            "success": True,
            "template": tpl_name,
            "available_areas": available_areas,
            "areas": available_areas,
        }

    @app.post("/api/template")
    async def select_template(req: SelectTemplateRequest) -> Dict[str, Any]:
        """Wechselt die aktive Berichtsvorlage (hamburg oder op-de-wisch)."""
        bot.check_ttl()
        template_key = _normalize_template(req.template)
        is_odw = template_key == "op-de-wisch"
        bot.case.set_template(template_key, selected=True)
        logger.info("Berichtsvorlage gewählt: %s", template_key)

        area_defs = OP_DE_WISCH_SECTION_DEFINITIONS if is_odw else SECTION_DEFINITIONS
        valid_ids = {s.id for s in area_defs}
        if bot.selected_bereich_id and bot.selected_bereich_id not in valid_ids:
            bot.selected_bereich_id = None

        available_areas = [
            {
                "id": s.id,
                "title": s.title,
                "confirmed": bot.case.is_area_confirmed(s.id),
            }
            for s in area_defs
        ]
        return {
            "success": True,
            "template": template_key,
            "available_areas": available_areas,
            "areas": available_areas,
        }

    @app.post("/api/area")
    async def select_area(req: SelectAreaRequest) -> Dict[str, Any]:
        """Wählt den aktiven Lebensbereich für den Fachdialog aus (M5)."""
        bot.check_ttl()
        _validate_area_for_template(req.bereich_id)

        bot.selected_bereich_id = req.bereich_id
        built = ContextBuilder.build(bot.case, req.bereich_id)

        # M2: Nur Bereichs-ID und Zähler loggen
        logger.info(
            "Lebensbereich ausgewählt: %s (Doku: %d+%d von %d)",
            req.bereich_id,
            built.assigned_count,
            built.unassigned_count,
            built.total_count,
        )

        context_status_data = None
        if (
            bot.provider is not None
            and hasattr(bot.provider, "count_tokens")
            and callable(getattr(bot.provider, "count_tokens"))
        ):
            try:
                _, cs = bot.fit_context_for_prompt(user_message="", bereich_id=req.bereich_id)
                context_status_data = cs.to_dict()
            except Exception:
                pass

        return {
            "success": True,
            "bereich_id": req.bereich_id,
            "title": built.bereich_title,
            "assigned_count": built.assigned_count,
            "unassigned_count": built.unassigned_count,
            "total_count": built.total_count,
            "confirmed": bot.case.is_area_confirmed(req.bereich_id),
            "context_status": context_status_data,
        }

    @app.get("/api/area/{bereich_id}/review")
    async def review_area(bereich_id: ValidAreaId) -> Dict[str, Any]:
        """Liefert die zugeordneten Verlaufs-Einträge und den Alt-SOB-Teil zur fachlichen Prüfung."""
        bot.check_ttl()
        _validate_area_for_template(bereich_id)
        review_data = ContextBuilder.get_review_data(bot.case, bereich_id)
        logger.info(
            "Bereich zur Prüfung aufgerufen: %s (Doku: %d+%d)",
            bereich_id,
            review_data["assigned_count"],
            review_data["unassigned_count"],
        )
        return {
            "success": True,
            **review_data,
        }

    @app.post("/api/area/review")
    async def review_area_post(req: SelectAreaRequest) -> Dict[str, Any]:
        """Liefert die zugeordneten Verlaufs-Einträge und den Alt-SOB-Teil via POST."""
        bot.check_ttl()
        _validate_area_for_template(req.bereich_id)
        review_data = ContextBuilder.get_review_data(bot.case, req.bereich_id)
        logger.info(
            "Bereich zur Prüfung aufgerufen: %s (Doku: %d+%d)",
            req.bereich_id,
            review_data["assigned_count"],
            review_data["unassigned_count"],
        )
        return {
            "success": True,
            **review_data,
        }

    @app.post("/api/area/confirm")
    async def confirm_area(req: SelectAreaRequest) -> Dict[str, Any]:
        """Setzt die fachliche Bestätigung 'Bereich geprüft' im RAM (Legal 1)."""
        bot.check_ttl()
        _validate_area_for_template(req.bereich_id)
        bot.case.confirm_area(req.bereich_id)
        logger.info("Lebensbereich bestätigt: %s", req.bereich_id)
        return {
            "success": True,
            "bereich_id": req.bereich_id,
            "confirmed": True,
        }

    @app.post("/api/entry/assign")
    async def assign_entry(req: AssignEntryRequest) -> Dict[str, Any]:
        """Ordnet einen Verlaufseintrag manuell einem Lebensbereich zu (Issue #32, RAM-only)."""
        bot.check_ttl()

        # 1. Vorlagenwahl prüfen: Muss explizit durch Fachkraft gewählt sein
        if not getattr(bot.case, "template_selected", False):
            raise HTTPException(
                status_code=422,
                detail="❌ Bitte zuerst eine Vorlage wählen.",
            )

        # 2. CSV prüfen: Muss geladen sein
        if not bot.has_csv or bot.case.csv is None:
            raise HTTPException(
                status_code=422,
                detail="❌ Keine CSV-Datei geladen.",
            )

        # 3. Vorlagenabgleich: Gehört der Bereich zur aktiven Session-Vorlage?
        is_odw = ContextBuilder.is_odw_case(bot.case)
        area_defs = OP_DE_WISCH_SECTION_DEFINITIONS if is_odw else SECTION_DEFINITIONS
        valid_area_ids = {s.id for s in area_defs}
        if req.bereich_id not in valid_area_ids:
            raise HTTPException(
                status_code=422,
                detail=f"❌ Bereich „{req.bereich_id}“ gehört nicht zur gewählten Vorlage.",
            )

        # 4. Eintrags-ID prüfen: Muss ein gültiger Index in der CSV sein
        csv = bot.case.csv
        entries_list = getattr(csv, "entries", None)
        rows_list = getattr(csv, "rows", None)
        total_entries = len(entries_list) if entries_list else (len(rows_list) if rows_list else 0)

        try:
            idx = int(req.entry_id)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=422,
                detail=f"❌ Unbekannte Eintrags-ID „{req.entry_id}“.",
            )

        if idx < 0 or idx >= total_entries:
            raise HTTPException(
                status_code=422,
                detail=f"❌ Unbekannte Eintrags-ID „{req.entry_id}“.",
            )

        # 5. Automatische Zuordnung prüfen:
        # Nur unzugeordnete Einträge (oder bereits manuell zugeordnete) dürfen zugeordnet werden.
        if entries_list:
            entry_obj = entries_list[idx]
            search_text = f"{entry_obj.kategorie} {entry_obj.text}"
        else:
            row_obj = rows_list[idx]
            search_text = " ".join(row_obj)

        matched_sec_ids = classify_entry_sections(search_text)
        is_auto = len(matched_sec_ids) > 0
        if is_auto:
            raise HTTPException(
                status_code=422,
                detail="❌ Automatisch zugeordnete Einträge können nicht manuell zugeordnet werden.",
            )

        # 6. Manuelle Zuordnung im RAM speichern
        bot.case.assign_entry(str(idx), req.bereich_id)

        # 7. Logging: DSGVO-konform nur IDs und Zähler (keine Texte oder Namen)
        logger.info(
            "Eintrag manuell zugeordnet: entry_id=%s, bereich_id=%s",
            idx,
            req.bereich_id,
        )

        built = ContextBuilder.build(bot.case, req.bereich_id)
        return {
            "success": True,
            "entry_id": str(idx),
            "bereich_id": req.bereich_id,
            "assigned_count": built.assigned_count,
            "unassigned_count": built.unassigned_count,
            "total_count": built.total_count,
        }

    @app.post("/api/chat-stream")
    async def chat_stream(req: ChatMessageRequest):
        """Streamt die Modellantwort Token für Token via Server-Sent Events (SSE)."""
        bot.check_ttl()
        if bot.has_plan:
            if not bot.selected_bereich_id:
                raise HTTPException(
                    status_code=409,
                    detail="❌ Bitte zuerst einen Lebensbereich wählen",
                )
            if not bot.case.is_area_confirmed(bot.selected_bereich_id):
                raise HTTPException(
                    status_code=409,
                    detail="❌ Bitte den Bereich mit „Bereich prüfen“ bestätigen",
                )

        if bot.provider is None:
            reason = bot.model_error or "Modelldatei fehlt"
            raise HTTPException(status_code=503, detail=f"❌ Kein Modell geladen: {reason}")

        user_msg = req.message.strip()
        if not user_msg:
            raise HTTPException(status_code=400, detail="Nachricht darf nicht leer sein.")

        def event_generator():
            for item in bot.chat_stream(user_msg):
                if isinstance(item, dict) and "error" in item:
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                    return
                yield f"data: {json.dumps({'token': item}, ensure_ascii=False)}\n\n"
            done_payload: Dict[str, Any] = {"done": True, "model": bot.model_info}
            if getattr(bot, "last_context_status", None):
                done_payload["context_status"] = bot.last_context_status.to_dict()
            yield f"data: {json.dumps(done_payload, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/chat")
    async def chat_message(req: ChatMessageRequest) -> Dict[str, Any]:
        """Synchrone Variante für nicht-streamende Anfragen."""
        bot.check_ttl()
        if bot.provider is None:
            reason = bot.model_error or "Modelldatei fehlt"
            raise HTTPException(status_code=503, detail=f"❌ Kein Modell geladen: {reason}")

        user_msg = req.message.strip()
        if not user_msg:
            raise HTTPException(status_code=400, detail="Nachricht darf nicht leer sein.")

        reply = bot.chat(user_msg)
        return {
            "reply": reply,
            "model": bot.model_info,
            "history_length": len(bot.messages),
            "history": bot.get_history(),
        }

    @app.get("/api/models")
    async def get_models() -> Dict[str, Any]:
        """Listet alle gefundenen .gguf Modelle auf und gibt die durchsuchten Pfade an."""
        models = bot.get_available_models()
        candidate_dirs = [str(d) for d in get_candidate_model_directories()]
        return {
            "models": models,
            "active": bot.model_info,
            "searched_directories": candidate_dirs,
            "count": len(models),
        }

    @app.post("/api/select-model")
    async def select_model(req: SelectModelRequest) -> Dict[str, Any]:
        """Wechselt das aktive Modell auf ein anderes Modell aus models/."""
        try:
            new_info = bot.switch_model(req.model_name)
            return {"success": True, "model": new_info}
        except Exception as exc:
            logger.warning("Fehler beim Modellwechsel: %s", type(exc).__name__)
            raise HTTPException(
                status_code=400,
                detail=f"❌ Modellwechsel fehlgeschlagen ({type(exc).__name__}).",
            ) from exc

    @app.post("/api/plan")
    async def set_plan(req: SetPlanRequest):
        """Hinterlegt einen Vorbericht / Gesamtplan als Kontext für den Chatbot."""
        if not req.plan_text.strip():
            raise HTTPException(status_code=400, detail="Plan-Text darf nicht leer sein.")

        parsed_text_plan = parse_plan_text(req.plan_text, filename=req.filename or "vorbericht.txt")
        if parsed_text_plan.goals_count == 0:
            logger.warning("Kein Lebensbereich in /api/plan erkannt: goals_count=0")
            raise HTTPException(
                status_code=422,
                detail="❌ Kein gültiger Vorbericht erkannt: Es konnten keine Lebensbereiche oder Ziele extrahiert werden.",
            )

        template_name = "op-de-wisch" if getattr(parsed_text_plan, "template", "") == "op-de-wisch" else "hamburg"
        mismatch_resp = _check_template_mismatch(template_name)
        if mismatch_resp is not None:
            return mismatch_resp

        was_selected = bool(getattr(bot.case, "template_selected", False))
        template_source = "gewaehlt" if was_selected else "erkannt"
        info: Dict[str, Any] = {"goals_count": parsed_text_plan.goals_count}
        if req.filename:
            info["filename"] = req.filename
        if req.title:
            info["title"] = req.title

        bot.case.set_template(template_name, selected=was_selected)
        bot.set_plan(parsed_text_plan)
        bot.set_plan_context(req.plan_text, plan_info=info)
        bot.plan_data = parsed_text_plan
        return {
            "success": True,
            "has_plan": True,
            "goals_count": parsed_text_plan.goals_count,
            "plan_info": bot.plan_info,
            "preview": bot.plan_context[:200] if bot.plan_context else "",
            "hidden_removed": getattr(parsed_text_plan, "hidden_removed", 0),
            "is_fallback": getattr(parsed_text_plan, "is_fallback", True),
            "warning": getattr(parsed_text_plan, "warning", ""),
            "parser_source": getattr(parsed_text_plan, "parser_source", "text_plan"),
            "template": template_name,
            "template_source": template_source,
        }

    @app.post("/api/reset-plan")
    async def reset_plan() -> Dict[str, bool]:
        """Entfernt den hinterlegten Vorbericht / Gesamtplan."""
        bot.clear_plan()
        return {"success": True}

    @app.get("/api/export")
    async def export_chat() -> Dict[str, Any]:
        """Liefert den vollständigen Chatverlauf strukturiert als Markdown."""
        return {
            "markdown": bot.export_markdown(),
        }

    @app.get("/api/export/docx")
    async def export_chat_docx(template: str = ""):
        """Generiert und liefert den ausgefüllten Word-Bericht (.docx) basierend auf der gewählten Vorlage."""
        if not template:
            raise HTTPException(
                status_code=400,
                detail="Bitte eine Vorlage angeben (?template=hamburg oder ?template=op-de-wisch).",
            )
        try:
            result = export_chatbot_to_docx(bot, template_name=template)
            docx_bytes, filename = result
            warnings = getattr(result, "warnings", [])
            manual_fields = getattr(result, "manual_fields", [])
        except UnmappedChatContentError as exc:
            # DSGVO-konform: Nur Zähler loggen, keine Personen- oder Inhaltsdaten
            logger.warning(
                "DOCX-Export abgelehnt: Chat-Inhalt konnte keinem Bereich zugeordnet werden (%d unzugeordnete Blöcke)",
                len(getattr(exc, "unmapped_blocks", [])),
            )
            # Generisches Detail: keine Überschriften oder Dialogtexte zurückspiegeln
            raise HTTPException(
                status_code=422,
                detail=(
                    "❌ Dialog-Inhalt konnte keinem Bereich zugeordnet werden "
                    f"({len(getattr(exc, 'unmapped_blocks', []))} Abschnitt(e)). Bitte Bereichsüberschriften "
                    "wie „1.2 Alltägliche Lebensführung“ verwenden. (Fehlercode: DOCX_UNMAPPED)"
                ),
            ) from None
        except (UnknownTemplateError, EmptyExportError) as exc:
            # Feste Texte aus docx_exporter (Vorlagenwahl / leere Sitzung), ohne Falldaten
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except DocxExportError as exc:
            # DSGVO: nur Fehlerklasse loggen, nie den Exception-Text (kann Falldaten enthalten)
            logger.error("DOCX-Export fehlgeschlagen (Fehlercode: DOCX_EXPORT_ERROR, %s)", type(exc).__name__)
            raise HTTPException(
                status_code=500, detail="❌ Export fehlgeschlagen (Fehlercode: DOCX_EXPORT_ERROR)"
            ) from None
        except Exception as exc:
            logger.error("DOCX-Export unerwartet fehlgeschlagen (Fehlercode: DOCX_INTERNAL, %s)", type(exc).__name__)
            raise HTTPException(
                status_code=500, detail="❌ Export fehlgeschlagen (Fehlercode: DOCX_INTERNAL)"
            ) from None

        # DSGVO-konform: Keine personenbezogenen Dateinamen loggen
        logger.info("DOCX-Export erfolgreich ausgeliefert (Größe: %d Bytes)", len(docx_bytes))

        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(docx_bytes)),
        }
        expose_headers = ["Content-Disposition", "Content-Length"]
        if warnings:
            import json
            import urllib.parse

            headers["X-Export-Warnings"] = urllib.parse.quote(json.dumps(warnings))
            expose_headers.append("X-Export-Warnings")
        if manual_fields:
            import json
            import urllib.parse

            headers["X-Export-Manual"] = urllib.parse.quote(json.dumps(manual_fields))
            expose_headers.append("X-Export-Manual")

        headers["Access-Control-Expose-Headers"] = ", ".join(expose_headers)

        return StreamingResponse(
            io.BytesIO(docx_bytes),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers=headers,
        )

    @app.get("/api/diagnostics")
    async def get_diagnostics() -> Dict[str, Any]:
        """Liefert umfassende Diagnose-Informationen für Debugging und Monitoring."""
        plan_fn = bot.plan_data.filename if bot.plan_data else (bot.plan_info.get("filename") if bot.plan_info else None)
        plan_gc = bot.plan_data.goals_count if bot.plan_data else (bot.plan_info.get("goals_count", 0) if bot.plan_info else 0)
        return {
            "pid": os.getpid(),
            "cwd": str(Path.cwd()),
            "python": sys.version,
            "active_model": bot.model_info,
            "models_found": bot.get_available_models(),
            "search_paths": [str(d) for d in get_candidate_model_directories()],
            "has_csv": bot.csv_data is not None,
            "has_plan": bot.has_plan,
            "plan_filename": plan_fn,
            "plan_goals_count": plan_gc,
            "history_length": len(bot.messages),
            "recent_logs": get_recent_logs(max_lines=30),
        }

    @app.get("/api/logs", response_class=PlainTextResponse)
    async def get_logs() -> str:
        """Gibt die aktuellen Diagnose-Logs als Klartext zurück."""
        return get_recent_logs(max_lines=200)

    @app.get("/api/session")
    async def get_session() -> Dict[str, Any]:
        """Gibt den aktuellen Status der Sitzung (Modell, CSV, Plan und Verlauf) zurück."""
        bot.check_ttl()
        closed_reason = bot.pop_case_closed_reason()

        has_csv = bot.csv_data is not None
        csv_info = None
        if bot.csv_data:
            client_name = getattr(bot.csv_data, "client_name", "") or "Klient*in (anonymisiert)"
            is_vivendi = bool(getattr(bot.csv_data, "entries", None)) or ("vivendi" in bot.csv_data.filename.lower())
            source = "Vivendi PD Verlaufsbericht" if is_vivendi else f"Verlaufsdokumentation ({bot.csv_data.filename})"
            csv_info = {
                "filename": bot.csv_data.filename,
                "total_rows": bot.csv_data.total_rows,
                "entries_total": getattr(bot.csv_data, "entries_total", bot.csv_data.total_rows),
                "entries_in_context": getattr(bot.csv_data, "entries_in_context", bot.csv_data.total_rows),
                "oldest_included_date": getattr(bot.csv_data, "oldest_included_date", ""),
                "headers": bot.csv_data.headers,
                "preview": bot.csv_data.to_context_text(max_rows=3, update_counters=False),
                "client_name": client_name,
                "source": source,
            }

        has_plan = bot.has_plan
        plan_info = None
        plan_filename = None
        plan_goals_count = 0

        if has_plan:
            if bot.plan_data:
                plan_filename = bot.plan_data.filename
                plan_goals_count = bot.plan_data.goals_count
                plan_info = {
                    "has_plan": True,
                    "filename": bot.plan_data.filename,
                    "client_name": bot.plan_data.client_name,
                    "zeitraum_von": bot.plan_data.zeitraum_von,
                    "zeitraum_bis": bot.plan_data.zeitraum_bis,
                    "goals_count": bot.plan_data.goals_count,
                    "preview": bot.plan_data.preview,
                    "goals": [g.to_dict() for g in bot.plan_data.goals],
                    "is_fallback": getattr(bot.plan_data, "is_fallback", False),
                    "warning": getattr(bot.plan_data, "warning", ""),
                    "parser_source": getattr(bot.plan_data, "parser_source", ""),
                    "info": {
                        "filename": bot.plan_data.filename,
                        "client_name": bot.plan_data.client_name,
                        "goals_count": bot.plan_data.goals_count,
                    },
                }
            else:
                plan_filename = bot.plan_info.get("filename") or bot.plan_info.get("title") or "Plan"
                plan_goals_count = bot.plan_info.get("goals_count", 0)
                plan_info = {
                    "has_plan": True,
                    "filename": plan_filename,
                    "client_name": bot.plan_info.get("client_name", ""),
                    "zeitraum_von": bot.plan_info.get("zeitraum_von", ""),
                    "zeitraum_bis": bot.plan_info.get("zeitraum_bis", ""),
                    "goals_count": plan_goals_count,
                    "preview": (bot.plan_context[:300] + "...") if bot.plan_context and len(bot.plan_context) > 300 else (bot.plan_context or ""),
                    "goals": [],
                    "info": bot.plan_info,
                }

        is_heuristic = isinstance(bot.provider, ReflectionEngine) if bot.provider else False
        kv_warning = getattr(bot.provider, "kv_clear_warning", None) if bot.provider else None

        selected_area_info = None
        if bot.selected_bereich_id:
            built = ContextBuilder.build(bot.case, bot.selected_bereich_id)
            selected_area_info = {
                "id": bot.selected_bereich_id,
                "title": built.bereich_title,
                "assigned_count": built.assigned_count,
                "unassigned_count": built.unassigned_count,
                "total_count": built.total_count,
                "confirmed": bot.case.is_area_confirmed(bot.selected_bereich_id),
            }

        is_odw = ContextBuilder.is_odw_case(bot.case)
        area_defs = OP_DE_WISCH_SECTION_DEFINITIONS if is_odw else SECTION_DEFINITIONS
        available_areas = [
            {
                "id": s.id,
                "title": s.title,
                "confirmed": bot.case.is_area_confirmed(s.id),
            }
            for s in area_defs
        ]
        template_name = "op-de-wisch" if is_odw else "hamburg"

        context_status_data = None
        if (
            bot.selected_bereich_id
            and bot.provider is not None
            and hasattr(bot.provider, "count_tokens")
            and callable(getattr(bot.provider, "count_tokens"))
        ):
            try:
                _, cs = bot.fit_context_for_prompt(user_message="", bereich_id=bot.selected_bereich_id)
                context_status_data = cs.to_dict()
            except Exception:
                if getattr(bot, "last_context_status", None):
                    context_status_data = bot.last_context_status.to_dict()
        elif getattr(bot, "last_context_status", None):
            context_status_data = bot.last_context_status.to_dict()

        return {
            "fall_id": bot.case.fall_id,
            "case_closed_reason": closed_reason,
            "model_error": bot.model_error,
            "is_heuristic": is_heuristic,
            "kv_clear_warning": kv_warning,
            "start_check_skipped": os.getenv("SOB_SKIP_START_CHECK") == "1",
            "has_csv": has_csv,
            "has_plan": has_plan,
            "plan_filename": plan_filename,
            "plan_goals_count": plan_goals_count,
            "csv": csv_info,
            "plan": plan_info,
            "model": bot.model_info,
            "template": template_name,
            "template_selected": bool(getattr(bot.case, "template_selected", False)),
            "selected_area": selected_area_info,
            "available_areas": available_areas,
            "context_status": context_status_data,
            "messages": bot.get_history(),
        }

    @app.post("/api/reset-chat")
    async def reset_chat() -> Dict[str, bool]:
        """Leert nur den Chatverlauf."""
        bot.clear_history()
        return {"success": True}

    @app.post("/api/reset-csv")
    async def reset_csv() -> Dict[str, bool]:
        """Entfernt die geladene CSV-Datei."""
        bot.set_csv(None)
        return {"success": True}

    @app.post("/api/reset")
    async def reset_all() -> Dict[str, bool]:
        """Setzt Chat, CSV und Vorbericht zurück."""
        bot.clear()
        return {"success": True}

    return app
