"""Identity, authorship and licence terms for the client.

GPLv3 asks an interactive program to keep its copyright notice, warranty
disclaimer and licence terms reachable from the interface - the licence text
itself suggests an about box for a GUI. So the wording below is not decoration,
and the warranty paragraph in particular should not be trimmed for looks.
"""
import logging

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QDialog

from constants import (
    APP_NAME,
    AUTHOR,
    COPYRIGHT_YEAR,
    LICENSE_NAME,
    LICENSE_URL,
    REPO_URL,
    VERSION_DATE,
    VERSION_NUMBER,
)
from paths import APP_ICON
from ui.Ui_about_dialog import Ui_AboutDialog
from ui.theme import use_dark_titlebar

logger = logging.getLogger(__name__)

ICON_SIZE = 72


class AboutDialog(QDialog, Ui_AboutDialog):

    def __init__(self, parent=None):
        super(AboutDialog, self).__init__(parent)
        self.setupUi(self)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setWindowTitle(f'About {APP_NAME}')
        use_dark_titlebar(self)

        self._set_icon()

        version = '.'.join(str(part) for part in VERSION_NUMBER[:3])
        self.lbl_about_name.setText(APP_NAME)
        self.lbl_about_version.setText(f'Version {version}  ·  {VERSION_DATE}')
        self.lbl_about_author.setText(f'© {COPYRIGHT_YEAR} {AUTHOR}')

        # Shown without the scheme, which is noise once it is a link anyway.
        self.lbl_about_link.setText(
            f'<a href="{REPO_URL}">{REPO_URL.split("//", 1)[-1]}</a>')

        self.lbl_about_licence.setText(
            f'{APP_NAME} is free software: you can redistribute it and modify '
            f'it under the terms of the {LICENSE_NAME}, as published by the '
            f'Free Software Foundation.'
            f'<br><br>'
            f'It is distributed WITHOUT ANY WARRANTY - without even the '
            f'implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR '
            f'PURPOSE.'
            f'<br><br>'
            f'Built with PyQt5 and Qt, which carry their own licences. '
            f'<a href="{LICENSE_URL}">Read the full licence</a>.'
        )

    def _set_icon(self):
        icon = QIcon(APP_ICON)
        # Same trap as MainWindow: a QIcon built from a missing path reports
        # itself non-null and only fails when asked for a pixmap. Here that
        # would leave a 72px hole rather than break anything, so drop the label.
        if not icon.availableSizes():
            logger.warning('Could not load %s for the About dialog', APP_ICON)
            self.lbl_about_icon.hide()
            return

        ratio = self.devicePixelRatioF()
        pixmap = icon.pixmap(int(ICON_SIZE * ratio), int(ICON_SIZE * ratio))
        pixmap.setDevicePixelRatio(ratio)
        self.lbl_about_icon.setPixmap(pixmap)
