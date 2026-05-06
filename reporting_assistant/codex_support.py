from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Iterable

from .calendar_utils import calculate_effective_hours, classify_day
from .catalog import ClientCatalog, ReportCatalog, normalize_name
from .config import load_config
from .models import GeneratedEntry, ReportTemplate, TimeSegment
from .word_service import WordReportService


@dataclass(slots=True)
class CodexUpdateResult:
    client: str
    report_id: str
    report_display_name: str
    target_date: str
    resolved_document: str
    created_month_document: bool
    update_mode: str
    day_type: str
    business_day: bool
    segments: list[dict[str, str]]


def parse_date(value: str) -> date:
    normalized = value.strip().lower()
    today = datetime.now().date()
    if normalized in {"today", "hoy"}:
        return today
    if normalized in {"yesterday", "ayer"}:
        return today - timedelta(days=1)
    return datetime.strptime(value.strip(), "%Y-%m-%d").date()


def parse_segment(value: str) -> tuple[str, str]:
    raw = value.strip()
    for separator in ("-", ",", "|"):
        if separator in raw:
            start, end = [part.strip() for part in raw.split(separator, 1)]
            return start, end
    raise ValueError(
        f"Segmento invalido '{value}'. Use el formato HH:MM-HH:MM, por ejemplo 18:00-21:00."
    )


def resolve_report_selector(catalog: ReportCatalog, selector: str) -> ReportTemplate:
    documents = catalog.scan_documents()
    if not documents:
        raise RuntimeError("No se encontraron reportes Word en la carpeta configurada.")

    wanted = normalize_name(selector)
    exact = next(
        (
            document
            for document in documents
            if wanted in {
                normalize_name(document.report_id),
                normalize_name(document.display_name),
                normalize_name(document.template_path.stem),
                normalize_name(document.template_path.name),
            }
        ),
        None,
    )
    if exact:
        return exact

    partial = [
        document
        for document in documents
        if wanted in normalize_name(document.display_name)
        or wanted in normalize_name(document.report_id)
        or wanted in normalize_name(document.template_path.name)
    ]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        options = ", ".join(document.template_path.name for document in partial)
        raise RuntimeError(f"El selector '{selector}' coincide con varios reportes. Opciones: {options}")

    available = ", ".join(document.template_path.name for document in documents)
    raise RuntimeError(f"No se encontro el reporte '{selector}'. Disponibles: {available}")


def build_segments(
    target_date: date,
    regular_start: str,
    regular_end: str,
    rest_minutes: int,
    overtime_segments: Iterable[str],
) -> list[TimeSegment]:
    segments = [
        TimeSegment(
            start=regular_start,
            end=regular_end,
            effective=calculate_effective_hours(regular_start, regular_end, rest_minutes=rest_minutes),
            label="jornada",
        )
    ]
    for raw_segment in overtime_segments:
        start, end = parse_segment(raw_segment)
        segments.append(
            TimeSegment(
                start=start,
                end=end,
                effective=calculate_effective_hours(start, end, rest_minutes=0),
                label="extra",
            )
        )
    return segments


def build_generated_entry(activity_text: str) -> GeneratedEntry:
    clean_text = activity_text.strip()
    if not clean_text:
        raise RuntimeError("El texto del reporte esta vacio.")
    return GeneratedEntry(
        title="",
        summary="",
        numbered_sections=[],
        full_text=clean_text,
        raw_model_response="direct",
    )


def codex_date_selector_help(reference_date: date | None = None) -> str:
    today = reference_date or datetime.now().date()
    yesterday = today - timedelta(days=1)
    return (
        "Seleccione la fecha del reporte:\n"
        f"1. Hoy ({today.isoformat()})\n"
        f"2. Ayer ({yesterday.isoformat()})\n"
        "3. Otra fecha (YYYY-MM-DD)"
    )


def codex_style_guide() -> str:
    return (
        "El reporte se guarda de forma directa: escriba exactamente el texto que debe quedar en Word. "
        "Puede usar **texto** para aplicar negrilla en el documento."
    )


def update_report_from_codex(
    client: str,
    report_selector: str,
    target_date: str,
    activity_text: str,
    regular_start: str,
    regular_end: str,
    rest_minutes: int = 60,
    overtime_segments: Iterable[str] | None = None,
    day_type: str | None = None,
    update_mode: str = "replace",
    reports_dir: str | None = None,
    clients_root: str | None = None,
) -> CodexUpdateResult:
    config = load_config()
    target = parse_date(target_date)
    classification = classify_day(target, config.country_holidays)
    final_day_type = (day_type or classification.kind).strip()
    segments = build_segments(
        target,
        regular_start=regular_start,
        regular_end=regular_end,
        rest_minutes=rest_minutes,
        overtime_segments=overtime_segments or [],
    )

    if reports_dir:
        reports_path = reports_dir
        client_name = client or ""
    else:
        selected_client = ClientCatalog(clients_root or config.clients_root).resolve(client)
        reports_path = selected_client.reports_dir
        client_name = selected_client.name

    catalog = ReportCatalog(reports_path)
    report = resolve_report_selector(catalog, report_selector)
    resolved = catalog.resolve_document(report, target)

    service = WordReportService()
    if resolved.created_from_template:
        service.prepare_new_month_document(resolved, target)

    service.write_day_block(
        resolved.path,
        target,
        build_generated_entry(activity_text),
        segments,
        update_mode=update_mode,
    )

    return CodexUpdateResult(
        client=client_name,
        report_id=report.report_id,
        report_display_name=report.display_name,
        target_date=target.strftime("%d/%m/%Y"),
        resolved_document=str(resolved.path),
        created_month_document=resolved.created_from_template,
        update_mode=update_mode,
        day_type=final_day_type,
        business_day=classification.is_business_day,
        segments=[asdict(segment) for segment in segments],
    )


def result_to_json(result: CodexUpdateResult) -> str:
    return json.dumps(asdict(result), indent=2, ensure_ascii=False)
