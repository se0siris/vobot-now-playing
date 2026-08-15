"""Windows 7+ taskbar button integration: artwork, transport buttons, progress.

Four separate Win32 features hang off one taskbar button, and they are not
interchangeable - which is worth stating plainly, because the obvious mental
model ("put the cover art on the taskbar icon") maps to two different APIs with
very different results:

  * **The icon itself** is the window's icon. `setWindowIcon()` drives it, so the
    cover art can genuinely replace it - but only once the process has an
    explicit AppUserModelID. Without one Windows attributes the button to
    whatever launched us and keeps showing python.exe's icon no matter what the
    window says. `app_setup.py` sets one; this module depends on that and is
    otherwise silently inert. Measured: with the AppID set, the taskbar icon
    follows `setWindowIcon()` immediately.
  * **The overlay** is a 16x16 badge in the button's corner. It is for status,
    not artwork - a cover is unrecognisable at that size, so this carries the
    play/pause/stop glyph instead.
  * **The hover thumbnail** is where artwork actually works, at a size where a
    familiar cover is recognisable. By default Windows shows a live capture of
    the window, which is useless here - the window is usually minimised or in
    the tray. `setIconicThumbnailPixmap()` substitutes our own bitmap instead.
  * **The progress bar** is meant for file operations, so track position is a
    non-standard use of it.

All of it needs a taskbar button to exist, and that is the constraint the whole
feature is shaped around: **a hidden window has no taskbar button**, so
`hide()` takes the icon, the badge, the thumbnail and the progress bar with it.
`showMinimized()` keeps all four. Hence `settings.taskbar_button()`, which turns
the window's hide into a minimise - measured both ways rather than assumed.

Everything here is **off by default**: these are extras to opt into, and each
one changes something the user did not ask to have changed. Stock, this app
keeps a taskbar button that behaves like any other application's.

Three switches, and the grouping is deliberate:

  * `taskbar_media_controls()` turns the button into a media control - the
    status badge, the transport buttons, and the artwork thumbnail in place of
    the live window preview. One switch, because those three are one idea, and
    a user who wants none of them wants none of them.
  * `taskbar_artwork_icon()` is separate, because replacing the app's own icon
    is a bigger imposition than anything above: it is how the app is recognised
    in the taskbar, in Alt-Tab and in its title bar.
  * `taskbar_progress()` is separate again, being the one that borrows a piece
    of Windows chrome that already means something else.
"""
import ctypes
import logging

from PyQt5.QtCore import QObject, QRectF, Qt, pyqtSignal
from PyQt5.QtGui import (QBrush, QColor, QIcon, QPainter, QPainterPath, QPen,
                         QPixmap)
from PyQt5.QtWinExtras import (QWinTaskbarButton, QWinThumbnailToolBar,
                               QWinThumbnailToolButton)

import settings

logger = logging.getLogger(__name__)

# Windows asks for the thumbnail at whatever size it wants and scales what it
# gets. Composing at a fixed 16:10 means the aspect never changes under it, so a
# cover is never stretched; these are generous enough that the downscale is the
# only resampling that happens.
THUMBNAIL_SIZE = (320, 200)
LIVE_PREVIEW_SIZE = (640, 400)

# The client's own panel colour, so the ground behind a square cover on a wide
# thumbnail looks like part of this app rather than an accident.
THUMBNAIL_GROUND = QColor(26, 33, 51)

# Drawn large and scaled down: the badge lands at 16x16 on a standard DPI and
# more on a scaled display, and Windows picks whichever it wants from the icon.
BADGE_SIZE = 32
BADGE_FILL = QColor(0x2E, 0x9B, 0xF0)
# Half of what the spike used. Enough to separate the glyph from any taskbar
# colour behind it, thin enough not to swallow it at 16x16.
BADGE_STROKE = 0.05

PROGRESS_STEPS = 1000

# What Windows asks a window icon for: 16 in the title bar, 32 on the taskbar,
# more again on a scaled display, and 48/256 in Alt-Tab and the task switcher.
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)

# DWM window attributes behind the iconic thumbnail.
# https://learn.microsoft.com/en-us/windows/win32/api/dwmapi/ne-dwmapi-dwmwindowattribute
DWMWA_FORCE_ICONIC_REPRESENTATION = 7
DWMWA_HAS_ICONIC_BITMAP = 10


def _set_iconic_attributes(hwnd: int, enabled: bool, invalidate: bool = True) -> bool:
    """Turn the iconic hover preview on or off, going around Qt to do it.

    `QWinThumbnailToolBar.setIconicPixmapNotificationsEnabled()` is not
    symmetrical: enabling works, disabling does not, and measured on Windows 11
    the preview stays stuck on the last artwork given to it for the life of the
    window. It appears only to push the DWM attributes when turning *on*.

    Invalidating the cached bitmap is not enough on its own either - tried, and
    the preview came back unchanged. Both attributes have to be cleared first,
    and only then does invalidating drop what Windows had already cached.

    Qt's own call is still made alongside this, so its internal state stays
    consistent and re-enabling keeps working through the supported path.

    On its own this is still not enough - the toolbar buttons have to go too,
    which is the part that actually makes it stick. See _destroy_buttons().
    """
    try:
        value = ctypes.c_int(1 if enabled else 0)
        dwm = ctypes.windll.dwmapi
        results = []
        for attribute in (DWMWA_HAS_ICONIC_BITMAP,
                          DWMWA_FORCE_ICONIC_REPRESENTATION):
            results.append(dwm.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)))
        # Drops the bitmap Windows is holding, so the next hover asks again
        # rather than re-serving what it already had.
        if invalidate:
            results.append(dwm.DwmInvalidateIconicBitmaps(hwnd))
            logger.debug('Iconic thumbnail %s on hwnd 0x%X: hr=%s',
                         'enabled' if enabled else 'disabled', hwnd,
                         ['0x%08X' % (r & 0xFFFFFFFF) for r in results])
        return all(r == 0 for r in results)
    except Exception as exc:
        logger.warning('Could not %s the iconic thumbnail: %s',
                       'enable' if enabled else 'disable', exc)
        return False


def _play_path(size):
    inset = size * 0.22
    path = QPainterPath()
    path.moveTo(inset * 1.15, inset)
    path.lineTo(size - inset, size / 2)
    path.lineTo(inset * 1.15, size - inset)
    path.closeSubpath()
    return path


def _pause_path(size):
    inset = size * 0.24
    bar = (size - inset * 2) * 0.34
    path = QPainterPath()
    path.addRect(QRectF(inset, inset, bar, size - inset * 2))
    path.addRect(QRectF(size - inset - bar, inset, bar, size - inset * 2))
    return path


def _stop_path(size):
    inset = size * 0.26
    path = QPainterPath()
    path.addRect(QRectF(inset, inset, size - inset * 2, size - inset * 2))
    return path


_BADGE_PATHS = {'play': _play_path, 'pause': _pause_path, 'stop': _stop_path}


def badge_icon(kind: str) -> QIcon:
    """A transport glyph for the taskbar button's corner.

    Filled, with a white stroke around it. The taskbar background is whatever
    the user's wallpaper and accent colour make it, so a glyph with no outline
    disappears against some of them - the stroke is what makes one badge work
    everywhere rather than only on a dark theme.
    """
    builder = _BADGE_PATHS.get(kind)
    if builder is None:
        return QIcon()

    pixmap = QPixmap(BADGE_SIZE, BADGE_SIZE)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(QPen(Qt.white, BADGE_SIZE * BADGE_STROKE))
    painter.setBrush(QBrush(BADGE_FILL))
    painter.drawPath(builder(BADGE_SIZE))
    painter.end()

    icon = QIcon()
    icon.addPixmap(pixmap)
    icon.addPixmap(pixmap.scaled(16, 16, Qt.KeepAspectRatio,
                                 Qt.SmoothTransformation))
    return icon


def _icon_from_artwork(artwork: QPixmap) -> QIcon:
    """Build a window icon from a cover, at the sizes Windows actually asks for.

    Handing over one 544x544 pixmap and letting Windows shrink it is visibly
    worse than doing the reduction here: this is a smooth scale per size, where
    the shell's own is not, and 16px is small enough for the difference to
    decide whether a cover is recognisable at all - which is the entire reason
    for putting it there.
    """
    icon = QIcon()
    for size in ICON_SIZES:
        icon.addPixmap(artwork.scaled(size, size, Qt.KeepAspectRatio,
                                      Qt.SmoothTransformation))
    return icon


def compose_thumbnail(artwork: QPixmap | None, size) -> QPixmap:
    """Fit artwork onto a wide, dark ground for the hover preview."""
    width, height = size
    canvas = QPixmap(width, height)
    canvas.fill(THUMBNAIL_GROUND)
    if artwork is None or artwork.isNull():
        return canvas

    scaled = artwork.scaled(width, height, Qt.KeepAspectRatio,
                            Qt.SmoothTransformation)
    painter = QPainter(canvas)
    painter.drawPixmap((width - scaled.width()) // 2,
                       (height - scaled.height()) // 2, scaled)
    painter.end()
    return canvas


class TaskbarIntegration(QObject):
    """Owns the taskbar button and its thumbnail toolbar for one window."""

    command = pyqtSignal(str)   # 'previous' | 'play_pause' | 'next'

    def __init__(self, window):
        super().__init__(window)
        self._window = window
        self._button = None
        self._thumbbar = None
        self._buttons = {}
        self._app_icon = window.windowIcon()
        # cacheKey() of the artwork the icon was last built from, or None for
        # the app mark. See _apply_icon().
        self._icon_key = None
        self._have_progress = False
        # Whether the hover preview is currently ours rather than Windows' own.
        self._iconic_enabled = False

        # What is playing, held rather than acted on directly, so that toggling
        # a setting can re-apply it without waiting for the next track.
        self._artwork = None
        self._status = None
        self._enabled = {'previous': False, 'play_pause': False, 'next': False}

    # -- Setup -------------------------------------------------------------

    @property
    def attached(self) -> bool:
        return self._button is not None

    def attach(self):
        """Bind to the window's native handle. Safe to call more than once.

        Deliberately not done in __init__: both QWinTaskbarButton and
        QWinThumbnailToolBar need a QWindow, and that does not exist until the
        widget has been shown at least once. Callers hook this to showEvent and
        it no-ops thereafter.
        """
        if self._button is not None:
            return
        handle = self._window.windowHandle()
        if handle is None:
            return

        try:
            self._button = QWinTaskbarButton(self)
            self._button.setWindow(handle)

            self._thumbbar = QWinThumbnailToolBar(self)
            self._thumbbar.setWindow(handle)
            self._thumbbar.iconicThumbnailPixmapRequested.connect(
                self._send_thumbnail)
            self._thumbbar.iconicLivePreviewPixmapRequested.connect(
                self._send_live_preview)
        except Exception:
            logger.exception('Taskbar integration unavailable')
            self._button = None
            self._thumbbar = None
            return

        logger.debug('Taskbar integration attached')
        self.apply_settings()

    # -- State -------------------------------------------------------------

    def set_track(self, artwork, status, can_previous, can_next, can_play_pause):
        """Artwork is the undecorated cover, or None."""
        self._artwork = artwork
        self._status = status
        self._enabled = {'previous': can_previous,
                         'play_pause': can_play_pause,
                         'next': can_next}
        self._refresh()

    def clear(self):
        """Nothing is playing."""
        self._artwork = None
        self._status = None
        self._enabled = dict.fromkeys(self._enabled, False)
        self._refresh()
        self.set_progress(None, False)

    def _refresh(self):
        """Apply the held state, as far as the settings allow.

        The single point where "what is playing" meets "what the user asked to
        see", so a track change and a settings change take the same path and
        cannot leave the button half-updated between them.
        """
        if self._button is None:
            return

        media = settings.taskbar_media_controls()

        if media and self._status:
            self._button.setOverlayIcon(badge_icon(self._status))
            self._button.setOverlayAccessibleDescription(self._status)
        else:
            self._button.clearOverlayIcon()

        if media:
            self._create_buttons()
            for key, button in self._buttons.items():
                button.setEnabled(self._enabled[key])
            self._buttons['play_pause'].setIcon(
                _transport_icon('pause' if self._status == 'play' else 'play'))
        else:
            self._destroy_buttons()

        self._set_iconic(media)
        if media:
            self._push_thumbnail()

        self._apply_icon()

    def _create_buttons(self):
        """Add the transport buttons, if they are not already there."""
        if self._buttons or self._thumbbar is None:
            return
        for name, glyph, tip in (('previous', 'previous', 'Previous'),
                                 ('play_pause', 'play', 'Play / Pause'),
                                 ('next', 'next', 'Next')):
            button = QWinThumbnailToolButton(self._thumbbar)
            button.setToolTip(tip)
            button.setIcon(_transport_icon(glyph))
            # The preview should not vanish on a click: the point of these is
            # transport control without going to the window, and pausing then
            # wanting next means re-hovering otherwise.
            button.setDismissOnClick(False)
            button.setEnabled(False)
            button.clicked.connect(
                lambda _=False, key=name: self.command.emit(key))
            self._thumbbar.addButton(button)
            self._buttons[name] = button
        logger.debug('Thumbnail toolbar buttons created')

    def _destroy_buttons(self):
        """Remove them outright rather than hiding them.

        Hiding is not enough, and this is the whole reason disabling the feature
        appeared not to work: with buttons present the iconic hover preview
        could not be turned off at all - clearing the DWM attributes reported
        success and changed nothing, three different ways. Measured against a
        toolbar with no buttons, the identical calls revert it immediately. So
        the buttons go, and come back when the feature does.
        """
        if not self._buttons or self._thumbbar is None:
            return
        self._buttons = {}
        self._thumbbar.clear()
        logger.debug('Thumbnail toolbar buttons removed')

    def _set_iconic(self, enabled):
        """Switch the hover preview between our artwork and Windows' own capture.

        What actually makes disabling work is removing the toolbar buttons -
        see _destroy_buttons(). The re-assertion here is belt and braces and was
        *not* sufficient on its own: it was tried alone against a toolbar that
        still had buttons, and the preview stayed stuck.

        It is kept because it costs two DwmSetWindowAttribute calls and nothing
        guarantees some other shell interaction will not re-arm the attributes.
        Deliberately *without* the invalidate, though: dropping the cached
        bitmap repeatedly would have Windows re-requesting a thumbnail
        continuously. Only the transition invalidates, which is the moment the
        stale artwork actually has to go.
        """
        changed = enabled != self._iconic_enabled
        self._iconic_enabled = enabled

        if changed:
            # Qt's own call, so its bookkeeping matches and re-enabling keeps
            # going through the supported path.
            self._thumbbar.setIconicPixmapNotificationsEnabled(enabled)
        elif enabled:
            # On and unchanged: nothing re-arms what is already armed.
            return

        _set_iconic_attributes(int(self._window.winId()), enabled,
                               invalidate=changed)
        if changed:
            logger.debug('Iconic hover preview -> %s',
                         'artwork' if enabled else 'window capture')

    def set_progress(self, fraction, playing):
        """Position through the track, 0.0-1.0, or None for no position."""
        if self._button is None:
            return
        if fraction is None or not settings.taskbar_progress():
            if self._have_progress:
                self._button.progress().setVisible(False)
                self._have_progress = False
            return

        progress = self._button.progress()
        progress.setRange(0, PROGRESS_STEPS)
        progress.setValue(round(max(0.0, min(1.0, fraction)) * PROGRESS_STEPS))
        progress.setVisible(True)
        self._have_progress = True
        # Paused turns the bar yellow, which is exactly the distinction wanted
        # and costs nothing - Windows already draws a paused state.
        progress.setPaused(not playing)

    @property
    def shows_progress(self) -> bool:
        """Whether a progress bar is on screen that something must keep ticking.

        The window's own repaint timer stops when it is off screen; this is what
        tells it that minimised is not off screen after all, because the taskbar
        bar is still being watched.
        """
        return self._have_progress

    def apply_settings(self):
        """Re-read the settings that change what is displayed."""
        if self._button is None:
            return
        self._refresh()
        if not settings.taskbar_progress() and self._have_progress:
            self._button.progress().setVisible(False)
            self._have_progress = False

    # -- Internals ---------------------------------------------------------

    def _apply_icon(self):
        """Swap the window icon between the app mark and the current cover.

        The window icon is what the taskbar draws, so this is the only way to
        put artwork there - and it is also the Alt-Tab and title bar icon, which
        is the trade being made.

        Guarded on the identity of the pixmap, not on whether there is one. That
        distinction is the whole point: a guard on "are we showing artwork?"
        is true for every track after the first, so the first cover would stick
        for the rest of the session. QPixmap.cacheKey() changes whenever a new
        cover is decoded and stays put when the panel re-uses the one it holds,
        which is exactly the question being asked.
        """
        artwork = self._artwork
        if not (settings.taskbar_artwork_icon()
                and artwork is not None
                and not artwork.isNull()):
            artwork = None

        key = artwork.cacheKey() if artwork is not None else None
        if key == self._icon_key:
            return
        self._icon_key = key
        self._window.setWindowIcon(
            _icon_from_artwork(artwork) if artwork is not None else self._app_icon)

    def _push_thumbnail(self):
        if self._thumbbar is None or not self._iconic_enabled:
            return
        self._thumbbar.setIconicThumbnailPixmap(
            compose_thumbnail(self._artwork, THUMBNAIL_SIZE))
        self._thumbbar.setIconicLivePreviewPixmap(
            compose_thumbnail(self._artwork, LIVE_PREVIEW_SIZE))

    # Windows asks for these; it does not always stop asking the moment the
    # attributes are cleared. Answering anyway is what made disabling look
    # broken: handing back a pixmap re-arms DWMWA_HAS_ICONIC_BITMAP, so the
    # feature turned itself straight back on. The guard is the fix - a request
    # that goes unanswered leaves Windows to capture the window itself, which
    # is exactly what is wanted.

    def _send_thumbnail(self):
        if not self._iconic_enabled:
            logger.debug('Thumbnail requested while off - refused')
            return
        self._thumbbar.setIconicThumbnailPixmap(
            compose_thumbnail(self._artwork, THUMBNAIL_SIZE))

    def _send_live_preview(self):
        if not self._iconic_enabled:
            logger.debug('Live preview requested while off - refused')
            return
        self._thumbbar.setIconicLivePreviewPixmap(
            compose_thumbnail(self._artwork, LIVE_PREVIEW_SIZE))


def _transport_icon(kind: str) -> QIcon:
    """Toolbar glyphs, drawn white on transparent.

    The thumbnail toolbar strip is drawn by Windows on its own dark chrome, so
    unlike the corner badge these need no outline - and a stroke at this size
    only muddies them.
    """
    size = 32
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QBrush(Qt.white))

    if kind == 'play':
        painter.drawPath(_play_path(size))
    elif kind == 'pause':
        painter.drawPath(_pause_path(size))
    else:
        bar = size * 0.10
        inset = size * 0.22
        forward = kind == 'next'
        path = QPainterPath()
        if forward:
            path.moveTo(inset, inset)
            path.lineTo(size - inset - bar, size / 2)
            path.lineTo(inset, size - inset)
            path.closeSubpath()
            path.addRect(QRectF(size - inset - bar, inset, bar, size - inset * 2))
        else:
            path.moveTo(size - inset, inset)
            path.lineTo(inset + bar, size / 2)
            path.lineTo(size - inset, size - inset)
            path.closeSubpath()
            path.addRect(QRectF(inset, inset, bar, size - inset * 2))
        painter.drawPath(path)

    painter.end()
    return QIcon(pixmap)
