from __future__ import annotations

import re
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
        inputs = self._entry_inputs(page)
        date_text = f"{entry.target_date.strftime('%d/%m/%Y')} {entry.start}"
        inputs[0].fill(date_text)
        inputs[1].fill(str(entry.hours))
        inputs[2].fill(str(entry.minutes))
        comment = page.get_by_placeholder(re.compile("Comentario", re.IGNORECASE))
        comment.fill(entry.comment)
        self._click_confirm_entry(page)
        page.wait_for_timeout(700)

    def _click_add_entry(self, page) -> None:
        if self._entry_form_is_open(page):
            return
        button = page.get_by_text("Agregar entrada", exact=True)
        try:
            target = button.first
            if target.is_visible(timeout=1500) and target.is_enabled(timeout=500):
                target.click()
                page.wait_for_timeout(300)
                return
        except Exception:
            pass
        if self._entry_form_is_open(page):
            return
        raise BitrixBrowserError(
            "El boton Agregar entrada no esta disponible. Abra el formulario de entrada en Seguimiento del tiempo y vuelva a intentar."
        )

    def _entry_form_is_open(self, page) -> bool:
        try:
            return page.get_by_placeholder(re.compile("Comentario", re.IGNORECASE)).first.is_visible(timeout=500)
        except Exception:
            return False

    def _entry_inputs(self, page):
        inputs = page.locator("input:visible")
        matches = []
        for index in range(inputs.count()):
            item = inputs.nth(index)
            try:
                input_type = (item.get_attribute("type", timeout=300) or "").lower()
                if input_type in {"checkbox", "radio", "hidden"}:
                    continue
            except Exception:
                pass
            try:
                value = item.input_value(timeout=500)
            except Exception:
                value = ""
            if re.match(r"\d{2}/\d{2}/\d{4}\s+\d{1,2}:\d{2}", value) or value.endswith((" h", " min")):
                matches.append(item)
        if len(matches) >= 3:
            return matches[-3:]
        raise BitrixBrowserError("No encontre los campos Fecha, Horas y Minutos en el modal de Bitrix.")

    def _click_confirm_entry(self, page) -> None:
        buttons = page.locator("button:visible, [role='button']:visible")
        for index in range(buttons.count() - 1, -1, -1):
            button = buttons.nth(index)
            try:
                label = " ".join((button.inner_text(timeout=300) or "").split())
                aria = button.get_attribute("aria-label", timeout=300) or ""
                class_name = button.get_attribute("class", timeout=300) or ""
                if label in {"", "✓"} and ("ui-btn-primary" in class_name or "primary" in class_name or aria):
                    button.click()
                    return
            except Exception:
                continue
        page.keyboard.press("Enter")

    def _is_visible_text(self, page, text: str) -> bool:
        try:
            return page.get_by_text(text, exact=True).first.is_visible(timeout=800)
        except Exception:
            return False
