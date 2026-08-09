"""Dark theme for the Now Playing client.

Two layers: a Fusion palette so the bits Qt draws itself (spin box arrows, check
indicators, focus rings, scrollbars) come out light, and a stylesheet on top for
the bespoke panel, footer and buttons. Doing only the stylesheet leaves those
native-drawn widgets dark-on-dark.

Blues are lifted from the Mini Dock app icon so the two halves of the project
look related.
"""
import logging

from ctypes import byref, c_int, sizeof, windll

from PyQt5.QtGui import QColor, QPalette

logger = logging.getLogger(__name__)

# DWM window attributes. 20 is the documented dark mode flag; Windows 10 builds
# before 20H1 used 19 for the same thing. The colour attributes are Windows 11
# only and fail harmlessly before that.
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_USE_IMMERSIVE_DARK_MODE_LEGACY = 19
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36

# Surfaces.
BACKGROUND = '#10141f'
PANEL_TOP = '#1a2133'
FOOTER = '#0c1017'
SURFACE = '#1e2637'
SURFACE_HOVER = '#28324a'
SURFACE_PRESSED = '#171e2c'
BORDER = '#28324a'
ART_EMPTY = '#0b0e16'

# Text.
TEXT = '#f1f4fa'
TEXT_SECONDARY = '#9fabc4'
TEXT_MUTED = '#6e7a93'

# Accents.
ACCENT = '#56c4ff'
ACCENT_DEEP = '#1c74d6'
OK = '#46d17f'
ERROR = '#ff6b6b'
IDLE = '#5a677f'

STYLESHEET = f"""
QWidget {{
    font-family: 'Segoe UI', 'Segoe UI Variable', sans-serif;
    font-size: 9pt;
    color: {TEXT};
}}

QMainWindow, QDialog {{
    background: {BACKGROUND};
}}

/* -- Now playing panel ------------------------------------------------- */

#panel_now_playing {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                stop:0 {PANEL_TOP}, stop:1 {BACKGROUND});
}}

#lbl_art {{
    background: {ART_EMPTY};
    border: 1px solid {BORDER};
    border-radius: 10px;
    color: {TEXT_MUTED};
    font-size: 8pt;
}}

/* Artwork is handed over already rounded, so the label must not paint a
   second, differently-shaped frame behind it. */
#lbl_art[has_art="true"] {{
    background: transparent;
    border: none;
}}

#lbl_title {{
    font-size: 19pt;
    font-weight: 600;
    color: {TEXT};
}}

#lbl_artist {{
    font-size: 12pt;
    color: {TEXT_SECONDARY};
}}

#lbl_album {{
    font-size: 10pt;
    color: {TEXT_MUTED};
}}

#lbl_status {{
    font-size: 9pt;
    font-weight: 600;
    color: {TEXT_MUTED};
}}

#lbl_status[playing="true"] {{
    color: {ACCENT};
}}

/* -- Footer ------------------------------------------------------------ */

#footer {{
    background: {FOOTER};
    border-top: 1px solid {BORDER};
}}

#lbl_device {{
    color: {TEXT_SECONDARY};
}}

#lbl_device_dot {{
    border-radius: 5px;
    background: {IDLE};
}}

#lbl_device_dot[state="ok"] {{
    background: {OK};
}}

#lbl_device_dot[state="error"] {{
    background: {ERROR};
}}

/* -- Controls ---------------------------------------------------------- */

QPushButton {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 7px 18px;
    color: {TEXT};
}}

QPushButton:hover {{
    background: {SURFACE_HOVER};
    border-color: {ACCENT_DEEP};
}}

QPushButton:pressed {{
    background: {SURFACE_PRESSED};
}}

QPushButton:disabled {{
    color: {TEXT_MUTED};
    background: {SURFACE_PRESSED};
    border-color: {BORDER};
}}

QPushButton:default {{
    border-color: {ACCENT_DEEP};
}}

/* Transport controls: square-ish, glyph-only, sized so the three read as a set. */
#button_previous, #button_play_pause, #button_next {{
    min-width: 48px;
    min-height: 34px;
    padding: 0;
    font-size: 11pt;
}}

#button_play_pause {{
    min-width: 62px;
    color: {ACCENT};
}}

#button_play_pause:disabled {{
    color: {TEXT_MUTED};
}}

QLineEdit, QSpinBox {{
    background: {ART_EMPTY};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 8px;
    selection-background-color: {ACCENT_DEEP};
}}

QLineEdit:focus, QSpinBox:focus {{
    border-color: {ACCENT};
}}

QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 8px;
    margin-top: 10px;
    padding: 14px 14px 12px 14px;
    font-weight: 600;
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 0 6px;
    color: {TEXT_SECONDARY};
}}

QCheckBox {{
    spacing: 8px;
}}

#lbl_test_result[result="ok"] {{
    color: {OK};
}}

#lbl_test_result[result="error"] {{
    color: {ERROR};
}}

QMenu {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    padding: 4px;
}}

QMenu::item {{
    padding: 6px 22px 6px 16px;
    border-radius: 4px;
}}

QMenu::item:selected {{
    background: {ACCENT_DEEP};
}}

QMenu::separator {{
    height: 1px;
    background: {BORDER};
    margin: 4px 8px;
}}

QToolTip {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    color: {TEXT};
    padding: 4px 6px;
}}
"""


def dark_palette() -> QPalette:
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(BACKGROUND))
    palette.setColor(QPalette.WindowText, QColor(TEXT))
    palette.setColor(QPalette.Base, QColor(ART_EMPTY))
    palette.setColor(QPalette.AlternateBase, QColor(SURFACE))
    palette.setColor(QPalette.Text, QColor(TEXT))
    palette.setColor(QPalette.Button, QColor(SURFACE))
    palette.setColor(QPalette.ButtonText, QColor(TEXT))
    palette.setColor(QPalette.BrightText, QColor(ERROR))
    palette.setColor(QPalette.Highlight, QColor(ACCENT_DEEP))
    palette.setColor(QPalette.HighlightedText, QColor(TEXT))
    palette.setColor(QPalette.ToolTipBase, QColor(SURFACE))
    palette.setColor(QPalette.ToolTipText, QColor(TEXT))
    # Fusion draws check boxes, spin arrows and frames from these, and the
    # defaults are near-black against a dark window - the outlines vanish.
    palette.setColor(QPalette.Light, QColor(SURFACE_HOVER))
    palette.setColor(QPalette.Midlight, QColor(SURFACE))
    palette.setColor(QPalette.Mid, QColor(BORDER))
    palette.setColor(QPalette.Dark, QColor('#0a0d14'))
    palette.setColor(QPalette.Shadow, QColor('#05070b'))
    palette.setColor(QPalette.PlaceholderText, QColor(TEXT_MUTED))
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor(TEXT_MUTED))
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(TEXT_MUTED))
    palette.setColor(QPalette.Disabled, QPalette.WindowText, QColor(TEXT_MUTED))
    return palette


def apply_theme(app) -> None:
    app.setStyle('Fusion')
    app.setPalette(dark_palette())
    app.setStyleSheet(STYLESHEET)


def _colorref(hex_colour: str) -> int:
    """Qt-style '#rrggbb' to the 0x00bbggrr COLORREF that DWM wants."""
    value = hex_colour.lstrip('#')
    red, green, blue = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    return (blue << 16) | (green << 8) | red


def _set_dwm_attribute(hwnd: int, attribute: int, value: int) -> bool:
    try:
        stored = c_int(value)
        return windll.dwmapi.DwmSetWindowAttribute(
            hwnd, attribute, byref(stored), sizeof(stored)) == 0
    except (AttributeError, OSError):
        return False


def use_dark_titlebar(widget) -> None:
    """Make the window frame match the window.

    Qt has no say over the non-client area, so a dark window otherwise sits
    under a light caption bar. Immersive dark mode alone is not enough either -
    it loses to the user's "show accent colour on title bars" setting - so on
    Windows 11 the caption is painted explicitly as well.
    """
    hwnd = int(widget.winId())

    for attribute in (DWMWA_USE_IMMERSIVE_DARK_MODE,
                      DWMWA_USE_IMMERSIVE_DARK_MODE_LEGACY):
        if _set_dwm_attribute(hwnd, attribute, 1):
            break
    else:
        logger.debug('DWM would not switch the title bar to dark mode')

    painted = all([
        _set_dwm_attribute(hwnd, DWMWA_CAPTION_COLOR, _colorref(BACKGROUND)),
        _set_dwm_attribute(hwnd, DWMWA_TEXT_COLOR, _colorref(TEXT)),
        _set_dwm_attribute(hwnd, DWMWA_BORDER_COLOR, _colorref(BORDER)),
    ])
    if not painted:
        logger.debug('Caption colours need Windows 11; leaving the frame alone')


def restyle(widget) -> None:
    """Re-evaluate stylesheet rules after a dynamic property changed.

    Qt only matches property selectors when the widget is polished, so a widget
    whose 'state' or 'playing' property changes keeps its old look until this
    runs.
    """
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()
