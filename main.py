#!/usr/bin/env python3
"""Entry point for the CAN DBC Emulator QML application."""

import sys
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle

from candbcemu.can_controller import CanController


def main() -> int:
    QQuickStyle.setStyle("Material")

    app = QGuiApplication(sys.argv)
    app.setOrganizationName("candbcemu")
    app.setApplicationName("CAN DBC Emulator")

    controller = CanController()

    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("canController", controller)
    engine.load(QUrl.fromLocalFile(str(Path(__file__).resolve().parent / "qml" / "main.qml")))

    if not engine.rootObjects():
        return -1

    args = app.arguments()
    if len(args) > 1:
        controller.loadDbc(args[1])

    exit_code = app.exec()
    controller.disconnectBus()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
