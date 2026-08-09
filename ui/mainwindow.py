import logging

from PyQt5.QtCore import QRectF, QThread, QTimer, Qt, pyqtSlot
from PyQt5.QtGui import QIcon, QPainter, QPainterPath, QPixmap
from PyQt5.QtWidgets import (
    QAction,
    QApplication,
    QMainWindow,
    QMenu,
    QSystemTrayIcon,
)

import settings

from device_link import explain_socket_error
from paths import APP_ICON
from ui.Ui_mainwindow import Ui_MainWindow
from ui.notifications import NotificationsWrapper
from ui.settings_dialog import SettingsDialog
from ui.theme import restyle, use_dark_titlebar

logger = logging.getLogger(__name__)

ART_SIZE = 240
ART_RADIUS = 10

# Geometric Shapes block, so these render in Segoe UI without falling back to
# an emoji font.
STATUS_GLYPHS = {
    'PLAYING': '▶',        # right-pointing triangle
    'PAUSED': '▮▮',   # two vertical bars
    'STOPPED': '■',        # filled square
}


def rounded_pixmap(pixmap: QPixmap, size: int, radius: int, device_pixel_ratio: float = 1.0) -> QPixmap:
    """Scale artwork to fit a square and clip it to rounded corners."""
    target = int(size * device_pixel_ratio)
    scaled = pixmap.scaled(target, target, Qt.KeepAspectRatio, Qt.SmoothTransformation)

    out = QPixmap(target, target)
    out.fill(Qt.transparent)

    x = (target - scaled.width()) // 2
    y = (target - scaled.height()) // 2

    painter = QPainter(out)
    painter.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(
        QRectF(x, y, scaled.width(), scaled.height()),
        radius * device_pixel_ratio,
        radius * device_pixel_ratio,
    )
    painter.setClipPath(path)
    painter.drawPixmap(x, y, scaled)
    painter.end()

    out.setDevicePixelRatio(device_pixel_ratio)
    return out


def placeholder_pixmap(icon: QIcon, size: int, device_pixel_ratio: float = 1.0) -> QPixmap:
    """The app mark, faded, for when there is no artwork to show."""
    target = int(size * device_pixel_ratio)
    out = QPixmap(target, target)
    out.fill(Qt.transparent)

    mark = int(target * 0.3)
    source = icon.pixmap(mark, mark)

    painter = QPainter(out)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)
    painter.setOpacity(0.22)
    painter.drawPixmap((target - mark) // 2, (target - mark) // 2, source)
    painter.end()

    out.setDevicePixelRatio(device_pixel_ratio)
    return out


class MainWindow(QMainWindow, Ui_MainWindow):

    def __init__(self):
        super(MainWindow, self).__init__()
        self.setupUi(self)
        self.setWindowTitle(f'{QApplication.applicationName()} - v{QApplication.applicationVersion()}')

        self.app_icon = QIcon(APP_ICON)
        self.setWindowIcon(self.app_icon)
        use_dark_titlebar(self)

        # Set when the user really means to exit, so closeEvent can tell a close
        # that should hide to the tray from one that should quit.
        self._quitting = False
        self._tray_hint_shown = False
        self._current_art_id = None

        self.tray_icon = None
        self.setup_tray()

        self.button_settings.clicked.connect(self.show_settings)
        self.button_hide.clicked.connect(self.hide_to_tray)

        self.show_no_session()
        self.update_device_label(None, '')

        geometry = settings.geometry()
        if geometry:
            self.restoreGeometry(geometry)

        self.notifications_wrapper = NotificationsWrapper()
        self._notifications_thread = QThread()
        self._notifications_thread.setObjectName('media-monitor')

        QApplication.instance().aboutToQuit.connect(self.shutdown_worker)

        logger.debug('MainWindow initialized')
        QTimer.singleShot(0, self.setup_notifications)

    # -- Worker ------------------------------------------------------------

    def setup_notifications(self):
        self.notifications_wrapper.moveToThread(self._notifications_thread)
        self._notifications_thread.started.connect(self.notifications_wrapper.start)
        self.notifications_wrapper.signal_track.connect(self.receive_track)
        self.notifications_wrapper.signal_device_state.connect(self.receive_device_state)
        self._notifications_thread.start()

    @pyqtSlot()
    def shutdown_worker(self):
        if not self._notifications_thread.isRunning():
            return
        logger.debug('Stopping the media monitor...')
        self.notifications_wrapper.stop()
        self._notifications_thread.quit()
        if not self._notifications_thread.wait(3000):
            logger.warning('Media monitor did not stop in time; terminating it')
            self._notifications_thread.terminate()
            self._notifications_thread.wait(1000)

    # -- Tray --------------------------------------------------------------

    def setup_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            logger.warning('No system tray available; the window will close normally')
            return

        menu = QMenu(self)

        self.action_show = QAction('Show Now Playing', self)
        self.action_show.triggered.connect(self.show_from_tray)
        menu.addAction(self.action_show)

        self.action_settings = QAction('Settings...', self)
        self.action_settings.triggered.connect(self.show_settings)
        menu.addAction(self.action_settings)

        menu.addSeparator()

        self.action_quit = QAction('Quit', self)
        self.action_quit.triggered.connect(self.quit_application)
        menu.addAction(self.action_quit)

        self.tray_icon = QSystemTrayIcon(self.app_icon, self)
        self.tray_icon.setContextMenu(menu)
        self.tray_icon.setToolTip(QApplication.applicationName())
        self.tray_icon.activated.connect(self.on_tray_activated)
        self.tray_icon.show()

        # Hiding the window must not end the process - the whole point of the
        # tray icon is that the dock keeps being fed while nothing is on screen.
        QApplication.instance().setQuitOnLastWindowClosed(False)

    @pyqtSlot(QSystemTrayIcon.ActivationReason)
    def on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            if self.isVisible() and not self.isMinimized():
                self.hide()
            else:
                self.show_from_tray()

    @pyqtSlot()
    def show_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    @pyqtSlot()
    def hide_to_tray(self):
        if self.tray_icon is None:
            self.showMinimized()
            return
        self.hide()
        self.notify_hidden()

    def notify_hidden(self):
        """Explain the disappearing window, but only the first time."""
        if self._tray_hint_shown or self.tray_icon is None:
            return
        self._tray_hint_shown = True
        self.tray_icon.showMessage(
            QApplication.applicationName(),
            'Still running - your dock keeps updating. Click the tray icon to bring this back.',
            self.app_icon,
            4000,
        )

    @pyqtSlot()
    def quit_application(self):
        self._quitting = True
        QApplication.instance().quit()

    # -- Settings ----------------------------------------------------------

    @pyqtSlot()
    def show_settings(self):
        dialog = SettingsDialog(self)
        if not dialog.exec_():
            return

        self.notifications_wrapper.set_device_address(dialog.host, dialog.port)
        # Show the new target straight away rather than waiting for a push.
        self.update_device_label(None, '')

    # -- State display -----------------------------------------------------

    @pyqtSlot(object)
    def receive_track(self, track):
        if track is None:
            self.show_no_session()
            return

        self.lbl_title.setText(track.title or 'Unknown track')
        self._set_optional(self.lbl_artist, track.artist)
        self._set_optional(self.lbl_album, track.album)

        glyph = STATUS_GLYPHS.get(track.status, '')
        label = track.status_text
        self.lbl_status.setText(f'{glyph}  {label}'.strip())
        self.lbl_status.setProperty('playing', 'true' if track.is_playing else 'false')
        restyle(self.lbl_status)

        self.set_artwork(track.thumbnail, track.art_id)

        tooltip = ' - '.join(part for part in (track.artist, track.title) if part)
        if self.tray_icon is not None:
            self.tray_icon.setToolTip(tooltip or QApplication.applicationName())

    @staticmethod
    def _set_optional(label, text):
        """Empty metadata should collapse rather than leave a hole in the stack."""
        label.setText(text)
        label.setVisible(bool(text))

    def show_no_session(self):
        self.lbl_title.setText('Nothing playing')
        self._set_optional(self.lbl_artist, '')
        self._set_optional(self.lbl_album, '')
        self.lbl_status.setText('Waiting for a media player')
        self.lbl_status.setProperty('playing', 'false')
        restyle(self.lbl_status)
        self.set_artwork(None, None)
        if self.tray_icon is not None:
            self.tray_icon.setToolTip(QApplication.applicationName())

    def set_artwork(self, thumb_bytes, art_id):
        # Re-clipping the same artwork on every playback event is wasted work.
        if art_id is not None and art_id == self._current_art_id:
            return
        self._current_art_id = art_id

        if not thumb_bytes:
            self.show_placeholder_art()
            return

        source = QPixmap()
        if not source.loadFromData(thumb_bytes):
            logger.warning('Could not decode the thumbnail Windows gave us')
            self.show_placeholder_art()
            return

        logger.debug('Received thumbnail (%d KB)', len(thumb_bytes) // 1024)
        self.lbl_art.setPixmap(
            rounded_pixmap(source, ART_SIZE, ART_RADIUS, self.devicePixelRatioF()))
        self.lbl_art.setProperty('has_art', 'true')
        restyle(self.lbl_art)

    def show_placeholder_art(self):
        self.lbl_art.setPixmap(
            placeholder_pixmap(self.app_icon, ART_SIZE, self.devicePixelRatioF()))
        self.lbl_art.setProperty('has_art', 'false')
        restyle(self.lbl_art)

    @pyqtSlot(bool, str)
    def receive_device_state(self, ok, message):
        self.update_device_label(ok, message)

    def update_device_label(self, ok, message):
        """Footer indicator. ok=None means 'not heard from yet'."""
        address = f'{settings.device_host()}:{settings.device_port()}'
        if ok is None:
            state, text, tooltip = '', f'Contacting {address}', ''
        elif ok:
            state, text, tooltip = 'ok', f'Connected  ·  {address}', ''
        else:
            short = message or 'Not connected'
            state = 'error'
            text = f'{short}  ·  {address}'
            tooltip = explain_socket_error(short)

        self.lbl_device.setText(text)
        self.lbl_device.setToolTip(tooltip)
        self.lbl_device_dot.setToolTip(tooltip)
        self.lbl_device_dot.setProperty('state', state)
        restyle(self.lbl_device_dot)

    # -- Window ------------------------------------------------------------

    def closeEvent(self, event):
        settings.set_geometry(self.saveGeometry())

        if self._quitting or self.tray_icon is None or not settings.close_to_tray():
            self.shutdown_worker()
            if self.tray_icon is not None:
                self.tray_icon.hide()
            event.accept()
            if not self._quitting:
                # quitOnLastWindowClosed is off while the tray icon exists, so
                # closing the window has to end the application itself.
                self._quitting = True
                QApplication.instance().quit()
            return

        # Closing means "get out of my way", not "stop sending to the dock".
        event.ignore()
        self.hide()
        self.notify_hidden()
