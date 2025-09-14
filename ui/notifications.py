import json
import logging
import asyncio

from io import BytesIO

import winrt.windows.media.control as media_control
import winrt.windows.storage.streams as streams
import socket

from PIL import Image
from PIL.Image import Resampling

from PyQt5.QtCore import QObject, pyqtSignal

from constants import TCP_IP, TCP_PORT

logger = logging.getLogger(__name__)


def to_rgb565_bytes(image: Image.Image) -> bytes:
    # Ensure image is in RGB mode
    if image.mode != 'RGB':
        image = image.convert('RGB')
    pixels = image.getdata()

    rgb565_data = bytearray()
    for r, g, b in pixels:
        # Convert to 5-6-5 format.
        r_5 = (r >> 3) & 0x1F    # Red (5 bits)
        g_6 = (g >> 2) & 0x3F    # Green (6 bits)
        b_5 = (b >> 3) & 0x1F    # Blue (5 bits)

        # Pack into 16-bit value (little-endian for ESP32).
        rgb16 = (r_5 << 11) | (g_6 << 5) | b_5
        rgb565_data.append(rgb16 & 0xFF)         # Low byte.
        rgb565_data.append((rgb16 >> 8) & 0xFF)  # High byte.

    return bytes(rgb565_data)


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


def resize_thumbnail(thumbnail_bytes, size=(320, 240)):
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
    logger.debug('Resized thumbnail to %dx%d, {%d} bytes (RGB565)', width, height, len(thumb_bytes))
    return thumb_bytes, width, height


def send_tcp_message(meta: dict, image_bytes: bytes = None):
    message = json.dumps(meta).encode('utf-8') + b'\n'  # newline-terminated JSON header
    byte_length = len(image_bytes) if image_bytes else 0
    logger.debug('Header length: %d bytes, image length: %d bytes', len(message), byte_length)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        sock.connect((TCP_IP, TCP_PORT))
        sock.sendall(message)
        logger.debug('Header sent over TCP.')
        if image_bytes:
            sock.sendall(image_bytes)
        sock.close()
        logger.debug('Image sent over TCP.')


class NotificationsWrapper(QObject):

    # Signals.
    signal_thumb_bytes = pyqtSignal(bytes)

    def __init__(self, parent=None):
        super(NotificationsWrapper, self).__init__(parent)

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
            if thumb_bytes:
                self.signal_thumb_bytes.emit(thumb_bytes)
                resized_thumb_bytes, width, height = resize_thumbnail(thumb_bytes)
                byte_length = len(resized_thumb_bytes)
            else:
                logger.debug('No thumbnail available.')
                resized_thumb_bytes, width, height = None, 0, 0
                byte_length = 0

            logger.info('--- Now Playing ---')

            # Send header (JSON) and image bytes over TCP
            now_playing_data = {
                'status': status.name,
                'title': media_props.title,
                'artist': media_props.artist,
                'album': media_props.album_title,
                'image_len': byte_length,
                'width': width,
                'height': height
            }
            logger.info(now_playing_data)
            send_tcp_message(now_playing_data, resized_thumb_bytes)

        except Exception as e:
            logger.info(f'Error: {e}')
