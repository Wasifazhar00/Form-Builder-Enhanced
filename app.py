from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from formbuilder.ui import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Bank Form Automation")
    app.setOrganizationName("Local Form Tools")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover - GUI startup safety net
        try:
            QMessageBox.critical(None, "Application startup failed", str(exc))
        except Exception:
            print(f"Application startup failed: {exc}", file=sys.stderr)
        raise
