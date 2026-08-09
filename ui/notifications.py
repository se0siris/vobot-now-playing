import hashlib
import json
import logging
import time
import asyncio

from io import BytesIO

import winrt.windows.media.control as media_control
import winrt.windows.storage.streams as streams
import socket

from PIL import Image, ImageChops
from PIL.Image import Resampling

from PyQt5.QtCore import QObject, pyqtSignal

from constants import (
    FRAME_SIZE_DEFAULT,
    PROTOCOL_VERSION,
    TCP_IP,
    TCP_PORT,
    TCP_TIMEOUT,
)

logger = logging.getLogger(__name__)

# Windows raises a playback event for seeks and position updates too, so the
# same track state gets reported several times a second. Identical pushes are
# skipped, but one is forced through this often so a device that restarted
# mid-track picks the display back up without waiting for the next song.
HEARTBEAT_SECONDS = 30


def to_rgb565_bytes(image: Image.Image) -> bytes:
    """Pack an image to little-endian RGB565.

    Done with per-band lookup tables rather than a per-pixel Python loop: the
    loop ran 76,800 iterations per frame, this is a handful of C-speed calls.

    The two halves of each 16-bit pixel occupy disjoint bit fields, so
    ImageChops.add doubles as a bitwise OR, and merging as 'LA' interleaves
    low/high bytes in one pass.
    """
    if image.mode != 'RGB':
        image = image.convert('RGB')

    red, green, blue = image.split()
    high = ImageChops.add(
        red.point(lambda v: v & 0xF8),
        green.point(lambda v: v >> 5)
    )
    low = ImageChops.add(
        green.point(lambda v: (v & 0x1C) << 3),
        blue.point(lambda v: v >> 3)
    )
    return Image.merge('LA', (low, high)).tobytes()


async def get_thumbnail_data(thumbnail):
    if thumbnail is None:
        return None

    stream_op = thumbnail.open_read_async()
    stream = await stream_op
    input_stream = stream.get_input_stream_at(0)

    # Allocate a buffer.
    logger.debug('Reading into buffer of size: %d bytes', stream.size)
    buffer = streams.Buffer(stream.size)
    read_op = input_stream.read_async(buffer, buffer.capacity, streams.InputStreamOptions.NONE)
    read_buffer = await read_op

    # Read bytes from IBuffer using DataReader.
    data_reader = streams.DataReader.from_buffer(read_buffer)

    byte_array = bytearray(read_buffer.length)
    data_reader.read_bytes(byte_array)
    bytes_data = bytes(byte_array)

    data_reader.close()
    input_stream.close()
    stream.close()

    return bytes_data


def resize_thumbnail(thumbnail_bytes, size=FRAME_SIZE_DEFAULT):
    if thumbnail_bytes is None:
        return None, 0, 0
    image = Image.open(BytesIO(thumbnail_bytes))
    image = image.convert('RGB')
    image.thumbnail(size, Resampling.BICUBIC)
    width, height = image.size

    # Add padding on a black background if needed
    if width < size[0] or height < size[1]:
        new_image = Image.new('RGB', size, (0, 0, 0))
        new_image.paste(image, ((size[0] - width) // 2, (size[1] - height) // 2))
        image = new_image

        width = size[0]
        height = size[1]

    thumb_bytes = to_rgb565_bytes(image)
    logger.debug('Resized thumbnail to %dx%d, %d bytes (RGB565)', width, height, len(thumb_bytes))
    return thumb_bytes, width, height


def art_id_for(thumbnail_bytes) -> str | None:
    """Stable id for a piece of artwork, derived from the raw thumbnail."""
    if not thumbnail_bytes:
        return None
    return hashlib.sha1(thumbnail_bytes).hexdigest()[:16]


def thumbnail_rank(thumbnail_bytes) -> tuple[int, int]:
    """How good a thumbnail is: (pixel area, byte length), bigger is better.

    PIL only parses the header here, so this does not decode the image.
    """
    if not thumbnail_bytes:
        return 0, 0
    try:
        with Image.open(BytesIO(thumbnail_bytes)) as image:
            width, height = image.size
    except Exception:
        width = height = 0
    return width * height, len(thumbnail_bytes)


class DeviceLink:
    """Talks the Now Playing wire protocol to the Mini Dock.

    Holds the last artwork the device acknowledged so unchanged album art is not
    re-sent. That matters because playback_info_changed fires on every play,
    pause and seek - previously each one pushed a fresh 150KB frame.
    """

    def __init__(self, host: str = TCP_IP, port: int = TCP_PORT):
        self.host = host
        self.port = port
        self.frame_size = FRAME_SIZE_DEFAULT
        self._device_art_id: str | None = None

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

    def send(self, meta: dict, image_bytes: bytes | None) -> bool:
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
                    logger.warning('Device rejected update: %s', ack.get('error'))
                    self._device_art_id = None
                    return False

                if ack.get('send_art'):
                    if not image_bytes:
                        logger.warning('Device asked for artwork we do not have')
                        self._device_art_id = None
                        return False
                    sock.sendall(image_bytes)
                    final = self._read_ack(sock, buffer)
                    if not final.get('ok', False):
                        logger.warning('Device rejected artwork: %s', final.get('error'))
                        self._device_art_id = None
                        return False
                    logger.debug('Sent %d bytes of artwork', len(image_bytes))

                # Device is now known to hold this artwork (or none at all).
                self._device_art_id = art_id
                return True
        except Exception as exc:
            # Force a full resend once the device is reachable again.
            self._device_art_id = None
            logger.warning('Send to %s:%d failed: %s', self.host, self.port, exc)
            return False


class NotificationsWrapper(QObject):

    # Signals.
    signal_thumb_bytes = pyqtSignal(bytes)

    def __init__(self, parent=None):
        super(NotificationsWrapper, self).__init__(parent)
        self.device = DeviceLink()
        # Encoded frame cache, so re-sending after a device restart does not
        # re-run the resize/pack work.
        self._cached_art_id: str | None = None
        self._cached_frame: bytes | None = None
        self._cached_size: tuple[int, int] = (0, 0)
        # Best artwork seen for the track currently playing. Windows republishes
        # the thumbnail several times per track and the versions are not equally
        # good - see _best_thumbnail().
        self._track_key: tuple | None = None
        self._best_rank: tuple[int, int] | None = None
        self._best_raw: bytes | None = None
        self._last_sent_key: tuple | None = None
        self._last_sent_at: float = 0.0

    def start(self):
        logger.debug('NotificationsWrapper starting...')
        asyncio.run(self.main())

    async def handle_media_properties_changed(self, session, args):
        await self.get_now_playing(session)

    async def handle_playback_info_changed(self, session, args):
        await self.get_now_playing(session)

    async def main(self):
        logger.debug('NotificationsWrapper started.')
        sessions = await media_control.GlobalSystemMediaTransportControlsSessionManager.request_async()
        session = sessions.get_current_session()
        if not session:
            logger.info('No active media session.')
            return

        loop = asyncio.get_running_loop()

        def on_media_properties_changed(sender, args):
            loop.call_soon_threadsafe(asyncio.create_task, self.handle_media_properties_changed(sender, args))

        def on_playback_info_changed(sender, args):
            loop.call_soon_threadsafe(asyncio.create_task, self.handle_playback_info_changed(sender, args))

        session.add_media_properties_changed(on_media_properties_changed)
        session.add_playback_info_changed(on_playback_info_changed)
        await self.get_now_playing(session)
        logger.info('Listening for media property and playback info changes. Press Ctrl+C to exit.')
        try:
            while True:
                await asyncio.sleep(3600)
        except KeyboardInterrupt:
            logger.info('\nStopped listening.')

    def _best_thumbnail(self, track_key, thumb_bytes):
        """Pick the best artwork seen so far for this track.

        A single track produces several media_properties_changed events, and the
        thumbnail attached to them is not always the album art - sources also
        publish a small placeholder (typically the player's own icon, identical
        across every track). Whichever arrived last used to win, so good art was
        replaced by the placeholder a few seconds in.

        Ranking by pixel area handles either arrival order, and only ever
        upgrades within a track, so it does not churn the device.
        """
        if track_key != self._track_key:
            self._track_key = track_key
            self._best_rank = None
            self._best_raw = None

        rank = thumbnail_rank(thumb_bytes)
        if self._best_rank is not None and rank <= self._best_rank:
            logger.debug('Keeping better artwork %s over incoming %s',
                         self._best_rank, rank)
            return self._best_raw

        self._best_rank = rank
        self._best_raw = thumb_bytes
        return thumb_bytes

    def _frame_for(self, thumb_bytes, art_id):
        """Encoded RGB565 frame for this artwork, reusing the cache when possible."""
        target = self.device.frame_size
        if (art_id == self._cached_art_id
                and self._cached_frame is not None
                and self._cached_size == target):
            return self._cached_frame, target[0], target[1]

        frame, width, height = resize_thumbnail(thumb_bytes, target)
        self._cached_art_id = art_id
        self._cached_frame = frame
        self._cached_size = (width, height)
        return frame, width, height

    async def get_now_playing(self, session=None):
        try:
            if session is None:
                sessions = await media_control.GlobalSystemMediaTransportControlsSessionManager.request_async()
                session = sessions.get_current_session()

            if not session:
                logger.info('No active media session.')
                return

            media_props = await session.try_get_media_properties_async()
            playback_info = session.get_playback_info()
            status = playback_info.playback_status

            thumb_bytes = await get_thumbnail_data(media_props.thumbnail)

            track_key = (media_props.title, media_props.artist,
                         media_props.album_title)
            if thumb_bytes:
                thumb_bytes = self._best_thumbnail(track_key, thumb_bytes)

            art_id = art_id_for(thumb_bytes)

            if thumb_bytes:
                self.signal_thumb_bytes.emit(thumb_bytes)
                frame_bytes, width, height = self._frame_for(thumb_bytes, art_id)
            else:
                logger.debug('No thumbnail available.')
                frame_bytes, width, height = None, 0, 0
                self._cached_art_id = None
                self._cached_frame = None

            logger.info('--- Now Playing ---')

            now_playing_data = {
                'status': status.name,
                'title': media_props.title,
                'artist': media_props.artist,
                'album': media_props.album_title,
                'art_id': art_id,
                'width': width,
                'height': height,
            }
            # Skip pushes that carry nothing new. Beyond saving the round trip,
            # it keeps the device from re-applying identical text to a label
            # that is mid-scroll.
            payload_key = (status.name, media_props.title, media_props.artist,
                           media_props.album_title, art_id)
            now = time.monotonic()
            if (payload_key == self._last_sent_key
                    and now - self._last_sent_at < HEARTBEAT_SECONDS):
                logger.debug('No change since last push; skipping')
                return

            logger.info(now_playing_data)
            if self.device.send(now_playing_data, frame_bytes):
                self._last_sent_key = payload_key
                self._last_sent_at = now
            else:
                # Retry on the next event rather than waiting for a change.
                self._last_sent_key = None

        except Exception as e:
            logger.info(f'Error: {e}')
