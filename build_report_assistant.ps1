param(
    [string]$Python = "python"
)

Set-Location -Path $PSScriptRoot

& $Python -m pip install -r requirements.txt
& $Python -m PyInstaller `
    --noconfirm `
    --windowed `
    --name ReporteServiciosAI `
    --collect-all holidays `
    --hidden-import win32timezone `
    launch_report_assistant.pyw

