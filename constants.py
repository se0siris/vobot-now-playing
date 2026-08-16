VERSION_NUMBER = (1, 1, 0, 0)
VERSION_DATE = '16/08/2026'
VERSION = f'{".".join(map(str, VERSION_NUMBER[:3]))} - {VERSION_DATE}'

APP_NAME = 'Vobot Now Playing'
ORG_NAME = 'overThere'

# Identity shown in the About dialog. AUTHOR is the copyright holder, which is
# not the same thing as ORG_NAME above - that one only names the settings folder
# and the exe's CompanyName field.
AUTHOR = 'Gary Hughes'
COPYRIGHT_YEAR = 2026
REPO_URL = 'https://github.com/se0siris/vobot-now-playing'
# PyQt5 is GPL v3, so the bundled client can only be distributed under GPL v3
# terms. This is a consequence of the dependency, not a preference.
LICENSE_NAME = 'GNU General Public License v3'
LICENSE_URL = 'https://www.gnu.org/licenses/gpl-3.0.html'

# Defaults only - the address in use is stored per-installation via settings.py
# and changed from the Settings dialog.
#
# Deliberately blank: a hardcoded address is only ever right on one network, so
# a fresh install discovers the dock over UDP instead of failing against
# someone else's IP. See discovery.py.
TCP_IP = ''
TCP_PORT = 32150
# Handshake budget: connect, send a short header, read one JSON line. If that
# does not come back quickly the device is not there.
TCP_TIMEOUT = 5
# The 150KB artwork body is a different proposition - measured at 2-3 seconds
# over WiFi, so it cannot share the handshake budget. One garbage collection on
# the device (200-280ms) or a WiFi retransmit was enough to blow the 5 second
# limit mid-transfer, which left the dock draining a dead socket and answering
# 'busy' to the retry.
TCP_ART_TIMEOUT = 20

# Wire protocol spoken with the Mini Dock app. 2 added the art_id handshake, so
# unchanged album art is not re-sent on every play/pause event. 3 added the IDLE
# status, pushed when Windows has no media session, and UDP discovery. 4 added
# the `light` field, driving the dock's ambient light from the artwork.
PROTOCOL_VERSION = 4

# Ambient light brightness, 0-100, as the dock's peripherals API takes it. Only
# a default: the value in use is per-installation, via settings.py. 60 rather
# than full, because the strip faces the wall behind the dock and 100 washes out
# the colour it is meant to be showing.
LIGHT_BRIGHTNESS_DEFAULT = 60

# Fallback frame geometry. The device reports its own in the handshake ack and
# that value is adopted for subsequent frames.
FRAME_SIZE_DEFAULT = (320, 240)
