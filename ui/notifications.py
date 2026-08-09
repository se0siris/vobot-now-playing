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

import discovery
import settings

from device_link import DeviceLink, SendResult
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

# A dock that is merely switched off should not have us broadcasting every time
# a push fails - that would be every poll.
DISCOVERY_COOLDOWN = 60

# Artwork at least this many pixels is good enough to fill the 320x240 panel, so
# there is nothing to gain by looking for a better version.
#
# Below it, the poll keeps re-reading the thumbnail. Sources sometimes publish a
# small placeholder first and the real cover a moment later, and while a track is
# paused no WinRT events fire at all - so without this the client would hold a
# 60x60 image, stretched over the whole panel, for the life of that track.
GOOD_ART_AREA = 240 * 240

# Pushed when Windows has no media session at all, so the dock can go back to
# its placeholder instead of holding the last track for ever.
IDLE_PAYLOAD = {
    'status': 'IDLE',
    'title': '',
    'artist': '',
    'album': '',
    'art_id': None,
    'width': 0,
    'height': 0,
}


# Transport commands the UI can ask for, mapped to the session method that
# performs them. Which are actually usable is reported per-source in TrackInfo.
TRANSPORT_COMMANDS = {
    'previous': 'try_skip_previous_async',
    'next': 'try_skip_next_async',
    'play_pause': 'try_toggle_play_pause_async',
}


@dataclass(frozen=True)
class TrackInfo:
    """A snapshot of what Windows says is playing."""
    title: str
    artist: str
    album: str
    status: str
    art_id: str | None = None
    thumbnail: bytes | None = None
    # What this source will accept. Sources differ - a browser tab often offers
    # no previous/next at all - so the buttons follow these rather than assuming.
    can_previous: bool = False
    can_next: bool = False
    can_play_pause: bool = False

    @property
    def is_playing(self) -> bool:
        return self.status == 'PLAYING'

    @property
    def status_text(self) -> str:
        return self.status.replace('_', ' ').title()


def _available_controls(playback_info) -> tuple[bool, bool, bool]:
    """Which transport buttons this source will honour.

    Reported by Windows per session rather than assumed: a browser tab commonly
    offers play/pause with no track skipping, and a source that has just started
    may briefly offer nothing at all.
    """
    try:
        controls = playback_info.controls
        if controls is None:
            return False, False, False
        return (
            bool(controls.is_previous_enabled),
            bool(controls.is_next_enabled),
            bool(controls.is_play_pause_toggle_enabled
                 or controls.is_play_enabled
                 or controls.is_pause_enabled),
        )
    except Exception:
        logger.debug('Could not read the available controls', exc_info=True)
        return False, False, False


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
    # A dock was found at a new address; the GUI thread owns saving it.
    signal_device_discovered = pyqtSignal(str, int)

    def __init__(self, parent=None):
        super(NotificationsWrapper, self).__init__(parent)
        self.device = DeviceLink()
        self._artwork = ArtworkPicker()
        self._frames = FrameCache()

        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        # One refresh at a time; see _schedule_refresh().
        self._refresh_task: asyncio.Task | None = None
        self._refresh_wanted = False
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
        self._last_discovery_at: float = 0.0

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

    def send_command(self, command: str):
        """Run a transport control. Safe to call from the GUI thread."""
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._start_command, command)

    def _start_command(self, command: str):
        asyncio.create_task(self._run_command(command))

    async def _run_command(self, command: str):
        method_name = TRANSPORT_COMMANDS.get(command)
        session = self._session
        if session is None or method_name is None:
            logger.debug('Ignoring transport command %r', command)
            return
        try:
            accepted = await getattr(session, method_name)()
            logger.info('Transport command %s: %s', command,
                        'accepted' if accepted else 'refused by the source')
        except Exception:
            logger.exception('Transport command %s failed', command)
            return
        # The source raises its own change event, but not always promptly - and
        # not at all if it refused. Refresh so the UI cannot sit on stale state.
        self._schedule_refresh()

    def _apply_device_address(self, host: str, port: int):
        self.device.set_address(host, port)
        # Forget the dedupe state and the cached device status so the new device
        # gets a full push and the UI hears about it either way.
        self._last_sent_key = None
        self._last_device_ok = None
        self._schedule_refresh()

    def _schedule_refresh(self):
        """Ask for a refresh, collapsing a burst of events into one.

        A single track change raises a handful of WinRT events in the same
        instant, and each used to start its own task - nine concurrent reads of
        the same thumbnail stream for one change. Now a refresh already running
        just sets a flag, and runs exactly once more when it finishes, so the
        last state is never missed but the middle of a burst is not fetched.
        """
        self._refresh_wanted = True
        if self._refresh_task is not None and not self._refresh_task.done():
            return
        self._refresh_task = asyncio.create_task(self._refresh_until_settled())

    async def _refresh_until_settled(self):
        while self._refresh_wanted:
            self._refresh_wanted = False
            await self.get_now_playing()

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
        self._schedule_refresh()

    def _on_session_event(self, sender, args):
        """WinRT calls this on a pool thread - hop back onto our loop."""
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._schedule_refresh)

    # -- Reporting ---------------------------------------------------------

    async def _push(self, payload, frame_bytes):
        """Send one state to the dock, skipping anything it already has.

        Beyond saving the round trip, deduping keeps the device from re-applying
        identical text to a label that is mid-scroll. The heartbeat forces one
        through periodically so a device that restarted picks the display back
        up without waiting for the next song.
        """
        payload_key = tuple(payload.get(key) for key in
                            ('status', 'title', 'artist', 'album', 'art_id'))
        now = time.monotonic()
        if (payload_key == self._last_sent_key
                and now - self._last_sent_at < HEARTBEAT_SECONDS):
            logger.debug('No change since last push; skipping')
            return

        if not self.device.host:
            await self._discover_device('no address is configured')
            if not self.device.host:
                self._report_device(SendResult(False, 'No dock configured'))
                return

        logger.info('Pushing: %s', payload)
        result = self.device.send(payload, frame_bytes)
        if result:
            self._last_sent_key = payload_key
            self._last_sent_at = now
        else:
            # Retry on the next event rather than waiting for a change.
            self._last_sent_key = None
            await self._maybe_rediscover()
        self._report_device(result)

    async def _maybe_rediscover(self):
        """After a failed push, see whether the dock simply moved."""
        if not settings.auto_discover():
            return
        now = time.monotonic()
        if now - self._last_discovery_at < DISCOVERY_COOLDOWN:
            return
        self._last_discovery_at = now
        await self._discover_device('the saved address stopped responding')

    async def _discover_device(self, reason: str) -> bool:
        """Broadcast for a dock and adopt it if it is somewhere new.

        Runs off the loop: the search blocks for over a second.
        """
        logger.info('Searching for a dock - %s', reason)
        loop = asyncio.get_running_loop()
        devices = await loop.run_in_executor(None, discovery.discover)
        if not devices:
            logger.info('No dock answered the discovery probe')
            return False

        device = devices[0]
        if (device.host, device.port) == (self.device.host, self.device.port):
            logger.info('Discovery returned the address already in use')
            return False

        logger.info('Adopting discovered dock at %s:%d', device.host, device.port)
        self.device.set_address(device.host, device.port)
        self._last_sent_key = None
        self._last_device_ok = None
        self.signal_device_discovered.emit(device.host, device.port)
        return True

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
                # Tell the dock too, so it drops the last track's artwork and
                # goes back to its placeholder rather than showing a stale one.
                self._artwork.reset()
                self._frames.clear()
                await self._push(dict(IDLE_PAYLOAD), None)
                return

            media_props = await session.try_get_media_properties_async()
            playback_info = session.get_playback_info()
            status = playback_info.playback_status

            title = media_props.title or ''
            artist = media_props.artist or ''
            album = media_props.album_title or ''
            track_key = (title, artist, album)

            # A poll tick re-reads the metadata cheaply, but the thumbnail is a
            # cross-process stream read - worth skipping once we hold artwork
            # good enough to fill the panel. Until then keep looking, because a
            # better version often turns up shortly after the first.
            holding_good_art = (self._artwork.key == track_key
                                and self._artwork.current
                                and self._artwork.best_area >= GOOD_ART_AREA)
            if poll and holding_good_art:
                thumb_bytes = self._artwork.current
            else:
                raw_thumb = await get_thumbnail_data(media_props.thumbnail)
                thumb_bytes = self._artwork.best_for(track_key, raw_thumb) if raw_thumb else None

            art_id = art_id_for(thumb_bytes)

            if thumb_bytes:
                frame_bytes, width, height = self._frames.frame_for(
                    thumb_bytes, art_id, self.device.frame_size)
                if frame_bytes is None:
                    # Undecodable. Announcing an art_id we cannot then supply
                    # would only earn a geometry error from the device.
                    art_id = None
                    self._frames.clear()
            else:
                logger.debug('No thumbnail available.')
                frame_bytes, width, height = None, 0, 0
                self._frames.clear()

            can_previous, can_next, can_play_pause = _available_controls(playback_info)
            self.signal_track.emit(TrackInfo(
                title=title,
                artist=artist,
                album=album,
                status=status.name,
                art_id=art_id,
                thumbnail=thumb_bytes,
                can_previous=can_previous,
                can_next=can_next,
                can_play_pause=can_play_pause,
            ))

            await self._push({
                'status': status.name,
                'title': title,
                'artist': artist,
                'album': album,
                'art_id': art_id,
                'width': width,
                'height': height,
            }, frame_bytes)

        except Exception:
            logger.exception('Failed to read or push the current media session')
