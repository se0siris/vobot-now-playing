from ctypes import windll, c_int64
from multiprocessing import freeze_support
freeze_support()

import sys
import os

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.RoundPreferFloor)
QApplication.setAttribute(Qt.AA_DisableWindowContextHelpButton, True)
QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

# DpiAwareness should already be set in the application's manifest when packaged, but just in case,
# and for testing when developing, we'll set it via an API call too.
# https://docs.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setprocessdpiawarenesscontext
# https://docs.microsoft.com/en-us/windows/win32/hidpi/dpi-awareness-context
try:
    windll.user32.SetProcessDpiAwarenessContext(c_int64(-2))
except AttributeError:
    # Using a Windows build that doesn't have SetProcessDpiAwarenessContext?
    pass
is_frozen = getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS')

app = QApplication(sys.argv)


# If compiled as a one-file PyInstaller package look for Qt5 Plugins in the TEMP folder.
if is_frozen:
    app.addLibraryPath(
        os.path.normpath(
            os.path.join(sys._MEIPASS, 'PyQt5/Qt/plugins')
        )
    )

app_font = app.font()
app_font.setPointSize(8)
app.setFont(app_font)
