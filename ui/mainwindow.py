import logging

from PyQt5.QtCore import QEvent, QRectF, Qt, QThread, QTimer, pyqtSlot
from PyQt5.QtGui import QIcon, QPainter, QPainterPath, QPixmap
from PyQt5.QtWidgets import (
    QAction,
    QApplication,
    QMainWindow,
    QMenu,
    QStyle,
    QSystemTrayIcon,
)

import settings
from device_link import explain_socket_error
from paths import APP_ICON
from ui.about_dialog import AboutDialog
from ui.notifications import NotificationsWrapper
from ui.settings_dialog import SettingsDialog
from ui.taskbar import TaskbarIntegration
from ui.theme import restyle, use_dark_titlebar
from ui.Ui_mainwindow import Ui_MainWindow

logger = logging.getLogger(__name__)

ART_SIZE = 240
ART_RADIUS = 10

# Geometric Shapes block, so these render in Segoe UI without falling back to
# an emoji font.
STATUS_GLYPHS = {
    'PLAYING': '▶',  # right-pointing triangle
    'PAUSED': '▮▮',  # two vertical bars
    'STOPPED': '■',  # filled square
}

# The transport button reuses the status glyphs, so the two can never drift.
PLAY_GLYPH = STATUS_GLYPHS['PLAYING']
PAUSE_GLYPH = STATUS_GLYPHS['PAUSED']

# How far to darken artwork that belongs to the track that just ended. Enough to
# read as "on its way" beside the new title, without hiding what is still a
# perfectly good cover for the fraction of a second before the real one lands.
STALE_ART_DIM = 0.45

# The position bar counts thousandths of a track rather than percent, because a
# QProgressBar takes an integer and 1% of a three minute track is a step every
# 1.8 seconds - visible as a stutter. Must match `maximum` in mainwindow.ui.
PROGRESS_STEPS = 1000

# How often the position is recomputed while playing. The bar is around 300px
# wide, so half a second is under a pixel on a three minute track.
PROGRESS_INTERVAL_MS = 500


def format_duration(seconds: float) -> str:
    """m:ss, widening to h:mm:ss only for something long enough to need it."""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f'{hours}:{minutes:02d}:{secs:02d}'
    return f'{minutes}:{secs:02d}'


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


def dimmed_pixmap(pixmap: QPixmap, opacity: float = STALE_ART_DIM) -> QPixmap:
    """A darkened copy, for artwork known to belong to the track that just ended.

    Painted over rather than recomputed from the source, so the artwork does not
    have to be rescaled and reclipped to be marked stale.
    """
    out = QPixmap(pixmap)
    painter = QPainter(out)
    painter.setOpacity(opacity)
    painter.fillRect(out.rect(), Qt.black)
    painter.end()
    out.setDevicePixelRatio(pixmap.devicePixelRatioF())
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
        super().__init__()
        self.setupUi(self)
        self.setWindowTitle(QApplication.applicationName())

        self.app_icon = QIcon(APP_ICON)
        # NOT isNull(): QIcon stores the path lazily and reports a missing file
        # as non-null, only failing when something asks it for a pixmap. An icon
        # that yields no pixmap gives an *absent* tray icon, not a blank one -
        # and with close-to-tray on, that leaves no way to quit but Task Manager.
        if not self.app_icon.availableSizes():
            logger.warning('Could not load %s; falling back to a stock icon', APP_ICON)
            self.app_icon = self.style().standardIcon(QStyle.SP_MediaPlay)
        self.setWindowIcon(self.app_icon)
        use_dark_titlebar(self)

        # Set when the user really means to exit, so closeEvent can tell a close
        # that should hide to the tray from one that should quit.
        self._quitting = False
        self._tray_hint_shown = False
        self._current_art_id = None
        # The undimmed artwork on show, kept so it can be darkened and restored
        # without decoding and reclipping the thumbnail again.
        self._art_pixmap = None
        self._art_dimmed = False

        # The position anchor the display is extrapolating from, and the state
        # it was read in. See Timeline in ui/notifications.py.
        self._timeline = None
        self._timeline_playing = False
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(PROGRESS_INTERVAL_MS)
        self._progress_timer.timeout.connect(self.refresh_progress)

        # The cover as Windows handed it over, undecorated - the panel's own copy
        # is rounded and resized for the panel, which is not what the taskbar
        # icon or the hover thumbnail want.
        self._art_source = None

        self.tray_icon = None
        self.setup_tray()

        # Bound to the native window handle on the first showEvent; until then
        # every call on it is a no-op, so nothing has to check.
        self.taskbar = TaskbarIntegration(self)
        self.taskbar.command.connect(self.send_command)

        self.button_about.clicked.connect(self.show_about)
        self.button_settings.clicked.connect(self.show_settings)
        self.button_hide.clicked.connect(self.hide_to_tray)

        self.button_previous.clicked.connect(lambda: self.send_command('previous'))
        self.button_play_pause.clicked.connect(lambda: self.send_command('play_pause'))
        self.button_next.clicked.connect(lambda: self.send_command('next'))

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
        self.notifications_wrapper.signal_device_discovered.connect(self.receive_discovered_device)
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

        # Also in the footer, like Settings - the window is often hidden, and
        # GPLv3 wants the licence notice reachable from the running program.
        self.action_about = QAction('About...', self)
        self.action_about.triggered.connect(self.show_about)
        menu.addAction(self.action_about)

        menu.addSeparator()

        self.action_quit = QAction('Quit', self)
        self.action_quit.triggered.connect(self.quit_application)
        menu.addAction(self.action_quit)

        self.tray_icon = QSystemTrayIcon(self.app_icon, self)
        self.tray_icon.setContextMenu(menu)
        self.tray_icon.setToolTip(QApplication.applicationName())
        self.tray_icon.activated.connect(self.on_tray_activated)
        self.tray_icon.messageClicked.connect(self.on_message_clicked)
        self.tray_icon.show()

        # Hiding the window must not end the process - the whole point of the
        # tray icon is that the dock keeps being fed while nothing is on screen.
        QApplication.instance().setQuitOnLastWindowClosed(False)

    @pyqtSlot(QSystemTrayIcon.ActivationReason)
    def on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            if self.isVisible() and not self.isMinimized():
                # Same route as the Hide button, so the tray icon and the button
                # cannot disagree about what hiding means.
                self.hide_to_tray()
            else:
                self.show_from_tray()

    @pyqtSlot()
    def show_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    @pyqtSlot()
    def hide_to_tray(self):
        """Get out of the way - by minimising or by vanishing entirely.

        Minimising is not a lesser hide here, it is the one that keeps the
        taskbar button alive, and with it the artwork icon, the transport
        buttons, the hover thumbnail and the progress bar. Hiding takes all four
        away, which is the whole reason the setting exists.
        """
        if self.tray_icon is None or settings.taskbar_button():
            self.showMinimized()
            return
        self.hide()
        self.notify_hidden()

    def notify_hidden(self):
        """Explain the disappearing window - once a session, and only until the
        user has had enough of being told.

        Qt 5's tray API has no way to put a button in a balloon: showMessage()
        takes a title, a message, an icon and a timeout, and the only interaction
        it offers back is messageClicked on the whole balloon. Real buttons would
        mean Windows toast notifications through WinRT, which need an
        AppUserModelID backed by a Start Menu shortcut - an installer, for this.
        So the balloon says what clicking it does, and clicking it anywhere
        counts. See on_message_clicked().
        """
        if self._tray_hint_shown or self.tray_icon is None:
            return
        if not settings.tray_hint():
            return
        self._tray_hint_shown = True
        self.tray_icon.showMessage(
            QApplication.applicationName(),
            'Still running - your dock keeps updating. Click the tray icon to '
            'bring this back.\n\nClick here to stop showing this.',
            self.app_icon,
            4000,
        )

    @pyqtSlot()
    def on_message_clicked(self):
        """The balloon was clicked, which is the only 'button' Qt gives us.

        Nothing else in this app raises a tray message, so a click here can only
        mean the hide hint - there is no ambiguity to resolve. If another message
        is ever added, this needs to know which one was on screen.
        """
        if not settings.tray_hint():
            return
        settings.set_tray_hint(False)
        logger.info('Tray hint dismissed for good')

    @pyqtSlot()
    def quit_application(self):
        self._quitting = True
        QApplication.instance().quit()

    # -- Settings ----------------------------------------------------------

    def send_command(self, command):
        """Hand a transport control to the worker, which owns the session."""
        logger.debug('Transport button: %s', command)
        self.notifications_wrapper.send_command(command)

    @pyqtSlot()
    def show_about(self):
        AboutDialog(self).exec_()

    @pyqtSlot()
    def show_settings(self):
        dialog = SettingsDialog(self)
        if not dialog.exec_():
            return

        # Turning the hint back on should mean it can appear again now, not only
        # after a restart - this flag is "already shown this run", and the user
        # has just said they want to see it.
        if dialog.check_tray_hint.isChecked():
            self._tray_hint_shown = False

        self.notifications_wrapper.set_device_address(dialog.host, dialog.port)
        # Everything else the dialog saved is read by the worker on its next
        # push, so nudge it into making one - otherwise turning the ambient light
        # on does nothing visible until the next track change or heartbeat.
        self.notifications_wrapper.refresh_settings()
        # The artwork icon and the progress bar are both read from settings at
        # draw time, so this is what makes a change to either visible now rather
        # than at the next track.
        self.taskbar.apply_settings()
        self.sync_progress_timer()
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

        # The button offers the action, not the current state: showing pause
        # while playing is what every other transport control does.
        self.button_play_pause.setText(PAUSE_GLYPH if track.is_playing else PLAY_GLYPH)
        self.button_previous.setEnabled(track.can_previous)
        self.button_play_pause.setEnabled(track.can_play_pause)
        self.button_next.setEnabled(track.can_next)

        self.set_artwork(track.thumbnail, track.art_id, track.artwork_pending)
        self.set_timeline(track.timeline, track.is_playing)

        tooltip = ' - '.join(part for part in (track.artist, track.title) if part)
        if self.tray_icon is not None:
            self.tray_icon.setToolTip(tooltip or QApplication.applicationName())

        # After set_artwork, which is what refreshes _art_source.
        self.taskbar.set_track(
            self._art_source,
            'play' if track.is_playing else ('pause' if track.status == 'PAUSED' else 'stop'),
            track.can_previous,
            track.can_next,
            track.can_play_pause,
        )

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
        # Kept visible but dead, so the panel does not reflow when playback stops.
        self.button_play_pause.setText(PLAY_GLYPH)
        for button in (self.button_previous, self.button_play_pause, self.button_next):
            button.setEnabled(False)
        self.set_artwork(None, None)
        self.set_timeline(None, False)
        if self.tray_icon is not None:
            self.tray_icon.setToolTip(QApplication.applicationName())
        self.taskbar.clear()

    def set_artwork(self, thumb_bytes, art_id, pending=False):
        if pending:
            # The track changed but its artwork has not arrived. Keep the cover we
            # have and darken it rather than swapping in the leftover the session
            # is still serving, which is usually a far smaller version of this
            # very image and would read as the display getting worse.
            self._dim_artwork()
            return

        # Re-clipping the same artwork on every playback event is wasted work -
        # unless it is currently dimmed, which this call is here to undo.
        if art_id is not None and art_id == self._current_art_id and not self._art_dimmed:
            return
        self._current_art_id = art_id
        self._art_dimmed = False

        if not thumb_bytes:
            self._art_pixmap = None
            self._art_source = None
            self.show_placeholder_art()
            return

        source = QPixmap()
        if not source.loadFromData(thumb_bytes):
            logger.warning('Could not decode the thumbnail Windows gave us')
            self._art_pixmap = None
            self._art_source = None
            self.show_placeholder_art()
            return

        logger.debug('Received thumbnail (%d KB)', len(thumb_bytes) // 1024)
        self._art_source = source
        self._art_pixmap = rounded_pixmap(
            source,
            ART_SIZE,
            ART_RADIUS,
            self.devicePixelRatioF(),
        )
        self.lbl_art.setPixmap(self._art_pixmap)
        self.lbl_art.setProperty('has_art', 'true')
        restyle(self.lbl_art)

    def _dim_artwork(self):
        """Mark the artwork on show as belonging to the track that just ended."""
        # Nothing to dim, or already dimmed - the chase re-reads several times per
        # track change and each one arrives here.
        if self._art_pixmap is None or self._art_dimmed:
            return
        self._art_dimmed = True
        self.lbl_art.setPixmap(dimmed_pixmap(self._art_pixmap))

    # -- Playback position -------------------------------------------------

    def set_timeline(self, timeline, playing):
        """Take a fresh anchor from the session and redraw against it."""
        self._timeline = timeline
        self._timeline_playing = playing
        # Sources that report no position get no bar at all, rather than an
        # empty one - the row carries its own top margin, so hiding it closes
        # the gap too. Live streams land here as well, having no end to run to.
        self.panel_progress.setVisible(timeline is not None)

        # Unconditional, like the status line above: this runs once per refresh,
        # and tracking the previous value to save a polish of one small widget
        # would cost more in edge cases than it saves.
        self.progress_position.setProperty('playing', 'true' if playing else 'false')
        restyle(self.progress_position)

        # refresh_progress() returns early with no anchor, so a source that
        # reports no position - a live stream - would otherwise leave the last
        # track's bar sitting on the taskbar button.
        if timeline is None:
            self.taskbar.set_progress(None, False)

        self.refresh_progress()
        self.sync_progress_timer()

    def sync_progress_timer(self):
        """Tick only when something is actually moving, and only when on screen.

        A paused source needs no timer - its anchor already is the answer - and
        neither does a window in the tray, which is the normal state here: the
        dock keeps being fed with nothing on screen, and a repaint every half
        second for the rest of the day would be the one expensive thing about
        this feature.
        """
        # Minimised is off screen for the panel but *not* for the taskbar, which
        # is still drawing a progress bar someone can see - so that case keeps
        # ticking. A hidden window has no taskbar button at all, so it does not.
        on_screen = self.isVisible() and not self.isMinimized()
        on_taskbar = self.isVisible() and self.taskbar.shows_progress
        should_run = self._timeline is not None and self._timeline_playing and (on_screen or on_taskbar)
        if should_run == self._progress_timer.isActive():
            return
        if should_run:
            self._progress_timer.start()
        else:
            self._progress_timer.stop()

    @pyqtSlot()
    def refresh_progress(self):
        """Redraw the bar from the anchor, without asking the session anything.

        The anchor is timestamped, so this is self-correcting: a window that was
        hidden for an hour comes back showing the right position, and no count
        of missed ticks has to be kept.
        """
        timeline = self._timeline
        if timeline is None:
            return

        # Once, not once per widget - two calls a tick would use two different
        # `now`s and could disagree about the last second of a track.
        position = timeline.position_at(self._timeline_playing)
        span = timeline.duration
        fraction = (position - timeline.start) / span if span > 0 else 0.0

        self.progress_position.setValue(round(fraction * PROGRESS_STEPS))
        self.lbl_elapsed.setText(format_duration(position - timeline.start))
        self.lbl_duration.setText(format_duration(span))
        self.taskbar.set_progress(
            fraction if span > 0 else None,
            self._timeline_playing,
        )

    def show_placeholder_art(self):
        self.lbl_art.setPixmap(
            placeholder_pixmap(self.app_icon, ART_SIZE, self.devicePixelRatioF()),
        )
        self.lbl_art.setProperty('has_art', 'false')
        restyle(self.lbl_art)

    @pyqtSlot(bool, str)
    def receive_device_state(self, ok, message):
        self.update_device_label(ok, message)

    @pyqtSlot(str, int)
    def receive_discovered_device(self, host, port):
        """The worker found a dock somewhere new. Saving is the GUI thread's job
        so settings are only ever written from one thread."""
        logger.info('Saving discovered dock address %s:%d', host, port)
        settings.set_device_address(host, port)
        self.update_device_label(None, '')

    def update_device_label(self, ok, message):
        """Footer indicator. ok=None means 'not heard from yet'."""
        host = settings.device_host()
        address = f'{host}:{settings.device_port()}' if host else 'no dock set'
        if ok is None:
            text = f'Contacting {address}' if host else 'Searching for a dock...'
            state, tooltip = '', ''
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

    # The position bar is the only thing here that costs anything while nobody
    # is looking, so its timer follows the window on and off screen. Coming back
    # needs no catch-up beyond a redraw: the anchor is timestamped.

    def showEvent(self, event):
        super().showEvent(event)
        # First show is where windowHandle() finally exists, which is what the
        # taskbar button and thumbnail toolbar both need. No-ops after that.
        self.taskbar.attach()
        self.refresh_progress()
        self.sync_progress_timer()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.sync_progress_timer()

    def changeEvent(self, event):
        super().changeEvent(event)
        # Minimising does not hide a window, so hideEvent never fires for it.
        if event.type() == QEvent.WindowStateChange:
            self.refresh_progress()
            self.sync_progress_timer()

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
        self.hide_to_tray()
