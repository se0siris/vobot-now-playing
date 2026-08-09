VERSION_NUMBER = (1, 0, 0, 0)
VERSION_DATE = '14/09/2025'
VERSION = f"{'.'.join(map(str, VERSION_NUMBER[:3]))} - {VERSION_DATE}"

APP_NAME = 'Vobot Now Playing'
ORG_NAME = 'overThere'

TCP_IP = '192.168.1.26'
TCP_PORT = 32150
TCP_TIMEOUT = 5

# Wire protocol spoken with the Mini Dock app. 2 added the art_id handshake, so
# unchanged album art is not re-sent on every play/pause event.
PROTOCOL_VERSION = 2

# Fallback frame geometry. The device reports its own in the handshake ack and
# that value is adopted for subsequent frames.
FRAME_SIZE_DEFAULT = (320, 240)