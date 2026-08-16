# -*- mode: python ; coding: utf-8 -*-
import sys
import re
import PyInstaller.utils.win32.versioninfo as version_info

# Must come before the project imports below: PyInstaller execs this file
# without the spec's own directory on sys.path, so they only resolve by luck of
# the working directory otherwise.
sys.path.insert(0, SPECPATH)

from pyinstaller_monkey_patch import manifest
from constants import AUTHOR, COPYRIGHT_YEAR, VERSION_NUMBER, APP_NAME, ORG_NAME

# AUTHOR, not ORG_NAME: the copyright holder is the person, and this has to
# agree with the About dialog and the LICENSE. COPYRIGHT_YEAR rather than the
# build date, so rebuilding next year does not silently restamp the exe with a
# year the rest of the project does not claim.
copyright_text = f'Copyright © {COPYRIGHT_YEAR} {AUTHOR}'
version_string = '.'.join(map(str, VERSION_NUMBER))

version = version_info.VSVersionInfo(
    ffi=version_info.FixedFileInfo(
        # filevers and prodvers should be always a tuple with four items: (1, 2, 3, 4)
        # Set not needed items to zero 0.
        filevers=VERSION_NUMBER,
        prodvers=VERSION_NUMBER,
        # Contains a bitmask that specifies the valid bits 'flags'r
        mask=0x3f,
        # Contains a bitmask that specifies the Boolean attributes of the file.
        flags=0x0,
        # The operating system for which this file was designed.
        # 0x4 - NT and there is no need to change it.
        OS=0x40004,
        # The general type of file.
        # 0x1 - the file is an application.
        fileType=0x1,
        # The function of the file.
        # 0x0 - the function is not defined for this fileType
        subtype=0x0,
        # Creation date and time stamp.
        date=(0, 0)
    ),
    kids=[
        version_info.StringFileInfo(
            [
                version_info.StringTable(
                    '040904b0',
                    [version_info.StringStruct('CompanyName', ORG_NAME),
                     version_info.StringStruct('FileDescription', APP_NAME),
                     version_info.StringStruct('FileVersion', version_string),
                     version_info.StringStruct('InternalName', APP_NAME),
                     version_info.StringStruct('LegalCopyright', copyright_text),
                     version_info.StringStruct('OriginalFilename', f'{APP_NAME}.exe'),
                     version_info.StringStruct('ProductName', f'{APP_NAME}.exe'),
                     version_info.StringStruct('ProductVersion', version_string)])
            ]),
        version_info.VarFileInfo(
            [version_info.VarStruct('Translation', [1033, 1200])]
        )
    ]
)

excludes = (
    'numpy', 'pywin', 'tcl', 'tk', 'Tkinter', '_tkinter', 'test', 'lib2to3', 'Include',
    'ImageTk', 'PIL._imagingtk', 'PIL._avif', 'PyInstaller', '_ssl', 'bz2', '_bsddb',
    'lzma', 'pyconfig',
    # Saves ~5MB of libcrypto. hashlib.sha1 (used for art_id) falls back to
    # CPython's built-in _sha1 when _hashlib is absent.
    '_hashlib',
    # Nothing here spawns a process: the worker is a QThread and
    # run_in_executor(None, ...) is a *thread* pool. With no child processes
    # there is nothing for freeze_support() to guard against either.
    'multiprocessing',
)
# NOT excluded, however tempting:
#   decimal - PIL.PngImagePlugin imports it, and without it PNG never registers.
#             Image.open() then rejects every PNG thumbnail Windows hands over
#             with UnidentifiedImageError, while JPEG ones still work - which is
#             what makes it look like an intermittent artwork bug.

# Pillow imports its format plugins by name at runtime, from Image.init(), so
# nothing in the source tree references them and the analysis cannot see them.
# Without these Image.open() finds no plugin willing to claim the bytes and
# raises UnidentifiedImageError on artwork that decodes perfectly from source.
# Only the formats Windows actually hands out as thumbnails are listed.
hiddenimports = [
    'PIL.BmpImagePlugin',
    'PIL.GifImagePlugin',
    'PIL.JpegImagePlugin',
    'PIL.PngImagePlugin',
    'PIL.WebPImagePlugin',
]

block_cipher = None
a = Analysis(
    ['vobot_now_playing.py'],
    pathex=[SPECPATH],
    binaries=[],
    # paths.resource_path() resolves under sys._MEIPASS when frozen, which for a
    # onedir build is the _internal folder - NOT the folder holding the exe. So
    # the tree has to be bundled here; copying resources/ next to the exe by hand
    # puts it somewhere the app never looks.
    datas=[('resources', 'resources')],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
    optimize=1
)

# Exclude DLLs that aren't needed.
#
# Two image format plugins have to survive this list:
#   qico  - QIcon loads resources/app_icon.ico for the window and tray. Without
#           it the icon yields no pixmap, and Windows then draws no tray icon at
#           all - not a blank one - leaving no way to quit but Task Manager.
#   qjpeg - MainWindow.set_artwork() previews the raw thumbnail with
#           QPixmap.loadFromData(), and those are commonly JPEG. (PNG is built
#           into QtGui, so it needs no plugin.)
ex_plugins = (
    'd3dcompiler', 'libeay32', 'libegl', 'libgles', 'opengl', 'ssleay32', 'genericbearer',
    'qgif', 'qicns', 'qtga', 'qtiff', 'qwbmp', 'qwebp', 'qsvg',
    'qminimal', 'qoffscreen', 'qwebgl', 'qxdgdesktopportal',
    'qsqlmysql', 'qsqlodbc', 'qsqlpsql', 'qsqlite', 'qwindowsvistastyle', 'qtbase_', 'dbus',
    '5qml', '5quick', '5websockets', 'api-ms-win', 'libcrypto-1_1',
    # Whole plugin families this app never touches.
    'geoservices', 'mediaservice', 'playlistformats', 'position', 'sensors',
    'sensorgestures', 'texttospeech', 'audio', 'designer', 'translations', 'qml',
    'QtHelp', 'QtMultimedia', 'QtMultimediaWidgets', 'QtNetwork', 'QtPositioning', 'QtQml', 'QtQuick', 'QtQuickWidgets',
    'QtSql', 'QtSvg', 'QtTest', 'QtWebChannel', 'QtWebEngine', 'QtWebEngineCore', 'QtWebEngineWidgets', 'QtWebSockets',
    'QtXml', 'QtXmlPatterns', 'Qt5Location', 'Qt5Multimedia', 'Qt5MultimediaWidgets', 'Qt5Network', 'Qt5Nfc',
    'Qt5PrintSupport', 'Qt5Bluetooth', 'Qt5Sql', 'QtPrintSupport', 'QtBluetooth', 'Qt5Help', 'Qt5WebView',
    'qtuiotouchplugin', 'windowsprintersupport'
)
# IGNORECASE matters: the filter tests x[0].lower(), so every mixed-case entry
# above ('QtNetwork', 'QtXmlPatterns', ...) could never match and was silently
# doing nothing - which is how Qt5XmlPatterns.dll, Qt5Network.dll and friends
# kept ending up in the build.
regex_plugin_filter = re.compile(r'^.*(?:{0:s}).*$'.format('|'.join(ex_plugins)),
                                 re.IGNORECASE)

a.binaries = [x for x in a.binaries if not regex_plugin_filter.match(x[0].lower())]
a.datas = [x for x in a.datas if not regex_plugin_filter.match(x[0].lower())]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(pyz,
          a.scripts,
          [],
          exclude_binaries=True,
          name=APP_NAME,
          debug=False,
          bootloader_ignore_signals=False,
          strip=False,
          upx=False,
          console=False,
          icon=r'resources/app_icon.ico',
          version=version,
          manifest=manifest
          )

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME
)
