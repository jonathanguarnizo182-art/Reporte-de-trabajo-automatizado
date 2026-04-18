from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .calendar_utils import month_name_es
from .models import ReportTemplate


MONTHS_BY_NAME = {
    "ENERO": 1,
    "FEBRERO": 2,
    "MARZO": 3,
    "ABRIL": 4,
    "MAYO": 5,
    "JUNIO": 6,
    "JULIO": 7,
    "AGOSTO": 8,
    "SEPTIEMBRE": 9,
    "SETIEMBRE": 9,
    "OCTUBRE": 10,
    "NOVIEMBRE": 11,
    "DICIEMBRE": 12,
}


@dataclass(slots=True)
class ResolvedDocument:
    path: Path
    exists: bool
    created_from_template: bool


def normalize_name(value: str) -> str:
    value = value.upper()
    value = value.replace("_", " ")
    value = re.sub(r"[^\w\s&-]", " ", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def extract_company(value: str) -> str:
    upper = normalize_name(Path(value).stem)
    match = re.search(
        r"REPORTE DE SERVICIOS\s+(.+?)(?:\s+ENERO|\s+FEBRERO|\s+MARZO|\s+ABRIL|\s+MAYO|\s+JUNIO|\s+JULIO|\s+AGOSTO|\s+SEPTIEMBRE|\s+SETIEMBRE|\s+OCTUBRE|\s+NOVIEMBRE|\s+DICIEMBRE|$)",
        upper,
    )
    if not match:
        return ""
    company = match.group(1).strip(" _-")
    company = re.sub(r"\b20\d{2}\b", "", company)
    return re.sub(r"\s+", " ", company).strip(" _-")


def build_report_id(path: Path) -> str:
    stem = normalize_name(path.stem)
    stem = re.sub(
        r"\b(ENERO|FEBRERO|MARZO|ABRIL|MAYO|JUNIO|JULIO|AGOSTO|SEPTIEMBRE|SETIEMBRE|OCTUBRE|NOVIEMBRE|DICIEMBRE)\b",
        "",
        stem,
    )
    stem = re.sub(r"\b20\d{2}\b", "", stem)
    return re.sub(r"\s+", " ", stem).strip(" _-")


def parse_month_year_from_name(name: str) -> tuple[int | None, int | None]:
    upper = normalize_name(Path(name).stem)
    month = None
    year = None
    for token, month_number in MONTHS_BY_NAME.items():
        if re.search(rf"\b{token}\b", upper):
            month = month_number
            break
    year_match = re.search(r"\b(20\d{2})\b", upper)
    if year_match:
        year = int(year_match.group(1))
    return month, year


class ReportCatalog:
    def __init__(self, reports_dir: str | Path):
        self.reports_dir = Path(reports_dir)

    def scan(self) -> list[ReportTemplate]:
        templates: dict[str, ReportTemplate] = {}
        for path in sorted(self.reports_dir.glob("*.docx")):
            if path.name.startswith("~$"):
                continue
            report_id = build_report_id(path)
            company = extract_company(path.stem)
            display_name = company or path.stem
            template = ReportTemplate(
                report_id=report_id,
                family_name=report_id,
                display_name=display_name,
                template_path=path,
                company=company,
            )
            previous = templates.get(report_id)
            if previous is None or path.stat().st_mtime > previous.template_path.stat().st_mtime:
                templates[report_id] = template
        return sorted(templates.values(), key=lambda item: item.display_name.upper())

    def resolve_document(self, report: ReportTemplate, target_date: date) -> ResolvedDocument:
        for candidate in self.reports_dir.glob("*.docx"):
            if candidate.name.startswith("~$"):
                continue
            candidate_report_id = build_report_id(candidate)
            month, year = parse_month_year_from_name(candidate.name)
            if candidate_report_id == report.report_id and month == target_date.month and year == target_date.year:
                return ResolvedDocument(candidate, True, False)

        target_name = self._build_target_name(report.template_path, target_date)
        target_path = self.reports_dir / target_name
        shutil.copy2(report.template_path, target_path)
        return ResolvedDocument(target_path, False, True)

    def _build_target_name(self, template_path: Path, target_date: date) -> str:
        stem = build_report_id(template_path)
        month_name = month_name_es(target_date.month)
        return f"{stem}_{month_name}_{target_date.year}.docx"
