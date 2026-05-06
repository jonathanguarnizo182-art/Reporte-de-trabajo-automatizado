from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .config import CONFIG_DIR
from .models import BitrixTimeEntry


class BitrixBrowserError(RuntimeError):
    pass


class BitrixBrowserAutomation:
    def __init__(self, profile_dir: str | Path | None = None):
        self.profile_dir = Path(profile_dir) if profile_dir else CONFIG_DIR / "bitrix_chrome_profile"
        self._playwright = None
        self._context = None

    def open_browser(self, url: str = "https://grupo-aci.bitrix24.es/") -> None:
        context = self._ensure_context()
        page = self._active_page()
        page.goto(url, wait_until="domcontentloaded")
        page.bring_to_front()

    def send_time_entries(
        self,
        entries: list[BitrixTimeEntry],
        progress_callback: Callable[[int, int, BitrixTimeEntry], None] | None = None,
    ) -> None:
        if not entries:
            raise BitrixBrowserError("No hay entradas seleccionadas para enviar a Bitrix.")
        self._validate_entries(entries)
        page = self._active_page()
        self._ensure_task_page(page)
        self._ensure_time_tracking_modal(page)
        total = len(entries)
        for index, entry in enumerate(entries, start=1):
            if progress_callback is not None:
                progress_callback(index, total, entry)
            self._add_time_entry(page, entry)

    def close(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
        finally:
            self._context = None
            if self._playwright is not None:
                self._playwright.stop()
                self._playwright = None

    def _ensure_context(self):
        if self._context is not None:
            return self._context
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BitrixBrowserError(
                "Playwright no esta instalado. Ejecute: pip install -r requirements.txt"
            ) from exc
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                str(self.profile_dir),
                channel="chrome",
                headless=False,
                viewport={"width": 1400, "height": 900},
                args=["--start-maximized"],
            )
        except Exception:
            self._playwright.stop()
            self._playwright = None
            raise
        return self._context

    def _active_page(self):
        context = self._ensure_context()
        if not context.pages:
            return context.new_page()
        page = context.pages[-1]
        page.bring_to_front()
        return page

    def _ensure_task_page(self, page) -> None:
        if "bitrix24" not in page.url or "/tasks/task/view/" not in page.url:
            raise BitrixBrowserError(
                "Abra manualmente la tarea de Bitrix en el Chrome controlado antes de enviar."
            )

    def _ensure_time_tracking_modal(self, page) -> None:
        if self._is_visible_text(page, "Seguimiento del tiempo"):
            return
        candidates = [
            page.get_by_text("Seguimiento del tiempo", exact=True),
            page.locator("[title*='Seguimiento'][title*='tiempo']"),
            page.locator("[aria-label*='Seguimiento'][aria-label*='tiempo']"),
        ]
        for candidate in candidates:
            try:
                if candidate.first.is_visible(timeout=1200):
                    candidate.first.click()
                    page.get_by_text("Seguimiento del tiempo", exact=True).wait_for(timeout=5000)
                    return
            except Exception:
                continue
        raise BitrixBrowserError(
            "No pude abrir el modal Seguimiento del tiempo. Abra ese modal en Bitrix y vuelva a enviar."
        )

    def _add_time_entry(self, page, entry: BitrixTimeEntry) -> None:
        self._discard_open_entry_form(page)
        self._click_add_entry(page)
        date_text = f"{entry.target_date.strftime('%d/%m/%Y')} {entry.start}"
        self._fill_entry_fields(page, date_text, entry.hours, entry.minutes, entry.comment)
        self._click_confirm_entry(page)
        self._wait_until_entry_saved(page, entry)

    def _validate_entries(self, entries: list[BitrixTimeEntry]) -> None:
        for entry in entries:
            if not entry.target_date or not entry.start.strip() or not entry.comment.strip():
                raise BitrixBrowserError("Hay entradas incompletas en el reporte seleccionado.")
            if entry.hours < 0 or entry.minutes < 0 or entry.minutes >= 60:
                label = entry.source_label or entry.target_date.strftime("%d/%m/%Y")
                raise BitrixBrowserError(f"La duracion de {label} no es valida: {entry.duration_text}.")
            if entry.hours == 0 and entry.minutes == 0:
                label = entry.source_label or entry.target_date.strftime("%d/%m/%Y")
                raise BitrixBrowserError(f"La duracion de {label} esta en cero.")

    def _click_add_entry(self, page) -> None:
        if self._entry_form_is_open(page):
            return
        button = page.locator("button:visible, [role='button']:visible").filter(has_text="Agregar entrada")
        for index in range(button.count()):
            try:
                target = button.nth(index)
                if target.is_visible(timeout=600) and target.is_enabled(timeout=600):
                    target.click()
                    self._wait_for_entry_form(page)
                    return
            except Exception:
                continue
        if self._entry_form_is_open(page):
            return
        raise BitrixBrowserError(
            "El boton Agregar entrada no esta disponible. Abra el formulario de entrada en Seguimiento del tiempo y vuelva a intentar."
        )

    def _wait_for_entry_form(self, page) -> None:
        try:
            page.get_by_placeholder(re.compile("Comentario", re.IGNORECASE)).first.wait_for(state="visible", timeout=5000)
            return
        except Exception as exc:
            self._save_debug_artifacts(page)
            raise BitrixBrowserError("Bitrix no mostro el formulario de entrada despues de pulsar Agregar entrada.") from exc

    def _entry_form_is_open(self, page) -> bool:
        try:
            return page.get_by_placeholder(re.compile("Comentario", re.IGNORECASE)).first.is_visible(timeout=500)
        except Exception:
            return False

    def _fill_entry_fields(self, page, date_text: str, hours: int, minutes: int, comment_text: str) -> None:
        comment = page.get_by_placeholder(re.compile("Comentario", re.IGNORECASE)).first
        try:
            comment.wait_for(state="visible", timeout=3000)
            container = self._entry_form_container(comment)
            date_input, hours_input, minutes_input = self._time_entry_inputs(container)
            if date_input is None or hours_input is None or minutes_input is None:
                editable_inputs = self._editable_inputs(container)
                date_input = self._find_input_by_value(editable_inputs, r"\d{2}/\d{2}/\d{4}\s+\d{1,2}:\d{2}")
                hours_input = self._find_input_by_value(editable_inputs, r"\d+\s*h")
                minutes_input = self._find_input_by_value(editable_inputs, r"\d+\s*min")
            if date_input is None or hours_input is None or minutes_input is None:
                page_inputs = self._editable_inputs(page)
                if date_input is None:
                    date_input = self._find_input_by_label(page_inputs, "Fecha")
                if hours_input is None:
                    hours_input = self._find_input_by_label(page_inputs, "Horas:")
                if minutes_input is None:
                    minutes_input = self._find_input_by_label(page_inputs, "Minutos:")
            if date_input is None or hours_input is None or minutes_input is None:
                self._save_debug_artifacts(page)
                raise BitrixBrowserError("No encontre los campos Fecha, Horas y Minutos en el formulario activo.")
            self._replace_input_value(date_input, date_text, self._date_time_pattern(date_text))
            self._replace_input_value(hours_input, str(hours), rf"\b{hours}\b")
            self._replace_input_value(minutes_input, str(minutes), rf"\b{minutes}\b")
            comment.fill(comment_text)
            self._assert_comment_value(comment, comment_text)
        except BitrixBrowserError:
            raise
        except Exception as exc:
            self._save_debug_artifacts(page)
            raise BitrixBrowserError(f"No pude llenar el formulario de Bitrix: {exc}") from exc

    def _entry_form_container(self, comment):
        return comment.locator("xpath=ancestor::*[.//input][1]")

    def _time_entry_inputs(self, container):
        date_input = self._first_visible_enabled(
            container.locator(".tasks-time-tracking-list-item-edit-time-calendar input")
        )
        time_inputs = container.locator(".tasks-time-tracking-list-item-edit-time-field input")
        hours_input = self._nth_visible_enabled(time_inputs, 0)
        minutes_input = self._nth_visible_enabled(time_inputs, 1)
        return date_input, hours_input, minutes_input

    def _first_visible_enabled(self, locator):
        return self._nth_visible_enabled(locator, 0)

    def _nth_visible_enabled(self, locator, visible_index: int):
        seen = 0
        for index in range(locator.count()):
            item = locator.nth(index)
            try:
                if item.is_visible(timeout=300) and item.is_enabled(timeout=300):
                    if seen == visible_index:
                        return item
                    seen += 1
            except Exception:
                continue
        return None

    def _editable_inputs(self, container):
        inputs = container.locator("input:visible")
        output = []
        for index in range(inputs.count()):
            item = inputs.nth(index)
            try:
                input_type = (item.get_attribute("type", timeout=300) or "").lower()
                readonly = item.get_attribute("readonly", timeout=300)
                disabled = item.get_attribute("disabled", timeout=300)
                if input_type in {"checkbox", "radio", "hidden"} or readonly is not None or disabled is not None:
                    continue
                if item.is_visible(timeout=300) and item.is_enabled(timeout=300):
                    output.append(item)
            except Exception:
                continue
        return output

    def _find_input_by_value(self, inputs: list, pattern: str):
        regex = re.compile(pattern, re.IGNORECASE)
        for item in inputs:
            try:
                if regex.search(item.input_value(timeout=300)):
                    return item
            except Exception:
                continue
        return None

    def _find_input_by_label(self, inputs: list, label_text: str):
        for item in inputs:
            try:
                label = item.locator(
                    "xpath=ancestor::*[contains(@class, 'ui-system-input')][1]/*[contains(@class, 'ui-system-input-label')]"
                )
                if label.first.is_visible(timeout=300):
                    text = " ".join(label.first.inner_text(timeout=300).split())
                    if text.lower() == label_text.lower():
                        return item
            except Exception:
                continue
        return None

    def _date_time_pattern(self, value: str) -> str:
        match = re.fullmatch(r"(\d{2}/\d{2}/\d{4})\s+0?(\d{1,2}):(\d{2})", value.strip())
        if not match:
            return re.escape(value.strip())
        day, hour, minute = match.groups()
        return rf"{re.escape(day)}\s+0?{int(hour)}:{re.escape(minute)}"

    def _replace_input_value(self, input_locator, value: str, expected_pattern: str | None = None) -> None:
        input_locator.click()
        input_locator.press("Control+A")
        input_locator.press("Backspace")
        input_locator.type(value, delay=10)
        input_locator.evaluate(
            """(element) => {
                element.dispatchEvent(new Event('input', { bubbles: true }));
                element.dispatchEvent(new Event('change', { bubbles: true }));
                element.dispatchEvent(new Event('blur', { bubbles: true }));
            }"""
        )
        input_locator.press("Tab")
        if expected_pattern is None:
            return
        regex = re.compile(expected_pattern)
        current = ""
        for _ in range(10):
            current = input_locator.input_value(timeout=500)
            if regex.search(current):
                return
            input_locator.page.wait_for_timeout(100)
        input_locator.evaluate(
            """(element, nextValue) => {
                element.value = nextValue;
                element.dispatchEvent(new Event('input', { bubbles: true }));
                element.dispatchEvent(new Event('change', { bubbles: true }));
                element.dispatchEvent(new Event('blur', { bubbles: true }));
            }""",
            value,
        )
        input_locator.press("Tab")
        for _ in range(10):
            current = input_locator.input_value(timeout=500)
            if regex.search(current):
                return
            input_locator.page.wait_for_timeout(100)
        raise BitrixBrowserError(f"Bitrix no acepto el valor '{value}'. Valor visible actual: '{current}'.")

    def _assert_comment_value(self, comment_locator, expected: str) -> None:
        current = ""
        for _ in range(10):
            current = comment_locator.input_value(timeout=500)
            if current.strip() == expected.strip():
                return
            comment_locator.page.wait_for_timeout(100)
        raise BitrixBrowserError("Bitrix no acepto el comentario completo en el formulario activo.")

    def _click_confirm_entry_legacy(self, page) -> None:
        comment = page.get_by_placeholder(re.compile("Comentario", re.IGNORECASE)).first
        container = self._entry_form_container(comment)
        buttons = container.locator("button:visible, [role='button']:visible")
        for index in range(buttons.count()):
            button = buttons.nth(index)
            try:
                label = " ".join((button.inner_text(timeout=300) or "").split())
                class_name = button.get_attribute("class", timeout=300) or ""
                if label in {"", "✓"} and ("ui-btn-primary" in class_name or "primary" in class_name):
                    button.click()
                    return
            except Exception:
                continue
        page.keyboard.press("Control+Enter")

    def _wait_until_entry_saved_legacy(self, page, entry: BitrixTimeEntry) -> None:
        expected_date = f"{entry.target_date.strftime('%d/%m/%Y')} {entry.start.lstrip('0')}"
        try:
            page.get_by_text(expected_date, exact=False).first.wait_for(state="visible", timeout=6000)
            return
        except Exception:
            page.wait_for_timeout(1000)

    def _click_confirm_entry(self, page) -> None:
        comment = page.get_by_placeholder(re.compile("Comentario", re.IGNORECASE)).first
        container = self._entry_form_container(comment)
        if self._click_check_button(container.locator("button:visible")):
            return
        if self._click_check_button(page.locator("button:visible")):
            return
        self._save_debug_artifacts(page)
        raise BitrixBrowserError("No pude pulsar el chulo azul para guardar la entrada de tiempo.")

    def _click_check_button(self, buttons) -> bool:
        for index in range(buttons.count()):
            button = buttons.nth(index)
            try:
                label = " ".join((button.inner_text(timeout=300) or "").split())
                class_name = button.get_attribute("class", timeout=300) or ""
                icon = button.locator(".ui-icon-set").first
                icon_class = icon.get_attribute("class", timeout=300) if icon.count() else ""
                is_check_icon = "--check" in (icon_class or "")
                is_filled_icon_button = "--style-filled" in class_name and "--with-icon" in class_name
                if is_check_icon or (label in {"", "✓"} and is_filled_icon_button):
                    button.click()
                    return True
            except Exception:
                continue
        return False

    def _wait_until_entry_saved(self, page, entry: BitrixTimeEntry) -> None:
        expected_date = f"{entry.target_date.strftime('%d/%m/%Y')} {entry.start.lstrip('0')}"
        expected_duration = f"{entry.hours:02d}:{entry.minutes:02d}:00"
        try:
            page.locator(".tasks-time-tracking-list-item-edit").wait_for(state="detached", timeout=8000)
            page.get_by_text(expected_date, exact=False).first.wait_for(state="visible", timeout=6000)
            page.get_by_text(expected_duration, exact=False).first.wait_for(state="visible", timeout=6000)
            return
        except Exception as exc:
            self._save_debug_artifacts(page)
            raise BitrixBrowserError(
                f"No pude confirmar que Bitrix guardara la entrada {expected_date} ({expected_duration})."
            ) from exc

    def _discard_open_entry_form(self, page) -> None:
        forms = page.locator(".tasks-time-tracking-list-item-edit")
        if forms.count() == 0:
            return
        form = forms.first
        try:
            close_icon = form.locator(".tasks-time-tracking-list-item-edit-close-icon").first
            if close_icon.is_visible(timeout=500):
                close_icon.click()
                form.wait_for(state="detached", timeout=3000)
        except Exception as exc:
            self._save_debug_artifacts(page)
            raise BitrixBrowserError("Hay un formulario de Bitrix abierto que no se pudo cancelar antes de continuar.") from exc

    def _save_debug_artifacts(self, page) -> None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        debug_dir = CONFIG_DIR / "bitrix_debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        try:
            page.screenshot(path=str(debug_dir / f"bitrix_{timestamp}.png"), full_page=True)
        except Exception:
            pass
        try:
            (debug_dir / f"bitrix_{timestamp}.html").write_text(page.content(), encoding="utf-8")
        except Exception:
            pass

    def _is_visible_text(self, page, text: str) -> bool:
        try:
            return page.get_by_text(text, exact=True).first.is_visible(timeout=800)
        except Exception:
            return False
