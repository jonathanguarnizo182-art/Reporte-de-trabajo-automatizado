from __future__ import annotations

import calendar
import html
import logging
import re
from html.parser import HTMLParser
from datetime import date, datetime
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, Button, BooleanVar, StringVar, Text, Tk, Toplevel, filedialog, messagebox, ttk
import tkinter.font as tkfont

try:
    import win32clipboard  # type: ignore
except ImportError:  # pragma: no cover
    win32clipboard = None

from .calendar_utils import SPANISH_MONTHS, calculate_effective_hours, classify_day, default_schedule_for_day
from .catalog import ClientCatalog, ReportCatalog, normalize_name, extract_report_metadata
from .config import AppConfig, load_config, save_config
from .models import ClientProject, GeneratedEntry, NewReportMetadata, ReportTemplate, TimeSegment, WorkdayPayload
from .scheduler import install_tasks
from .word_service import WordReportService, WordUnavailableError


LOGGER = logging.getLogger(__name__)


class RichTextFragment:
    def __init__(self, text: str, bold_ranges: list[tuple[int, int]]):
        self.text = text
        self.bold_ranges = bold_ranges


class ClipboardHtmlParser(HTMLParser):
    BLOCK_TAGS = {"p", "div", "section", "article", "header", "footer", "h1", "h2", "h3", "h4", "h5", "h6"}
    BULLET_PREFIX = "\u2022  "

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.bold_ranges: list[tuple[int, int]] = []
        self.bold_depth = 0
        self.bold_start: int | None = None
        self.in_list_item = False

    @property
    def position(self) -> int:
        return sum(len(part) for part in self.parts)

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in {"strong", "b"}:
            self._start_bold()
        elif tag == "br":
            self._newline()
        elif tag == "li":
            self._newline()
            self._append(self.BULLET_PREFIX)
            self.in_list_item = True
        elif tag in self.BLOCK_TAGS:
            self._newline()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"strong", "b"}:
            self._end_bold()
        elif tag == "li":
            self.in_list_item = False
            self._newline()
        elif tag in self.BLOCK_TAGS or tag in {"ul", "ol"}:
            self._newline()

    def handle_data(self, data: str) -> None:
        self._append_normalized_data(data)

    def fragment(self) -> RichTextFragment:
        while self.parts and not self.parts[0].strip():
            self._drop_prefix(len(self.parts[0]))
            self.parts.pop(0)
        text = "".join(self.parts).strip()
        trim_left = len("".join(self.parts)) - len("".join(self.parts).lstrip())
        if trim_left:
            self.bold_ranges = [(max(0, start - trim_left), max(0, end - trim_left)) for start, end in self.bold_ranges]
        return RichTextFragment(text, self._merge_ranges([(start, end) for start, end in self.bold_ranges if end > start]))

    def _append(self, value: str) -> None:
        if not value:
            return
        self.parts.append(html.unescape(value.replace("\xa0", " ")))

    def _append_normalized_data(self, value: str) -> None:
        if not value:
            return
        normalized = re.sub(r"\s+", " ", html.unescape(value.replace("\xa0", " ")))
        if not normalized.strip():
            return
        current = "".join(self.parts)
        if current.endswith("\n") or current.endswith("\t") or not current:
            normalized = normalized.lstrip()
        else:
            normalized = normalized.strip()
            if (
                current
                and not current.endswith((" ", "\n", "\t", "(", "¿", "¡"))
                and not normalized.startswith((".", ",", ";", ":", "!", "?", ")", "]", "}"))
            ):
                normalized = f" {normalized}"
        if normalized:
            self.parts.append(normalized)

    def _newline(self) -> None:
        current = "".join(self.parts)
        if current and not current.endswith("\n"):
            self.parts.append("\n")

    def _start_bold(self) -> None:
        if self.bold_depth == 0:
            self.bold_start = self.position
        self.bold_depth += 1

    def _end_bold(self) -> None:
        if self.bold_depth == 0:
            return
        self.bold_depth -= 1
        if self.bold_depth == 0 and self.bold_start is not None:
            self.bold_ranges.append((self.bold_start, self.position))
            self.bold_start = None

    def _drop_prefix(self, length: int) -> None:
        self.bold_ranges = [(max(0, start - length), max(0, end - length)) for start, end in self.bold_ranges]

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


class UpdateModeDialog(Toplevel):
    def __init__(self, parent, existing_text: str):
        super().__init__(parent)
        self.title("La fecha ya existe")
        self.result = "cancel"
        self.geometry("680x360")
        self.transient(parent)
        self.grab_set()

        ttk.Label(self, text="Ya existe informacion para esta fecha. Elija como continuar.").pack(
            anchor="w", padx=12, pady=(12, 6)
        )
        preview = Text(self, height=12, wrap="word")
        preview.pack(fill=BOTH, expand=True, padx=12)
        preview.insert("1.0", existing_text.strip() or "(Sin contenido)")
        preview.configure(state="disabled")

        buttons = ttk.Frame(self)
        buttons.pack(fill="x", padx=12, pady=12)
        ttk.Button(buttons, text="Reemplazar", command=lambda: self._close("replace")).pack(side=LEFT)
        ttk.Button(buttons, text="Agregar", command=lambda: self._close("append")).pack(side=LEFT, padx=8)
        ttk.Button(buttons, text="Cancelar", command=lambda: self._close("cancel")).pack(side=RIGHT)

    def _close(self, result: str) -> None:
        self.result = result
        self.destroy()


class CalendarPickerDialog(Toplevel):
    def __init__(self, parent, initial_date: date, country_code: str, on_select):
        super().__init__(parent)
        self.title("Seleccionar fecha")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.country_code = country_code
        self.on_select = on_select
        self.selected_date = initial_date
        self.year = initial_date.year
        self.month = initial_date.month
        self.info_var = StringVar(value="")

        self._build_ui()
        self._render_month()

    def _build_ui(self) -> None:
        container = ttk.Frame(self, padding=10)
        container.pack(fill=BOTH, expand=True)

        header = ttk.Frame(container)
        header.grid(row=0, column=0, columnspan=7, sticky="ew", pady=(0, 8))
        ttk.Button(header, text="<", width=3, command=self._previous_month).pack(side=LEFT)
        self.title_var = StringVar()
        ttk.Label(header, textvariable=self.title_var, anchor="center", width=28).pack(side=LEFT, expand=True, fill="x", padx=8)
        ttk.Button(header, text=">", width=3, command=self._next_month).pack(side=RIGHT)

        for column, label in enumerate(["Lun", "Mar", "Mie", "Jue", "Vie", "Sab", "Dom"]):
            ttk.Label(container, text=label, anchor="center", width=8).grid(row=1, column=column, padx=1, pady=(0, 3))

        self.days_frame = ttk.Frame(container)
        self.days_frame.grid(row=2, column=0, columnspan=7)

        legend = ttk.Frame(container)
        legend.grid(row=3, column=0, columnspan=7, sticky="ew", pady=(8, 0))
        self._legend_item(legend, "Habil", "#ffffff").pack(side=LEFT, padx=(0, 8))
        self._legend_item(legend, "Sab/Dom", "#e8e8e8").pack(side=LEFT, padx=8)
        self._legend_item(legend, "Festivo", "#ffd6d6").pack(side=LEFT, padx=8)
        self._legend_item(legend, "Seleccionado", "#b7d7ff").pack(side=LEFT, padx=8)

        ttk.Label(container, textvariable=self.info_var, anchor="w").grid(row=4, column=0, columnspan=7, sticky="ew", pady=(8, 0))

    def _legend_item(self, parent, text: str, color: str) -> ttk.Frame:
        frame = ttk.Frame(parent)
        swatch = Button(frame, width=2, height=1, relief="solid", bd=1, bg=color, state="disabled")
        swatch.pack(side=LEFT)
        ttk.Label(frame, text=f" {text}").pack(side=LEFT)
        return frame

    def _previous_month(self) -> None:
        if self.month == 1:
            self.month = 12
            self.year -= 1
        else:
            self.month -= 1
        self._render_month()

    def _next_month(self) -> None:
        if self.month == 12:
            self.month = 1
            self.year += 1
        else:
            self.month += 1
        self._render_month()

    def _render_month(self) -> None:
        for child in self.days_frame.winfo_children():
            child.destroy()

        self.title_var.set(f"{SPANISH_MONTHS[self.month]} {self.year}")
        weeks = calendar.Calendar(firstweekday=0).monthdatescalendar(self.year, self.month)
        for row, week in enumerate(weeks):
            for column, current in enumerate(week):
                self._render_day(row, column, current)
        self._update_info(self.selected_date)

    def _render_day(self, row: int, column: int, current: date) -> None:
        classification = classify_day(current, self.country_code)
        in_month = current.month == self.month
        selected = current == self.selected_date
        background = "#ffffff"
        foreground = "#111111"
        if not in_month:
            background = "#f5f5f5"
            foreground = "#999999"
        elif classification.kind == "festivo":
            background = "#ffd6d6"
            foreground = "#8a0000"
        elif current.weekday() >= 5:
            background = "#e8e8e8"
            foreground = "#444444"
        if selected:
            background = "#b7d7ff"
            foreground = "#003b75"

        button = Button(
            self.days_frame,
            text=str(current.day),
            width=8,
            height=2,
            relief="solid" if selected else "flat",
            bd=1,
            bg=background,
            fg=foreground,
            activebackground=background,
            command=lambda value=current: self._select_day(value),
        )
        button.grid(row=row, column=column, padx=1, pady=1)
        button.bind("<Enter>", lambda _event, value=current: self._update_info(value))
        button.bind("<Leave>", lambda _event: self._update_info(self.selected_date))

    def _select_day(self, value: date) -> None:
        self.selected_date = value
        self.on_select(value)
        self.destroy()

    def _update_info(self, value: date) -> None:
        classification = classify_day(value, self.country_code)
        if classification.holiday_name:
            detail = f"{classification.kind}: {classification.holiday_name}"
        else:
            detail = classification.kind
        self.info_var.set(f"{value.strftime('%d/%m/%Y')} - {detail}")


class SettingsDialog(Toplevel):
    def __init__(self, parent, config: AppConfig):
        super().__init__(parent)
        self.title("Configuracion")
        self.result: AppConfig | None = None
        self.geometry("760x320")
        self.transient(parent)
        self.grab_set()

        self.clients_root = StringVar(value=config.clients_root)
        self.template_path = StringVar(value=config.template_path)
        self.mon_wed = StringVar(value=config.popup_hour_mon_wed)
        self.thu_fri = StringVar(value=config.popup_hour_thu_fri)

        container = ttk.Frame(self, padding=12)
        container.pack(fill=BOTH, expand=True)

        fields = [
            ("Carpeta clientes", self.clients_root),
            ("Plantilla general", self.template_path),
            ("Popup lun-mie", self.mon_wed),
            ("Popup jue-vie", self.thu_fri),
        ]
        for row, (label, variable) in enumerate(fields):
            ttk.Label(container, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(container, textvariable=variable, width=72).grid(row=row, column=1, sticky="ew", pady=4)

        ttk.Button(container, text="Elegir carpeta", command=self._browse_clients_root).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(container, text="Elegir archivo", command=self._browse_template).grid(row=1, column=2, padx=(8, 0))
        container.columnconfigure(1, weight=1)

        buttons = ttk.Frame(container)
        buttons.grid(row=len(fields), column=0, columnspan=3, sticky="ew", pady=(16, 0))
        ttk.Button(buttons, text="Guardar", command=self._save).pack(side=LEFT)
        ttk.Button(buttons, text="Cancelar", command=self.destroy).pack(side=LEFT, padx=8)

    def _browse_clients_root(self) -> None:
        directory = filedialog.askdirectory(initialdir=self.clients_root.get() or ".")
        if directory:
            self.clients_root.set(directory)

    def _browse_template(self) -> None:
        filename = filedialog.askopenfilename(
            initialdir=str(Path(self.clients_root.get() or ".").expanduser()),
            filetypes=[("Documentos Word", "*.docx")],
        )
        if filename:
            self.template_path.set(filename)

    def _save(self) -> None:
        self.result = AppConfig(
            clients_root=self.clients_root.get().strip(),
            template_path=self.template_path.get().strip(),
            popup_hour_mon_wed=self.mon_wed.get().strip() or "18:00",
            popup_hour_thu_fri=self.thu_fri.get().strip() or "17:30",
        )
        self.destroy()


class NewReportDialog(Toplevel):
    def __init__(
        self,
        parent,
        client_name: str,
        company_options: list[str],
        metadata_by_company: dict[str, NewReportMetadata],
        initial_metadata: NewReportMetadata | None = None,
        default_reporter: str = "Jonathan Sechagua Guarnizo",
    ):
        super().__init__(parent)
        self.title("Crear nuevo reporte")
        self.result: tuple[date, NewReportMetadata] | None = None
        self.geometry("760x390")
        self.transient(parent)
        self.grab_set()

        today = date.today()
        self.year = StringVar(value=str(today.year))
        self.month = StringVar(value=f"{today.month:02d}")
        self.metadata_by_company = metadata_by_company
        self.company_options = self._unique_options([client_name.replace("_", " "), *company_options])
        default_company = (
            initial_metadata.company
            if initial_metadata and initial_metadata.company
            else self.company_options[0]
            if self.company_options
            else client_name.replace("_", " ")
        )
        self.company = StringVar(value=default_company)
        self.project_code = StringVar(value=initial_metadata.project_code if initial_metadata else "")
        self.function = StringVar(value=initial_metadata.function if initial_metadata else "")
        self.reporter = StringVar(value=(initial_metadata.reporter if initial_metadata and initial_metadata.reporter else default_reporter))
        self.city = StringVar(value=(initial_metadata.city if initial_metadata and initial_metadata.city else "Bogota"))
        self.received_by = StringVar(value=(initial_metadata.received_by if initial_metadata and initial_metadata.received_by else ""))

        container = ttk.Frame(self, padding=12)
        container.pack(fill=BOTH, expand=True)

        fields = [
            ("Mes completo", self.month, "entry"),
            ("Anio", self.year, "entry"),
            ("Empresa / Cliente", self.company, "combo"),
            ("Codigo Proyecto", self.project_code, "entry"),
            ("Funcion", self.function, "entry"),
            ("Persona que Reporta", self.reporter, "entry"),
            ("Ciudad de ejecucion", self.city, "entry"),
            ("Recibido por", self.received_by, "entry"),
        ]
        for row, (label, variable, widget_type) in enumerate(fields):
            ttk.Label(container, text=label).grid(row=row, column=0, sticky="w", pady=4)
            if widget_type == "combo":
                company_combo = ttk.Combobox(
                    container,
                    textvariable=variable,
                    values=self.company_options,
                    state="normal",
                    width=62,
                )
                company_combo.grid(row=row, column=1, sticky="ew", pady=4)
                company_combo.bind("<<ComboboxSelected>>", lambda _event: self._apply_company_metadata())
                company_combo.bind("<FocusOut>", lambda _event: self._apply_company_metadata(fill_empty_only=True))
            else:
                ttk.Entry(container, textvariable=variable, width=64).grid(row=row, column=1, sticky="ew", pady=4)

        container.columnconfigure(1, weight=1)
        buttons = ttk.Frame(container)
        buttons.grid(row=len(fields), column=0, columnspan=2, sticky="ew", pady=(16, 0))
        ttk.Button(buttons, text="Crear", command=self._create).pack(side=LEFT)
        ttk.Button(buttons, text="Cancelar", command=self.destroy).pack(side=LEFT, padx=8)
        ttk.Label(
            container,
            text="Se crearan todos los dias calendario del mes seleccionado.",
            foreground="#555555",
        ).grid(row=len(fields) + 1, column=0, columnspan=2, sticky="w", pady=(10, 0))

    def _unique_options(self, values: list[str]) -> list[str]:
        options: list[str] = []
        seen: set[str] = set()
        for value in values:
            clean = " ".join(value.strip().split())
            key = clean.upper()
            if clean and key not in seen:
                options.append(clean)
                seen.add(key)
        return options

    def _apply_company_metadata(self, fill_empty_only: bool = False) -> None:
        metadata = self.metadata_by_company.get(normalize_name(self.company.get()))
        if not metadata:
            return

        def set_value(variable: StringVar, value: str) -> None:
            if value and (not fill_empty_only or not variable.get().strip()):
                variable.set(value)

        set_value(self.project_code, metadata.project_code)
        set_value(self.function, metadata.function)
        set_value(self.reporter, metadata.reporter)
        set_value(self.city, metadata.city)
        set_value(self.received_by, metadata.received_by)

    def _create(self) -> None:
        try:
            month = int(self.month.get().strip())
            year = int(self.year.get().strip())
            target = date(year, month, 1)
        except ValueError:
            messagebox.showerror("Fecha invalida", "Mes y anio deben ser numericos.")
            return
        if not 1 <= target.month <= 12:
            messagebox.showerror("Fecha invalida", "El mes debe estar entre 1 y 12.")
            return
        metadata = NewReportMetadata(
            company=self.company.get().strip(),
            project_code=self.project_code.get().strip(),
            function=self.function.get().strip(),
            reporter=self.reporter.get().strip(),
            city=self.city.get().strip(),
            received_by=self.received_by.get().strip(),
        )
        if not metadata.company:
            messagebox.showerror("Datos incompletos", "Indique la empresa o cliente.")
            return
        self.result = (target, metadata)
        self.destroy()


class ReportAutomationApp:
    def __init__(self, root: Tk, auto_open: bool = False):
        self.root = root
        self.root.title("Reportes de Servicio")
        self.root.geometry("1120x820")

        self.config = load_config()
        self.client_catalog = ClientCatalog(self.config.clients_root)
        self.report_catalog: ReportCatalog | None = None
        self.word_service = None
        self.clients: list[ClientProject] = []
        self.templates: list[ReportTemplate] = []

        try:
            self.word_service = WordReportService()
        except WordUnavailableError as exc:
            LOGGER.warning("%s", exc)

        self.client_var = StringVar()
        self.report_var = StringVar()
        self.date_var = StringVar(value=date.today().isoformat())
        self.day_type_var = StringVar()
        self.start_var = StringVar()
        self.end_var = StringVar()
        self.break_var = StringVar(value=str(self.config.default_rest_minutes))
        self.extra_var = BooleanVar(value=False)
        self.extra_start_var = StringVar(value="18:00")
        self.extra_end_var = StringVar(value="21:00")
        self.service_mode_var = StringVar(value=self.config.preferred_service_mode)
        self.status_var = StringVar(value="Seleccione cliente, reporte y escriba el avance.")
        self.business_day_var = StringVar(value="")
        self.save_button = None
        self._save_in_progress = False

        self._build_ui()
        self._refresh_clients(select_default=True)
        self._apply_date_defaults()
        if auto_open:
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(1500, lambda: self.root.attributes("-topmost", False))

    def _build_ui(self) -> None:
        root_frame = ttk.Frame(self.root, padding=12)
        root_frame.pack(fill=BOTH, expand=True)

        header = ttk.Frame(root_frame)
        header.pack(fill="x")
        ttk.Label(header, text="Cliente").grid(row=0, column=0, sticky="w", pady=4)
        self.client_combo = ttk.Combobox(header, textvariable=self.client_var, state="readonly", width=32)
        self.client_combo.grid(row=0, column=1, sticky="ew", padx=(8, 12), pady=4)
        self.client_combo.bind("<<ComboboxSelected>>", lambda _event: self._on_client_changed())
        ttk.Button(header, text="Actualizar clientes", command=self._refresh_clients).grid(row=0, column=2, padx=4)
        ttk.Button(header, text="Configuracion", command=self._open_settings).grid(row=0, column=3, padx=4)
        ttk.Button(header, text="Instalar popup", command=self._install_popup_tasks).grid(row=0, column=4, padx=4)

        ttk.Label(header, text="Reporte").grid(row=1, column=0, sticky="w", pady=4)
        self.report_combo = ttk.Combobox(header, textvariable=self.report_var, state="readonly", width=56)
        self.report_combo.grid(row=1, column=1, sticky="ew", padx=(8, 12), pady=4)
        self.report_combo.bind("<<ComboboxSelected>>", lambda _event: self._load_current_day_entry(show_errors=False))
        ttk.Button(header, text="Actualizar reportes", command=self._refresh_reports).grid(row=1, column=2, padx=4)
        ttk.Button(header, text="Crear nuevo reporte", command=self._create_new_report).grid(row=1, column=3, padx=4)
        header.columnconfigure(1, weight=1)

        date_frame = ttk.Frame(root_frame)
        date_frame.pack(fill="x", pady=(12, 0))
        ttk.Label(date_frame, text="Fecha").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(date_frame, textvariable=self.date_var, width=16).grid(row=0, column=1, sticky="w", padx=(8, 4), pady=4)
        ttk.Button(date_frame, text="Calendario", command=self._open_calendar_picker).grid(row=0, column=2, padx=(0, 12))
        ttk.Button(date_frame, text="Aplicar jornada sugerida", command=self._apply_date_defaults).grid(row=0, column=3, padx=4)
        ttk.Label(date_frame, textvariable=self.business_day_var).grid(row=0, column=4, sticky="w", padx=8)

        schedule = ttk.LabelFrame(root_frame, text="Jornada")
        schedule.pack(fill="x", pady=(12, 8))
        ttk.Label(schedule, text="Tipo de dia").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(schedule, textvariable=self.day_type_var, width=14).grid(row=0, column=1, sticky="w", padx=8, pady=6)
        ttk.Label(schedule, text="Inicio").grid(row=0, column=2, sticky="w", padx=8, pady=6)
        ttk.Entry(schedule, textvariable=self.start_var, width=10).grid(row=0, column=3, sticky="w", padx=8, pady=6)
        ttk.Label(schedule, text="Fin").grid(row=0, column=4, sticky="w", padx=8, pady=6)
        ttk.Entry(schedule, textvariable=self.end_var, width=10).grid(row=0, column=5, sticky="w", padx=8, pady=6)
        ttk.Label(schedule, text="Descanso (min)").grid(row=0, column=6, sticky="w", padx=8, pady=6)
        ttk.Entry(schedule, textvariable=self.break_var, width=10).grid(row=0, column=7, sticky="w", padx=8, pady=6)

        ttk.Checkbutton(schedule, text="Hubo horas extra", variable=self.extra_var).grid(row=1, column=0, sticky="w", padx=8, pady=6)
        ttk.Label(schedule, text="Extra inicio").grid(row=1, column=2, sticky="w", padx=8, pady=6)
        ttk.Entry(schedule, textvariable=self.extra_start_var, width=10).grid(row=1, column=3, sticky="w", padx=8, pady=6)
        ttk.Label(schedule, text="Extra fin").grid(row=1, column=4, sticky="w", padx=8, pady=6)
        ttk.Entry(schedule, textvariable=self.extra_end_var, width=10).grid(row=1, column=5, sticky="w", padx=8, pady=6)
        ttk.Label(schedule, text="Modo servicio").grid(row=1, column=6, sticky="w", padx=8, pady=6)
        ttk.Combobox(schedule, textvariable=self.service_mode_var, values=["remoto", "presencial"], state="readonly", width=12).grid(
            row=1, column=7, sticky="w", padx=8, pady=6
        )

        text_frame = ttk.LabelFrame(root_frame, text="Actividad realizada")
        text_frame.pack(fill=BOTH, expand=True, pady=(8, 8))
        toolbar = ttk.Frame(text_frame)
        toolbar.pack(fill="x", padx=8, pady=(8, 4))
        self.save_button = ttk.Button(toolbar, text="Guardar en Word", command=self._save_to_word)
        self.save_button.pack(side=LEFT)
        ttk.Button(toolbar, text="Limpiar", command=lambda: self.notes_widget.delete("1.0", END)).pack(side=LEFT, padx=8)
        ttk.Button(toolbar, text="Copiar", command=self._copy_text).pack(side=LEFT)
        ttk.Button(toolbar, text="Ver", command=lambda: self._load_current_day_entry(show_errors=True)).pack(side=LEFT, padx=8)
        self.notes_widget = Text(text_frame, wrap="word")
        self._configure_notes_formatting()
        self.notes_widget.pack(fill=BOTH, expand=True, padx=8, pady=(0, 8))

        footer = ttk.Frame(root_frame)
        footer.pack(fill="x")
        ttk.Label(footer, textvariable=self.status_var).pack(side=LEFT)

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self.root, self.config)
        self.root.wait_window(dialog)
        if dialog.result:
            current = self.config
            current.clients_root = dialog.result.clients_root
            current.template_path = dialog.result.template_path
            current.popup_hour_mon_wed = dialog.result.popup_hour_mon_wed
            current.popup_hour_thu_fri = dialog.result.popup_hour_thu_fri
            save_config(current)
            self.config = load_config()
            self.client_catalog = ClientCatalog(self.config.clients_root)
            self._refresh_clients(select_default=True)
            self.status_var.set("Configuracion guardada.")

    def _refresh_clients(self, select_default: bool = False) -> None:
        self.clients = self.client_catalog.scan()
        values = [client.name for client in self.clients]
        self.client_combo["values"] = values
        if not values:
            self.status_var.set("No se encontraron clientes con carpeta Avance de servicio.")
            self.report_combo["values"] = []
            return
        if select_default and self.config.default_client in values:
            self.client_var.set(self.config.default_client)
        elif not self.client_var.get() or self.client_var.get() not in values:
            self.client_var.set(values[0])
        self._on_client_changed()

    def _on_client_changed(self) -> None:
        client = self._selected_client()
        self.config.default_client = client.name
        save_config(self.config)
        self.report_catalog = ReportCatalog(client.reports_dir)
        self._refresh_reports()

    def _refresh_reports(self) -> None:
        if self.report_catalog is None:
            return
        self.templates = self.report_catalog.scan_documents()
        values = [template.template_path.name for template in self.templates]
        self.report_combo["values"] = values
        if not values:
            self.report_var.set("")
            self.status_var.set("No hay reportes .docx para este cliente. Use Crear nuevo reporte.")
            return
        if self.config.default_report_id:
            preferred = next((item for item in self.templates if item.report_id == self.config.default_report_id), None)
            if preferred:
                self.report_var.set(preferred.template_path.name)
                self._load_current_day_entry(show_errors=False)
                return
        self.report_var.set(values[0])
        self._load_current_day_entry(show_errors=False)

    def _selected_client(self) -> ClientProject:
        name = self.client_var.get().strip()
        for client in self.clients:
            if client.name == name:
                return client
        raise RuntimeError("Seleccione un cliente valido.")

    def _selected_report(self) -> ReportTemplate:
        selected = self.report_var.get().strip()
        for template in self.templates:
            if template.template_path.name == selected:
                if self.config.default_report_id != template.report_id:
                    self.config.default_report_id = template.report_id
                    save_config(self.config)
                return template
        raise RuntimeError("Seleccione un reporte valido.")

    def _parse_target_date(self) -> date:
        return datetime.strptime(self.date_var.get().strip(), "%Y-%m-%d").date()

    def _open_calendar_picker(self) -> None:
        try:
            initial_date = self._parse_target_date()
        except ValueError:
            initial_date = date.today()
            self.date_var.set(initial_date.isoformat())
        CalendarPickerDialog(
            self.root,
            initial_date,
            self.config.country_holidays,
            self._set_selected_date,
        )

    def _set_selected_date(self, selected_date: date) -> None:
        self.date_var.set(selected_date.isoformat())
        self._apply_date_defaults()

    def _apply_date_defaults(self) -> None:
        try:
            target_date = self._parse_target_date()
        except ValueError:
            self.status_var.set("La fecha debe estar en formato YYYY-MM-DD.")
            return
        classification = classify_day(target_date, self.config.country_holidays)
        start, end = default_schedule_for_day(target_date)
        self.start_var.set(start)
        self.end_var.set(end)
        self.day_type_var.set(classification.kind)
        if classification.holiday_name:
            self.business_day_var.set(f"Festivo detectado: {classification.holiday_name}")
        else:
            self.business_day_var.set("Dia habil" if classification.is_business_day else f"No habil ({classification.kind})")
        if target_date.weekday() <= 2:
            self.extra_start_var.set("18:00")
            self.extra_end_var.set("21:00")
        else:
            self.extra_start_var.set("17:30")
            self.extra_end_var.set("20:30")
        self._load_current_day_entry(show_errors=False)

    def _load_current_day_entry(self, show_errors: bool = False) -> None:
        if self.word_service is None:
            if show_errors:
                messagebox.showerror("Word no disponible", "Instale pywin32 para leer documentos Word.")
            return
        try:
            report = self._selected_report()
            target_date = self._parse_target_date()
            exists, existing_text = self.word_service.entry_exists(report.template_path, target_date)
        except Exception as exc:
            LOGGER.exception("Error leyendo actividad existente: %s", exc)
            self.status_var.set("No fue posible leer la actividad de la fecha.")
            if show_errors:
                messagebox.showerror("Error al visualizar", str(exc))
            return

        self.notes_widget.delete("1.0", END)
        self.notes_widget.tag_remove("bold", "1.0", END)
        if exists and existing_text.strip():
            self.notes_widget.insert("1.0", existing_text.strip())
            self.status_var.set(f"Actividad cargada para {target_date.strftime('%d/%m/%Y')}.")
        else:
            self.status_var.set("No hay actividad guardada para esta fecha.")

    def _collect_segments(self) -> list[TimeSegment]:
        rest_minutes = int(self.break_var.get().strip() or "0")
        start = self.start_var.get().strip()
        end = self.end_var.get().strip()
        segments = [
            TimeSegment(
                start=start,
                end=end,
                effective=calculate_effective_hours(start, end, rest_minutes=rest_minutes),
                label="jornada",
            )
        ]
        if self.extra_var.get():
            extra_start = self.extra_start_var.get().strip()
            extra_end = self.extra_end_var.get().strip()
            segments.append(
                TimeSegment(
                    start=extra_start,
                    end=extra_end,
                    effective=calculate_effective_hours(extra_start, extra_end, rest_minutes=0),
                    label="extra",
                )
            )
        return segments

    def _build_payload(self) -> WorkdayPayload:
        report = self._selected_report()
        target_date = self._parse_target_date()
        classification = classify_day(target_date, self.config.country_holidays)
        notes = self.notes_widget.get("1.0", "end-1c").rstrip()
        if not notes.strip():
            raise RuntimeError("Escriba la actividad realizada antes de guardar.")
        return WorkdayPayload(
            report=report,
            target_date=target_date,
            is_business_day=classification.is_business_day,
            day_type=self.day_type_var.get().strip() or classification.kind,
            segments=self._collect_segments(),
            had_overtime=self.extra_var.get(),
            notes=notes,
            transcript="",
            context={"service_mode": self.service_mode_var.get().strip()},
        )

    def _save_to_word(self) -> None:
        if self._save_in_progress:
            return
        if self.word_service is None:
            messagebox.showerror("Word no disponible", "Instale pywin32 para escribir en documentos Word.")
            return
        try:
            payload = self._build_payload()
        except Exception as exc:
            messagebox.showerror("Datos incompletos", str(exc))
            return

        entry = GeneratedEntry("", "", [], payload.notes, "direct", self._collect_bold_ranges())
        self._set_save_busy(True, "Guardando en Word...")
        try:
            if self.report_catalog is None:
                raise RuntimeError("Seleccione primero un cliente.")
            resolved = self.report_catalog.resolve_document(payload.report, payload.target_date)
            if resolved.created_from_template:
                self.word_service.prepare_new_month_document(resolved, payload.target_date)
            if self.word_service.lock_file_exists(resolved.path):
                raise RuntimeError(
                    "El documento parece estar abierto en Microsoft Word. "
                    "Cierre ese archivo y vuelva a intentar guardar desde la app."
                )

            exists, existing_text = self.word_service.entry_exists(resolved.path, payload.target_date)
            update_mode = "replace"
            if exists and existing_text.strip():
                self._set_save_busy(False)
                dialog = UpdateModeDialog(self.root, existing_text)
                self.root.wait_window(dialog)
                update_mode = dialog.result
                if update_mode == "cancel":
                    self.status_var.set("Operacion cancelada.")
                    return
                self._set_save_busy(True, "Guardando en Word...")

            self.word_service.write_day_block(
                resolved.path,
                payload.target_date,
                entry,
                payload.segments,
                update_mode=update_mode,
            )
            saved, saved_text = self.word_service.entry_exists(resolved.path, payload.target_date)
            if not saved:
                raise RuntimeError("No se pudo confirmar la fecha guardada dentro del documento.")
            if not saved_text.strip():
                raise RuntimeError("La fecha se creo, pero el texto no quedo guardado en el documento.")
            self.status_var.set(f"Reporte actualizado en: {resolved.path.name}")
            messagebox.showinfo("Reporte diligenciado", f"Documento actualizado:\n{resolved.path}")
            self._refresh_reports()
        except Exception as exc:
            LOGGER.exception("Error guardando en Word: %s", exc)
            self.status_var.set("No fue posible guardar en Word.")
            messagebox.showerror("Error al guardar", str(exc))
        finally:
            self._set_save_busy(False)

    def _set_save_busy(self, busy: bool, status: str | None = None) -> None:
        self._save_in_progress = busy
        if self.save_button is not None:
            self.save_button.configure(state="disabled" if busy else "normal")
        if status:
            self.status_var.set(status)

    def _create_new_report(self) -> None:
        if self.word_service is None:
            messagebox.showerror("Word no disponible", "Instale pywin32 para crear documentos Word.")
            return
        try:
            client = self._selected_client()
        except Exception as exc:
            messagebox.showerror("Cliente invalido", str(exc))
            return

        dialog = NewReportDialog(
            self.root,
            client.name,
            self._company_options(),
            self._metadata_by_company(),
            self._selected_report_metadata(),
        )
        self.root.wait_window(dialog)
        if not dialog.result:
            return
        target_date, metadata = dialog.result

        created = None
        try:
            target_client = self.client_catalog.resolve_or_create(metadata.company)
            catalog = ReportCatalog(target_client.reports_dir)
            created = catalog.create_month_report(
                target_date=target_date,
                company=metadata.company,
                template_path=self.config.template_path or None,
            )
            self.word_service.prepare_full_month_document(
                created.path,
                target_date,
                metadata,
                country_code=self.config.country_holidays,
                rest_minutes=self.config.default_rest_minutes,
            )
            self.config.default_client = target_client.name
            self.config.default_report_id = ""
            save_config(self.config)
            self._refresh_clients(select_default=False)
            self.client_var.set(target_client.name)
            self.report_catalog = catalog
            self._refresh_reports()
            self.report_var.set(created.path.name)
            self._load_current_day_entry(show_errors=False)
            self.status_var.set(f"Nuevo reporte creado en {target_client.name}: {created.path.name}")
            messagebox.showinfo("Reporte creado", f"Documento creado:\n{created.path}")
        except Exception as exc:
            if created is not None and created.path.exists():
                try:
                    created.path.unlink()
                except Exception:
                    LOGGER.exception("No se pudo eliminar el reporte parcial: %s", created.path)
            LOGGER.exception("Error creando reporte: %s", exc)
            messagebox.showerror("Error al crear reporte", str(exc))

    def _company_options(self) -> list[str]:
        options = [client.name.replace("_", " ") for client in self.clients]
        for client in self.clients:
            for template in ReportCatalog(client.reports_dir).scan_documents():
                metadata = extract_report_metadata(template.template_path)
                if metadata.company:
                    options.append(metadata.company)
                elif template.company:
                    options.append(template.company)
        return options

    def _metadata_by_company(self) -> dict[str, NewReportMetadata]:
        output: dict[str, NewReportMetadata] = {}
        templates: list[ReportTemplate] = []
        for client in self.clients:
            templates.extend(ReportCatalog(client.reports_dir).scan_documents())
        for template in sorted(templates, key=lambda item: item.template_path.stat().st_mtime, reverse=True):
            metadata = extract_report_metadata(template.template_path)
            for company in {metadata.company, template.company, template.template_path.parent.parent.name.replace("_", " ")}:
                if company and normalize_name(company) not in output:
                    output[normalize_name(company)] = metadata
        return output

    def _selected_report_metadata(self) -> NewReportMetadata | None:
        try:
            return extract_report_metadata(self._selected_report().template_path)
        except Exception:
            return None

    def _copy_text(self) -> None:
        text = self.notes_widget.get("1.0", END).strip()
        if not text:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status_var.set("Texto copiado al portapapeles.")

    def _configure_notes_formatting(self) -> None:
        base_font = tkfont.nametofont(self.notes_widget.cget("font"))
        bold_font = base_font.copy()
        bold_font.configure(weight="bold")
        self.notes_widget.tag_configure("bold", font=bold_font)
        self.notes_widget.bind("<<Paste>>", self._paste_rich_text)
        self.notes_widget.bind("<Control-v>", self._paste_rich_text)
        self.notes_widget.bind("<Control-V>", self._paste_rich_text)

    def _paste_rich_text(self, _event=None):
        fragment = self._read_clipboard_html_fragment() or self._read_plain_clipboard_fragment()
        if fragment is None or not fragment.text:
            return None
        try:
            if self.notes_widget.tag_ranges("sel"):
                self.notes_widget.delete("sel.first", "sel.last")
        except Exception:
            pass
        start_index = self.notes_widget.index("insert")
        self.notes_widget.insert("insert", fragment.text)
        for start, end in fragment.bold_ranges:
            self.notes_widget.tag_add("bold", f"{start_index}+{start}c", f"{start_index}+{end}c")
        return "break"

    def _read_plain_clipboard_fragment(self) -> RichTextFragment | None:
        try:
            return RichTextFragment(self.root.clipboard_get(), [])
        except Exception:
            return None

    def _read_clipboard_html_fragment(self) -> RichTextFragment | None:
        if win32clipboard is None:
            return None
        try:
            win32clipboard.OpenClipboard()
            html_format = win32clipboard.RegisterClipboardFormat("HTML Format")
            if not win32clipboard.IsClipboardFormatAvailable(html_format):
                return None
            raw = win32clipboard.GetClipboardData(html_format)
        except Exception:
            return None
        finally:
            try:
                win32clipboard.CloseClipboard()
            except Exception:
                pass
        html_fragment = self._extract_html_fragment(raw)
        parser = ClipboardHtmlParser()
        parser.feed(html_fragment)
        return parser.fragment()

    def _extract_html_fragment(self, value) -> str:
        raw_bytes = value if isinstance(value, bytes) else str(value).encode("utf-8", errors="ignore")
        header = raw_bytes[:512].decode("ascii", errors="ignore")
        start_match = re.search(r"StartFragment:(\d+)", header)
        end_match = re.search(r"EndFragment:(\d+)", header)
        if start_match and end_match:
            start = int(start_match.group(1))
            end = int(end_match.group(1))
            if 0 <= start < end <= len(raw_bytes):
                return raw_bytes[start:end].decode("utf-8", errors="ignore")
        text = raw_bytes.decode("utf-8", errors="ignore")
        start_marker = "<!--StartFragment-->"
        end_marker = "<!--EndFragment-->"
        if start_marker in text and end_marker in text:
            return text.split(start_marker, 1)[1].split(end_marker, 1)[0]
        return text

    def _collect_bold_ranges(self) -> list[tuple[int, int]]:
        ranges = []
        tag_ranges = self.notes_widget.tag_ranges("bold")
        for start_index, end_index in zip(tag_ranges[0::2], tag_ranges[1::2]):
            start = self._text_offset(start_index)
            end = self._text_offset(end_index)
            ranges.append((start, end))
        text_length = len(self.notes_widget.get("1.0", "end-1c").rstrip())
        return [(start, min(end, text_length)) for start, end in ranges if start < text_length and end > start]

    def _text_offset(self, index) -> int:
        counted = self.notes_widget.count("1.0", index, "chars", return_ints=True)
        if isinstance(counted, int):
            return counted
        if counted:
            return int(counted[0])
        line_text, column_text = self.notes_widget.index(index).split(".")
        line = int(line_text)
        column = int(column_text)
        if line <= 1:
            return column
        text_before_line = self.notes_widget.get("1.0", f"{line}.0")
        return len(text_before_line) + column

    def _install_popup_tasks(self) -> None:
        try:
            install_tasks(Path(__file__).resolve().parent.parent, self.config)
            self.status_var.set("Tareas programadas creadas correctamente.")
            messagebox.showinfo("Popup instalado", "Se crearon las tareas programadas.")
        except Exception as exc:
            messagebox.showerror("No fue posible instalar el popup", str(exc))


def launch_app(auto_open: bool = False) -> None:
    logging.basicConfig(level=logging.INFO)
    root = Tk()
    style = ttk.Style()
    if "vista" in style.theme_names():
        style.theme_use("vista")
    ReportAutomationApp(root, auto_open=auto_open)
    root.mainloop()
