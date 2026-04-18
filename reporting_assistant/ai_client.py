from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .config import AppConfig
from .models import GeneratedEntry, WorkdayPayload
from .prompting import STYLE_RULES, build_report_prompt, fallback_entry, render_generated_entry

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None


LOGGER = logging.getLogger(__name__)


class AIService:
    def __init__(self, config: AppConfig):
        self.config = config
        self.client = OpenAI(api_key=config.openai_api_key) if OpenAI and config.openai_api_key else None

    @property
    def available(self) -> bool:
        return self.client is not None

    def transcribe_audio(self, audio_path: Path) -> str:
        if not self.client:
            raise RuntimeError("Configure OPENAI_API_KEY o la clave en la configuración.")
        with audio_path.open("rb") as audio_file:
            transcript = self.client.audio.transcriptions.create(
                model=self.config.transcription_model,
                file=audio_file,
            )
        return (getattr(transcript, "text", "") or "").strip()

    def generate_daily_entry(self, payload: WorkdayPayload) -> GeneratedEntry:
        if not self.client:
            return fallback_entry(payload)

        context = {
            "empresa": payload.report.company,
            "reporte": payload.report.display_name,
            "fecha": payload.target_date.strftime("%d/%m/%Y"),
            "tipo_dia": payload.day_type,
            "modo_servicio": payload.context.get("service_mode", "remoto"),
        }
        prompt = build_report_prompt(
            activity_input=(payload.transcript or payload.notes).strip(),
            report_context=context,
            style_rules=STYLE_RULES,
            time_segments=payload.segments,
        )
        try:
            response = self.client.responses.create(
                model=self.config.openai_model,
                input=prompt,
            )
            output_text = getattr(response, "output_text", "") or ""
            parsed = self._parse_json(output_text)
            title = str(parsed.get("title", "")).strip()
            summary = str(parsed.get("summary", "")).strip()
            sections = [str(item).strip() for item in parsed.get("numbered_sections", []) if str(item).strip()]
            if not title or not summary or not sections:
                raise ValueError("La respuesta del modelo no tuvo el JSON esperado.")
            return GeneratedEntry(
                title=title,
                summary=summary,
                numbered_sections=sections,
                full_text=render_generated_entry(title, summary, sections),
                raw_model_response=output_text,
            )
        except Exception as exc:  # pragma: no cover
            LOGGER.exception("Error generando contenido con IA: %s", exc)
            return fallback_entry(payload)

    def _parse_json(self, text: str) -> dict[str, object]:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
            cleaned = re.sub(r"```$", "", cleaned).strip()
        if not cleaned.startswith("{"):
            match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
            if match:
                cleaned = match.group(0)
        return json.loads(cleaned)

