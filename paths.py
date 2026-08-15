"""Locating bundled files, both when running from source and when frozen."""

import os
import sys

is_frozen = getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS')

APP_ROOT = sys._MEIPASS if is_frozen else os.path.dirname(os.path.abspath(__file__))


def resource_path(*parts) -> str:
    """Absolute path to a file shipped with the application."""
    return os.path.join(APP_ROOT, *parts)


APP_ICON = resource_path('resources', 'app_icon.ico')
