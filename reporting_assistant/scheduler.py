from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .config import AppConfig


def install_tasks(app_dir: Path, config: AppConfig) -> None:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    launcher = app_dir / "launch_report_assistant.pyw"
    command = f'"{pythonw}" "{launcher}" --auto-open'
    task_specs = [
        ("ReporteServiciosPopup_LunMie", "MON,TUE,WED", config.popup_hour_mon_wed),
        ("ReporteServiciosPopup_JueVie", "THU,FRI", config.popup_hour_thu_fri),
    ]
    for task_name, days, start_time in task_specs:
        subprocess.run(
            [
                "schtasks",
                "/Create",
                "/F",
                "/SC",
                "WEEKLY",
                "/D",
                days,
                "/TN",
                task_name,
                "/TR",
                command,
                "/ST",
                start_time,
            ],
            check=True,
            shell=False,
        )
