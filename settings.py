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

from constants import TCP_IP, TCP_PORT

logger = logging.getLogger(__name__)

# Keys.
KEY_HOST = 'device/host'
KEY_PORT = 'device/port'
KEY_AUTO_DISCOVER = 'device/auto_discover'
KEY_CLOSE_TO_TRAY = 'window/close_to_tray'
KEY_START_MINIMIZED = 'window/start_minimized'
KEY_GEOMETRY = 'window/geometry'

# Written out on first run so the file exists, with every key present, before
# anyone goes looking for it. Geometry is deliberately absent - it is an opaque
# blob that only means anything once a window has been placed.
DEFAULTS = {
    KEY_HOST: TCP_IP,
    KEY_PORT: TCP_PORT,
    KEY_AUTO_DISCOVER: True,
    KEY_CLOSE_TO_TRAY: True,
    KEY_START_MINIMIZED: False,
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


def close_to_tray() -> bool:
    return _bool_setting(KEY_CLOSE_TO_TRAY)


def set_close_to_tray(enabled: bool) -> None:
    _settings().setValue(KEY_CLOSE_TO_TRAY, bool(enabled))


def start_minimized() -> bool:
    return _bool_setting(KEY_START_MINIMIZED)


def set_start_minimized(enabled: bool) -> None:
    _settings().setValue(KEY_START_MINIMIZED, bool(enabled))


def geometry() -> bytes | None:
    return _settings().value(KEY_GEOMETRY, None)


def set_geometry(value) -> None:
    _settings().setValue(KEY_GEOMETRY, value)


def _to_bool(value) -> bool:
    # An INI file stores bools as the strings 'true'/'false'.
    if isinstance(value, bool):
        return value
    return str(value).lower() in ('true', '1', 'yes')
