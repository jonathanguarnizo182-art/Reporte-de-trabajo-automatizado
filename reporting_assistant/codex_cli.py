from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from reporting_assistant.codex_support import result_to_json, update_report_from_codex
else:
    from .codex_support import result_to_json, update_report_from_codex


def _read_activity_text(activity_text: str | None, activity_file: str | None) -> str:
    if activity_file:
        return Path(activity_file).read_text(encoding="utf-8").strip()
    if activity_text:
        return activity_text.strip()
    stdin_text = sys.stdin.read().strip()
    if stdin_text:
        return stdin_text
    raise RuntimeError("Debe enviar el texto del reporte con --activity-text, --activity-file o por stdin.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Actualiza un reporte Word con texto directo.")
    parser.add_argument("--client", default="", help="Cliente dentro de la carpeta CLIENTES.")
    parser.add_argument("--report-id", required=True, help="ID, nombre o archivo del reporte a actualizar.")
    parser.add_argument(
        "--target-date",
        required=True,
        help="Fecha objetivo en formato YYYY-MM-DD, o los atajos today/hoy/yesterday/ayer.",
    )
    parser.add_argument("--day-type", default="", help="Tipo de dia: habil, sabado, domingo o festivo.")
    parser.add_argument("--regular-start", required=True, help="Hora inicio jornada, formato HH:MM.")
    parser.add_argument("--regular-end", required=True, help="Hora fin jornada, formato HH:MM.")
    parser.add_argument("--rest-minutes", type=int, default=60, help="Minutos de descanso a descontar.")
    parser.add_argument(
        "--overtime-segment",
        action="append",
        default=[],
        help="Segmento de horas extra en formato HH:MM-HH:MM. Puede repetirse.",
    )
    parser.add_argument("--activity-text", default="", help="Texto directo para el bloque diario.")
    parser.add_argument("--activity-file", default="", help="Archivo UTF-8 con el texto directo del reporte.")
    parser.add_argument(
        "--append",
        action="store_true",
        help="Agrega informacion a una fecha existente en lugar de reemplazar.",
    )
    parser.add_argument(
        "--reports-dir",
        default="",
        help="Carpeta directa de reportes Word. Si se omite, usa --client.",
    )
    parser.add_argument("--clients-root", default="", help="Carpeta raiz de clientes.")
    args = parser.parse_args()

    if not args.client and not args.reports_dir:
        raise RuntimeError("Debe enviar --client o --reports-dir.")

    result = update_report_from_codex(
        client=args.client,
        report_selector=args.report_id,
        target_date=args.target_date,
        activity_text=_read_activity_text(args.activity_text, args.activity_file),
        regular_start=args.regular_start,
        regular_end=args.regular_end,
        rest_minutes=args.rest_minutes,
        overtime_segments=args.overtime_segment,
        day_type=args.day_type,
        update_mode="append" if args.append else "replace",
        reports_dir=args.reports_dir or None,
        clients_root=args.clients_root or None,
    )
    print(result_to_json(result))


if __name__ == "__main__":
    main()
