from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .config import CONFIG_DIR
from .models import BitrixTaskTarget, BitrixTimeEntry, TimeSegment


class BitrixBrowserError(RuntimeError):
    pass


@dataclass(slots=True)
class ExistingTimeEntry:
    id: str
    target_date: date
    start: str
    seconds: int
    text: str


class BitrixBrowserAutomation:
    BASE_URL = "https://grupo-aci.bitrix24.es"

    def __init__(self, profile_dir: str | Path | None = None):
        self.profile_dir = Path(profile_dir) if profile_dir else CONFIG_DIR / "bitrix_chrome_profile"
        self._playwright = None
        self._context = None

    def open_browser(self, url: str = "https://grupo-aci.bitrix24.es/") -> None:
        context = self._ensure_context()
        page = self._active_page()
        page.goto(url, wait_until="domcontentloaded")
        page.bring_to_front()

    def find_task_for_project(self, project_code: str) -> BitrixTaskTarget:
        project_code = project_code.strip()
        if not project_code:
            raise BitrixBrowserError("El reporte no tiene Codigo Proyecto para buscar la tarea en Bitrix.")
        page = self._active_page()
        self._open_tasks_page(page)
        self._search_task(page, project_code)
        return self._open_unique_task_result(page, project_code)

    def automate_report_for_task(
        self,
        task: BitrixTaskTarget,
        entries: list[BitrixTimeEntry],
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        if not entries:
            raise BitrixBrowserError("No hay dias diligenciados para automatizar en Bitrix.")
        self._validate_entries(entries)
        page = self._active_page()
        if not self._current_page_has_task(page, task.task_id):
            page.goto(self._canonical_task_url(page, task.task_id), wait_until="domcontentloaded")
            self._soft_wait_after_navigation(page)
        self._ensure_task_page(page)
        self._wait_for_task_card_ready(page, task)
        total = len(entries)
        for index, entry in enumerate(entries, start=1):
            self._progress(
                progress_callback,
                f"Seguimiento {index}/{total} - {entry.target_date.strftime('%d/%m/%Y')} {entry.start}",
            )
            action = self._sync_time_entry(page, entry)
            self._progress(
                progress_callback,
                f"Seguimiento {index}/{total} {action} - {entry.target_date.strftime('%d/%m/%Y')} {entry.start}",
            )

        self._close_time_tracking_modal(page)
        workday_entries = [
            segment_entry
            for entry in entries
            for segment_entry in self._workday_segment_entries(entry)
        ]
        workday_total = len(workday_entries)
        for index, entry in enumerate(workday_entries, start=1):
            self._progress(
                progress_callback,
                f"Jornada {index}/{workday_total} - {entry.target_date.strftime('%d/%m/%Y')} {entry.start}-{entry.end}",
            )
            self._fill_workday_entry(page, entry)
            page.reload(wait_until="domcontentloaded")
            self._soft_wait_after_navigation(page)

        self._open_worktime_page(page)
        for index, entry in enumerate(workday_entries, start=1):
            self._progress(
                progress_callback,
                f"Vinculando jornada {index}/{workday_total} - {entry.target_date.strftime('%d/%m/%Y')} {entry.start}-{entry.end}",
            )
            self._link_worktime_day_to_task(page, entry, task.url)

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
        task_id = self._task_id_from_url(page.url)
        self._wait_for_task_card_ready(
            page,
            BitrixTaskTarget(task_id=task_id, title="", url=self._canonical_task_url(page, task_id)),
        )
        total = len(entries)
        for index, entry in enumerate(entries, start=1):
            if progress_callback is not None:
                progress_callback(index, total, entry)
            self._sync_time_entry(page, entry)

    def _progress(self, callback: Callable[[str], None] | None, message: str) -> None:
        if callback is not None:
            callback(message)

    def _workday_segment_entries(self, entry: BitrixTimeEntry) -> list[BitrixTimeEntry]:
        segments = entry.segments or [
            TimeSegment(
                start=entry.start,
                end=entry.end,
                effective=entry.duration_text,
                label="tramo 1",
            )
        ]
        output: list[BitrixTimeEntry] = []
        for index, segment in enumerate(segments):
            duration = self._parse_effective_duration(segment.effective)
            if duration is None:
                raise BitrixBrowserError(
                    f"La duracion del tramo {index + 1} de {entry.source_label} no es valida: {segment.effective}."
                )
            output.append(
                BitrixTimeEntry(
                    target_date=entry.target_date,
                    start=segment.start,
                    hours=duration[0],
                    minutes=duration[1],
                    comment=entry.comment,
                    source_label=f"{entry.target_date.strftime('%d/%m/%Y')} {segment.start}",
                    end=segment.end,
                    break_duration="01:00" if index == 0 else "00:00",
                    workday_reason=entry.workday_reason,
                    segments=[segment],
                )
            )
        return output

    def _soft_wait_after_navigation(self, page, timeout_ms: int = 1800) -> None:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        page.wait_for_timeout(timeout_ms)

    def _open_tasks_page(self, page) -> None:
        user_id = self._current_user_id(page)
        page.goto(f"{self.BASE_URL}/company/personal/user/{user_id}/tasks/", wait_until="domcontentloaded")
        self._soft_wait_after_navigation(page)
        try:
            page.get_by_text("Tareas y proyectos", exact=True).first.wait_for(state="visible", timeout=6000)
        except Exception:
            pass

    def _search_task(self, page, query: str) -> None:
        search_inputs = [
            page.get_by_placeholder(re.compile("buscar", re.IGNORECASE)).first,
            page.locator(".main-ui-filter-search input:visible").first,
            page.locator("input[class*='main-ui-filter']:visible").first,
            page.locator("input:visible").first,
        ]
        last_error = None
        for search in search_inputs:
            try:
                search.wait_for(state="visible", timeout=5000)
                search.click()
                search.press("Control+A")
                search.fill(query)
                search.press("Enter")
                page.wait_for_timeout(2500)
                return
            except Exception as exc:
                last_error = exc
                continue
        self._save_debug_artifacts(page)
        raise BitrixBrowserError(f"No pude escribir el Codigo Proyecto en el buscador de tareas: {last_error}")

    def _open_unique_task_result(self, page, project_code: str) -> BitrixTaskTarget:
        result = page.evaluate(
            """(query) => {
                const normalize = (value) => (value || '')
                    .normalize('NFD')
                    .replace(/[\\u0300-\\u036f]/g, '')
                    .replace(/\\s+/g, ' ')
                    .trim()
                    .toLowerCase();
                const wanted = normalize(query);
                const anchors = [...document.querySelectorAll('a[href*="/tasks/task/view/"]')];
                const allTasks = [];
                const strongMatches = [];
                for (const anchor of anchors) {
                    const href = anchor.href || '';
                    const id = (href.match(/\\/tasks\\/task\\/view\\/(\\d+)\\//) || [])[1];
                    if (!id) {
                        continue;
                    }
                    const row = anchor.closest('tr, .main-grid-row, [class*="task"], [class*="item"]');
                    const anchorTitle = (anchor.innerText || anchor.textContent || '').trim();
                    const rowTitle = (row?.innerText || row?.textContent || anchorTitle).trim();
                    const text = normalize(`${anchorTitle} ${rowTitle}`);
                    const item = { id, title: anchorTitle || rowTitle, url: href, text };
                    if (!allTasks.some((existing) => existing.id === id)) {
                        allTasks.push(item);
                    }
                    if (text.includes(wanted) && !strongMatches.some((existing) => existing.id === id)) {
                        strongMatches.push(item);
                    }
                }
                if (strongMatches.length > 0) {
                    return strongMatches.map(({ id, title, url }) => ({ id, title, url }));
                }
                if (allTasks.length === 1) {
                    return allTasks.map(({ id, title, url }) => ({ id, title, url }));
                }
                const queryTokens = wanted.split(/\\s+/).filter((token) => token.length >= 3);
                const tokenMatches = allTasks.filter((item) => queryTokens.every((token) => item.text.includes(token)));
                return tokenMatches.map(({ id, title, url }) => ({ id, title, url }));
            }""",
            project_code,
        )
        if not result:
            self._save_debug_artifacts(page)
            raise BitrixBrowserError(f"No encontre una tarea de Bitrix para: {project_code}")
        if len(result) > 1:
            options = "; ".join(item.get("title", "") for item in result[:5])
            self._save_debug_artifacts(page)
            raise BitrixBrowserError(f"El Codigo Proyecto coincide con varias tareas. Opciones: {options}")
        target = result[0]
        clean_url = self._canonical_task_url(page, int(target["id"]))
        page.goto(clean_url, wait_until="domcontentloaded")
        self._soft_wait_after_navigation(page)
        return BitrixTaskTarget(task_id=int(target["id"]), title=target["title"], url=clean_url)

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

    def _current_page_has_task(self, page, task_id: int) -> bool:
        return f"/tasks/task/view/{task_id}/" in page.url

    def _canonical_task_url(self, page, task_id: int) -> str:
        user_id = self._current_user_id(page)
        return f"{self.BASE_URL}/company/personal/user/{user_id}/tasks/task/view/{task_id}/"

    def _wait_for_task_card_ready(self, page, task: BitrixTaskTarget, timeout_ms: int = 65000) -> None:
        start = time.monotonic()
        deadline = start + timeout_ms / 1000
        reopened = False
        reloaded = False
        clean_url = self._canonical_task_url(page, task.task_id)
        while time.monotonic() < deadline:
            if self._task_card_is_ready(page, task.task_id):
                return
            elapsed = time.monotonic() - start
            if not reopened and elapsed > 5:
                self._open_full_task_card_from_shell(page, task)
                reopened = True
            if not reloaded and elapsed > 18:
                page.goto(clean_url, wait_until="domcontentloaded")
                self._soft_wait_after_navigation(page, 2200)
                reloaded = True
            page.wait_for_timeout(500)
        self._save_debug_artifacts(page)
        raise BitrixBrowserError(
            "Bitrix se quedo cargando la tarea y no mostro la tarjeta completa. "
            "No se escribio ningun seguimiento de tiempo."
        )

    def _task_card_is_ready(self, page, task_id: int) -> bool:
        for context in self._interactive_contexts(page):
            try:
                ready = context.evaluate(
                    """(taskId) => {
                        const isVisible = (node) => {
                            if (!node) {
                                return false;
                            }
                            const style = window.getComputedStyle(node);
                            const box = node.getBoundingClientRect();
                            return style.display !== 'none'
                                && style.visibility !== 'hidden'
                                && box.width > 0
                                && box.height > 0;
                        };
                        const busy = [...document.querySelectorAll('[aria-busy="true"], .ui-skeleton, .tasks-skeleton')]
                            .some(isVisible);
                        const fullCard = document.querySelector(`.tasks-full-card[data-task-id="${taskId}"]`);
                        const title = document.querySelector(`[data-task-id="${taskId}"][data-task-field-id="title"]`);
                        const tracking = document.querySelector('.tasks-task-time-tracking');
                        const addButton = [...document.querySelectorAll('button, [role="button"]')]
                            .some((node) => (node.textContent || '').trim() === 'Agregar entrada');
                        return Boolean((fullCard || title) && (tracking || addButton) && !busy);
                    }""",
                    str(task_id),
                )
                if ready:
                    return True
            except Exception:
                continue
        return False

    def _open_full_task_card_from_shell(self, page, task: BitrixTaskTarget) -> None:
        try:
            user_id = self._current_user_id(page)
            page.evaluate(
                """async ({ taskId, url, closeCompleteUrl }) => {
                    const root = window.top || window;
                    const runtime = root.BX?.Runtime || window.BX?.Runtime;
                    if (!runtime?.loadExtension) {
                        return;
                    }
                    const module = await runtime.loadExtension('tasks.v2.application.task-card');
                    const taskCard = module?.TaskCard || root.BX?.Tasks?.V2?.Application?.TaskCard;
                    taskCard?.showFullCard?.({ taskId, closeCompleteUrl, url });
                }""",
                {
                    "taskId": task.task_id,
                    "url": f"/company/personal/user/{user_id}/tasks/task/view/{task.task_id}/",
                    "closeCompleteUrl": f"/company/personal/user/{user_id}/tasks/",
                },
            )
        except Exception:
            pass

    def _interactive_contexts(self, page):
        contexts = [page]
        try:
            contexts.extend(frame for frame in page.frames if frame is not page.main_frame)
        except Exception:
            pass
        return contexts

    def _ensure_time_tracking_modal(self, page):
        modal_context = self._time_tracking_modal_context(page)
        if modal_context is not None:
            return modal_context
        modal_context = self._open_time_tracking_modal_by_dom(page)
        if modal_context is not None:
            return modal_context
        for context in self._interactive_contexts(page):
            candidates = [
                context.locator(".tasks-task-time-tracking:visible").first,
                context.locator("[data-task-field-id*='timeTracking']:visible").first,
                context.locator("[data-task-chip-id*='timeTracking']:visible").first,
                context.get_by_text(re.compile(r"Seguimiento\s+del\s+tiempo", re.IGNORECASE)).first,
                context.locator("[title*='Seguimiento'][title*='tiempo']").first,
                context.locator("[aria-label*='Seguimiento'][aria-label*='tiempo']").first,
                context.locator("[class*='time-tracking']:visible").first,
                context.locator("[class*='timer']:visible").filter(has_text=re.compile(r"\d{1,3}:\d{2}")).first,
            ]
            for candidate in candidates:
                try:
                    if candidate.is_visible(timeout=1200):
                        candidate.click()
                        modal_context = self._wait_for_time_tracking_modal(page)
                        if modal_context is not None:
                            return modal_context
                except Exception:
                    modal_context = self._time_tracking_modal_context(page)
                    if modal_context is not None:
                        return modal_context
                    continue
        modal_context = self._time_tracking_modal_context(page)
        if modal_context is not None:
            return modal_context
        self._save_debug_artifacts(page)
        raise BitrixBrowserError(
            "No pude abrir ni detectar el modal Seguimiento del tiempo en la pagina ni en sus iframes."
        )

    def _open_time_tracking_modal_by_dom(self, page):
        for context in self._interactive_contexts(page):
            try:
                opened = context.evaluate(
                    """() => {
                        const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                        const isVisible = (node) => {
                            if (!node) {
                                return false;
                            }
                            const style = window.getComputedStyle(node);
                            const box = node.getBoundingClientRect();
                            return style.display !== 'none'
                                && style.visibility !== 'hidden'
                                && box.width > 0
                                && box.height > 0;
                        };
                        const rows = [...document.querySelectorAll('.b24-field-list-row, .tasks-task-time-tracking')];
                        const row = rows.find((node) => {
                            if (!isVisible(node)) {
                                return false;
                            }
                            if (node.matches('.tasks-task-time-tracking')) {
                                return true;
                            }
                            return normalize(node.textContent).includes('seguimiento del tiempo');
                        });
                        if (!row) {
                            return false;
                        }
                        const target = row.querySelector('.b24-hover-pill, button, [role="button"]') || row;
                        target.scrollIntoView({ block: 'center', inline: 'center' });
                        for (const eventName of ['pointerdown', 'mousedown', 'mouseup', 'click']) {
                            target.dispatchEvent(new MouseEvent(eventName, {
                                bubbles: true,
                                cancelable: true,
                                view: window,
                            }));
                        }
                        return true;
                    }"""
                )
                if opened:
                    modal_context = self._wait_for_time_tracking_modal(page)
                    if modal_context is not None:
                        return modal_context
            except Exception:
                modal_context = self._time_tracking_modal_context(page)
                if modal_context is not None:
                    return modal_context
                continue
        return None

    def _time_tracking_modal_context(self, page):
        for context in self._interactive_contexts(page):
            if self._time_tracking_modal_is_open_in_context(context):
                return context
        return None

    def _time_tracking_modal_is_open(self, page) -> bool:
        return self._time_tracking_modal_context(page) is not None

    def _time_tracking_modal_is_open_in_context(self, context) -> bool:
        checks = [
            context.get_by_text("Agregar entrada", exact=True).first,
            context.locator(".tasks-task-time-tracking-sheet:visible").first,
            context.locator(".tasks-time-tracking-list:visible").first,
            context.locator("button:visible, [role='button']:visible").filter(has_text="Agregar entrada").first,
            context.get_by_placeholder(re.compile("Comentario", re.IGNORECASE)).first,
        ]
        for check in checks:
            try:
                if check.is_visible(timeout=500):
                    return True
            except Exception:
                continue
        return False

    def _wait_for_time_tracking_modal(self, page):
        for _ in range(12):
            modal_context = self._time_tracking_modal_context(page)
            if modal_context is not None:
                return modal_context
            page.wait_for_timeout(250)
        self._save_debug_artifacts(page)
        raise BitrixBrowserError("Bitrix abrio la tarea, pero no pude confirmar el modal Seguimiento del tiempo.")

    def _close_time_tracking_modal(self, page) -> None:
        for context in self._interactive_contexts(page):
            try:
                close_buttons = context.locator(
                    ".tasks-task-time-tracking-sheet-close, [aria-label*='Cerrar'], [title*='Cerrar']"
                )
                for index in range(close_buttons.count()):
                    button = close_buttons.nth(index)
                    if button.is_visible(timeout=300):
                        button.click()
                        page.wait_for_timeout(800)
                        return
            except Exception:
                pass

    def _fill_workday_entry(self, page, entry: BitrixTimeEntry) -> None:
        if not entry.end:
            raise BitrixBrowserError(f"La fecha {entry.source_label} no tiene hora fin en el reporte.")
        self._open_workday_widget(page)
        self._start_workday_if_needed(page)
        self._open_workday_editor(page)
        self._fill_workday_editor(page, entry)

    def _open_workday_widget(self, page) -> None:
        candidates = [
            page.locator("[class*='timeman']:visible").filter(has_text=re.compile(r"\d{1,2}:\d{2}")).first,
            page.locator("[class*='timeman']:visible").first,
            page.locator("[title*='Tiempo']:visible").first,
            page.locator("[aria-label*='Tiempo']:visible").first,
            page.locator("button:visible, [role='button']:visible").filter(has_text=re.compile(r"\d{1,2}:\d{2}")).last,
            page.get_by_text(re.compile(r"\d{1,2}:\d{2}")).last,
        ]
        for candidate in candidates:
            try:
                if candidate.is_visible(timeout=1500):
                    candidate.click()
                    page.wait_for_timeout(1000)
                    return
            except Exception:
                continue
        if self._open_workday_widget_by_dom(page):
            return
        self._save_debug_artifacts(page)
        raise BitrixBrowserError("No pude abrir el widget superior de Tiempo de trabajo.")

    def _open_workday_widget_by_dom(self, page) -> bool:
        try:
            opened = page.evaluate(
                """() => {
                    const isVisible = (node) => {
                        if (!node) {
                            return false;
                        }
                        const style = window.getComputedStyle(node);
                        const box = node.getBoundingClientRect();
                        return style.display !== 'none'
                            && style.visibility !== 'hidden'
                            && box.width > 0
                            && box.height > 0;
                    };
                    const candidates = [...document.querySelectorAll(
                        '[class*="timeman"], [id*="timeman"], [title*="Tiempo"], [aria-label*="Tiempo"], button, [role="button"]'
                    )].filter(isVisible);
                    const target = candidates.find((node) => {
                        const text = (node.textContent || node.getAttribute('title') || node.getAttribute('aria-label') || '').trim();
                        return /tiempo|jornada|\\d{1,2}:\\d{2}/i.test(text);
                    });
                    if (!target) {
                        return false;
                    }
                    target.scrollIntoView({ block: 'center', inline: 'center' });
                    for (const eventName of ['pointerdown', 'mousedown', 'mouseup', 'click']) {
                        target.dispatchEvent(new MouseEvent(eventName, {
                            bubbles: true,
                            cancelable: true,
                            view: window,
                        }));
                    }
                    return true;
                }"""
            )
            if opened:
                page.wait_for_timeout(1200)
                return True
        except Exception:
            pass
        return False

    def _start_workday_if_needed(self, page) -> None:
        if self._workday_menu_has_state(page, "En el trabajo"):
            return
        if self._start_workday_from_profile_menu(page):
            return
        self._save_debug_artifacts(page)
        raise BitrixBrowserError("No pude iniciar el widget superior de Tiempo de trabajo.")

    def _workday_menu_has_state(self, page, state_text: str) -> bool:
        try:
            return bool(
                page.evaluate(
                    """(stateText) => {
                        const normalize = (value) => (value || '')
                            .normalize('NFD')
                            .replace(/[\\u0300-\\u036f]/g, '')
                            .replace(/\\s+/g, ' ')
                            .trim()
                            .toLowerCase();
                        const isVisible = (node) => {
                            if (!node) {
                                return false;
                            }
                            const style = window.getComputedStyle(node);
                            const box = node.getBoundingClientRect();
                            return style.display !== 'none'
                                && style.visibility !== 'hidden'
                                && box.width > 0
                                && box.height > 0;
                        };
                        const wanted = normalize(stateText);
                        const panels = [...document.querySelectorAll(
                            '#bx-avatar-header-popup, .tm-control-panel, .timeman-instant-container, [data-testid="bx-avatar-widget-content-main"]'
                        )];
                        return panels.some((node) => isVisible(node) && normalize(node.textContent).includes(wanted));
                    }""",
                    state_text,
                )
            )
        except Exception:
            return False

    def _start_workday_from_profile_menu(self, page) -> bool:
        try:
            clicked = page.evaluate(
                """() => {
                    const isVisible = (node) => {
                        if (!node) {
                            return false;
                        }
                        const style = window.getComputedStyle(node);
                        const box = node.getBoundingClientRect();
                        return style.display !== 'none'
                            && style.visibility !== 'hidden'
                            && box.width > 0
                            && box.height > 0;
                    };
                    const clickNode = (node) => {
                        node.scrollIntoView({ block: 'center', inline: 'center' });
                        for (const eventName of ['pointerdown', 'mousedown', 'mouseup', 'click']) {
                            node.dispatchEvent(new MouseEvent(eventName, {
                                bubbles: true,
                                cancelable: true,
                                view: window,
                            }));
                        }
                    };
                    const containers = [...document.querySelectorAll('div, section, article')]
                        .filter((node) => isVisible(node) && /fuera del trabajo/i.test(node.textContent || ''));
                    for (const container of containers) {
                        const buttons = [...container.querySelectorAll('button, [role="button"], span, div')]
                            .filter((node) => isVisible(node) && /^\\s*(Iniciar|Empezar)\\s*$/i.test(node.textContent || ''));
                        if (buttons.length > 0) {
                            clickNode(buttons[0]);
                            return true;
                        }
                    }
                    return false;
                }"""
            )
            if clicked:
                for _ in range(10):
                    page.wait_for_timeout(300)
                    if self._workday_menu_has_state(page, "En el trabajo"):
                        return True
                self._open_workday_widget(page)
                for _ in range(8):
                    page.wait_for_timeout(300)
                    if self._workday_menu_has_state(page, "En el trabajo"):
                        return True
        except Exception:
            pass
        return False

    def _open_workday_editor(self, page) -> None:
        if self._workday_editor_is_open(page):
            return
        candidates = [
            page.locator("[title*='Editar']:visible").first,
            page.locator("[aria-label*='Editar']:visible").first,
            page.locator(".ui-icon-set.--edit, .ui-icon-set.--pencil, [class*='edit'], [class*='pencil']").first,
        ]
        for candidate in candidates:
            try:
                if candidate.is_visible(timeout=1200):
                    candidate.click()
                    self._wait_for_workday_editor(page)
                    return
            except Exception:
                continue
        if self._open_workday_editor_by_dom(page):
            return
        self._save_debug_artifacts(page)
        raise BitrixBrowserError("No pude abrir la edicion del dia de trabajo.")

    def _workday_editor_is_open(self, page) -> bool:
        try:
            return page.get_by_text(re.compile(r"Editar\s+el\s+d[ií]a\s+de\s+trabajo", re.IGNORECASE)).first.is_visible(
                timeout=500
            )
        except Exception:
            return False

    def _wait_for_workday_editor(self, page) -> None:
        page.get_by_text(re.compile(r"Editar\s+el\s+d[ií]a\s+de\s+trabajo", re.IGNORECASE)).first.wait_for(
            timeout=6000
        )

    def _open_workday_editor_by_dom(self, page) -> bool:
        try:
            opened = page.evaluate(
                """() => {
                    const isVisible = (node) => {
                        if (!node) {
                            return false;
                        }
                        const style = window.getComputedStyle(node);
                        const box = node.getBoundingClientRect();
                        return style.display !== 'none'
                            && style.visibility !== 'hidden'
                            && box.width > 0
                            && box.height > 0;
                    };
                    const clickNode = (node) => {
                        node.scrollIntoView({ block: 'center', inline: 'center' });
                        for (const eventName of ['pointerdown', 'mousedown', 'mouseup', 'click']) {
                            node.dispatchEvent(new MouseEvent(eventName, {
                                bubbles: true,
                                cancelable: true,
                                view: window,
                            }));
                        }
                    };
                    const explicitEditor = document.querySelector('.tm-timer__editor-opener');
                    if (isVisible(explicitEditor)) {
                        clickNode(explicitEditor);
                        return true;
                    }
                    const explicitEditorIcon = document.querySelector('.tm-timer__editor-opener-img');
                    if (isVisible(explicitEditorIcon)) {
                        clickNode(explicitEditorIcon.closest('button') || explicitEditorIcon);
                        return true;
                    }
                    const containers = [...document.querySelectorAll('div, section, article')]
                        .filter((node) => isVisible(node) && /en el trabajo|fuera del trabajo/i.test(node.textContent || ''));
                    const scopedCandidates = [];
                    for (const container of containers) {
                        scopedCandidates.push(...container.querySelectorAll(
                            '[title*="Editar"], [aria-label*="Editar"], [class*="edit"], [class*="pencil"], svg, i, span, button'
                        ));
                    }
                    const globalCandidates = [...document.querySelectorAll(
                        '[title*="Editar"], [aria-label*="Editar"], [class*="edit"], [class*="pencil"], .ui-icon-set, svg, i, span, button'
                    )];
                    const candidates = [...scopedCandidates, ...globalCandidates].filter(isVisible);
                    const target = candidates.find((node) => {
                        const text = `${node.textContent || ''} ${node.getAttribute('title') || ''} ${node.getAttribute('aria-label') || ''} ${node.className || ''}`;
                        const box = node.getBoundingClientRect();
                        return /edit|editar|pencil|pen|lapiz|lápiz/i.test(text)
                            || (box.width <= 40 && box.height <= 40 && containers.some((container) => container.contains(node)));
                    });
                    if (!target) {
                        return false;
                    }
                    clickNode(target);
                    return true;
                }"""
            )
            if opened:
                self._wait_for_workday_editor(page)
                return True
        except Exception:
            pass
        return False

    def _fill_workday_editor(self, page, entry: BitrixTimeEntry) -> None:
        date_text = entry.target_date.strftime("%d/%m/%Y")
        try:
            self._fill_workday_editor_by_dom(page, entry)
            self._click_save_workday(page)
        except Exception as exc:
            self._save_debug_artifacts(page)
            raise BitrixBrowserError(f"No pude diligenciar el tiempo de trabajo de {date_text}: {exc}") from exc

    def _fill_workday_editor_by_dom(self, page, entry: BitrixTimeEntry) -> None:
        date_text = entry.target_date.strftime("%d/%m/%Y")
        start_hour, start_minute = self._parse_start_time(entry.start)
        end_hour, end_minute = self._parse_start_time(entry.end)
        payload = {
            "start": entry.start,
            "end": entry.end,
            "startHour": f"{start_hour:02d}",
            "startMinute": f"{start_minute:02d}",
            "endHour": f"{end_hour:02d}",
            "endMinute": f"{end_minute:02d}",
            "dateText": date_text,
            "breakDuration": entry.break_duration,
            "reason": entry.workday_reason,
        }
        result = None
        for _ in range(24):
            result = page.evaluate(
                """({ start, end, startHour, startMinute, endHour, endMinute, dateText, breakDuration, reason }) => {
                const isVisible = (node) => {
                    if (!node) {
                        return false;
                    }
                    const style = window.getComputedStyle(node);
                    const box = node.getBoundingClientRect();
                    return style.display !== 'none'
                        && style.visibility !== 'hidden'
                        && box.width > 0
                        && box.height > 0;
                };
                const popups = [...document.querySelectorAll('[id^="timeman_edit_popup_"]')]
                    .filter((node) => isVisible(node))
                    .map((node) => ({
                        node,
                        title: /editar\\s+el\\s+d[ií]a\\s+de\\s+trabajo/i.test(node.textContent || ''),
                        timeCount: node.querySelectorAll('input.bxc-cus-sel').length,
                        hiddenCount: node.querySelectorAll('input[name="timeman_edit_from"], input[name="timeman_edit_to"]').length,
                    }))
                    .sort((a, b) => {
                        const score = (item) => (item.title ? 100 : 0) + item.timeCount * 10 + item.hiddenCount;
                        return score(b) - score(a);
                    });
                const popup = popups[0]?.node || null;
                if (!popup) {
                    return { ok: false, notReady: true, error: 'No encontre el popup de edicion de tiempo de trabajo.' };
                }
                const setValue = (element, value) => {
                    if (!element) {
                        return false;
                    }
                    element.value = value;
                    for (const eventName of ['input', 'change', 'keyup', 'blur']) {
                        element.dispatchEvent(new Event(eventName, { bubbles: true }));
                    }
                    return true;
                };
                const timeInputs = [...popup.querySelectorAll('input.bxc-cus-sel')];
                if (timeInputs.length < 4) {
                    return {
                        ok: false,
                        notReady: true,
                        error: `Bitrix mostro ${timeInputs.length} campos de hora, se esperaban 4.`,
                    };
                }
                setValue(popup.querySelector('input[name="timeman_edit_from"]'), start);
                setValue(popup.querySelector('input[name="timeman_edit_to"]'), end);
                setValue(timeInputs[0], startHour);
                setValue(timeInputs[1], startMinute);
                setValue(timeInputs[2], endHour);
                setValue(timeInputs[3], endMinute);

                const dateInputs = [...popup.querySelectorAll('input[data-role="date-picker"], input.bx-tm-popup-clock-wnd-custom-date-picker')];
                for (const input of dateInputs) {
                    setValue(input, dateText);
                }
                const breakInput = popup.querySelector('input.bx-tm-report-edit');
                if (breakInput) {
                    setValue(breakInput, breakDuration);
                }
                const textarea = popup.querySelector('textarea');
                if (textarea) {
                    setValue(textarea, reason);
                }
                return {
                    ok: true,
                    fromHidden: popup.querySelector('input[name="timeman_edit_from"]')?.value || '',
                    toHidden: popup.querySelector('input[name="timeman_edit_to"]')?.value || '',
                    visibleTimes: timeInputs.slice(0, 4).map((input) => input.value),
                    dateValues: dateInputs.map((input) => input.value),
                    breakValue: breakInput?.value || '',
                    reasonValue: textarea?.value || '',
                };
            }""",
                payload,
            )
            if result and result.get("ok"):
                break
            if not isinstance(result, dict) or not result.get("notReady"):
                break
            page.wait_for_timeout(250)
        if not result or not result.get("ok"):
            error = result.get("error") if isinstance(result, dict) else "respuesta vacia"
            raise BitrixBrowserError(f"Bitrix no permitio llenar el formulario de jornada: {error}")
        expected_times = [f"{start_hour:02d}", f"{start_minute:02d}", f"{end_hour:02d}", f"{end_minute:02d}"]
        if result.get("fromHidden") != entry.start or result.get("toHidden") != entry.end:
            raise BitrixBrowserError(
                f"Bitrix no tomo las horas ocultas. Esperado {entry.start}-{entry.end}, "
                f"visible {result.get('fromHidden')}-{result.get('toHidden')}."
            )
        if result.get("visibleTimes") != expected_times:
            raise BitrixBrowserError(
                f"Bitrix no tomo las horas visibles. Esperado {expected_times}, visible {result.get('visibleTimes')}."
            )
        date_values = result.get("dateValues") or []
        if date_values and any(value != date_text for value in date_values):
            raise BitrixBrowserError(f"Bitrix no tomo la fecha {date_text}. Valores visibles: {date_values}.")
        if result.get("breakValue") and result.get("breakValue") != entry.break_duration:
            raise BitrixBrowserError(
                f"Bitrix no tomo el descanso {entry.break_duration}. Valor visible: {result.get('breakValue')}."
            )

    def _click_change_day(self, page) -> None:
        try:
            page.get_by_text("Cambiar el dia", exact=False).first.click()
            page.wait_for_timeout(800)
        except Exception:
            try:
                page.get_by_text("Cambiar el día", exact=False).first.click()
                page.wait_for_timeout(800)
            except Exception:
                pass

    def _find_workday_break_input(self, page):
        inputs = self._visible_inputs(page)
        candidates = [
            item
            for item in inputs
            if re.search(r"^\d{1,2}:\d{2}$", self._input_value(item).strip()) and self._input_value(item).strip() not in {"00:00"}
        ]
        if candidates:
            return candidates[-1]
        return None

    def _click_save_workday(self, page) -> None:
        clicked = page.evaluate(
            """() => {
                const isVisible = (node) => {
                    if (!node) {
                        return false;
                    }
                    const style = window.getComputedStyle(node);
                    const box = node.getBoundingClientRect();
                    return style.display !== 'none'
                        && style.visibility !== 'hidden'
                        && box.width > 0
                        && box.height > 0;
                };
                const popup = [...document.querySelectorAll('[id^="timeman_edit_popup_"]')]
                    .find((node) => isVisible(node));
                if (!popup) {
                    return { ok: false, error: 'No encontre el popup de edicion.' };
                }
                const buttons = [...popup.querySelectorAll('button, [role="button"]')].filter(isVisible);
                const button = buttons.find((node) => /^(guardar|finalizar)$/i.test((node.textContent || '').trim()))
                    || buttons.find((node) => (node.className || '').includes('popup-window-button-create'));
                if (!button) {
                    return { ok: false, error: `Botones visibles: ${buttons.map((node) => (node.textContent || '').trim()).join(', ')}` };
                }
                button.scrollIntoView({ block: 'center', inline: 'center' });
                for (const eventName of ['pointerdown', 'mousedown', 'mouseup', 'click']) {
                    button.dispatchEvent(new MouseEvent(eventName, {
                        bubbles: true,
                        cancelable: true,
                        view: window,
                    }));
                }
                return { ok: true, label: (button.textContent || '').trim() };
            }"""
        )
        if not clicked or not clicked.get("ok"):
            error = clicked.get("error") if isinstance(clicked, dict) else "respuesta vacia"
            self._save_debug_artifacts(page)
            raise BitrixBrowserError(f"No pude pulsar Guardar/Finalizar en el dia de trabajo: {error}")
        for _ in range(20):
            page.wait_for_timeout(250)
            if not self._workday_editor_is_open(page):
                return
        self._save_debug_artifacts(page)
        raise BitrixBrowserError("Bitrix no cerro la edicion del dia de trabajo despues de guardar.")

    def _open_worktime_page(self, page) -> None:
        candidates = [
            page.get_by_text("Tiempo de trabajo", exact=True).first,
            page.locator("[title*='Tiempo de trabajo']:visible").first,
        ]
        for candidate in candidates:
            try:
                if candidate.is_visible(timeout=1500):
                    candidate.click()
                    page.wait_for_timeout(1800)
                    return
            except Exception:
                continue
        user_id = self._current_user_id(page)
        page.goto(f"{self.BASE_URL}/company/personal/user/{user_id}/timeman/", wait_until="domcontentloaded")
        self._soft_wait_after_navigation(page)

    def _current_user_id(self, page) -> str:
        try:
            value = page.evaluate("() => window.BX?.message?.('USER_ID') || window.BX?.message?.USER_ID")
            if value:
                return str(value)
        except Exception:
            pass
        match = re.search(r"/company/personal/user/(\d+)/", page.url)
        if match:
            return match.group(1)
        return "487"

    def _link_worktime_day_to_task(self, page, entry: BitrixTimeEntry, task_url: str) -> None:
        date_label = entry.target_date.strftime("%d/%m/%Y")
        self._open_worktime_day_details(page, entry)
        if self._worktime_detail_contains_task_url(page, task_url):
            self._close_worktime_detail(page)
            return
        try:
            editor = page.locator("textarea:visible, [contenteditable='true']:visible").last
            if editor.evaluate("el => el.tagName.toLowerCase()") == "textarea":
                editor.fill(task_url)
            else:
                editor.click()
                page.keyboard.type(task_url)
            page.wait_for_timeout(1200)
            send = page.locator("button:visible, [role='button']:visible").filter(has_text=re.compile("ENVIAR", re.IGNORECASE))
            send.first.click()
            page.wait_for_timeout(1200)
            self._close_worktime_detail(page)
        except Exception as exc:
            self._save_debug_artifacts(page)
            raise BitrixBrowserError(f"No pude vincular la jornada {date_label} con la tarea: {exc}") from exc

    def _open_worktime_day_details(self, page, entry: BitrixTimeEntry) -> None:
        durations = [(entry.hours, entry.minutes)]
        clock_duration = self._clock_duration(entry.start, entry.end)
        if clock_duration is not None and clock_duration not in durations:
            durations.append(clock_duration)
        duration_patterns = [
            pattern
            for hours, minutes in durations
            for pattern in (f"{hours} h {minutes} m", f"{hours} h", f"{hours:02d}:{minutes:02d}")
        ]
        for pattern in duration_patterns:
            try:
                candidates = page.get_by_text(pattern, exact=False)
                for index in range(candidates.count()):
                    target = candidates.nth(index)
                    if not target.is_visible(timeout=500):
                        continue
                    target.click()
                    page.wait_for_timeout(1200)
                    if self._worktime_detail_matches_entry(page, entry):
                        return
                    self._close_worktime_detail(page)
            except Exception:
                continue
        self._save_debug_artifacts(page)
        raise BitrixBrowserError(
            f"No pude abrir un detalle de Tiempo de trabajo que coincida con "
            f"{entry.target_date.strftime('%d/%m/%Y')} {entry.start}-{entry.end}."
        )

    def _worktime_detail_matches_entry(self, page, entry: BitrixTimeEntry) -> bool:
        try:
            text = self._normalize_text(page.locator("body").inner_text(timeout=1500))
        except Exception:
            return False
        if entry.start not in text or entry.end not in text:
            return False
        day_no_zero = f"{entry.target_date.day}/{entry.target_date.month:02d}/{entry.target_date.year}"
        date_tokens = [
            entry.target_date.strftime("%d/%m/%Y"),
            entry.target_date.strftime("%d.%m.%Y"),
            day_no_zero,
        ]
        return any(token and token in text for token in date_tokens) or bool(re.search(r"\bTotal\b", text))

    def _worktime_detail_contains_task_url(self, page, task_url: str) -> bool:
        try:
            text = page.locator("body").inner_text(timeout=1500)
        except Exception:
            return False
        canonical = task_url.rstrip("/")
        return canonical in text or f"{canonical}/" in text

    def _close_worktime_detail(self, page) -> None:
        selectors = [
            "[aria-label*='Cerrar']:visible",
            "[title*='Cerrar']:visible",
            ".popup-window-close-icon:visible",
            ".ui-sidepanel-close:visible",
        ]
        for selector in selectors:
            try:
                buttons = page.locator(selector)
                for index in range(buttons.count()):
                    button = buttons.nth(index)
                    if button.is_visible(timeout=250):
                        button.click()
                        page.wait_for_timeout(700)
                        return
            except Exception:
                continue
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
        except Exception:
            pass

    def _sync_time_entry(self, page, entry: BitrixTimeEntry) -> str:
        existing_entries = self._list_existing_time_entries(page)
        same_date = [item for item in existing_entries if item.target_date == entry.target_date]
        date_label = entry.target_date.strftime("%d/%m/%Y")
        if len(same_date) > 1:
            raise BitrixBrowserError(
                f"Bitrix tiene {len(same_date)} registros de Seguimiento del tiempo para {date_label}. "
                "No debe existir mas de un registro con la misma fecha para este proyecto. "
                "Corrija esos duplicados en Bitrix y vuelva a ejecutar."
            )
        if not same_date:
            self._add_time_entry(page, entry)
            return "creado"
        existing = same_date[0]
        if self._existing_time_entry_matches(existing, entry):
            return "omitido"
        self._update_time_entry(page, existing, entry)
        return "actualizado"

    def _list_existing_time_entries(self, page) -> list[ExistingTimeEntry]:
        internal_items = self._list_existing_time_entries_internal(page)
        if internal_items is not None:
            return internal_items
        try:
            modal_context = self._ensure_time_tracking_modal(page)
        except BitrixBrowserError as exc:
            raise BitrixBrowserError(
                "No pude leer los registros actuales de Seguimiento del tiempo. "
                "Para evitar duplicados no se escribio nada."
            ) from exc
        modal_items = self._list_existing_time_entries_from_modal(modal_context)
        if modal_items is not None:
            return modal_items
        self._save_debug_artifacts(page)
        raise BitrixBrowserError(
            "Bitrix no expuso los registros actuales de Seguimiento del tiempo. "
            "Para evitar duplicados no se escribio nada."
        )

    def _list_existing_time_entries_internal(self, page) -> list[ExistingTimeEntry] | None:
        task_id = self._task_id_from_url(page.url)
        for frame in page.frames:
            try:
                result = frame.evaluate(
                    """async (taskId) => {
                        const runtime = window.BX?.Runtime || window.top?.BX?.Runtime;
                        const tryLoad = async (name) => {
                            try {
                                if (runtime?.loadExtension) {
                                    await runtime.loadExtension(name);
                                }
                            } catch (_) {}
                        };
                        await tryLoad('tasks.v2.application.task-card');
                        await tryLoad('tasks.v2.provider.service.time-tracking-service');
                        await tryLoad('tasks.v2.component.fields.time-tracking');
                        const bxCandidates = [window.BX, window.top?.BX].filter(Boolean);
                        const normalize = (item) => ({
                            id: String(item?.id ?? item?.ID ?? item?.elapsedId ?? ''),
                            taskId: Number(item?.taskId ?? item?.TASK_ID ?? item?.task_id ?? taskId),
                            createdAtTs: Number(item?.createdAtTs ?? item?.CREATED_DATE_TS ?? item?.createdAt ?? 0),
                            seconds: Number(item?.seconds ?? item?.SECONDS ?? item?.duration ?? 0),
                            text: String(item?.text ?? item?.TEXT ?? item?.comment ?? item?.COMMENT_TEXT ?? ''),
                        });
                        for (const bx of bxCandidates) {
                            const service = bx?.Tasks?.V2?.Provider?.Service?.timeTrackingService;
                            if (service?.list) {
                                const listed = await service.list(Number(taskId), { reset: true });
                                const source = Array.isArray(listed)
                                    ? listed
                                    : Array.isArray(listed?.items)
                                        ? listed.items
                                        : Array.isArray(listed?.elapsedTimes)
                                            ? listed.elapsedTimes
                                            : [];
                                if (source.length > 0) {
                                    return { ok: true, items: source.map(normalize), source: 'service' };
                                }
                            }
                            const store = bx?.Tasks?.V2?.Core?.getStore?.();
                            const model = bx?.Tasks?.V2?.Const?.Model?.ElapsedTimes;
                            const getters = store?.getters || {};
                            const candidates = [];
                            if (model) {
                                for (const name of ['getByTaskId', 'getListByTaskId', 'getAll', 'getList']) {
                                    const getter = getters[`${model}/${name}`];
                                    if (typeof getter === 'function') {
                                        try {
                                            candidates.push(getter(Number(taskId)));
                                        } catch (_) {
                                            try {
                                                candidates.push(getter(String(taskId)));
                                            } catch (__) {}
                                        }
                                    }
                                }
                            }
                            for (const candidate of candidates) {
                                const source = Array.isArray(candidate)
                                    ? candidate
                                    : Array.isArray(candidate?.items)
                                        ? candidate.items
                                        : candidate && typeof candidate === 'object'
                                            ? Object.values(candidate)
                                            : [];
                                const filtered = source
                                    .map(normalize)
                                    .filter((item) => !item.taskId || item.taskId === Number(taskId));
                                if (filtered.length > 0) {
                                    return { ok: true, items: filtered, source: 'store' };
                                }
                            }
                        }
                        return { ok: false, reason: 'no-internal-source' };
                    }""",
                    task_id,
                )
            except Exception:
                continue
            if isinstance(result, dict) and result.get("ok"):
                return [item for item in (self._parse_existing_time_entry(raw) for raw in result.get("items", [])) if item]
        return None

    def _list_existing_time_entries_from_modal(self, context) -> list[ExistingTimeEntry] | None:
        try:
            rows = context.evaluate(
                """() => {
                    const rowNodes = [...document.querySelectorAll('.tasks-time-tracking-list-item')]
                        .filter((node) => !node.querySelector('.tasks-time-tracking-list-item-edit'));
                    if (rowNodes.length === 0) {
                        return [];
                    }
                    const output = [];
                    for (const row of rowNodes) {
                        const text = (row.innerText || row.textContent || '').replace(/\\s+/g, ' ').trim();
                        const dateMatch = text.match(/\\b\\d{2}\\/\\d{2}\\/\\d{4}\\s+\\d{1,2}:\\d{2}\\b/);
                        const durationMatch = text.match(/\\b\\d{1,3}:\\d{2}:\\d{2}\\b/);
                        const commentNode = row.querySelector(
                            '.tasks-time-tracking-list-item-view-text, [class*="comment"], [class*="description"]'
                        );
                        const comment = (commentNode?.innerText || commentNode?.textContent || '').trim();
                        if (dateMatch && durationMatch) {
                            output.push({
                                id: row.getAttribute('data-id') || row.dataset?.id || '',
                                visibleDate: dateMatch[0],
                                durationText: durationMatch[0],
                                text: comment || text,
                            });
                        }
                    }
                    return output;
                }"""
            )
        except Exception:
            return None
        output = []
        for raw in rows or []:
            parsed = self._parse_existing_time_entry(raw)
            if parsed is not None:
                output.append(parsed)
        return output

    def _parse_existing_time_entry(self, raw: dict) -> ExistingTimeEntry | None:
        if not isinstance(raw, dict):
            return None
        parsed_date, parsed_start = self._parse_existing_date_time(raw)
        if parsed_date is None:
            return None
        seconds = self._parse_existing_seconds(raw)
        return ExistingTimeEntry(
            id=str(raw.get("id") or ""),
            target_date=parsed_date,
            start=parsed_start,
            seconds=seconds,
            text=str(raw.get("text") or ""),
        )

    def _parse_existing_date_time(self, raw: dict) -> tuple[date | None, str]:
        visible = str(raw.get("visibleDate") or "")
        match = re.search(r"(\d{2})/(\d{2})/(\d{4})\s+(\d{1,2}):(\d{2})", visible)
        if match:
            day, month, year, hour, minute = match.groups()
            return date(int(year), int(month), int(day)), f"{int(hour):02d}:{int(minute):02d}"
        timestamp = raw.get("createdAtTs")
        try:
            if timestamp:
                parsed = datetime.fromtimestamp(int(timestamp))
                return parsed.date(), f"{parsed.hour:02d}:{parsed.minute:02d}"
        except Exception:
            return None, ""
        return None, ""

    def _parse_existing_seconds(self, raw: dict) -> int:
        value = raw.get("seconds")
        try:
            if value is not None:
                return int(value)
        except Exception:
            pass
        duration_text = str(raw.get("durationText") or "")
        match = re.search(r"(\d{1,3}):(\d{2}):(\d{2})", duration_text)
        if match:
            hours, minutes, seconds = (int(part) for part in match.groups())
            return hours * 3600 + minutes * 60 + seconds
        return 0

    def _existing_time_entry_matches(self, existing: ExistingTimeEntry, entry: BitrixTimeEntry) -> bool:
        return (
            existing.target_date == entry.target_date
            and existing.start == entry.start
            and existing.seconds == self._entry_seconds(entry)
            and self._normalize_text(existing.text) == self._normalize_text(entry.comment)
        )

    def _entry_seconds(self, entry: BitrixTimeEntry) -> int:
        return entry.hours * 3600 + entry.minutes * 60

    def _normalize_text(self, value: str) -> str:
        return " ".join((value or "").split()).strip()

    def _update_time_entry(self, page, existing: ExistingTimeEntry, entry: BitrixTimeEntry) -> None:
        service_error = None
        if existing.id:
            try:
                saved_id = self._update_time_entry_via_bitrix_service(page, existing, entry)
                self._wait_until_entry_saved(page, entry, saved_id)
                self._confirm_synced_time_entry(page, entry)
                return
            except BitrixBrowserError as exc:
                service_error = exc
        try:
            modal_context = self._ensure_time_tracking_modal(page)
            self._open_existing_time_entry_editor(page, modal_context, existing, entry)
            saved_id = self._save_entry_via_vue_form(page, modal_context, entry)
            self._wait_until_entry_saved(page, entry, saved_id, context=modal_context)
            self._confirm_synced_time_entry(page, entry)
            return
        except BitrixBrowserError as exc:
            self._save_debug_artifacts(page)
            detail = f" Error interno: {service_error}" if service_error else ""
            raise BitrixBrowserError(
                f"No pude actualizar el Seguimiento del tiempo de {entry.target_date.strftime('%d/%m/%Y')}.{detail}"
            ) from exc

    def _update_time_entry_via_bitrix_service(
        self,
        page,
        existing: ExistingTimeEntry,
        entry: BitrixTimeEntry,
    ) -> str:
        task_id = self._task_id_from_url(page.url)
        hours, minutes = self._parse_start_time(entry.start)
        payload = {
            "id": existing.id,
            "year": entry.target_date.year,
            "month": entry.target_date.month,
            "day": entry.target_date.day,
            "hour": hours,
            "minute": minutes,
            "seconds": self._entry_seconds(entry),
            "text": entry.comment,
        }
        errors = []
        for frame in page.frames:
            try:
                result = frame.evaluate(
                    """async ({ taskId, entry }) => {
                        const runtime = window.BX?.Runtime || window.top?.BX?.Runtime;
                        const tryLoad = async (name) => {
                            try {
                                if (runtime?.loadExtension) {
                                    await runtime.loadExtension(name);
                                }
                            } catch (_) {}
                        };
                        await tryLoad('tasks.v2.application.task-card');
                        await tryLoad('tasks.v2.provider.service.time-tracking-service');
                        await tryLoad('tasks.v2.component.fields.time-tracking');
                        const bxCandidates = [window.BX, window.top?.BX].filter(Boolean);
                        const text = bxCandidates.map((bx) => bx?.Text).find(Boolean);
                        const timezone = bxCandidates.map((bx) => bx?.Main?.timezone).find(Boolean);
                        const endpoint = bxCandidates
                            .map((bx) => bx?.Tasks?.V2?.Const?.Endpoint?.TaskTimeTrackingUpdate)
                            .find(Boolean);
                        const apiClient = bxCandidates
                            .map((bx) => bx?.Tasks?.V2?.Lib?.apiClient)
                            .find(Boolean);
                        const localTarget = new Date(
                            entry.year,
                            entry.month - 1,
                            entry.day,
                            entry.hour,
                            entry.minute,
                            0,
                            0,
                        );
                        let createdAtMs = localTarget.getTime();
                        if (timezone?.getOffset) {
                            createdAtMs = localTarget.getTime() - timezone.getOffset(localTarget.getTime());
                            createdAtMs = localTarget.getTime() - timezone.getOffset(createdAtMs);
                        }
                        const item = {
                            id: entry.id,
                            taskId,
                            createdAtTs: Math.floor(createdAtMs / 1000),
                            seconds: entry.seconds,
                            text: entry.text,
                            source: 'manual',
                            rights: { edit: true, remove: true },
                        };
                        for (const bx of bxCandidates) {
                            const service = bx?.Tasks?.V2?.Provider?.Service?.timeTrackingService;
                            if (service?.update) {
                                const attempts = [
                                    () => service.update(taskId, item),
                                    () => service.update(taskId, entry.id, item),
                                    () => service.update(item),
                                ];
                                for (const attempt of attempts) {
                                    try {
                                        const updated = await attempt();
                                        if (service.list) {
                                            await service.list(taskId, { reset: true });
                                        }
                                        return { ok: true, id: String(updated?.id || item.id) };
                                    } catch (_) {}
                                }
                            }
                        }
                        if (apiClient && endpoint) {
                            const updated = await apiClient.post(endpoint, {
                                task: {
                                    id: taskId,
                                    elapsedTime: {
                                        id: item.id,
                                        taskId: item.taskId,
                                        seconds: item.seconds,
                                        source: item.source,
                                        text: item.text,
                                        createdAtTs: item.createdAtTs,
                                        rights: item.rights,
                                    },
                                },
                            });
                            return { ok: true, id: String(updated?.id || item.id) };
                        }
                        return { ok: false, error: 'Bitrix no expuso metodo interno para actualizar seguimiento.' };
                    }""",
                    {"taskId": task_id, "entry": payload},
                )
            except Exception as exc:
                errors.append(f"{frame.url}: {exc}")
                continue
            if result and result.get("ok"):
                return str(result.get("id") or existing.id)
            error = result.get("error") if isinstance(result, dict) else "respuesta vacia"
            errors.append(f"{frame.url}: {error}")
        self._save_debug_artifacts(page)
        detail = " | ".join(errors[-5:]) if errors else "no hubo frames disponibles"
        raise BitrixBrowserError(f"Bitrix no acepto actualizar la entrada por API interna: {detail}")

    def _open_existing_time_entry_editor(self, page, context, existing: ExistingTimeEntry, entry: BitrixTimeEntry) -> None:
        date_label = entry.target_date.strftime("%d/%m/%Y")
        opened = context.evaluate(
            """(dateLabel) => {
                const rows = [...document.querySelectorAll('.tasks-time-tracking-list-item')]
                    .filter((node) => !node.querySelector('.tasks-time-tracking-list-item-edit'));
                const row = rows.find((node) => (node.innerText || node.textContent || '').includes(dateLabel));
                if (!row) {
                    return false;
                }
                const button = row.querySelector(
                    '[class*="--edit"], [class*="edit"], [title*="Editar"], [aria-label*="Editar"], button'
                );
                const target = button || row;
                target.scrollIntoView({ block: 'center', inline: 'center' });
                for (const eventName of ['pointerdown', 'mousedown', 'mouseup', 'click']) {
                    target.dispatchEvent(new MouseEvent(eventName, {
                        bubbles: true,
                        cancelable: true,
                        view: window,
                    }));
                }
                return true;
            }""",
            date_label,
        )
        if not opened:
            self._save_debug_artifacts(page)
            raise BitrixBrowserError(f"No encontre el registro existente de {date_label} para editarlo.")
        self._wait_for_entry_form(page, context)

    def _confirm_synced_time_entry(self, page, entry: BitrixTimeEntry) -> None:
        existing_entries = self._list_existing_time_entries(page)
        same_date = [item for item in existing_entries if item.target_date == entry.target_date]
        if len(same_date) != 1 or not self._existing_time_entry_matches(same_date[0], entry):
            self._save_debug_artifacts(page)
            raise BitrixBrowserError(
                f"No pude confirmar que Seguimiento del tiempo quedara sincronizado para "
                f"{entry.target_date.strftime('%d/%m/%Y')}."
            )

    def _clock_duration(self, start: str, end: str) -> tuple[int, int] | None:
        try:
            start_hour, start_minute = self._parse_start_time(start)
            end_hour, end_minute = self._parse_start_time(end)
        except BitrixBrowserError:
            return None
        total = (end_hour * 60 + end_minute) - (start_hour * 60 + start_minute)
        if total < 0:
            total += 24 * 60
        return total // 60, total % 60

    def _add_time_entry(self, page, entry: BitrixTimeEntry) -> None:
        self._discard_open_entry_form(page)
        service_error = None
        try:
            saved_id = self._add_time_entry_via_bitrix_service(page, entry)
            self._wait_until_entry_saved(page, entry, saved_id)
            return
        except BitrixBrowserError as exc:
            service_error = exc
            self._discard_open_entry_form(page)
        try:
            modal_context = self._ensure_time_tracking_modal(page)
        except BitrixBrowserError as exc:
            raise BitrixBrowserError(
                "No pude guardar la entrada por el servicio interno de Bitrix y tampoco pude usar "
                f"el modal visual. Error interno: {service_error}"
            ) from exc
        try:
            saved_id = self._add_time_entry_via_bitrix_service(page, entry)
            self._wait_until_entry_saved(page, entry, saved_id, context=modal_context)
            return
        except BitrixBrowserError as exc:
            service_error = exc
            self._discard_open_entry_form(page)
            modal_context = self._time_tracking_modal_context(page) or modal_context
        self._click_add_entry(page, modal_context)
        try:
            saved_id = self._save_entry_via_vue_form(page, modal_context, entry)
            self._wait_until_entry_saved(page, entry, saved_id, context=modal_context)
            return
        except BitrixBrowserError:
            pass
        date_text = f"{entry.target_date.strftime('%d/%m/%Y')} {entry.start}"
        self._fill_entry_fields(page, modal_context, date_text, entry.hours, entry.minutes, entry.comment)
        self._click_confirm_entry(page, modal_context)
        self._wait_until_entry_saved(page, entry, context=modal_context)

    def _add_time_entry_via_bitrix_service(self, page, entry: BitrixTimeEntry) -> str:
        task_id = self._task_id_from_url(page.url)
        hours, minutes = self._parse_start_time(entry.start)
        payload = {
            "year": entry.target_date.year,
            "month": entry.target_date.month,
            "day": entry.target_date.day,
            "hour": hours,
            "minute": minutes,
            "seconds": entry.hours * 3600 + entry.minutes * 60,
            "text": entry.comment,
        }
        errors = []
        for frame in page.frames:
            try:
                result = frame.evaluate(
                    """async ({ taskId, entry }) => {
                        const loaded = [];
                        const loadErrors = [];
                        const runtime = window.BX?.Runtime || window.top?.BX?.Runtime;
                        const tryLoad = async (name) => {
                            try {
                                if (runtime?.loadExtension) {
                                    await runtime.loadExtension(name);
                                    loaded.push(name);
                                }
                            } catch (error) {
                                loadErrors.push(`${name}: ${error?.message || error}`);
                            }
                        };
                        await tryLoad('tasks.v2.application.task-card');
                        await tryLoad('tasks.v2.provider.service.time-tracking-service');
                        await tryLoad('tasks.v2.component.fields.time-tracking');
                        const bxCandidates = [window.BX, window.top?.BX].filter(Boolean);
                        const service = bxCandidates
                            .map((bx) => bx?.Tasks?.V2?.Provider?.Service?.timeTrackingService)
                            .find(Boolean);
                        const apiClient = bxCandidates
                            .map((bx) => bx?.Tasks?.V2?.Lib?.apiClient)
                            .find(Boolean);
                        const endpoint = bxCandidates
                            .map((bx) => bx?.Tasks?.V2?.Const?.Endpoint?.TaskTimeTrackingAdd)
                            .find(Boolean);
                        const text = bxCandidates.map((bx) => bx?.Text).find(Boolean);
                        const timezone = bxCandidates.map((bx) => bx?.Main?.timezone).find(Boolean);
                        if (!service && (!apiClient || !endpoint)) {
                            return {
                                ok: false,
                                error: `Bitrix no expuso servicio ni API interna de seguimiento. loaded=${loaded.join(',')} errors=${loadErrors.join(' | ')}`,
                            };
                        }
                        const localTarget = new Date(
                            entry.year,
                            entry.month - 1,
                            entry.day,
                            entry.hour,
                            entry.minute,
                            0,
                            0,
                        );
                        let createdAtMs = localTarget.getTime();
                        if (timezone?.getOffset) {
                            createdAtMs = localTarget.getTime() - timezone.getOffset(localTarget.getTime());
                            createdAtMs = localTarget.getTime() - timezone.getOffset(createdAtMs);
                        }
                        const item = {
                            id: text?.getRandom ? text.getRandom() : `codex-${Date.now()}-${Math.random()}`,
                            taskId,
                            createdAtTs: Math.floor(createdAtMs / 1000),
                            seconds: entry.seconds,
                            text: entry.text,
                            source: 'manual',
                            rights: { edit: true, remove: true },
                        };
                        if (service) {
                            const id = await service.add(taskId, item);
                            if (id) {
                                if (service.list) {
                                    await service.list(taskId, { reset: true });
                                }
                                return { ok: true, id: String(id), method: 'service', createdAtTs: item.createdAtTs };
                            }
                        }
                        if (apiClient && endpoint) {
                            const added = await apiClient.post(endpoint, {
                                task: {
                                    id: taskId,
                                    elapsedTime: {
                                        id: item.id,
                                        taskId: item.taskId,
                                        seconds: item.seconds,
                                        source: item.source,
                                        text: item.text,
                                        createdAtTs: item.createdAtTs,
                                        rights: item.rights,
                                    },
                                },
                            });
                            if (added?.id) {
                                if (service?.list) {
                                    await service.list(taskId, { reset: true });
                                }
                                return { ok: true, id: String(added.id), method: 'api', createdAtTs: item.createdAtTs };
                            }
                            return { ok: false, error: `API interna sin ID de respuesta: ${JSON.stringify(added)}` };
                        }
                        return { ok: false, error: 'Servicio interno no devolvio ID.' };
                    }""",
                    {"taskId": task_id, "entry": payload},
                )
            except Exception as exc:
                errors.append(f"{frame.url}: {exc}")
                continue
            if result and result.get("ok"):
                return str(result["id"])
            error = result.get("error") if isinstance(result, dict) else "respuesta vacia"
            errors.append(f"{frame.url}: {error}")
        self._save_debug_artifacts(page)
        detail = " | ".join(errors[-5:]) if errors else "no hubo frames disponibles"
        raise BitrixBrowserError(f"Bitrix no acepto la entrada de tiempo por API interna: {detail}")

    def _task_id_from_url(self, url: str) -> int:
        match = re.search(r"/tasks/task/view/(\d+)/", url)
        if not match:
            raise BitrixBrowserError("No pude detectar el ID de la tarea en la URL de Bitrix.")
        return int(match.group(1))

    def _parse_start_time(self, value: str) -> tuple[int, int]:
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
        if not match:
            raise BitrixBrowserError(f"La hora de inicio no es valida para Bitrix: {value}.")
        hours = int(match.group(1))
        minutes = int(match.group(2))
        if hours > 23 or minutes > 59:
            raise BitrixBrowserError(f"La hora de inicio no es valida para Bitrix: {value}.")
        return hours, minutes

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
            for index, segment in enumerate(entry.segments):
                if not segment.start.strip() or not segment.end.strip() or not segment.effective.strip():
                    label = entry.source_label or entry.target_date.strftime("%d/%m/%Y")
                    raise BitrixBrowserError(f"El tramo {index + 1} de {label} esta incompleto.")
                if self._parse_effective_duration(segment.effective) is None:
                    label = entry.source_label or entry.target_date.strftime("%d/%m/%Y")
                    raise BitrixBrowserError(f"La duracion del tramo {index + 1} de {label} no es valida.")

    def _save_entry_via_vue_form(self, page, context, entry: BitrixTimeEntry) -> str:
        hours, minutes = self._parse_start_time(entry.start)
        payload = {
            "year": entry.target_date.year,
            "month": entry.target_date.month,
            "day": entry.target_date.day,
            "hour": hours,
            "minute": minutes,
            "seconds": entry.hours * 3600 + entry.minutes * 60,
            "text": entry.comment,
        }
        try:
            result = context.evaluate(
                """async (entry) => {
                    const editForms = [...document.querySelectorAll('.tasks-time-tracking-list-item-edit')];
                    const roots = editForms
                        .map((form) => form.closest('.tasks-time-tracking-list-item') || form)
                        .filter(Boolean);
                    const findComponent = (root) => {
                        const nodes = [root, ...root.querySelectorAll('*')];
                        for (const node of nodes) {
                            let component = node.__vueParentComponent || null;
                            while (component) {
                                const ctx = component.ctx || {};
                                if (typeof ctx.handleSave === 'function') {
                                    return ctx;
                                }
                                component = component.parent || null;
                            }
                        }
                        return null;
                    };
                    let component = null;
                    for (const root of roots) {
                        component = findComponent(root);
                        if (component) {
                            break;
                        }
                    }
                    if (!component) {
                        return { ok: false, error: 'No encontre el componente Vue del formulario de tiempo.' };
                    }
                    const localTarget = new Date(
                        entry.year,
                        entry.month - 1,
                        entry.day,
                        entry.hour,
                        entry.minute,
                        0,
                        0,
                    );
                    const timezone = window.BX?.Main?.timezone;
                    let createdAtMs = localTarget.getTime();
                    if (timezone?.getOffset) {
                        createdAtMs = localTarget.getTime() - timezone.getOffset(localTarget.getTime());
                    }
                    const beforeId = component.localElapsedId || component.elapsedId || '';
                    await component.handleSave({
                        createdAtTs: Math.floor(createdAtMs / 1000),
                        seconds: entry.seconds,
                        text: entry.text,
                        source: 'manual',
                        rights: { edit: true, remove: true },
                    });
                    const id = component.localElapsedId || beforeId || '';
                    return { ok: true, id: String(id) };
                }""",
                payload,
            )
        except Exception as exc:
            self._save_debug_artifacts(page)
            raise BitrixBrowserError(f"No pude guardar la entrada desde el componente visual de Bitrix: {exc}") from exc
        if not result or not result.get("ok"):
            error = result.get("error") if isinstance(result, dict) else "respuesta vacia"
            self._save_debug_artifacts(page)
            raise BitrixBrowserError(f"Bitrix no acepto la entrada desde el componente visual: {error}")
        return str(result.get("id") or "")

    def _click_add_entry(self, page, context) -> None:
        if self._entry_form_is_open(context):
            return
        button = context.locator("button:visible, [role='button']:visible").filter(has_text="Agregar entrada")
        for index in range(button.count()):
            try:
                target = button.nth(index)
                if target.is_visible(timeout=600) and target.is_enabled(timeout=600):
                    target.click()
                    self._wait_for_entry_form(page, context)
                    return
            except Exception:
                continue
        if self._entry_form_is_open(context):
            return
        self._save_debug_artifacts(page)
        raise BitrixBrowserError(
            "El boton Agregar entrada no esta disponible. Abra el formulario de entrada en Seguimiento del tiempo y vuelva a intentar."
        )

    def _wait_for_entry_form(self, page, context) -> None:
        try:
            context.get_by_placeholder(re.compile("Comentario", re.IGNORECASE)).first.wait_for(
                state="visible",
                timeout=5000,
            )
            return
        except Exception as exc:
            self._save_debug_artifacts(page)
            raise BitrixBrowserError("Bitrix no mostro el formulario de entrada despues de pulsar Agregar entrada.") from exc

    def _entry_form_is_open(self, context) -> bool:
        try:
            return context.get_by_placeholder(re.compile("Comentario", re.IGNORECASE)).first.is_visible(timeout=500)
        except Exception:
            return False

    def _fill_entry_fields(self, page, context, date_text: str, hours: int, minutes: int, comment_text: str) -> None:
        comment = context.get_by_placeholder(re.compile("Comentario", re.IGNORECASE)).first
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
                page_inputs = self._editable_inputs(context)
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
            self._replace_input_value(hours_input, str(hours), self._numeric_time_pattern(hours))
            self._replace_input_value(minutes_input, str(minutes), self._numeric_time_pattern(minutes))
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

    def _visible_inputs(self, container):
        inputs = container.locator("input:visible")
        output = []
        for index in range(inputs.count()):
            item = inputs.nth(index)
            try:
                input_type = (item.get_attribute("type", timeout=300) or "").lower()
                disabled = item.get_attribute("disabled", timeout=300)
                if input_type in {"checkbox", "radio", "hidden"} or disabled is not None:
                    continue
                if item.is_visible(timeout=300) and item.is_enabled(timeout=300):
                    output.append(item)
            except Exception:
                continue
        return output

    def _input_value(self, input_locator) -> str:
        try:
            return input_locator.input_value(timeout=300)
        except Exception:
            return ""

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

    def _numeric_time_pattern(self, value: int) -> str:
        if value == 0:
            return r"^$|\b0\b"
        return rf"\b{value}\b"

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

    def _click_confirm_entry(self, page, context) -> None:
        comment = context.get_by_placeholder(re.compile("Comentario", re.IGNORECASE)).first
        container = self._entry_form_container(comment)
        if self._click_check_button(container.locator("button:visible")):
            return
        if self._click_check_button(context.locator("button:visible")):
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

    def _wait_until_entry_saved(self, page, entry: BitrixTimeEntry, saved_id: str | None = None, context=None) -> None:
        if saved_id:
            if self._entry_exists_in_bitrix_store(page, saved_id):
                return
            page.wait_for_timeout(700)
            return
        context = context or page
        expected_date = f"{entry.target_date.strftime('%d/%m/%Y')} {entry.start.lstrip('0')}"
        expected_duration = f"{entry.hours:02d}:{entry.minutes:02d}:00"
        try:
            context.locator(".tasks-time-tracking-list-item-edit").wait_for(state="detached", timeout=8000)
            context.get_by_text(expected_date, exact=False).first.wait_for(state="visible", timeout=6000)
            context.get_by_text(expected_duration, exact=False).first.wait_for(state="visible", timeout=6000)
            return
        except Exception as exc:
            self._save_debug_artifacts(page)
            raise BitrixBrowserError(
                f"No pude confirmar que Bitrix guardara la entrada {expected_date} ({expected_duration})."
            ) from exc

    def _entry_exists_in_bitrix_store(self, page, saved_id: str) -> bool:
        for frame in page.frames:
            try:
                exists = bool(
                    frame.evaluate(
                        """(id) => {
                            const store = window.BX?.Tasks?.V2?.Core?.getStore?.();
                            const model = window.BX?.Tasks?.V2?.Const?.Model?.ElapsedTimes;
                            const getter = model ? store?.getters?.[`${model}/getById`] : null;
                            return Boolean(getter?.(id) || getter?.(Number(id)));
                        }""",
                        str(saved_id),
                    )
                )
                if exists:
                    return True
            except Exception:
                continue
        return False

    def _discard_open_entry_form(self, page) -> None:
        for context in self._interactive_contexts(page):
            forms = context.locator(".tasks-time-tracking-list-item-edit")
            try:
                if forms.count() == 0:
                    continue
                form = forms.first
                close_icon = form.locator(".tasks-time-tracking-list-item-edit-close-icon").first
                if close_icon.is_visible(timeout=500):
                    close_icon.click()
                    form.wait_for(state="detached", timeout=3000)
                    return
            except Exception as exc:
                self._save_debug_artifacts(page)
                raise BitrixBrowserError(
                    "Hay un formulario de Bitrix abierto que no se pudo cancelar antes de continuar."
                ) from exc

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
        frame_lines = []
        try:
            for index, frame in enumerate(page.frames):
                frame_lines.append(f"{index}: {frame.url}")
                try:
                    frame_html = frame.content()
                    safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", frame.url or f"frame_{index}")[:80]
                    (debug_dir / f"bitrix_{timestamp}_frame_{index}_{safe_name}.html").write_text(
                        frame_html,
                        encoding="utf-8",
                    )
                except Exception as exc:
                    frame_lines.append(f"  html error: {exc}")
            (debug_dir / f"bitrix_{timestamp}_frames.txt").write_text("\n".join(frame_lines), encoding="utf-8")
        except Exception:
            pass

    def _is_visible_text(self, page, text: str) -> bool:
        try:
            return page.get_by_text(text, exact=True).first.is_visible(timeout=800)
        except Exception:
            return False
