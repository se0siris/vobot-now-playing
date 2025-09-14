from init_logging import logger
import getpass
import os
import sys
import platform

import time
from io import StringIO
import traceback


from app_setup import app

from PyQt5.QtCore import QThread

from ui.mainwindow import MainWindow
from ui.message_boxes import message_box_error

from constants import VERSION, APP_NAME, ORG_NAME

is_frozen = getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS')


def except_hook(cls, exception, tb):
    separator = '-' * 70
    log_file = os.path.join(
        os.path.dirname(__file__),
        'error.log'
    )
    time_string = time.strftime('%Y-%m-%d, %H:%M:%S')
    machine_name = platform.node()
    user_name = getpass.getuser()

    tb_info_file = StringIO()
    traceback.print_tb(tb, None, tb_info_file)
    tb_info_file.seek(0)
    tb_info = tb_info_file.read()
    error_message = '{0:s}: \n{1:s}'.format(str(cls), str(exception))
    sections = [
        separator, time_string,
        f'Username: {user_name:s}',
        f'Machine: {machine_name:s}',
        f'Version: {VERSION:s}',
        separator, error_message,
        separator, tb_info
    ]
    msg = '\n'.join(sections)
    separator = os.linesep * 4 if os.path.isfile(log_file) else ''
    try:
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(separator)
            f.write(msg)
    except IOError:
        pass

    if is_frozen:
        message_box_error(
            'An unhandled exception occurred.',
            'The details have been written to the error.log file inside your application folder.',
            detailed_text=str(msg)
        )

    sys.__excepthook__(cls, exception, tb)


def is_remote_session():
    try:
        import win32api
    except ImportError:
        return False
    return bool(win32api.GetSystemMetrics(0x1000))



if __name__ == "__main__":
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(VERSION)
    app.setOrganizationName(ORG_NAME)

    logger.info(f'AppName: {app.applicationName()}')
    logger.info(f'AppVersion: {app.applicationVersion()}')
    logger.info(f'Company Name: {app.organizationName()}')

    # Error handling stuff.
    sys.excepthook = except_hook

    # Store whether we're using RDP for disabling some graphics.
    app.is_remote_session = is_remote_session()

    logger.info('Main Thread ID: %d', int(QThread.currentThreadId()))

    ui = MainWindow()
    ui.show()

    sys.exit(app.exec_())
