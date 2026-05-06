from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path


@dataclass(slots=True)
class TimeSegment:
    start: str
    end: str
    effective: str
    label: str = ""


@dataclass(slots=True)
class BitrixTimeEntry:
    target_date: date
    start: str
    hours: int
    minutes: int
    comment: str
    source_label: str = ""

    @property
    def duration_text(self) -> str:
        return f"{self.hours:02d}:{self.minutes:02d}"


@dataclass(slots=True)
class ClientProject:
    name: str
    root_path: Path
    reports_dir: Path


@dataclass(slots=True)
class ReportTemplate:
    report_id: str
    family_name: str
    display_name: str
    template_path: Path
    company: str = ""
    project_code: str = ""
    service_mode: str = "remoto"


@dataclass(slots=True)
class NewReportMetadata:
    company: str
    project_code: str
    function: str
    reporter: str
    city: str
    received_by: str = ""


@dataclass(slots=True)
class GeneratedEntry:
    title: str
    summary: str
    numbered_sections: list[str]
    full_text: str
    raw_model_response: str = ""
    bold_ranges: list[tuple[int, int]] = field(default_factory=list)


@dataclass(slots=True)
class WorkdayPayload:
    report: ReportTemplate
    target_date: date
    is_business_day: bool
    day_type: str
    segments: list[TimeSegment]
    had_overtime: bool
    notes: str
    transcript: str
    update_mode: str = "replace"
    existing_text: str = ""
    context: dict[str, str] = field(default_factory=dict)
