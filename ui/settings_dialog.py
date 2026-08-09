"""Device address and window behaviour, persisted to QSettings."""
import logging

from PyQt5.QtCore import QThreadPool, QRunnable, QObject, pyqtSignal, pyqtSlot, Qt
from PyQt5.QtWidgets import QDialog, QApplication, QInputDialog

import discovery
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


class _DiscoverySignals(QObject):
    finished = pyqtSignal(object)


class _DiscoveryTask(QRunnable):
    """Network search on a pool thread - it blocks for a second or more."""

    def __init__(self):
        super(_DiscoveryTask, self).__init__()
        self.signals = _DiscoverySignals()

    @pyqtSlot()
    def run(self):
        try:
            devices = discovery.discover()
        except Exception:
            logger.exception('Discovery failed')
            devices = []
        self.signals.finished.emit(devices)


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

        task = _ProbeTask(self.host, self.port)
        task.signals.finished.connect(self.on_probe_finished)
        self._pool.start(task)

    def discover(self):
        self.button_discover.setEnabled(False)
        self.button_test.setEnabled(False)
        self._set_test_result('Searching the network...', None)

        task = _DiscoveryTask()
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

    @pyqtSlot(bool, str)
    def on_probe_finished(self, ok: bool, error: str):
        self.button_test.setEnabled(True)
        if ok:
            self._set_test_result('Device is listening.', 'ok')
        else:
            self._set_test_result(explain_socket_error(error or 'No response.'), 'error')

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
