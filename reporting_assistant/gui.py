from __future__ import annotations

import logging
import threading
from datetime import date, datetime
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, BooleanVar, StringVar, Text, Tk, Toplevel, filedialog, messagebox, ttk

from .ai_client import AIService
from .audio import AudioRecorder
from .calendar_utils import calculate_effective_hours, classify_day, default_schedule_for_day
from .catalog import ReportCatalog
from .config import AppConfig, load_config, save_config
from .models import GeneratedEntry, ReportTemplate, TimeSegment, WorkdayPayload
from .scheduler import install_tasks
from .word_service import WordReportService, WordUnavailableError


LOGGER = logging.getLogger(__name__)


class UpdateModeDialog(Toplevel):
    def __init__(self, parent, existing_text: str):
        super().__init__(parent)
        self.title("La fecha ya existe")
        self.result = "cancel"
        self.geometry("680x360")
        self.transient(parent)
        self.grab_set()

        ttk.Label(self, text="Ya existe información para esta fecha. Elija cómo continuar.").pack(anchor="w", padx=12, pady=(12, 6))
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


class SettingsDialog(Toplevel):
    def __init__(self, parent, config: AppConfig):
        super().__init__(parent)
        self.title("Configuración")
        self.result: AppConfig | None = None
        self.geometry("700x360")
        self.transient(parent)
        self.grab_set()

        self.reports_dir = StringVar(value=config.reports_dir)
        self.api_key = StringVar(value=config.openai_api_key)
        self.model = StringVar(value=config.openai_model)
        self.transcription_model = StringVar(value=config.transcription_model)
        self.mon_wed = StringVar(value=config.popup_hour_mon_wed)
        self.thu_fri = StringVar(value=config.popup_hour_thu_fri)

        container = ttk.Frame(self, padding=12)
        container.pack(fill=BOTH, expand=True)
        fields = [
            ("Carpeta reportes", self.reports_dir, False),
            ("OpenAI API key", self.api_key, True),
            ("Modelo redacción", self.model, False),
            ("Modelo transcripción", self.transcription_model, False),
            ("Popup lun-mié", self.mon_wed, False),
            ("Popup jue-vie", self.thu_fri, False),
        ]
        for row, (label, variable, masked) in enumerate(fields):
            ttk.Label(container, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(container, textvariable=variable, width=60, show="*" if masked else "").grid(
                row=row,
                column=1,
                sticky="ew",
                pady=4,
            )
        ttk.Button(container, text="Elegir carpeta", command=self._browse_reports_dir).grid(row=0, column=2, padx=(8, 0))

        container.columnconfigure(1, weight=1)
        buttons = ttk.Frame(container)
        buttons.grid(row=len(fields), column=0, columnspan=3, sticky="ew", pady=(16, 0))
        ttk.Button(buttons, text="Guardar", command=self._save).pack(side=LEFT)
        ttk.Button(buttons, text="Cancelar", command=self.destroy).pack(side=LEFT, padx=8)

    def _browse_reports_dir(self) -> None:
        directory = filedialog.askdirectory(initialdir=self.reports_dir.get() or ".")
        if directory:
            self.reports_dir.set(directory)

    def _save(self) -> None:
        self.result = AppConfig(
            reports_dir=self.reports_dir.get().strip(),
            openai_api_key=self.api_key.get().strip(),
            openai_model=self.model.get().strip() or "gpt-5.2",
            transcription_model=self.transcription_model.get().strip() or "gpt-4o-mini-transcribe",
            popup_hour_mon_wed=self.mon_wed.get().strip() or "18:00",
            popup_hour_thu_fri=self.thu_fri.get().strip() or "17:30",
        )
        self.destroy()


class ReportAutomationApp:
    def __init__(self, root: Tk, auto_open: bool = False):
        self.root = root
        self.root.title("Automatización de Reportes de Servicios")
        self.root.geometry("1180x860")

        self.config = load_config()
        self.catalog = ReportCatalog(self.config.reports_dir)
        self.ai_service = AIService(self.config)
        self.recorder = AudioRecorder(self.config.recorder_sample_rate)
        self.word_service = None
        self.templates: list[ReportTemplate] = []
        self.generated_entry: GeneratedEntry | None = None
        self.last_transcript = ""
        self.current_audio_path: Path | None = None

        try:
            self.word_service = WordReportService()
        except WordUnavailableError as exc:
            LOGGER.warning("%s", exc)

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
        self.status_var = StringVar(value="Listo.")
        self.business_day_var = StringVar(value="")

        self._build_ui()
        self._refresh_templates(select_default=True)
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
        ttk.Label(header, text="Reporte").grid(row=0, column=0, sticky="w", pady=4)
        self.report_combo = ttk.Combobox(header, textvariable=self.report_var, state="readonly", width=48)
        self.report_combo.grid(row=0, column=1, sticky="ew", padx=(8, 12), pady=4)
        ttk.Button(header, text="Actualizar lista", command=self._refresh_templates).grid(row=0, column=2, padx=4)
        ttk.Button(header, text="Configuración", command=self._open_settings).grid(row=0, column=3, padx=4)
        ttk.Button(header, text="Instalar popup", command=self._install_popup_tasks).grid(row=0, column=4, padx=4)

        ttk.Label(header, text="Fecha").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(header, textvariable=self.date_var, width=16).grid(row=1, column=1, sticky="w", padx=(8, 12), pady=4)
        ttk.Button(header, text="Aplicar jornada sugerida", command=self._apply_date_defaults).grid(row=1, column=2, padx=4)
        ttk.Label(header, textvariable=self.business_day_var).grid(row=1, column=3, columnspan=2, sticky="w")
        header.columnconfigure(1, weight=1)

        schedule = ttk.LabelFrame(root_frame, text="Jornada")
        schedule.pack(fill="x", pady=(12, 8))
        ttk.Label(schedule, text="Tipo de día").grid(row=0, column=0, sticky="w", padx=8, pady=6)
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
        ttk.Combobox(schedule, textvariable=self.service_mode_var, values=["remoto", "presencial"], state="readonly", width=12).grid(row=1, column=7, sticky="w", padx=8, pady=6)

        body = ttk.Panedwindow(root_frame, orient="horizontal")
        body.pack(fill=BOTH, expand=True, pady=(8, 8))

        left = ttk.Frame(body)
        right = ttk.Frame(body)
        body.add(left, weight=1)
        body.add(right, weight=1)

        notes_frame = ttk.LabelFrame(left, text="Qué hizo en la jornada")
        notes_frame.pack(fill=BOTH, expand=True)
        toolbar = ttk.Frame(notes_frame)
        toolbar.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Button(toolbar, text="Iniciar / detener grabación", command=self._toggle_recording).pack(side=LEFT)
        ttk.Button(toolbar, text="Generar vista previa", command=self._generate_preview).pack(side=LEFT, padx=8)
        ttk.Button(toolbar, text="Limpiar", command=lambda: self.notes_widget.delete("1.0", END)).pack(side=LEFT)
        ttk.Label(toolbar, text="Puede hablar o escribir.").pack(side=RIGHT)
        self.notes_widget = Text(notes_frame, wrap="word")
        self.notes_widget.pack(fill=BOTH, expand=True, padx=8, pady=(0, 8))

        preview_frame = ttk.LabelFrame(right, text="Vista previa del reporte")
        preview_frame.pack(fill=BOTH, expand=True)
        actions = ttk.Frame(preview_frame)
        actions.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Button(actions, text="Guardar en Word", command=self._save_to_word).pack(side=LEFT)
        ttk.Button(actions, text="Copiar al portapapeles", command=self._copy_preview).pack(side=LEFT, padx=8)
        self.preview_widget = Text(preview_frame, wrap="word")
        self.preview_widget.pack(fill=BOTH, expand=True, padx=8, pady=(0, 8))

        footer = ttk.Frame(root_frame)
        footer.pack(fill="x")
        ttk.Label(footer, textvariable=self.status_var).pack(side=LEFT)

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self.root, self.config)
        self.root.wait_window(dialog)
        if dialog.result:
            current = self.config
            current.reports_dir = dialog.result.reports_dir
            current.openai_api_key = dialog.result.openai_api_key
            current.openai_model = dialog.result.openai_model
            current.transcription_model = dialog.result.transcription_model
            current.popup_hour_mon_wed = dialog.result.popup_hour_mon_wed
            current.popup_hour_thu_fri = dialog.result.popup_hour_thu_fri
            save_config(current)
            self.config = load_config()
            self.catalog = ReportCatalog(self.config.reports_dir)
            self.ai_service = AIService(self.config)
            self._refresh_templates(select_default=True)
            self.status_var.set("Configuración guardada.")

    def _refresh_templates(self, select_default: bool = False) -> None:
        self.templates = self.catalog.scan()
        values = [template.display_name for template in self.templates]
        self.report_combo["values"] = values
        if not values:
            self.status_var.set("No se encontraron archivos .docx en la carpeta de reportes.")
            return
        selected = False
        if select_default and self.config.default_report_id:
            for template in self.templates:
                if template.report_id == self.config.default_report_id:
                    self.report_var.set(template.display_name)
                    selected = True
                    break
        if not selected and not self.report_var.get():
            preferred = next(
                (template.display_name for template in self.templates if "EJEMPLO" not in template.display_name.upper()),
                values[0],
            )
            self.report_var.set(preferred)

    def _selected_report(self) -> ReportTemplate:
        display_name = self.report_var.get().strip()
        for template in self.templates:
            if template.display_name == display_name:
                if self.config.default_report_id != template.report_id:
                    self.config.default_report_id = template.report_id
                    save_config(self.config)
                return template
        raise RuntimeError("Seleccione un reporte válido.")

    def _parse_target_date(self) -> date:
        return datetime.strptime(self.date_var.get().strip(), "%Y-%m-%d").date()

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
            self.business_day_var.set("Día hábil" if classification.is_business_day else f"No hábil ({classification.kind})")
        if target_date.weekday() <= 2:
            self.extra_start_var.set("18:00")
            self.extra_end_var.set("21:00")
        else:
            self.extra_start_var.set("17:30")
            self.extra_end_var.set("20:30")

    def _toggle_recording(self) -> None:
        if not self.recorder.available:
            messagebox.showerror("Grabación no disponible", "Instale numpy, sounddevice y soundfile para usar la captura por voz.")
            return
        if self.recorder.stream is None:
            try:
                self.recorder.start()
                self.status_var.set("Grabando... presione nuevamente para detener y transcribir.")
            except Exception as exc:
                messagebox.showerror("Error al grabar", str(exc))
            return

        def worker():
            try:
                audio_path = self.recorder.stop()
                self.current_audio_path = audio_path
                self.root.after(0, lambda: self.status_var.set("Audio capturado. Transcribiendo..."))
                transcript = self.ai_service.transcribe_audio(audio_path)
                self.last_transcript = transcript
                self.root.after(0, lambda: self._append_notes(transcript))
                self.root.after(0, lambda: self.status_var.set("Transcripción lista."))
            except Exception as exc:  # pragma: no cover
                self.root.after(0, lambda: messagebox.showerror("Error de transcripción", str(exc)))
                self.root.after(0, lambda: self.status_var.set("No fue posible transcribir el audio."))

        threading.Thread(target=worker, daemon=True).start()

    def _append_notes(self, text: str) -> None:
        existing = self.notes_widget.get("1.0", END).strip()
        if existing:
            self.notes_widget.insert(END, "\n")
        self.notes_widget.insert(END, text)

    def _collect_segments(self) -> list[TimeSegment]:
        rest_minutes = int(self.break_var.get().strip() or "0")
        segments = [
            TimeSegment(
                start=self.start_var.get().strip(),
                end=self.end_var.get().strip(),
                effective=calculate_effective_hours(
                    self.start_var.get().strip(),
                    self.end_var.get().strip(),
                    rest_minutes=rest_minutes,
                ),
                label="jornada",
            )
        ]
        if self.extra_var.get():
            segments.append(
                TimeSegment(
                    start=self.extra_start_var.get().strip(),
                    end=self.extra_end_var.get().strip(),
                    effective=calculate_effective_hours(
                        self.extra_start_var.get().strip(),
                        self.extra_end_var.get().strip(),
                        rest_minutes=0,
                    ),
                    label="extra",
                )
            )
        return segments

    def _build_payload(self) -> WorkdayPayload:
        report = self._selected_report()
        target_date = self._parse_target_date()
        classification = classify_day(target_date, self.config.country_holidays)
        payload = WorkdayPayload(
            report=report,
            target_date=target_date,
            is_business_day=classification.is_business_day,
            day_type=self.day_type_var.get().strip() or classification.kind,
            segments=self._collect_segments(),
            had_overtime=self.extra_var.get(),
            notes=self.notes_widget.get("1.0", END).strip(),
            transcript=self.last_transcript.strip(),
            context={"service_mode": self.service_mode_var.get().strip()},
        )
        if not payload.notes and not payload.transcript:
            raise RuntimeError("Escriba o dicte lo realizado durante la jornada.")
        return payload

    def _generate_preview(self) -> None:
        try:
            payload = self._build_payload()
        except Exception as exc:
            messagebox.showerror("Datos incompletos", str(exc))
            return

        self.status_var.set("Generando vista previa con IA...")

        def worker():
            entry = self.ai_service.generate_daily_entry(payload)
            self.generated_entry = entry
            self.root.after(0, lambda: self._show_preview(entry.full_text))
            self.root.after(0, lambda: self.status_var.set("Vista previa generada."))

        threading.Thread(target=worker, daemon=True).start()

    def _show_preview(self, text: str) -> None:
        self.preview_widget.delete("1.0", END)
        self.preview_widget.insert("1.0", text)

    def _save_to_word(self) -> None:
        if self.word_service is None:
            messagebox.showerror("Word no disponible", "Instale pywin32 para escribir en documentos Word.")
            return
        if self.generated_entry is None and not self.preview_widget.get("1.0", END).strip():
            messagebox.showerror("Sin vista previa", "Genere o edite primero la vista previa.")
            return

        try:
            payload = self._build_payload()
        except Exception as exc:
            messagebox.showerror("Datos incompletos", str(exc))
            return

        entry = self.generated_entry or GeneratedEntry("", "", [], self.preview_widget.get("1.0", END).strip())
        entry = GeneratedEntry(entry.title, entry.summary, entry.numbered_sections, self.preview_widget.get("1.0", END).strip(), entry.raw_model_response)

        try:
            resolved = self.catalog.resolve_document(payload.report, payload.target_date)
            if resolved.created_from_template:
                self.word_service.prepare_new_month_document(resolved, payload.target_date)
            exists, existing_text = self.word_service.entry_exists(resolved.path, payload.target_date)
            update_mode = "replace"
            if exists:
                dialog = UpdateModeDialog(self.root, existing_text)
                self.root.wait_window(dialog)
                update_mode = dialog.result
                if update_mode == "cancel":
                    self.status_var.set("Operación cancelada.")
                    return
            self.word_service.write_day_block(resolved.path, payload.target_date, entry, payload.segments, update_mode=update_mode)
            self.status_var.set(f"Reporte actualizado en: {resolved.path.name}")
            messagebox.showinfo("Reporte diligenciado", f"Documento actualizado:\n{resolved.path}")
        except Exception as exc:
            LOGGER.exception("Error guardando en Word: %s", exc)
            messagebox.showerror("Error al guardar", str(exc))

    def _copy_preview(self) -> None:
        text = self.preview_widget.get("1.0", END).strip()
        if not text:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status_var.set("Vista previa copiada al portapapeles.")

    def _install_popup_tasks(self) -> None:
        try:
            install_tasks(Path(__file__).resolve().parent.parent, self.config)
            self.status_var.set("Tareas programadas creadas correctamente.")
            messagebox.showinfo("Popup instalado", "Se crearon las tareas programadas para lunes-miércoles y jueves-viernes.")
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
