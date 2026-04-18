from __future__ import annotations

import json
from textwrap import dedent

from .models import GeneratedEntry, TimeSegment, WorkdayPayload


STYLE_RULES = dedent(
    """
    Mantenga un estilo técnico, profesional y detallado.
    La estructura obligatoria es:
    1. Un título ejecutivo breve y preciso.
    2. Un párrafo de contexto que explique la jornada y el objetivo logrado.
    3. Una lista numerada de actividades, validaciones, pruebas, ajustes, resultados y estado.
    No invente hechos, equipos, variables, resultados ni validaciones que el usuario no haya mencionado.
    Si el usuario habló en frases cortas, expándalas con redacción profesional conservando únicamente hechos explícitos o inferencias de bajo riesgo.
    Use español profesional.
    """
).strip()


def build_report_prompt(
    activity_input: str,
    report_context: dict[str, str],
    style_rules: str,
    time_segments: list[TimeSegment],
) -> str:
    segments_text = "\n".join(
        f"- {segment.start} a {segment.end} ({segment.effective})"
        for segment in time_segments
    )
    return dedent(
        f"""
        Usted está redactando un reporte diario de servicios para Word.

        Contexto del reporte:
        {json.dumps(report_context, ensure_ascii=False, indent=2)}

        Segmentos de tiempo:
        {segments_text}

        Reglas de estilo:
        {style_rules}

        Notas del usuario:
        {activity_input}

        Responda únicamente en JSON con esta forma:
        {{"title": "str", "summary": "str", "numbered_sections": ["str"]}}
        """
    ).strip()


def render_generated_entry(title: str, summary: str, numbered_sections: list[str]) -> str:
    chunks = [title.strip(), summary.strip()]
    for index, section in enumerate(numbered_sections, start=1):
        chunks.append(f"{index}) {section.strip()}")
    return "\n".join(item for item in chunks if item)


def fallback_entry(payload: WorkdayPayload) -> GeneratedEntry:
    source = (payload.transcript or payload.notes).strip()
    title = "Actividad técnica reportada durante la jornada"
    summary = (
        "Durante la jornada se desarrollaron las actividades descritas por el usuario, "
        "manteniendo seguimiento técnico del avance y registrando los resultados informados."
    )
    sections = [line.strip("- ").strip() for line in source.splitlines() if line.strip()]
    if not sections and source:
        sections = [source]
    if not sections:
        sections = ["Pendiente completar detalle técnico de la jornada."]
    return GeneratedEntry(
        title=title,
        summary=summary,
        numbered_sections=sections,
        full_text=render_generated_entry(title, summary, sections),
        raw_model_response="",
    )

