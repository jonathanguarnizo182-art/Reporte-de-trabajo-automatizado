from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .config import CONFIG_DIR
from .models import BitrixTaskTarget, BitrixTimeEntry


class BitrixBrowserError(RuntimeError):
    pass


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
        page.goto(task.url, wait_until="domcontentloaded")
        self._soft_wait_after_navigation(page)
        self._ensure_task_page(page)
        total = len(entries)
        for index, entry in enumerate(entries, start=1):
            self._progress(
                progress_callback,
                f"Seguimiento {index}/{total} - {entry.target_date.strftime('%d/%m/%Y')} {entry.start}",
            )
            self._add_time_entry(page, entry)

        self._close_time_tracking_modal(page)
        for index, entry in enumerate(entries, start=1):
            self._progress(
                progress_callback,
                f"Jornada {index}/{total} - {entry.target_date.strftime('%d/%m/%Y')} {entry.start}-{entry.end}",
            )
            self._fill_workday_entry(page, entry)
            page.reload(wait_until="domcontentloaded")
            self._soft_wait_after_navigation(page)

        self._open_worktime_page(page)
        for index, entry in enumerate(entries, start=1):
            self._progress(
                progress_callback,
                f"Vinculando jornada {index}/{total} - {entry.target_date.strftime('%d/%m/%Y')}",
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
        total = len(entries)
        for index, entry in enumerate(entries, start=1):
            if progress_callback is not None:
                progress_callback(index, total, entry)
            self._add_time_entry(page, entry)

    def _progress(self, callback: Callable[[str], None] | None, message: str) -> None:
        if callback is not None:
            callback(message)

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
        page.goto(target["url"], wait_until="domcontentloaded")
        self._soft_wait_after_navigation(page)
        return BitrixTaskTarget(task_id=int(target["id"]), title=target["title"], url=page.url)

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
        self._save_debug_artifacts(page)
        raise BitrixBrowserError("No pude abrir el widget superior de Tiempo de trabajo.")

    def _start_workday_if_needed(self, page) -> None:
        for label in ("Iniciar", "Empezar"):
            try:
                button = page.get_by_text(label, exact=True).first
                if button.is_visible(timeout=800) and button.is_enabled(timeout=800):
                    button.click()
                    page.wait_for_timeout(1200)
                    return
            except Exception:
                continue

    def _open_workday_editor(self, page) -> None:
        candidates = [
            page.locator("[title*='Editar']:visible").first,
            page.locator("[aria-label*='Editar']:visible").first,
            page.locator(".ui-icon-set.--edit, [class*='edit']").first,
        ]
        for candidate in candidates:
            try:
                if candidate.is_visible(timeout=1200):
                    candidate.click()
                    page.get_by_text("Editar el dia de trabajo", exact=False).wait_for(timeout=6000)
                    return
            except Exception:
                continue
        self._save_debug_artifacts(page)
        raise BitrixBrowserError("No pude abrir la edicion del dia de trabajo.")

    def _fill_workday_editor(self, page, entry: BitrixTimeEntry) -> None:
        date_text = entry.target_date.strftime("%d/%m/%Y")
        start_hour, start_minute = self._parse_start_time(entry.start)
        end_hour, end_minute = self._parse_start_time(entry.end)
        try:
            inputs = self._visible_inputs(page)
            numeric_inputs = [item for item in inputs if self._input_value(item).strip().isdigit()]
            if len(numeric_inputs) >= 4:
                self._replace_input_value(numeric_inputs[0], f"{start_hour:02d}")
                self._replace_input_value(numeric_inputs[1], f"{start_minute:02d}")
                self._replace_input_value(numeric_inputs[2], f"{end_hour:02d}")
                self._replace_input_value(numeric_inputs[3], f"{end_minute:02d}")

            date_inputs = [item for item in inputs if re.search(r"\d{2}/\d{2}/\d{4}", self._input_value(item))]
            if len(date_inputs) < 2:
                self._click_change_day(page)
                inputs = self._visible_inputs(page)
                date_inputs = [item for item in inputs if re.search(r"\d{2}/\d{2}/\d{4}", self._input_value(item))]
            for date_input in date_inputs[:2]:
                self._replace_input_value(date_input, date_text, re.escape(date_text))

            break_input = self._find_workday_break_input(page)
            if break_input is not None:
                self._replace_input_value(break_input, entry.break_duration, re.escape(entry.break_duration))

            reason = page.locator("textarea:visible").last
            reason.fill(entry.workday_reason)
            self._click_finalize_workday(page)
        except Exception as exc:
            self._save_debug_artifacts(page)
            raise BitrixBrowserError(f"No pude diligenciar el tiempo de trabajo de {date_text}: {exc}") from exc

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

    def _click_finalize_workday(self, page) -> None:
        buttons = page.locator("button:visible, [role='button']:visible").filter(has_text=re.compile("FINALIZAR", re.IGNORECASE))
        for index in range(buttons.count()):
            button = buttons.nth(index)
            try:
                if button.is_enabled(timeout=600):
                    button.click()
                    page.wait_for_timeout(1800)
                    return
            except Exception:
                continue
        self._save_debug_artifacts(page)
        raise BitrixBrowserError("No pude pulsar Finalizar en el dia de trabajo.")

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
        except Exception as exc:
            self._save_debug_artifacts(page)
            raise BitrixBrowserError(f"No pude vincular la jornada {date_label} con la tarea: {exc}") from exc

    def _open_worktime_day_details(self, page, entry: BitrixTimeEntry) -> None:
        duration_patterns = [
            f"{entry.hours} h {entry.minutes} m",
            f"{entry.hours} h",
            f"{entry.hours:02d}:{entry.minutes:02d}",
        ]
        for pattern in duration_patterns:
            try:
                target = page.get_by_text(pattern, exact=False).first
                if target.is_visible(timeout=1200):
                    target.click()
                    page.wait_for_timeout(1200)
                    return
            except Exception:
                continue
        self._save_debug_artifacts(page)
        raise BitrixBrowserError(
            f"No pude abrir el detalle de Tiempo de trabajo para {entry.target_date.strftime('%d/%m/%Y')}."
        )

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
                        const service = window.BX?.Tasks?.V2?.Provider?.Service?.timeTrackingService;
                        const apiClient = window.BX?.Tasks?.V2?.Lib?.apiClient;
                        const endpoint = window.BX?.Tasks?.V2?.Const?.Endpoint?.TaskTimeTrackingAdd;
                        const text = window.BX?.Text;
                        const timezone = window.BX?.Main?.timezone;
                        if (!service && (!apiClient || !endpoint)) {
                            return { ok: false, error: 'Bitrix no expuso servicio ni API interna de seguimiento.' };
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
