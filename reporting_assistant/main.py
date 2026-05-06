from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from reporting_assistant.gui import launch_app
else:
    from .gui import launch_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Automatización de reportes de servicios.")
    parser.add_argument("--auto-open", action="store_true", help="Abre la ventana en modo popup.")
    args = parser.parse_args()
    launch_app(auto_open=args.auto_open)


if __name__ == "__main__":
    main()
