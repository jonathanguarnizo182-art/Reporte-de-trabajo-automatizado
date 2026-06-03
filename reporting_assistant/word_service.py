from __future__ import annotations

import copy
import re
import shutil
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from .calendar_utils import calculate_effective_hours, default_schedule_for_day, month_last_day
from .catalog import ResolvedDocument
from .models import BitrixReportPayload, BitrixTimeEntry, GeneratedEntry, NewReportMetadata, TimeSegment

try:
    import pythoncom  # type: ignore
    import win32com.client  # type: ignore
except ImportError:  # pragma: no cover
    pythoncom = None
    win32com = None


class WordUnavailableError(RuntimeError):
    pass


class WordReportService:
    W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    NS = {"w": W_NS}

    def __init__(self):
        if win32com is None:
            raise WordUnavailableError("Instale pywin32 para habilitar Word COM.")

    def lock_file_for(self, path: Path) -> Path:
        return path.with_name(f"~${path.name[2:]}")

    def lock_file_exists(self, path: Path) -> bool:
        return self.lock_file_for(path).exists()

    @contextmanager
    def open_document(self, path: Path, visible: bool = False):
        if pythoncom is not None:
            pythoncom.CoInitialize()
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = visible
        word.DisplayAlerts = 0
        doc = None
        try:
            doc = word.Documents.Open(
                str(path),
                ReadOnly=False,
                AddToRecentFiles=False,
                ConfirmConversions=False,
                NoEncodingDialog=True,
            )
            yield word, doc
        finally:
            try:
                if doc is not None:
                    doc.Close(SaveChanges=True)
            finally:
                word.Quit()
                if pythoncom is not None:
                    pythoncom.CoUninitialize()

    def prepare_new_month_document(self, resolved: ResolvedDocument, target_date: date) -> None:
        if not resolved.created_from_template:
            return
        with self.open_document(resolved.path) as (_, doc):
            self._update_header_date(doc, target_date)
            daily_indexes = self._daily_tables(doc)
            if not daily_indexes:
                return
            keep = daily_indexes[0]
            for table_index in reversed(daily_indexes[1:]):
                doc.Tables.Item(table_index).Delete()
            self._clear_day_table(doc.Tables.Item(keep))

    def prepare_full_month_document(
        self,
        path: Path,
        target_date: date,
        metadata: NewReportMetadata,
        country_code: str = "CO",
        rest_minutes: int = 60,
    ) -> None:
        root = self._read_document_xml(path)
        self._update_header_date_xml(root, target_date)
        self._update_report_metadata_xml(root, metadata)
        self._replace_daily_tables_for_month_xml(root, target_date, rest_minutes)
        self._write_document_xml(path, root)

    def entry_exists(self, path: Path, target_date: date) -> tuple[bool, str]:
        return self._entry_exists_direct(path, target_date)

    def list_bitrix_time_entries(self, path: Path) -> list[BitrixTimeEntry]:
        root = self._read_document_xml(path)
        entries: list[BitrixTimeEntry] = []
        for table in self._daily_tables_xml(root):
            body = self._xml_cell_text(table, 3, 1).strip()
            if not body:
                continue
            raw_date = self._xml_cell_text(table, 1, 2).strip()
            try:
                target_date = datetime.strptime(raw_date, "%d/%m/%Y").date()
            except ValueError:
                continue
            starts = [value.strip() for value in self._xml_cell_text(table, 1, 4).splitlines() if value.strip()]
            ends = [value.strip() for value in self._xml_cell_text(table, 1, 6).splitlines() if value.strip()]
            effective_values = [value.strip() for value in self._xml_cell_text(table, 1, 8).splitlines() if value.strip()]
            if not effective_values:
                continue
            if len(effective_values) > len(starts) or len(effective_values) > len(ends):
                raise RuntimeError(
                    f"La fecha {raw_date} tiene mas duraciones que horas de inicio o fin. "
                    "Revise HORA INICIO, HORA FIN y HORAS EFECTIVAS."
                )
            segments: list[TimeSegment] = []
            total_minutes = 0
            for index, effective in enumerate(effective_values):
                duration = self._parse_effective_duration(effective)
                if duration is None:
                    continue
                start = starts[index]
                end = ends[index]
                segments.append(TimeSegment(start=start, end=end, effective=effective, label=f"tramo {index + 1}"))
                total_minutes += duration[0] * 60 + duration[1]
            if not segments:
                continue
            entries.append(
                BitrixTimeEntry(
                    target_date=target_date,
                    start=segments[0].start,
                    hours=total_minutes // 60,
                    minutes=total_minutes % 60,
                    comment=body,
                    source_label=f"{raw_date} {segments[0].start}",
                    end=segments[-1].end,
                    segments=segments,
                )
            )
        return entries

    def bitrix_report_payload(self, path: Path) -> BitrixReportPayload:
        root = self._read_document_xml(path)
        project_code = self._project_code_xml(root)
        return BitrixReportPayload(project_code=project_code, entries=self.list_bitrix_time_entries(path))

    def write_day_block(
        self,
        path: Path,
        target_date: date,
        entry: GeneratedEntry,
        segments: list[TimeSegment],
        update_mode: str = "replace",
    ) -> None:
        self._write_day_block_direct(path, target_date, entry, segments, update_mode)

    def _entry_exists_direct(self, path: Path, target_date: date) -> tuple[bool, str]:
        root = self._read_document_xml(path)
        target = target_date.strftime("%d/%m/%Y")
        for table in self._daily_tables_xml(root):
            if self._xml_cell_text(table, 1, 2) == target:
                return True, self._xml_cell_text(table, 3, 1)
        return False, ""

    def _parse_effective_duration(self, value: str) -> tuple[int, int] | None:
        match = re.search(r"(\d{1,3})\s*:\s*(\d{2})", value)
        if match:
            return int(match.group(1)), int(match.group(2))
        hours_match = re.search(r"(\d{1,3})\s*h", value, flags=re.IGNORECASE)
        minutes_match = re.search(r"(\d{1,3})\s*m", value, flags=re.IGNORECASE)
        if hours_match or minutes_match:
            return (
                int(hours_match.group(1)) if hours_match else 0,
                int(minutes_match.group(1)) if minutes_match else 0,
            )
        return None

    def _write_day_block_direct(
        self,
        path: Path,
        target_date: date,
        entry: GeneratedEntry,
        segments: list[TimeSegment],
        update_mode: str,
    ) -> None:
        root = self._read_document_xml(path)
        table = self._find_or_create_day_table_xml(root, target_date)
        if update_mode == "append":
            existing = self._xml_cell_text(table, 3, 1).strip()
            body = f"{existing}\n\n{entry.full_text}".strip() if existing else entry.full_text
            bold_ranges = self._shift_ranges(entry.bold_ranges, len(body) - len(entry.full_text))
            self._fill_day_table_xml(table, target_date, body, segments, merge_segments=True, bold_ranges=bold_ranges)
        else:
            self._fill_day_table_xml(table, target_date, entry.full_text, segments, bold_ranges=entry.bold_ranges)
        self._write_document_xml(path, root)

    def _read_document_xml(self, path: Path):
        with ZipFile(path) as archive:
            xml = archive.read("word/document.xml")
        return ET.fromstring(xml)

    def _write_document_xml(self, path: Path, root) -> None:
        xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as tmp:
            tmp_path = Path(tmp.name)
        try:
            with ZipFile(path, "r") as source, ZipFile(tmp_path, "w", ZIP_DEFLATED) as target:
                for item in source.infolist():
                    data = xml if item.filename == "word/document.xml" else source.read(item.filename)
                    target.writestr(item, data)
            shutil.move(str(tmp_path), str(path))
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def _update_header_date_xml(self, root, target_date: date) -> None:
        tables = root.findall(".//w:tbl", self.NS)
        if not tables:
            raise RuntimeError("El documento no tiene tabla de encabezado.")
        header = tables[0]
        self._set_xml_cell_text(header, 1, 2, f"{month_last_day(target_date.year, target_date.month):02d}")
        self._set_xml_cell_text(header, 1, 3, f"{target_date.month:02d}")
        self._set_xml_cell_text(header, 1, 4, str(target_date.year))

    def _update_report_metadata_xml(self, root, metadata: NewReportMetadata) -> None:
        tables = root.findall(".//w:tbl", self.NS)
        if len(tables) < 2:
            return
        details = tables[1]
        updates = [
            ("Empresa:", metadata.company),
            ("Código Proyecto:", metadata.project_code),
            ("Funcion:", metadata.function),
            ("Función:", metadata.function),
            ("Persona que Reporta:", metadata.reporter),
            ("Ciudad de ejecucion:", metadata.city),
            ("Ciudad de ejecución:", metadata.city),
        ]
        for label, value in updates:
            self._set_xml_value_after_label(details, label, value)
        self._remove_received_by_rows_from_metadata_xml(details)
        self._update_signature_received_by_xml(root, metadata.received_by)
        if metadata.project_code:
            try:
                self._set_xml_cell_text(tables[0], 1, 6, metadata.project_code)
            except Exception:
                pass

    def _replace_daily_tables_for_month_xml(self, root, target_date: date, rest_minutes: int) -> None:
        daily_tables = self._daily_tables_xml(root)
        if not daily_tables:
            raise RuntimeError("El documento no tiene una tabla diaria base.")
        body = root.find(".//w:body", self.NS)
        if body is None:
            raise RuntimeError("No se pudo encontrar el cuerpo del documento Word.")

        template = copy.deepcopy(daily_tables[0])
        children = list(body)
        daily_ids = {id(table) for table in daily_tables}
        insert_at = next((index for index, child in enumerate(children) if id(child) in daily_ids), len(children))
        separator_template = self._daily_separator_after_first_table(children, daily_ids)
        removal_ids = self._daily_table_and_separator_ids(children, daily_ids)
        for child in children:
            if id(child) in removal_ids:
                body.remove(child)

        cursor = insert_at
        for current_day in self._all_days_for_month(target_date.year, target_date.month):
            table = copy.deepcopy(template)
            self._clear_day_table_xml(table)
            self._fill_day_table_xml(
                table,
                current_day,
                "",
                self._default_segments_for_date(current_day, rest_minutes),
            )
            body.insert(cursor, table)
            cursor += 1
            body.insert(cursor, copy.deepcopy(separator_template))
            cursor += 1

    def _all_days_for_month(self, year: int, month: int) -> list[date]:
        return [date(year, month, day) for day in range(1, month_last_day(year, month) + 1)]

    def _daily_separator_after_first_table(self, children: list, daily_ids: set[int]):
        for index, child in enumerate(children[:-1]):
            if id(child) in daily_ids and self._is_empty_paragraph(children[index + 1]):
                return copy.deepcopy(children[index + 1])
        return ET.Element(f"{{{self.W_NS}}}p")

    def _daily_table_and_separator_ids(self, children: list, daily_ids: set[int]) -> set[int]:
        removal_ids = set(daily_ids)
        for index, child in enumerate(children[:-1]):
            if id(child) in daily_ids and self._is_empty_paragraph(children[index + 1]):
                removal_ids.add(id(children[index + 1]))
        return removal_ids

    def _is_empty_paragraph(self, element) -> bool:
        return element.tag == f"{{{self.W_NS}}}p" and not self._xml_element_text(element).strip()

    def _daily_tables_xml(self, root) -> list:
        output = []
        for table in root.findall(".//w:tbl", self.NS):
            text = self._xml_table_text(table)
            if "FECHA:" in text and "ACTIVIDAD REALIZADA:" in text:
                output.append(table)
        return output

    def _xml_table_text(self, table) -> str:
        return " ".join(self._xml_element_text(table).split())

    def _xml_element_text(self, element) -> str:
        return "".join(node.text or "" for node in element.findall(".//w:t", self.NS))

    def _xml_rows(self, table) -> list:
        return table.findall("./w:tr", self.NS)

    def _xml_cell(self, table, row: int, column: int):
        rows = self._xml_rows(table)
        return rows[row - 1].findall("./w:tc", self.NS)[column - 1]

    def _xml_cell_text(self, table, row: int, column: int) -> str:
        try:
            cell = self._xml_cell(table, row, column)
        except IndexError:
            return ""
        parts = []
        for paragraph in cell.findall("./w:p", self.NS):
            text = "".join(node.text or "" for node in paragraph.findall(".//w:t", self.NS))
            if text:
                parts.append(text)
        return "\n".join(parts).strip()

    def _project_code_xml(self, root) -> str:
        tables = root.findall(".//w:tbl", self.NS)
        for label in ("Código Proyecto:", "Codigo Proyecto:"):
            for table in tables:
                value = self._xml_value_after_label(table, label)
                if value:
                    return value
        for table in tables[:1]:
            value = self._xml_value_after_label(table, "REQUERIMIENTO:")
            if value:
                return value
        return ""

    def _xml_value_after_label(self, table, label: str) -> str:
        normalized_label = self._normalize_label(label)
        inline_prefix = self._normalize_label(label.rstrip(":"))
        for row_index, row in enumerate(self._xml_rows(table), start=1):
            cells = row.findall("./w:tc", self.NS)
            for column_index, _cell in enumerate(cells, start=1):
                cell_text = " ".join(self._xml_cell_text(table, row_index, column_index).split())
                normalized_cell = self._normalize_label(cell_text)
                if normalized_cell == normalized_label and column_index < len(cells):
                    return self._xml_cell_text(table, row_index, column_index + 1).strip()
                if normalized_cell.startswith(inline_prefix) and ":" in cell_text:
                    return cell_text.split(":", 1)[1].strip()
        return ""

    def _set_xml_value_after_label(self, table, label: str, value: str) -> bool:
        if value is None:
            return False
        value = value.strip()
        normalized_label = self._normalize_label(label)
        inline_prefix = self._normalize_label(label.rstrip(":"))
        for row_index, row in enumerate(self._xml_rows(table), start=1):
            cells = row.findall("./w:tc", self.NS)
            for column_index, cell in enumerate(cells, start=1):
                cell_text = " ".join(self._xml_cell_text(table, row_index, column_index).split())
                normalized_cell = self._normalize_label(cell_text)
                if normalized_cell == normalized_label and column_index < len(cells):
                    self._set_xml_cell_text(table, row_index, column_index + 1, value)
                    return True
                if normalized_cell.startswith(inline_prefix) and ":" in cell_text:
                    self._set_xml_cell_text(table, row_index, column_index, f"{label.rstrip(':')}: {value}".strip())
                    return True
        return False

    def _remove_received_by_rows_from_metadata_xml(self, table) -> None:
        for row in list(self._xml_rows(table)):
            row_text = self._normalize_label(self._xml_element_text(row))
            if "recibido por" in row_text:
                table.remove(row)

    def _update_signature_received_by_xml(self, root, value: str) -> None:
        if not value.strip():
            return
        for table in root.findall(".//w:tbl", self.NS):
            table_text = self._normalize_label(self._xml_table_text(table))
            if "realizado por" not in table_text or "recibido por" not in table_text:
                continue
            for row_index, row in enumerate(self._xml_rows(table), start=1):
                cells = row.findall("./w:tc", self.NS)
                name_columns = [
                    column_index
                    for column_index, _cell in enumerate(cells, start=1)
                    if self._normalize_label(self._xml_cell_text(table, row_index, column_index)) == "nombre"
                ]
                if len(name_columns) >= 2 and name_columns[-1] < len(cells):
                    self._set_xml_cell_text(table, row_index, name_columns[-1] + 1, value.strip())
                    return

    def _normalize_label(self, value: str) -> str:
        value = unicodedata.normalize("NFKD", value or "")
        value = "".join(char for char in value if not unicodedata.combining(char))
        value = value.lower().strip()
        value = re.sub(r"\s+", " ", value)
        return value.rstrip(":").strip()

    def _set_xml_cell_text(
        self,
        table,
        row: int,
        column: int,
        text: str,
        formatted: bool = False,
        bold_ranges: list[tuple[int, int]] | None = None,
    ) -> None:
        cell = self._xml_cell(table, row, column)
        properties = cell.find("./w:tcPr", self.NS)
        for child in list(cell):
            if child is not properties:
                cell.remove(child)
        if properties is None:
            properties = ET.Element(f"{{{self.W_NS}}}tcPr")
            cell.insert(0, properties)
        clean_text, markdown_ranges, remapped_ranges = self._strip_bold_markers_with_ranges(text, bold_ranges or [])
        if clean_text != text:
            bold_ranges = [*remapped_ranges, *markdown_ranges]
            text = clean_text
        bold_ranges = self._merge_ranges(bold_ranges or [])
        lines = text.splitlines() or [""]
        offset = 0
        for index, line in enumerate(lines):
            paragraph = ET.SubElement(cell, f"{{{self.W_NS}}}p")
            self._add_compact_paragraph_properties(paragraph)
            line_ranges = self._ranges_for_line(bold_ranges, offset, len(line))
            if formatted and self._line_should_be_bold(index, line):
                line_ranges = self._merge_ranges([*line_ranges, (0, len(line))])
            self._append_text_runs(paragraph, line, line_ranges)
            offset += len(line) + 1

    def _add_compact_paragraph_properties(self, paragraph) -> None:
        properties = ET.SubElement(paragraph, f"{{{self.W_NS}}}pPr")
        spacing = ET.SubElement(properties, f"{{{self.W_NS}}}spacing")
        spacing.set(f"{{{self.W_NS}}}before", "0")
        spacing.set(f"{{{self.W_NS}}}after", "0")
        spacing.set(f"{{{self.W_NS}}}line", "240")
        spacing.set(f"{{{self.W_NS}}}lineRule", "auto")

    def _append_text_runs(self, paragraph, text: str, bold_ranges: list[tuple[int, int]] | None = None) -> None:
        bold_ranges = self._merge_ranges(bold_ranges or [])
        if not bold_ranges:
            self._append_run(paragraph, text, False)
            return
        cursor = 0
        for start, end in bold_ranges:
            if start > cursor:
                self._append_run(paragraph, text[cursor:start], False)
            self._append_run(paragraph, text[start:end], True)
            cursor = end
        if cursor < len(text):
            self._append_run(paragraph, text[cursor:], False)

    def _append_run(self, paragraph, text: str, bold: bool) -> None:
        run = ET.SubElement(paragraph, f"{{{self.W_NS}}}r")
        if bold:
            props = ET.SubElement(run, f"{{{self.W_NS}}}rPr")
            ET.SubElement(props, f"{{{self.W_NS}}}b")
        text_node = ET.SubElement(run, f"{{{self.W_NS}}}t")
        if text.startswith(" ") or text.endswith(" "):
            text_node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        text_node.text = text

    def _line_should_be_bold(self, index: int, line: str) -> bool:
        stripped = line.strip()
        return bool(stripped and (index == 0 or re.match(r"^\d+\)", stripped)))

    def _ranges_for_line(self, ranges: list[tuple[int, int]], offset: int, line_length: int) -> list[tuple[int, int]]:
        output = []
        line_start = offset
        line_end = offset + line_length
        for start, end in ranges:
            clipped_start = max(start, line_start)
            clipped_end = min(end, line_end)
            if clipped_end > clipped_start:
                output.append((clipped_start - line_start, clipped_end - line_start))
        return output

    def _strip_bold_markers_with_ranges(
        self,
        text: str,
        external_ranges: list[tuple[int, int]],
    ) -> tuple[str, list[tuple[int, int]], list[tuple[int, int]]]:
        clean_parts: list[str] = []
        ranges: list[tuple[int, int]] = []
        index_map = [0] * (len(text) + 1)
        cursor = 0
        while cursor < len(text):
            if text.startswith("**", cursor):
                end = text.find("**", cursor + 2)
                if end != -1:
                    start_offset = sum(len(part) for part in clean_parts)
                    value = text[cursor + 2 : end]
                    index_map[cursor] = start_offset
                    index_map[cursor + 1] = start_offset
                    clean_parts.append(value)
                    ranges.append((start_offset, start_offset + len(value)))
                    for source_index in range(cursor + 2, end):
                        index_map[source_index] = start_offset + (source_index - cursor - 2)
                    index_map[end] = start_offset + len(value)
                    index_map[end + 1] = start_offset + len(value)
                    cursor = end + 2
                    continue
            index_map[cursor] = sum(len(part) for part in clean_parts)
            clean_parts.append(text[cursor])
            cursor += 1
        index_map[len(text)] = sum(len(part) for part in clean_parts)
        remapped = []
        for start, end in external_ranges:
            start = max(0, min(start, len(text)))
            end = max(0, min(end, len(text)))
            mapped = (index_map[start], index_map[end])
            if mapped[1] > mapped[0]:
                remapped.append(mapped)
        return "".join(clean_parts), ranges, remapped

    def _shift_ranges(self, ranges: list[tuple[int, int]], offset: int) -> list[tuple[int, int]]:
        if offset <= 0:
            return ranges
        return [(start + offset, end + offset) for start, end in ranges]

    def _find_or_create_day_table_xml(self, root, target_date: date):
        target = target_date.strftime("%d/%m/%Y")
        daily_tables = self._daily_tables_xml(root)
        for table in daily_tables:
            if self._xml_cell_text(table, 1, 2) == target:
                return table
            if not self._xml_cell_text(table, 1, 2) and not self._xml_cell_text(table, 3, 1):
                return table
        if not daily_tables:
            raise RuntimeError("El documento no tiene una tabla diaria base.")
        new_table = copy.deepcopy(daily_tables[0])
        self._clear_day_table_xml(new_table)
        body = root.find(".//w:body", self.NS)
        if body is None:
            raise RuntimeError("No se pudo encontrar el cuerpo del documento Word.")
        insert_at = self._find_xml_insertion_index(body, daily_tables, target_date)
        body.insert(insert_at, new_table)
        return new_table

    def _find_xml_insertion_index(self, body, daily_tables: list, target_date: date) -> int:
        children = list(body)
        table_positions = {id(child): index for index, child in enumerate(children)}
        parsed = []
        for table in daily_tables:
            raw = self._xml_cell_text(table, 1, 2)
            try:
                parsed.append((table, datetime.strptime(raw, "%d/%m/%Y").date()))
            except ValueError:
                continue
        for table, existing_date in sorted(parsed, key=lambda item: item[1]):
            if target_date < existing_date:
                return table_positions.get(id(table), len(children))
        if daily_tables:
            return table_positions.get(id(daily_tables[-1]), len(children) - 1) + 1
        return len(children)

    def _clear_day_table_xml(self, table) -> None:
        self._set_xml_cell_text(table, 1, 2, "")
        self._set_xml_cell_text(table, 1, 4, "")
        self._set_xml_cell_text(table, 1, 6, "")
        self._set_xml_cell_text(table, 1, 8, "")
        self._set_xml_cell_text(table, 3, 1, "")

    def _fill_day_table_xml(
        self,
        table,
        target_date: date,
        body: str,
        segments: list[TimeSegment],
        merge_segments: bool = False,
        bold_ranges: list[tuple[int, int]] | None = None,
    ) -> None:
        self._set_xml_cell_text(table, 1, 2, target_date.strftime("%d/%m/%Y"))
        if merge_segments:
            starts = self._merge_values(self._xml_cell_text(table, 1, 4), [segment.start for segment in segments])
            ends = self._merge_values(self._xml_cell_text(table, 1, 6), [segment.end for segment in segments])
            effective = self._merge_values(self._xml_cell_text(table, 1, 8), [segment.effective for segment in segments])
        else:
            starts = [segment.start for segment in segments]
            ends = [segment.end for segment in segments]
            effective = [segment.effective for segment in segments]
        self._set_xml_cell_text(table, 1, 4, "\n".join(starts))
        self._set_xml_cell_text(table, 1, 6, "\n".join(ends))
        self._set_xml_cell_text(table, 1, 8, "\n".join(effective))
        self._set_xml_cell_text(table, 3, 1, body, formatted=True, bold_ranges=bold_ranges)

    def _merge_values(self, existing: str, new_values: list[str]) -> list[str]:
        values = [line.strip() for line in existing.splitlines() if line.strip()]
        for value in new_values:
            if value not in values:
                values.append(value)
        return values

    def _daily_tables(self, doc) -> list[int]:
        indexes: list[int] = []
        for idx in range(1, doc.Tables.Count + 1):
            text = self._table_text(doc.Tables.Item(idx))
            if "FECHA:" in text and "ACTIVIDAD REALIZADA:" in text:
                indexes.append(idx)
        return indexes

    def _update_header_date(self, doc, target_date: date) -> None:
        table = doc.Tables.Item(1)
        self._set_cell_text(table, 1, 2, f"{month_last_day(target_date.year, target_date.month):02d}")
        self._set_cell_text(table, 1, 3, f"{target_date.month:02d}")
        self._set_cell_text(table, 1, 4, str(target_date.year))

    def _update_report_metadata(self, doc, metadata: NewReportMetadata) -> None:
        if doc.Tables.Count < 2:
            return
        table = doc.Tables.Item(2)
        updates = [
            ("Empresa:", metadata.company),
            ("Código Proyecto:", metadata.project_code),
            ("Función:", metadata.function),
            ("Persona que Reporta:", metadata.reporter),
            ("Ciudad de ejecución:", metadata.city),
        ]
        for label, value in updates:
            self._set_value_after_label(table, label, value)

        if doc.Tables.Count >= 1 and metadata.project_code:
            header = doc.Tables.Item(1)
            try:
                self._set_cell_text(header, 1, 6, metadata.project_code)
            except Exception:
                pass

    def _set_value_after_label(self, table, label: str, value: str) -> None:
        if not value:
            return
        for row in range(1, table.Rows.Count + 1):
            for column in range(1, table.Columns.Count + 1):
                try:
                    if self._cell_text(table, row, column).strip().lower() == label.lower():
                        self._set_cell_text(table, row, column + 1, value)
                        return
                except Exception:
                    continue

    def _table_text(self, table) -> str:
        return " ".join(table.Range.Text.replace("\r", " ").replace("\x07", " ").split())

    def _cell_text(self, table, row: int, column: int) -> str:
        return table.Cell(row, column).Range.Text.replace("\r", "\n").replace("\x07", "").strip()

    def _set_cell_text(self, table, row: int, column: int, text: str) -> None:
        table.Cell(row, column).Range.Text = text

    def _set_formatted_body_text(self, table, row: int, column: int, text: str) -> None:
        clean_text, explicit_ranges = self._strip_bold_markers(text)
        cell = table.Cell(row, column)
        cell.Range.Text = clean_text

        content_range = cell.Range.Duplicate
        content_range.End = max(content_range.Start, content_range.End - 1)
        content_range.Bold = 0

        auto_ranges = self._derive_auto_bold_ranges(clean_text)
        all_ranges = self._merge_ranges([*explicit_ranges, *auto_ranges])
        for start, end in all_ranges:
            if end <= start:
                continue
            span = cell.Range.Document.Range(content_range.Start + start, content_range.Start + end)
            span.Bold = 1

    def _clear_day_table(self, table) -> None:
        self._set_cell_text(table, 1, 2, "")
        self._set_cell_text(table, 1, 4, "")
        self._set_cell_text(table, 1, 6, "")
        self._set_cell_text(table, 1, 8, "")
        self._set_cell_text(table, 3, 1, "")

    def _find_or_create_day_table(self, doc, target_date: date):
        target = target_date.strftime("%d/%m/%Y")
        daily_indexes = self._daily_tables(doc)
        for idx in daily_indexes:
            table = doc.Tables.Item(idx)
            date_cell = self._cell_text(table, 1, 2)
            body_cell = self._cell_text(table, 3, 1)
            if date_cell == target:
                return table
            if not date_cell and not body_cell:
                return table

        if not daily_indexes:
            raise RuntimeError("El documento no tiene una tabla diaria base.")

        insert_before_index = self._find_insertion_index(doc, target_date, daily_indexes)
        if insert_before_index <= doc.Tables.Count:
            new_table = self._copy_table_before(doc, doc.Tables.Item(daily_indexes[0]), doc.Tables.Item(insert_before_index))
        else:
            new_table = self._copy_table_after(doc, doc.Tables.Item(daily_indexes[0]), doc.Tables.Item(daily_indexes[-1]))
        self._clear_day_table(new_table)
        return new_table

    def _copy_table_before(self, doc, template, anchor):
        anchor_index = self._table_index(doc, anchor)
        template.Range.Copy()
        insertion_range = doc.Range(anchor.Range.Start, anchor.Range.Start)
        insertion_range.Paste()
        return doc.Tables.Item(anchor_index)

    def _copy_table_after(self, doc, template, anchor=None):
        anchor = anchor or template
        anchor_index = self._table_index(doc, anchor)
        template.Range.Copy()
        insertion_range = doc.Range(anchor.Range.End, anchor.Range.End)
        insertion_range.InsertParagraphAfter()
        insertion_range.Collapse(0)
        insertion_range.Paste()
        return doc.Tables.Item(anchor_index + 1)

    def _table_index(self, doc, table) -> int:
        target_start = table.Range.Start
        for index in range(1, doc.Tables.Count + 1):
            if doc.Tables.Item(index).Range.Start == target_start:
                return index
        raise RuntimeError("No se pudo ubicar la tabla dentro del documento.")

    def _find_insertion_index(self, doc, target_date: date, daily_indexes: list[int]) -> int:
        parsed: list[tuple[int, date]] = []
        for idx in daily_indexes:
            table = doc.Tables.Item(idx)
            raw = self._cell_text(table, 1, 2)
            try:
                parsed_date = datetime.strptime(raw, "%d/%m/%Y").date()
                parsed.append((idx, parsed_date))
            except ValueError:
                continue
        for idx, existing_date in sorted(parsed, key=lambda item: item[1]):
            if target_date < existing_date:
                return idx
        return daily_indexes[-1] + 1

    def _fill_day_table(self, table, target_date: date, body: str, segments: list[TimeSegment]) -> None:
        self._set_cell_text(table, 1, 2, target_date.strftime("%d/%m/%Y"))
        self._set_cell_text(table, 1, 4, "\r".join(segment.start for segment in segments))
        self._set_cell_text(table, 1, 6, "\r".join(segment.end for segment in segments))
        self._set_cell_text(table, 1, 8, "\r".join(segment.effective for segment in segments))
        self._set_formatted_body_text(table, 3, 1, body)

    def _default_segments_for_date(self, target_date: date, rest_minutes: int) -> list[TimeSegment]:
        start, end = default_schedule_for_day(target_date)
        return [
            TimeSegment(
                start=start,
                end=end,
                effective=calculate_effective_hours(start, end, rest_minutes=rest_minutes),
                label="jornada",
            )
        ]

    def _append_to_day_table(self, table, body: str, segments: list[TimeSegment], target_date: date) -> None:
        merged_body = self._cell_text(table, 3, 1).strip()
        merged_body = f"{merged_body}\n\n{body}".strip() if merged_body else body
        self._set_cell_text(table, 1, 2, target_date.strftime("%d/%m/%Y"))
        self._set_cell_text(
            table,
            1,
            4,
            self._merge_lines(self._cell_text(table, 1, 4), [segment.start for segment in segments]),
        )
        self._set_cell_text(
            table,
            1,
            6,
            self._merge_lines(self._cell_text(table, 1, 6), [segment.end for segment in segments]),
        )
        self._set_cell_text(
            table,
            1,
            8,
            self._merge_lines(self._cell_text(table, 1, 8), [segment.effective for segment in segments]),
        )
        self._set_formatted_body_text(table, 3, 1, merged_body)

    def _merge_lines(self, existing: str, new_values: list[str]) -> str:
        values = [line.strip() for line in existing.splitlines() if line.strip()]
        for value in new_values:
            if value not in values:
                values.append(value)
        return "\r".join(values)

    def _strip_bold_markers(self, text: str) -> tuple[str, list[tuple[int, int]]]:
        clean_parts: list[str] = []
        ranges: list[tuple[int, int]] = []
        cursor = 0
        length = len(text)
        while cursor < length:
            if text.startswith("**", cursor):
                end = text.find("**", cursor + 2)
                if end != -1:
                    start_offset = len("".join(clean_parts))
                    segment = text[cursor + 2 : end]
                    clean_parts.append(segment)
                    ranges.append((start_offset, start_offset + len(segment)))
                    cursor = end + 2
                    continue
            clean_parts.append(text[cursor])
            cursor += 1
        return "".join(clean_parts), ranges

    def _derive_auto_bold_ranges(self, text: str) -> list[tuple[int, int]]:
        ranges: list[tuple[int, int]] = []
        lines = text.splitlines(keepends=True)
        offset = 0
        title_done = False
        for raw_line in lines:
            line = raw_line.rstrip("\r\n")
            stripped = line.strip()
            if stripped:
                line_start = offset + raw_line.index(stripped)
                line_end = line_start + len(stripped)
                if not title_done:
                    ranges.append((line_start, line_end))
                    title_done = True
                elif re.match(r"^\d+\)", stripped):
                    ranges.append((line_start, line_end))
            offset += len(raw_line)
        return ranges

    def _merge_ranges(self, ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
        normalized = sorted((start, end) for start, end in ranges if end > start)
        if not normalized:
            return []
        merged = [normalized[0]]
        for start, end in normalized[1:]:
            last_start, last_end = merged[-1]
            if start <= last_end:
                merged[-1] = (last_start, max(last_end, end))
            else:
                merged.append((start, end))
        return merged
