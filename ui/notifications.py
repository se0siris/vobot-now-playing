"""Watches the Windows media session and pushes what it finds to the Mini Dock.

Runs its own asyncio loop on a worker QThread. Everything it learns is handed to
the UI as signals; the UI never touches WinRT or the socket itself.
"""
import asyncio
import logging
import time

from dataclasses import dataclass

import winrt.windows.media.control as media_control
import winrt.windows.storage.streams as streams

from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot

from device_link import DeviceLink, probe
from media_image import ArtworkPicker, FrameCache, art_id_for

logger = logging.getLogger(__name__)

# Windows raises a playback event for seeks and position updates too, so the
# same track state gets reported several times a second. Identical pushes are
# skipped, but one is forced through this often so a device that restarted
# mid-track picks the display back up without waiting for the next song.
HEARTBEAT_SECONDS = 30

# Events can be missed - a source that dies without a final notification leaves
# stale text on the dock, and a device that reboots while playback is paused
# would otherwise wait for the next track. Re-checking on a timer covers both.
POLL_SECONDS = 10


@dataclass(frozen=True)
class TrackInfo:
    """A snapshot of what Windows says is playing."""
    title: str
    artist: str
    album: str
    status: str
    art_id: str | None = None
    thumbnail: bytes | None = None

    @property
    def is_playing(self) -> bool:
        return self.status == 'PLAYING'

    @property
    def status_text(self) -> str:
        return self.status.replace('_', ' ').title()


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


class NotificationsWrapper(QObject):
    """Media session monitor. Lives on a worker thread, owns the DeviceLink."""

    # Emitted with a TrackInfo, or None when nothing is playing.
    signal_track = pyqtSignal(object)
    # Emitted after every push attempt: (reachable, message).
    signal_device_state = pyqtSignal(bool, str)

    def __init__(self, parent=None):
        super(NotificationsWrapper, self).__init__(parent)
        self.device = DeviceLink()
        self._artwork = ArtworkPicker()
        self._frames = FrameCache()

        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        # An address change that arrived before the loop was up.
        self._pending_address: tuple[str, int] | None = None

        # The manager has to outlive main(): dropping it unsubscribes us from
        # the session-changed event.
        self._manager = None
        self._session = None
        self._session_tokens: tuple | None = None

        self._last_sent_key: tuple | None = None
        self._last_sent_at: float = 0.0
        self._last_device_ok: bool | None = None

    # -- Lifecycle ---------------------------------------------------------

    @pyqtSlot()
    def start(self):
        logger.debug('NotificationsWrapper starting...')
        try:
            asyncio.run(self.main())
        except Exception:
            logger.exception('Media monitor stopped unexpectedly')
        logger.debug('NotificationsWrapper stopped.')

    # start() blocks this thread inside asyncio.run, so its Qt event loop never
    # gets to run. The two methods below are therefore called directly from the
    # GUI thread rather than through queued signals, and hand the work over via
    # call_soon_threadsafe - the only cross-thread entry point asyncio offers.

    def stop(self):
        """Ask the worker loop to finish. Safe to call from the GUI thread."""
        loop, stop_event = self._loop, self._stop_event
        if loop is None or stop_event is None:
            return
        loop.call_soon_threadsafe(stop_event.set)

    def set_device_address(self, host: str, port: int):
        """Retarget the device. Safe to call from the GUI thread."""
        loop = self._loop
        if loop is None:
            # Settings changed before the monitor finished starting; main()
            # picks this up rather than losing it.
            self._pending_address = (host, port)
            return
        loop.call_soon_threadsafe(self._apply_device_address, host, port)

    def _apply_device_address(self, host: str, port: int):
        self.device.set_address(host, port)
        # Forget the dedupe state and the cached device status so the new device
        # gets a full push and the UI hears about it either way.
        self._last_sent_key = None
        self._last_device_ok = None
        self._schedule_refresh()

    def _schedule_refresh(self):
        asyncio.create_task(self.get_now_playing())

    async def main(self):
        logger.debug('NotificationsWrapper started.')
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()

        if self._pending_address is not None:
            host, port = self._pending_address
            self._pending_address = None
            self.device.set_address(host, port)

        self._manager = await media_control.GlobalSystemMediaTransportControlsSessionManager.request_async()
        self._manager.add_current_session_changed(self._on_current_session_changed)

        self._bind_session(self._manager.get_current_session())
        await self.get_now_playing()
        logger.info('Listening for media session changes.')

        # Wake on stop, otherwise re-check on the poll interval.
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=POLL_SECONDS)
            except asyncio.TimeoutError:
                await self.get_now_playing(poll=True)

        self._bind_session(None)
        logger.info('Stopped listening.')

    # -- Session plumbing --------------------------------------------------

    def _bind_session(self, session):
        """Attach change handlers to the current session, detaching the old one."""
        if self._session is not None and self._session_tokens is not None:
            properties_token, playback_token = self._session_tokens
            try:
                self._session.remove_media_properties_changed(properties_token)
                self._session.remove_playback_info_changed(playback_token)
            except Exception:
                # The old session may already be gone; nothing to unhook.
                logger.debug('Could not detach from the previous session', exc_info=True)

        self._session = session
        self._session_tokens = None

        if session is None:
            logger.info('No active media session.')
            return

        self._session_tokens = (
            session.add_media_properties_changed(self._on_session_event),
            session.add_playback_info_changed(self._on_session_event),
        )
        logger.debug('Bound to media session %s', session.source_app_user_model_id)

    def _on_current_session_changed(self, sender, args):
        """The user switched player, or the last one closed."""
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._handle_session_change)

    def _handle_session_change(self):
        if self._manager is None:
            return
        self._bind_session(self._manager.get_current_session())
        # A new source means the old artwork and dedupe state are meaningless.
        self._last_sent_key = None
        asyncio.create_task(self.get_now_playing())

    def _on_session_event(self, sender, args):
        """WinRT calls this on a pool thread - hop back onto our loop."""
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._schedule_refresh)

    # -- Reporting ---------------------------------------------------------

    async def _check_device(self):
        """Reachability check, run off the loop so a timeout cannot stall it."""
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, probe, self.device.host, self.device.port)
        self._report_device(result)

    def _report_device(self, result):
        """Emit device state, but only when it actually changes."""
        ok = bool(result)
        message = '' if ok else (result.error or 'Not connected')
        if ok == self._last_device_ok:
            return
        self._last_device_ok = ok
        self.signal_device_state.emit(ok, message)

    async def get_now_playing(self, poll: bool = False):
        """Read the session and push it to the device if anything changed."""
        try:
            session = self._session
            if session is None and self._manager is not None:
                session = self._manager.get_current_session()
                if session is not None:
                    self._bind_session(session)

            if session is None:
                self.signal_track.emit(None)
                # Nothing to push, so the only way to keep the connection
                # indicator honest is to ask the device directly.
                await self._check_device()
                return

            media_props = await session.try_get_media_properties_async()
            playback_info = session.get_playback_info()
            status = playback_info.playback_status

            title = media_props.title or ''
            artist = media_props.artist or ''
            album = media_props.album_title or ''
            track_key = (title, artist, album)

            # A poll tick re-reads the metadata cheaply, but re-reading the
            # thumbnail stream every 10 seconds is pure waste when the track has
            # not moved on.
            if poll and self._artwork.key == track_key and self._artwork.current:
                thumb_bytes = self._artwork.current
            else:
                raw_thumb = await get_thumbnail_data(media_props.thumbnail)
                thumb_bytes = self._artwork.best_for(track_key, raw_thumb) if raw_thumb else None

            art_id = art_id_for(thumb_bytes)

            if thumb_bytes:
                frame_bytes, width, height = self._frames.frame_for(
                    thumb_bytes, art_id, self.device.frame_size)
            else:
                logger.debug('No thumbnail available.')
                frame_bytes, width, height = None, 0, 0
                self._frames.clear()

            self.signal_track.emit(TrackInfo(
                title=title,
                artist=artist,
                album=album,
                status=status.name,
                art_id=art_id,
                thumbnail=thumb_bytes,
            ))

            now_playing_data = {
                'status': status.name,
                'title': title,
                'artist': artist,
                'album': album,
                'art_id': art_id,
                'width': width,
                'height': height,
            }
            # Skip pushes that carry nothing new. Beyond saving the round trip,
            # it keeps the device from re-applying identical text to a label
            # that is mid-scroll.
            payload_key = (status.name, title, artist, album, art_id)
            now = time.monotonic()
            if (payload_key == self._last_sent_key
                    and now - self._last_sent_at < HEARTBEAT_SECONDS):
                logger.debug('No change since last push; skipping')
                return

            logger.info('Now playing: %s', now_playing_data)
            result = self.device.send(now_playing_data, frame_bytes)
            if result:
                self._last_sent_key = payload_key
                self._last_sent_at = now
            else:
                # Retry on the next event rather than waiting for a change.
                self._last_sent_key = None
            self._report_device(result)

        except Exception:
            logger.exception('Failed to read or push the current media session')
