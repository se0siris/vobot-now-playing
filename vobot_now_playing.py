import getpass
import os
import platform
import sys
import time
import traceback
from io import StringIO

from PyQt5.QtCore import QThread
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QMessageBox

import settings
import single_instance
from app_setup import app
from constants import APP_NAME, ORG_NAME, VERSION
from init_logging import logger
from paths import APP_ICON
from ui.mainwindow import MainWindow
from ui.message_boxes import message_box_error, message_box_ok
from ui.theme import apply_theme

is_frozen = getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS')


def except_hook(cls, exception, tb):
    separator = '-' * 70
    log_file = os.path.join(
        os.path.dirname(__file__),
        'error.log',
    )
    time_string = time.strftime('%Y-%m-%d, %H:%M:%S')
    machine_name = platform.node()
    user_name = getpass.getuser()

    tb_info_file = StringIO()
    traceback.print_tb(tb, None, tb_info_file)
    tb_info_file.seek(0)
    tb_info = tb_info_file.read()
    error_message = f'{cls}: \n{exception}'
    sections = [
        separator,
        time_string,
        f'Username: {user_name:s}',
        f'Machine: {machine_name:s}',
        f'Version: {VERSION:s}',
        separator,
        error_message,
        separator,
        tb_info,
    ]
    msg = '\n'.join(sections)
    separator = os.linesep * 4 if os.path.isfile(log_file) else ''
    try:
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(separator)
            f.write(msg)
    except OSError:
        pass

    if is_frozen:
        message_box_error(
            'An unhandled exception occurred.',
            'The details have been written to the error.log file inside your application folder.',
            detailed_text=str(msg),
        )

    sys.__excepthook__(cls, exception, tb)


if __name__ == '__main__':
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(VERSION)
    app.setOrganizationName(ORG_NAME)

    logger.info(f'AppName: {app.applicationName()}')
    logger.info(f'AppVersion: {app.applicationVersion()}')
    logger.info(f'Company Name: {app.organizationName()}')

    # Needs the names above, since they decide the settings folder.
    settings.init()

    # Error handling stuff.
    sys.excepthook = except_hook

    logger.info('Main Thread ID: %d', int(QThread.currentThreadId()))

    # Theme and icon before the first window exists, so nothing flashes unstyled.
    app.setWindowIcon(QIcon(APP_ICON))
    apply_theme(app)

    # The dock handles one client at a time and refuses overlapping pushes, so a
    # second copy would only fight this one for it.
    existing_pid = single_instance.claim()
    if existing_pid is not None:
        logger.warning('Already running (pid %s); exiting.', existing_pid or 'unknown')
        message_box_ok(
            f'{APP_NAME} is already running.',
            'Look for it in the notification area, next to the clock. Only one copy '
            'can talk to the Mini Dock at a time.',
            icon=QMessageBox.Information,
        )
        sys.exit(0)

    ui = MainWindow()
    if settings.start_minimized() and ui.tray_icon is not None:
        logger.info('Starting hidden in the notification area.')
    else:
        ui.show()

    sys.exit(app.exec_())
