"""The Now Playing wire protocol, as spoken to the Mini Dock.

Header is one line of JSON; the device replies with a JSON ack that says whether
it already holds the artwork, and reports its own panel geometry.
"""
import json
import logging
import socket

from dataclasses import dataclass

import settings

from constants import (
    FRAME_SIZE_DEFAULT,
    PROTOCOL_VERSION,
    TCP_ART_TIMEOUT,
    TCP_TIMEOUT,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SendResult:
    """Outcome of a push, carrying enough detail for the UI to explain itself."""
    ok: bool
    error: str | None = None

    def __bool__(self) -> bool:
        return self.ok


class DeviceLink:
    """Talks the Now Playing wire protocol to the Mini Dock.

    Holds the last artwork the device acknowledged so unchanged album art is not
    re-sent. That matters because playback_info_changed fires on every play,
    pause and seek - previously each one pushed a fresh 150KB frame.
    """

    def __init__(self, host: str | None = None, port: int | None = None):
        self.host = host if host is not None else settings.device_host()
        self.port = port if port is not None else settings.device_port()
        self.frame_size = FRAME_SIZE_DEFAULT
        self._device_art_id: str | None = None

    def set_address(self, host: str, port: int) -> None:
        """Point at a different device, forgetting what the old one held."""
        if (host, port) == (self.host, self.port):
            return
        logger.info('Device address changed to %s:%d', host, port)
        self.host = host
        self.port = port
        self._device_art_id = None
        self.frame_size = FRAME_SIZE_DEFAULT

    @property
    def device_art_id(self) -> str | None:
        """Artwork the device is known to be holding, if any.

        Lets a caller with nothing better to send announce what is already on the
        panel, so the exchange stays a header instead of a frame transfer. None
        whenever that is unknown - a fresh link, a failed send, or a device that
        reported a different panel size - in which case nothing may be assumed.
        """
        return self._device_art_id

    def _read_ack(self, sock: socket.socket, buffer: bytearray) -> dict:
        """Read one newline-terminated JSON object, keeping any trailing bytes."""
        while b'\n' not in buffer:
            chunk = sock.recv(1024)
            if not chunk:
                raise ConnectionError('Device closed the connection')
            buffer += chunk
        line, _, rest = bytes(buffer).partition(b'\n')
        buffer[:] = rest
        return json.loads(line.decode('utf-8'))

    def send(self, meta: dict, image_bytes: bytes | None) -> SendResult:
        art_id = meta.get('art_id')
        header = dict(meta)
        header['proto'] = PROTOCOL_VERSION
        header['image_len'] = len(image_bytes) if image_bytes else 0

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(TCP_TIMEOUT)
                sock.connect((self.host, self.port))

                sock.sendall(json.dumps(header).encode('utf-8') + b'\n')
                buffer = bytearray()
                ack = self._read_ack(sock, buffer)
                logger.debug('Device ack: %s', ack)

                # Adopt whatever geometry the device reports so a panel that is
                # not 320x240 still gets correctly sized frames next time.
                width, height = ack.get('w'), ack.get('h')
                if width and height and (width, height) != self.frame_size:
                    logger.info('Device frame size is %dx%d', width, height)
                    self.frame_size = (width, height)
                    self._device_art_id = None

                if not ack.get('ok', False):
                    error = ack.get('error') or 'Device rejected the update'
                    logger.warning('Device rejected update: %s', error)
                    self._device_art_id = None
                    return SendResult(False, error)

                if ack.get('send_art'):
                    if not image_bytes:
                        logger.warning('Device asked for artwork we do not have')
                        self._device_art_id = None
                        return SendResult(False, 'Device asked for artwork we do not have')
                    # Past the handshake now; the body needs a real budget.
                    sock.settimeout(TCP_ART_TIMEOUT)
                    sock.sendall(image_bytes)
                    final = self._read_ack(sock, buffer)
                    if not final.get('ok', False):
                        error = final.get('error') or 'Device rejected the artwork'
                        logger.warning('Device rejected artwork: %s', error)
                        self._device_art_id = None
                        return SendResult(False, error)
                    logger.debug('Sent %d bytes of artwork', len(image_bytes))

                # Device is now known to hold this artwork (or none at all).
                self._device_art_id = art_id
                return SendResult(True)
        except Exception as exc:
            # Force a full resend once the device is reachable again.
            self._device_art_id = None
            logger.warning('Send to %s:%d failed: %s', self.host, self.port, exc)
            return SendResult(False, describe_socket_error(exc))


def probe(host: str, port: int, timeout: float = TCP_TIMEOUT) -> SendResult:
    """Check the device is listening, without disturbing what it is showing.

    Connects and closes again without sending a header. The device's handler
    returns early on an empty read, so this costs it nothing.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((host, port))
        return SendResult(True)
    except Exception as exc:
        logger.debug('Probe of %s:%d failed: %s', host, port, exc)
        return SendResult(False, describe_socket_error(exc))


def describe_socket_error(exc: Exception) -> str:
    """Plain-English version of the usual connection failures.

    Kept short - this goes in the window footer, where there is no room to
    explain. explain_socket_error() carries the advice.
    """
    if isinstance(exc, socket.timeout):
        return 'Timed out'
    if isinstance(exc, ConnectionRefusedError):
        return 'Connection refused'
    if isinstance(exc, socket.gaierror):
        return 'Unknown address'
    if isinstance(exc, OSError) and exc.errno in (10051, 10065):
        # WSAENETUNREACH / WSAEHOSTUNREACH
        return 'Network unreachable'
    return str(exc) or exc.__class__.__name__


# Longer advice for tooltips and the settings dialog, keyed by the short text.
ERROR_ADVICE = {
    'Timed out': 'The dock did not answer. Check it is awake and on the same network.',
    'Connection refused': 'Something answered, but nothing is listening on that port. '
                          'Check the Now Playing app is running on the dock.',
    'Unknown address': 'That host name could not be resolved. Check the address.',
    'Network unreachable': 'No route to that address. Check the dock is on your network.',
}


def explain_socket_error(short_error: str) -> str:
    """Advice to pair with a short error, falling back to the error itself."""
    return ERROR_ADVICE.get(short_error, short_error)
