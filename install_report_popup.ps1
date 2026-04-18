param(
    [string]$Python = "python"
)

Set-Location -Path $PSScriptRoot

& $Python -c "from pathlib import Path; from reporting_assistant.config import load_config; from reporting_assistant.scheduler import install_tasks; config = load_config(); install_tasks(Path(r'$PSScriptRoot'), config); print('Tareas programadas creadas correctamente.')"

