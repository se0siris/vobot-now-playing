"""Device address and window behaviour, persisted to QSettings."""
import logging

from PyQt5.QtCore import QThreadPool, QRunnable, QObject, pyqtSignal, pyqtSlot, Qt
from PyQt5.QtWidgets import QDialog, QApplication

import settings

from device_link import explain_socket_error, probe
from ui.Ui_settings_dialog import Ui_SettingsDialog
from ui.theme import restyle, use_dark_titlebar

logger = logging.getLogger(__name__)


class _ProbeSignals(QObject):
    finished = pyqtSignal(bool, str)


class _ProbeTask(QRunnable):
    """Reachability check on a pool thread, so the dialog stays responsive."""

    def __init__(self, host: str, port: int):
        super(_ProbeTask, self).__init__()
        self.host = host
        self.port = port
        self.signals = _ProbeSignals()

    @pyqtSlot()
    def run(self):
        result = probe(self.host, self.port)
        self.signals.finished.emit(bool(result), result.error or '')


class SettingsDialog(QDialog, Ui_SettingsDialog):

    def __init__(self, parent=None):
        super(SettingsDialog, self).__init__(parent)
        self.setupUi(self)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setWindowTitle(f'{QApplication.applicationName()} - Settings')
        use_dark_titlebar(self)

        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)

        self.edit_host.setText(settings.device_host())
        self.spin_port.setValue(settings.device_port())
        self.check_close_to_tray.setChecked(settings.close_to_tray())
        self.check_start_minimized.setChecked(settings.start_minimized())

        self.button_test.clicked.connect(self.test_connection)
        self.edit_host.textChanged.connect(self.clear_test_result)
        self.spin_port.valueChanged.connect(self.clear_test_result)

    @property
    def host(self) -> str:
        return self.edit_host.text().strip()

    @property
    def port(self) -> int:
        return self.spin_port.value()

    def clear_test_result(self):
        self._set_test_result('', None)

    def _set_test_result(self, message: str, result: str | None):
        self.lbl_test_result.setText(message)
        self.lbl_test_result.setProperty('result', result or '')
        restyle(self.lbl_test_result)

    def test_connection(self):
        if not self.host:
            self._set_test_result('Enter an address first.', 'error')
            return

        self.button_test.setEnabled(False)
        self._set_test_result('Checking...', None)

        task = _ProbeTask(self.host, self.port)
        task.signals.finished.connect(self.on_probe_finished)
        self._pool.start(task)

    @pyqtSlot(bool, str)
    def on_probe_finished(self, ok: bool, error: str):
        self.button_test.setEnabled(True)
        if ok:
            self._set_test_result('Device is listening.', 'ok')
        else:
            self._set_test_result(explain_socket_error(error or 'No response.'), 'error')

    def accept(self):
        if not self.host:
            self._set_test_result('Enter an address first.', 'error')
            self.edit_host.setFocus()
            return

        settings.set_device_address(self.host, self.port)
        settings.set_close_to_tray(self.check_close_to_tray.isChecked())
        settings.set_start_minimized(self.check_start_minimized.isChecked())
        logger.info('Settings saved: device %s:%d', self.host, self.port)
        super(SettingsDialog, self).accept()
