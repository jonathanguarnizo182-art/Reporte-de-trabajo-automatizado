from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from .calendar_utils import month_last_day
from .catalog import ResolvedDocument
from .models import GeneratedEntry, TimeSegment

try:
    import win32com.client  # type: ignore
except ImportError:  # pragma: no cover
    win32com = None


class WordUnavailableError(RuntimeError):
    pass


class WordReportService:
    def __init__(self):
        if win32com is None:
            raise WordUnavailableError("Instale pywin32 para habilitar Word COM.")

    @contextmanager
    def open_document(self, path: Path, visible: bool = False):
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = visible
        doc = word.Documents.Open(str(path))
        try:
            yield word, doc
        finally:
            doc.Close(SaveChanges=True)
            word.Quit()

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

    def entry_exists(self, path: Path, target_date: date) -> tuple[bool, str]:
        target = target_date.strftime("%d/%m/%Y")
        with self.open_document(path) as (_, doc):
            for index in self._daily_tables(doc):
                table = doc.Tables.Item(index)
                if self._cell_text(table, 1, 2) == target:
                    return True, self._cell_text(table, 3, 1)
        return False, ""

    def write_day_block(
        self,
        path: Path,
        target_date: date,
        entry: GeneratedEntry,
        segments: list[TimeSegment],
        update_mode: str = "replace",
    ) -> None:
        with self.open_document(path) as (_, doc):
            table = self._find_or_create_day_table(doc, target_date)
            if update_mode == "append":
                self._append_to_day_table(table, entry.full_text, segments, target_date)
            else:
                self._fill_day_table(table, target_date, entry.full_text, segments)

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

    def _table_text(self, table) -> str:
        return " ".join(table.Range.Text.replace("\r", " ").replace("\x07", " ").split())

    def _cell_text(self, table, row: int, column: int) -> str:
        return table.Cell(row, column).Range.Text.replace("\r", "\n").replace("\x07", "").strip()

    def _set_cell_text(self, table, row: int, column: int, text: str) -> None:
        table.Cell(row, column).Range.Text = text

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
        template = doc.Tables.Item(daily_indexes[0])
        template.Range.Copy()
        anchor = doc.Tables.Item(insert_before_index)
        insertion_range = doc.Range(anchor.Range.Start, anchor.Range.Start)
        insertion_range.Paste()
        new_table = doc.Tables.Item(insert_before_index)
        self._clear_day_table(new_table)
        return new_table

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
        self._set_cell_text(table, 3, 1, body)

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
        self._set_cell_text(table, 3, 1, merged_body)

    def _merge_lines(self, existing: str, new_values: list[str]) -> str:
        values = [line.strip() for line in existing.splitlines() if line.strip()]
        for value in new_values:
            if value not in values:
                values.append(value)
        return "\r".join(values)
