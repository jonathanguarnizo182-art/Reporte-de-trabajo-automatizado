from __future__ import annotations

import re
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

    def send_time_entries(self, entries: list[BitrixTimeEntry]) -> None:
        if not entries:
            raise BitrixBrowserError("No hay entradas seleccionadas para enviar a Bitrix.")
        page = self._active_page()
        self._ensure_task_page(page)
        self._ensure_time_tracking_modal(page)
        for entry in entries:
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
        self._click_add_entry(page)
        date_text = f"{entry.target_date.strftime('%d/%m/%Y')} {entry.start}"
        self._fill_entry_fields(page, date_text, entry.hours, entry.minutes, entry.comment)
        self._click_confirm_entry(page)
        self._wait_until_entry_saved(page, entry)

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
            editable_inputs = self._editable_inputs(container)
            date_input = self._find_input_by_value(editable_inputs, r"\d{2}/\d{2}/\d{4}\s+\d{1,2}:\d{2}")
            hours_input = self._find_input_by_value(editable_inputs, r"\d+\s*h")
            minutes_input = self._find_input_by_value(editable_inputs, r"\d+\s*min")
            if not date_input or not hours_input or not minutes_input:
                self._save_debug_artifacts(page)
                raise BitrixBrowserError("No encontre los campos Fecha, Horas y Minutos en el formulario activo.")
            self._replace_input_value(date_input, date_text)
            self._replace_input_value(hours_input, str(hours))
            self._replace_input_value(minutes_input, str(minutes))
            comment.fill(comment_text)
        except BitrixBrowserError:
            raise
        except Exception as exc:
            self._save_debug_artifacts(page)
            raise BitrixBrowserError(f"No pude llenar el formulario de Bitrix: {exc}") from exc

    def _entry_form_container(self, comment):
        return comment.locator("xpath=ancestor::*[.//input][1]")

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

    def _replace_input_value(self, input_locator, value: str) -> None:
        input_locator.click()
        input_locator.press("Control+A")
        input_locator.type(value)

    def _click_confirm_entry(self, page) -> None:
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

    def _wait_until_entry_saved(self, page, entry: BitrixTimeEntry) -> None:
        expected_date = f"{entry.target_date.strftime('%d/%m/%Y')} {entry.start.lstrip('0')}"
        try:
            page.get_by_text(expected_date, exact=False).first.wait_for(state="visible", timeout=6000)
            return
        except Exception:
            page.wait_for_timeout(1000)

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
