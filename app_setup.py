import os
import sys
from ctypes import c_int64, windll

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from constants import APP_NAME, ORG_NAME

# Windows labels tray notifications, and groups taskbar buttons, by the
# process's Application User Model ID. Without one set, both are attributed to
# whatever launched us - so toasts arrive headed "Python" instead of the app.
# Must happen before any window is created.
# https://learn.microsoft.com/en-us/windows/win32/shell/appids
APP_USER_MODEL_ID = f'{ORG_NAME}.{APP_NAME}'.replace(' ', '')
try:
    windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
except (AttributeError, OSError):
    pass

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
            os.path.join(sys._MEIPASS, 'PyQt5/Qt/plugins'),
        )
    )

app_font = app.font()
app_font.setPointSize(8)
app.setFont(app_font)
