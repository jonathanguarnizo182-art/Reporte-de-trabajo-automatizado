from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from .calendar_utils import month_name_es
from .models import ClientProject, NewReportMetadata, ReportTemplate


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


@dataclass(slots=True)
class CreatedReportDocument:
    path: Path
    template_path: Path


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
    company = re.sub(r"\s+", " ", company).strip(" _-")
    if normalize_name(company) in MONTHS_BY_NAME:
        return ""
    return company


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

    def scan_documents(self) -> list[ReportTemplate]:
        documents: list[ReportTemplate] = []
        for path in sorted(self.reports_dir.glob("*.docx")):
            if path.name.startswith("~$"):
                continue
            report_id = build_report_id(path)
            company = extract_company(path.stem)
            month, year = parse_month_year_from_name(path.name)
            suffix = f" - {month_name_es(month)} {year}" if month and year else ""
            documents.append(
                ReportTemplate(
                    report_id=report_id,
                    family_name=report_id,
                    display_name=f"{path.stem}{suffix}",
                    template_path=path,
                    company=company,
                    project_code=extract_report_metadata(path).project_code,
                )
            )
        return documents

    def resolve_document(self, report: ReportTemplate, target_date: date) -> ResolvedDocument:
        for candidate in self.reports_dir.glob("*.docx"):
            if candidate.name.startswith("~$"):
                continue
            candidate_report_id = build_report_id(candidate)
            month, year = parse_month_year_from_name(candidate.name)
            if candidate_report_id == report.report_id and month == target_date.month and (year == target_date.year or year is None):
                return ResolvedDocument(candidate, True, False)

        target_name = self._build_target_name(report.template_path, target_date)
        target_path = self.reports_dir / target_name
        shutil.copy2(report.template_path, target_path)
        return ResolvedDocument(target_path, False, True)

    def _build_target_name(self, template_path: Path, target_date: date) -> str:
        stem = build_report_id(template_path)
        month_name = month_name_es(target_date.month)
        return f"{stem}_{month_name}_{target_date.year}.docx"

    def create_month_report(
        self,
        target_date: date,
        company: str,
        template_path: str | Path | None = None,
    ) -> CreatedReportDocument:
        source = Path(template_path) if template_path else find_template_source(self.reports_dir)
        if not source or not source.exists():
            raise RuntimeError("No se encontró una plantilla .docx para crear el reporte.")
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        target_path = self.reports_dir / build_month_report_name(company, target_date)
        if target_path.exists():
            raise RuntimeError(f"El reporte ya existe: {target_path.name}")
        shutil.copy2(source, target_path)
        return CreatedReportDocument(target_path, source)


class ClientCatalog:
    def __init__(self, clients_root: str | Path):
        self.clients_root = Path(clients_root)

    def scan(self) -> list[ClientProject]:
        clients: list[ClientProject] = []
        if not self.clients_root.exists():
            return clients
        for path in sorted(self.clients_root.iterdir()):
            if not path.is_dir():
                continue
            reports_dir = path / "Avance de servicio"
            if reports_dir.is_dir():
                clients.append(ClientProject(path.name, path, reports_dir))
        return clients

    def resolve(self, selector: str) -> ClientProject:
        wanted = normalize_name(selector)
        clients = self.scan()
        for client in clients:
            if normalize_name(client.name) == wanted or normalize_name(client.name.replace("_", " ")) == wanted:
                return client
        partial = [
            client
            for client in clients
            if wanted in normalize_name(client.name) or wanted in normalize_name(client.name.replace("_", " "))
        ]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            options = ", ".join(client.name for client in partial)
            raise RuntimeError(f"El cliente '{selector}' coincide con varios clientes: {options}")
        available = ", ".join(client.name for client in clients) or "(ninguno)"
        raise RuntimeError(f"No se encontró el cliente '{selector}'. Disponibles: {available}")

    def resolve_or_create(self, selector: str) -> ClientProject:
        try:
            return self.resolve(selector)
        except RuntimeError:
            folder_name = build_client_folder_name(selector)
            root_path = self.clients_root / folder_name
            reports_dir = root_path / "Avance de servicio"
            reports_dir.mkdir(parents=True, exist_ok=True)
            return ClientProject(folder_name, root_path, reports_dir)


def build_month_report_name(company: str, target_date: date) -> str:
    clean_company = normalize_name(company) or "CLIENTE"
    month = month_name_es(target_date.month)
    return f"A&CI R 03 REPORTE DE SERVICIOS {clean_company} {month} {target_date.year}.docx"


def build_client_folder_name(company: str) -> str:
    clean = normalize_name(company)
    clean = re.sub(r"\s+", "_", clean)
    return clean or "CLIENTE"


def find_template_source(reports_dir: str | Path, clients_root: str | Path | None = None) -> Path | None:
    candidates: list[Path] = []
    reports_path = Path(reports_dir)
    if reports_path.exists():
        candidates.extend(path for path in reports_path.glob("*.docx") if not path.name.startswith("~$"))
    root = Path(clients_root) if clients_root else reports_path.parent.parent
    if root.exists():
        candidates.extend(
            path
            for path in root.glob("*/Avance de servicio/*.docx")
            if not path.name.startswith("~$")
        )
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def extract_report_metadata(path: str | Path) -> NewReportMetadata:
    path = Path(path)
    try:
        with ZipFile(path) as archive:
            xml = archive.read("word/document.xml")
    except Exception:
        return NewReportMetadata(extract_company(path.stem), "", "", "", "")

    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return NewReportMetadata(extract_company(path.stem), "", "", "", "")

    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    cells: list[str] = []
    for table in root.findall(".//w:tbl", ns):
        for row in table.findall("./w:tr", ns):
            for cell in row.findall("./w:tc", ns):
                text = "".join(node.text or "" for node in cell.findall(".//w:t", ns))
                cells.append(" ".join(text.split()))

    def value_after(label: str) -> str:
        wanted = normalize_name(label)
        for index, cell in enumerate(cells[:-1]):
            if normalize_name(cell) == wanted:
                return cells[index + 1].strip()
        label_text = label.rstrip(":").strip()
        for cell in cells:
            match = re.match(rf"^{re.escape(label_text)}\s*:?\s*(.+)$", cell, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return ""

    def signature_received_by() -> str:
        for table in root.findall(".//w:tbl", ns):
            table_text = " ".join(
                "".join(node.text or "" for node in table.findall(".//w:t", ns)).split()
            )
            if "REALIZADO POR" not in normalize_name(table_text) or "RECIBIDO POR" not in normalize_name(table_text):
                continue
            for row in table.findall("./w:tr", ns):
                row_cells = []
                for cell in row.findall("./w:tc", ns):
                    text = "".join(node.text or "" for node in cell.findall(".//w:t", ns))
                    row_cells.append(" ".join(text.split()))
                name_indexes = [index for index, cell in enumerate(row_cells) if normalize_name(cell) == "NOMBRE"]
                if len(name_indexes) >= 2 and name_indexes[-1] + 1 < len(row_cells):
                    return row_cells[name_indexes[-1] + 1].strip()
        return ""

    return NewReportMetadata(
        company=value_after("Empresa:") or extract_company(path.stem),
        project_code=value_after("Código Proyecto:") or value_after("Codigo Proyecto:"),
        function=value_after("Función:") or value_after("Funcion:"),
        reporter=value_after("Persona que Reporta:"),
        city=value_after("Ciudad de ejecución:") or value_after("Ciudad de ejecucion:"),
        received_by=value_after("Recibido por:") or signature_received_by(),
    )
