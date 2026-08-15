"""Persisted user settings, held in a plain INI file.

The device address used to live in constants.py. It is per-installation rather
than per-build, so it belongs here alongside the window state.

QSettings would default to the registry on Windows. An INI file under %APPDATA%
is used instead so the values can be read and edited with a text editor:

    %APPDATA%\\overThere\\Vobot Now Playing\\settings.ini
"""
import logging
import os

from PyQt5.QtCore import QSettings, QStandardPaths

from constants import LIGHT_BRIGHTNESS_DEFAULT, TCP_IP, TCP_PORT

logger = logging.getLogger(__name__)

# Keys.
KEY_HOST = 'device/host'
KEY_PORT = 'device/port'
KEY_AUTO_DISCOVER = 'device/auto_discover'
KEY_LIGHT_ENABLED = 'light/enabled'
KEY_LIGHT_BRIGHTNESS = 'light/brightness'
KEY_CLOSE_TO_TRAY = 'window/close_to_tray'
KEY_START_MINIMIZED = 'window/start_minimized'
KEY_TRAY_HINT = 'window/tray_hint'
KEY_GEOMETRY = 'window/geometry'
KEY_TASKBAR_BUTTON = 'taskbar/keep_button'
KEY_TASKBAR_MEDIA_CONTROLS = 'taskbar/media_controls'
KEY_TASKBAR_ARTWORK_ICON = 'taskbar/artwork_icon'
KEY_TASKBAR_PROGRESS = 'taskbar/progress'

# Written out on first run so the file exists, with every key present, before
# anyone goes looking for it. Geometry is deliberately absent - it is an opaque
# blob that only means anything once a window has been placed.
DEFAULTS = {
    KEY_HOST: TCP_IP,
    KEY_PORT: TCP_PORT,
    KEY_AUTO_DISCOVER: True,
    # Off by default, and deliberately so: turning it on makes the dock's app
    # take ownership of the ambient light, suppressing whatever the device was
    # doing with it. Not something to do to someone who did not ask.
    KEY_LIGHT_ENABLED: False,
    KEY_LIGHT_BRIGHTNESS: LIGHT_BRIGHTNESS_DEFAULT,
    KEY_CLOSE_TO_TRAY: True,
    KEY_START_MINIMIZED: False,
    # On until the user says otherwise: the first time a window vanishes into
    # the tray is exactly when it needs explaining. It is the *repetition* that
    # grates, which is what clicking the notification turns off.
    KEY_TRAY_HINT: True,
    # All off: the taskbar features are extras to opt into, not the way this app
    # behaves out of the box. Stock, its taskbar button is an ordinary one -
    # the app's own icon, a preview of the window, nothing borrowed.
    #
    # Note the cost of the first being off: a hidden window has no taskbar
    # button, so hiding takes the media controls, the artwork icon and the
    # progress bar with it. The rest still apply while the window is open or
    # minimised, which is why they are independent of it rather than nested.
    KEY_TASKBAR_BUTTON: False,
    KEY_TASKBAR_MEDIA_CONTROLS: False,
    KEY_TASKBAR_ARTWORK_ICON: False,
    KEY_TASKBAR_PROGRESS: False,
}


def ini_path() -> str:
    """Full path to settings.ini.

    AppDataLocation resolves to %APPDATA%/<organisation>/<application>, so the
    names set on QApplication at startup decide the folder. Resolved on each
    call rather than at import time, because this module is imported before
    those names are set.
    """
    directory = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    # Qt hands back forward slashes; normalise so logs and errors read natively.
    return os.path.normpath(os.path.join(directory, 'settings.ini'))


def _settings() -> QSettings:
    return QSettings(ini_path(), QSettings.IniFormat)


def init() -> None:
    """Create the settings file, populated with any keys it is missing.

    QSettings writes nothing until a value changes, which would leave nothing on
    disk to edit until the user had already changed something in the UI.

    Call once at startup, after the application and organisation names are set.
    """
    path = ini_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)

    settings = _settings()
    missing = {key: value for key, value in DEFAULTS.items()
               if not settings.contains(key)}
    if missing:
        for key, value in missing.items():
            settings.setValue(key, value)
        settings.sync()

    logger.info('Settings file: %s', path)


def device_host() -> str:
    return str(_settings().value(KEY_HOST, DEFAULTS[KEY_HOST]))


def device_port() -> int:
    fallback = DEFAULTS[KEY_PORT]
    try:
        return int(_settings().value(KEY_PORT, fallback))
    except (TypeError, ValueError):
        logger.warning('Stored port is not a number; falling back to %d', fallback)
        return fallback


def set_device_address(host: str, port: int) -> None:
    settings = _settings()
    settings.setValue(KEY_HOST, host)
    settings.setValue(KEY_PORT, int(port))


def _bool_setting(key: str) -> bool:
    """Read a stored flag, falling back to its default from DEFAULTS."""
    return _to_bool(_settings().value(key, DEFAULTS[key]))


def auto_discover() -> bool:
    return _bool_setting(KEY_AUTO_DISCOVER)


def set_auto_discover(enabled: bool) -> None:
    _settings().setValue(KEY_AUTO_DISCOVER, bool(enabled))


def light_enabled() -> bool:
    return _bool_setting(KEY_LIGHT_ENABLED)


def set_light_enabled(enabled: bool) -> None:
    _settings().setValue(KEY_LIGHT_ENABLED, bool(enabled))


def light_brightness() -> int:
    """Ambient light brightness, 0-100.

    Clamped on the way out as well as parsed, since this is hand-editable and
    the dock would otherwise be handed a value its API does not accept.
    """
    fallback = DEFAULTS[KEY_LIGHT_BRIGHTNESS]
    try:
        value = int(_settings().value(KEY_LIGHT_BRIGHTNESS, fallback))
    except (TypeError, ValueError):
        logger.warning('Stored light brightness is not a number; falling back to %d',
                       fallback)
        return fallback
    return max(0, min(100, value))


def set_light_brightness(value: int) -> None:
    _settings().setValue(KEY_LIGHT_BRIGHTNESS, max(0, min(100, int(value))))


def close_to_tray() -> bool:
    return _bool_setting(KEY_CLOSE_TO_TRAY)


def set_close_to_tray(enabled: bool) -> None:
    _settings().setValue(KEY_CLOSE_TO_TRAY, bool(enabled))


def start_minimized() -> bool:
    return _bool_setting(KEY_START_MINIMIZED)


def set_start_minimized(enabled: bool) -> None:
    _settings().setValue(KEY_START_MINIMIZED, bool(enabled))


def tray_hint() -> bool:
    return _bool_setting(KEY_TRAY_HINT)


def set_tray_hint(enabled: bool) -> None:
    _settings().setValue(KEY_TRAY_HINT, bool(enabled))


def taskbar_button() -> bool:
    return _bool_setting(KEY_TASKBAR_BUTTON)


def set_taskbar_button(enabled: bool) -> None:
    _settings().setValue(KEY_TASKBAR_BUTTON, bool(enabled))


def taskbar_media_controls() -> bool:
    """Whether the taskbar button acts as a media control.

    Gates three things at once - the status badge, the transport buttons, and
    the artwork thumbnail that replaces the live window preview - because they
    are one idea rather than three preferences.
    """
    return _bool_setting(KEY_TASKBAR_MEDIA_CONTROLS)


def set_taskbar_media_controls(enabled: bool) -> None:
    _settings().setValue(KEY_TASKBAR_MEDIA_CONTROLS, bool(enabled))


def taskbar_artwork_icon() -> bool:
    return _bool_setting(KEY_TASKBAR_ARTWORK_ICON)


def set_taskbar_artwork_icon(enabled: bool) -> None:
    _settings().setValue(KEY_TASKBAR_ARTWORK_ICON, bool(enabled))


def taskbar_progress() -> bool:
    return _bool_setting(KEY_TASKBAR_PROGRESS)


def set_taskbar_progress(enabled: bool) -> None:
    _settings().setValue(KEY_TASKBAR_PROGRESS, bool(enabled))


def geometry() -> bytes | None:
    return _settings().value(KEY_GEOMETRY, None)


def set_geometry(value) -> None:
    _settings().setValue(KEY_GEOMETRY, value)


def _to_bool(value) -> bool:
    # An INI file stores bools as the strings 'true'/'false'.
    if isinstance(value, bool):
        return value
    return str(value).lower() in ('true', '1', 'yes')
