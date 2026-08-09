"""Device address and window behaviour, persisted to QSettings."""
import logging

from PyQt5.QtCore import QThreadPool, QRunnable, QObject, pyqtSignal, pyqtSlot, Qt
from PyQt5.QtWidgets import QDialog, QApplication, QInputDialog

import discovery
import settings

from device_link import SendResult, explain_socket_error, probe
from ui.Ui_settings_dialog import Ui_SettingsDialog
from ui.theme import restyle, use_dark_titlebar

logger = logging.getLogger(__name__)


class _TaskSignals(QObject):
    # object rather than a typed signature, so one task class serves any call.
    finished = pyqtSignal(object)


class _Task(QRunnable):
    """Run a blocking call on a pool thread, keeping the dialog responsive.

    Both things this dialog does off-thread - the reachability probe and the
    network search - are 'call a function, hand the result back', so they share
    one runnable rather than a class each.
    """

    def __init__(self, work, *args, default=None):
        super(_Task, self).__init__()
        self._work = work
        self._args = args
        self._default = default
        self.signals = _TaskSignals()

    @pyqtSlot()
    def run(self):
        try:
            result = self._work(*self._args)
        except Exception:
            logger.exception('%s failed', getattr(self._work, '__name__', 'Task'))
            result = self._default
        self.signals.finished.emit(result)


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
        self.check_auto_discover.setChecked(settings.auto_discover())
        self.check_close_to_tray.setChecked(settings.close_to_tray())
        self.check_start_minimized.setChecked(settings.start_minimized())

        self.button_test.clicked.connect(self.test_connection)
        self.button_discover.clicked.connect(self.discover)
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

        task = _Task(probe, self.host, self.port,
                     default=SendResult(False, 'Check failed'))
        task.signals.finished.connect(self.on_probe_finished)
        self._pool.start(task)

    def discover(self):
        self.button_discover.setEnabled(False)
        self.button_test.setEnabled(False)
        self._set_test_result('Searching the network...', None)

        task = _Task(discovery.discover, default=[])
        task.signals.finished.connect(self.on_discovery_finished)
        self._pool.start(task)

    @pyqtSlot(object)
    def on_discovery_finished(self, devices):
        self.button_discover.setEnabled(True)
        self.button_test.setEnabled(True)

        if not devices:
            self._set_test_result(
                'No dock answered. Check it is powered on, on the same network, '
                'and running the Now Playing app.', 'error')
            return

        device = devices[0]
        if len(devices) > 1:
            device = self._choose_device(devices)
            if device is None:
                self._set_test_result(f'{len(devices)} docks found.', None)
                return

        self.edit_host.setText(device.host)
        self.spin_port.setValue(device.port)
        # setText cleared the label via textChanged, so say this after.
        self._set_test_result(f'Found {device.label}.', 'ok')

    def _choose_device(self, devices):
        labels = [device.label for device in devices]
        choice, accepted = QInputDialog.getItem(
            self, f'{QApplication.applicationName()} - Choose a dock',
            f'{len(devices)} docks answered. Which one should be used?',
            labels, 0, False)
        if not accepted:
            return None
        return devices[labels.index(choice)]

    @pyqtSlot(object)
    def on_probe_finished(self, result):
        self.button_test.setEnabled(True)
        if result:
            self._set_test_result('Device is listening.', 'ok')
        else:
            self._set_test_result(
                explain_socket_error(result.error or 'No response.'), 'error')

    def accept(self):
        # An empty address is allowed only when something will go and find one.
        if not self.host and not self.check_auto_discover.isChecked():
            self._set_test_result(
                'Enter an address, or turn on automatic discovery.', 'error')
            self.edit_host.setFocus()
            return

        settings.set_device_address(self.host, self.port)
        settings.set_auto_discover(self.check_auto_discover.isChecked())
        settings.set_close_to_tray(self.check_close_to_tray.isChecked())
        settings.set_start_minimized(self.check_start_minimized.isChecked())
        logger.info('Settings saved: device %s:%d', self.host, self.port)
        super(SettingsDialog, self).accept()
