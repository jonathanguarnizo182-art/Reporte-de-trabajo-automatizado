from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta

try:
    import holidays  # type: ignore
except ImportError:  # pragma: no cover
    holidays = None


SPANISH_MONTHS = {
    1: "ENERO",
    2: "FEBRERO",
    3: "MARZO",
    4: "ABRIL",
    5: "MAYO",
    6: "JUNIO",
    7: "JULIO",
    8: "AGOSTO",
    9: "SEPTIEMBRE",
    10: "OCTUBRE",
    11: "NOVIEMBRE",
    12: "DICIEMBRE",
}


@dataclass(slots=True)
class DayClassification:
    kind: str
    is_business_day: bool
    holiday_name: str = ""


def month_name_es(month: int) -> str:
    return SPANISH_MONTHS[month]


def classify_day(target: date, country_code: str = "CO") -> DayClassification:
    holiday_name = ""
    is_holiday = False
    if holidays is not None:
        holiday_map = holidays.country_holidays(country_code, years=[target.year])
        if target in holiday_map:
            is_holiday = True
            holiday_name = str(holiday_map.get(target))

    if is_holiday:
        return DayClassification("festivo", False, holiday_name)
    if target.weekday() == 5:
        return DayClassification("sábado", False)
    if target.weekday() == 6:
        return DayClassification("domingo", False)
    return DayClassification("hábil", True)


def default_schedule_for_day(target: date) -> tuple[str, str]:
    if target.weekday() <= 2:
        return "08:00", "18:00"
    if target.weekday() <= 4:
        return "08:00", "17:30"
    return "08:00", "17:30"


def parse_hhmm(value: str) -> datetime:
    return datetime.strptime(value.strip(), "%H:%M")


def format_duration(delta: timedelta) -> str:
    total_minutes = int(delta.total_seconds() // 60)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours:02d}:{minutes:02d}h"


def calculate_effective_hours(start: str, end: str, rest_minutes: int = 0) -> str:
    start_dt = parse_hhmm(start)
    end_dt = parse_hhmm(end)
    if end_dt < start_dt:
        end_dt += timedelta(days=1)
    duration = end_dt - start_dt - timedelta(minutes=rest_minutes)
    if duration.total_seconds() < 0:
        duration = timedelta()
    return format_duration(duration)


def business_days_for_month(year: int, month: int, country_code: str = "CO") -> list[date]:
    _, days_in_month = calendar.monthrange(year, month)
    output: list[date] = []
    for day in range(1, days_in_month + 1):
        current = date(year, month, day)
        if classify_day(current, country_code).is_business_day:
            output.append(current)
    return output


def month_last_day(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]

