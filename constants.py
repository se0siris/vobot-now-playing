VERSION_NUMBER = (1, 0, 0, 0)
VERSION_DATE = '09/08/2026'
VERSION = f"{'.'.join(map(str, VERSION_NUMBER[:3]))} - {VERSION_DATE}"

APP_NAME = 'Vobot Now Playing'
ORG_NAME = 'overThere'

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
# status, pushed when Windows has no media session, and UDP discovery.
PROTOCOL_VERSION = 3

# Fallback frame geometry. The device reports its own in the handshake ack and
# that value is adopted for subsequent frames.
FRAME_SIZE_DEFAULT = (320, 240)