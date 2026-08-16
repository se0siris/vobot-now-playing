# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Gary Hughes
#
# MIT, not the GPLv3 covering the Windows client at the root of this repository.
# See esp32/LICENSE.
"""Windows Now Playing - Mini Dock app.

Listens on TCP for a push from the companion Windows client and displays the
current track's artwork, title, artist and playback state.

Memory design (the reason this file looks the way it does):
  * The 320x240 RGB565 frame is 153,600 bytes. It is received into one of two
    preallocated buffers and shown with lv.canvas.set_buffer(), so a track
    change costs no large allocation and never resizes the heap.
  * LVGL keeps the raw pointer handed to set_buffer(). Both buffers are module
    globals for exactly that reason - if they were locals, the GC could free
    pixels LVGL is still drawing from.
  * The two buffers swap: the network fills the back buffer while LVGL displays
    the front one, so a partially received frame is never on screen.
  * The socket reads straight into a slice of the back buffer, so receiving a
    frame allocates nothing of consequence. Reading into fresh bytes objects and
    copying instead produced ~150KB of garbage per frame, and gc.collect() costs
    200-280ms here - see _read_frame().

Frame transfer, measured on firmware v1.2.6 over WiFi at -50dBm. Recorded so the
obvious next lever is not pulled for nothing:

  * Reading into fresh bytes objects and copying them in: 2000-5000ms per frame.
  * Reading into a slice of the destination via readinto(): ~430ms, 350 KB/s.
    Same number of socket reads - what changed is the garbage, and so the odds of
    a collection landing mid-transfer.
  * 27 reads per frame, and that is the TCP receive window rather than anything
    this app chose: the boot log reports `tcp rx win: 5760` (4 x 1440 MSS), and
    153600/5760 is 26.7. READ_CHUNK is already larger than the window can hand
    over, so raising it does nothing. What is left is ~27 window round trips at
    ~15ms, and shrinking that needs a bigger window - an lwIP build setting, not
    an app one.

Scrolling titles step visibly rather than gliding. This was investigated at
length on firmware VOBOT v1.2.6 and is a display limit, not something this app
can fix - recorded here so it is not chased again:

  * LVGL's tick is fine: it advances every 33ms (30Hz).
  * Every refresh costs ~33ms, which is a full-screen blit at 60MHz SPI. That
    caps the panel near 30fps flat out and ~15fps while sharing the CPU.
  * The firmware idles its refresh at ~5/s and only speeds up while animating
    something itself - a system toast sliding in made this app's scroll smooth
    for exactly as long as the toast was on screen.
  * Driving lv.timer_handler() + lv.anim_refr_now() + lv.refr_now() from an
    asyncio task raised that from ~5/s to ~15/s. It was still visibly stepping,
    and cost ~50% CPU permanently, so it was removed. See git history.
  * Scroll pace is not adjustable on this build: labels have no
    set_style_anim_speed, lv_display_t has no set_refresh_period, and
    lv.anim_speed() raises. set_style_anim_duration applies but a fixed
    duration makes long titles scroll faster than short ones, which is worse
    than the theme default.
"""

import gc
import json
import logging
import time

import lvgl as lv
import net

try:
    import asyncio
except ImportError:
    import uasyncio as asyncio

# ---------------------------------------------------------------------------
# App identity
# ---------------------------------------------------------------------------
NAME = 'Windows Now Playing'
# Passive display, safe to open without interaction.
CAN_BE_AUTO_SWITCHED = False

# The installed folder is not reliably NAME. A Gallery install names it after
# the app, but a manual Thonny copy keeps whatever the source folder was called
# ('win_now_playing' here) - and an ICON path pointing at the wrong folder just
# renders as a blank slot in the menu. __name__ is the package name we were
# imported under, which tracks the real folder in both cases.
_FOLDER = __name__.rsplit('.', 1)[-1]
if not _FOLDER or _FOLDER.startswith('__'):
    _FOLDER = NAME
ICON = f'A:apps/{_FOLDER}/resources/icon.png'

logger = logging.getLogger(NAME.lower().replace(' ', '_'))

# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------
# 2 adds the art_id handshake: the client announces the artwork it intends to
# send, and only transfers the 150KB body when we say we do not already have it.
# 3 adds the IDLE status, pushed when Windows has no media session at all, and
# the UDP discovery service below. 4 adds the `light` header field, driving the
# ambient light from the artwork. Both ends ignore each other's version field, so
# an older client still works - it simply never sends IDLE or `light`, and a
# missing `light` is defined to mean "leave the light alone".
PROTOCOL_VERSION = 4

DEFAULT_PORT = 32150

# Discovery deliberately does NOT follow the configured TCP port: a client that
# already knew the port would not need to discover anything. Fixed here, and the
# reply carries whatever port the TCP server actually ended up on.
DISCOVERY_PORT = 32151
DISCOVERY_MAGIC = b'VOBOT-NOW-PLAYING-DISCOVER'
DISCOVERY_REPLY_MAGIC = 'VOBOT-NOW-PLAYING'
# A probe is a short fixed string; anything longer is not for us.
MAX_PROBE = 256

# Anything longer than this is not a header we sent for.
MAX_HEADER = 1024
# Body copy granularity. Small transient allocations the GC handles trivially,
# versus the ~150KB temporaries that repeated `buf += chunk` would produce.
CHUNK = 2048
# Reused source for blanking a buffer without allocating a full-size zero block.
_ZEROS = bytes(CHUNK)

# Ceiling on one socket read, kept separate from CHUNK because it costs nothing:
# the frame is read into a slice of the destination buffer, so this bounds how much
# a single read may take, not anything allocated. Larger than CHUNK so that when
# lwIP has several segments queued they are taken in one call rather than three.
READ_CHUNK = 8192


def _screen_resolution():
    try:
        import peripherals

        width, height = peripherals.screen.screen_resolution
        if width and height:
            return int(width), int(height)
    except Exception:
        pass
    return 320, 240


FRAME_W, FRAME_H = _screen_resolution()
FRAME_SIZE = FRAME_W * FRAME_H * 2  # RGB565, 2 bytes/pixel


# ---------------------------------------------------------------------------
# Ambient light
# ---------------------------------------------------------------------------
# The dock's RGB strip, driven from the dominant colour of the cover art the
# client is showing. Measured on this hardware: 14 LEDs, acquire() succeeds, and
# set_color([(r, g, b)], True) tiles one colour across the whole strip.
#
# Ownership is the thing to be careful with. acquire() suspends whatever the
# system was doing with the light and release() hands it back, so the app holds
# it only while a client is actually asking for a colour. That is why the header
# key is tri-state rather than a plain colour - see handle_client().
#
# The peripheral is feature-detected rather than assumed, the same way
# _screen_resolution() treats `peripherals`: a firmware without it must not take
# down on_start(), and one that raises must not do so on every push.

light_owned = False  # we hold the peripheral and the system effect is suspended
light_state = None  # last (r, g, b, brightness) applied, so repeats are free
light_available = True  # cleared for good on the first failure

# Distinguishes "the client said nothing about the light" from "the client said
# turn it off". A plain None cannot, and the difference is the whole design.
_NO_LIGHT = object()


def _ambient_light():
    """The ambient light object, or None if this build has no such thing."""
    global light_available

    if not light_available:
        return None
    try:
        import peripherals

        light = getattr(peripherals, 'ambient_light', None)
        if light is None:
            logger.warning('No peripherals.ambient_light on this firmware; the light is disabled')
            light_available = False
        return light
    except Exception as exc:
        logger.warning('peripherals unavailable (%s); the light is disabled', exc)
        light_available = False
        return None


def _apply_light(spec):
    """Set, or release, the ambient light from a client's `light` header field.

    `_NO_LIGHT` leaves it exactly as it is - which is what a client sends while
    it is still hunting for the track's real artwork, and what a pre-v4 client
    sends for everything. None releases it. A (r, g, b, brightness) tuple takes
    ownership and applies it.
    """
    global light_owned, light_state, light_available

    if spec is _NO_LIGHT:
        return
    if spec is None:
        _release_light()
        return
    if not light_available:
        return

    try:
        red, green, blue, level = (int(v) for v in spec)
    except Exception:
        logger.warning('Ignoring malformed light spec: %s', spec)
        return
    state = (red, green, blue, level)
    if light_owned and state == light_state:
        # The client re-sends its full state on every playback event. Rewriting
        # an unchanged colour is pure cost on the strip and in this handler.
        return

    light = _ambient_light()
    if light is None:
        return
    try:
        if not light_owned:
            if not light.acquire():
                logger.warning('Could not acquire the ambient light')
                return
            light_owned = True
            logger.info('Ambient light acquired (%s LEDs)', getattr(light, 'count', '?'))
        light.set_color([(red, green, blue)], True)
        light.brightness(max(0, min(100, level)))
        light_state = state
    except Exception as exc:
        # Once, not once per push - the client pushes several times a second
        # while a track plays and a traceback each time would bury the log.
        logger.warning('Ambient light failed (%s); disabling it', exc)
        light_available = False
        light_state = None


def _release_light():
    """Hand the light back, so whatever the system was doing with it resumes.

    Safe to call when nothing was ever acquired, which is the common case - the
    feature is off by default in the client.
    """
    global light_owned, light_state

    light_state = None
    if not light_owned:
        return
    light_owned = False

    light = _ambient_light()
    if light is None:
        return
    try:
        light.release()
        logger.info('Ambient light released')
    except Exception as exc:
        logger.warning('Ambient light release failed: %s', exc)


def _rgb565(red, green, blue):
    """Pack to the little-endian RGB565 pair the canvas and client both use."""
    value = ((red & 0xF8) << 8) | ((green & 0xFC) << 3) | (blue >> 3)
    return bytes([value & 0xFF, value >> 8])


# Ground colour behind the placeholder artwork, matching the client's dark
# panel so the two halves of the project look related.
IDLE_GROUND = _rgb565(26, 33, 51)
_IDLE_PATTERN = IDLE_GROUND * (CHUNK // 2)

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
app_mgr = None

scr = None  # root screen
canvas = None  # artwork canvas, created once and rebuffered per frame
info_bar = None  # translucent strip carrying title/artist
title_label = None
artist_label = None
state_label = None  # play/pause glyph badge
status_label = None  # centred error text
placeholder = None  # app mark, shown whenever there is no artwork

canvas_buf = None  # front buffer - what LVGL is displaying
back_buf = None  # back buffer - what the socket streams into

server = None
server_task = None
server_running = False
client_task = None
busy = False  # one client exchange at a time

discovery_socket = None
discovery_task = None
discovery_running = False

art_id = None  # id of the artwork currently in canvas_buf
have_art = False
# Whether the canvas already holds the placeholder ground. Repainting it is a
# 150KB fill plus a full-screen invalidate, and the client re-sends its idle
# state on every heartbeat.
ground_is_idle = False

# What is currently on the widgets, so repeat pushes become no-ops. See
# _set_label() for why writing the same value again is not harmless.
_shown = {}

# Bumped on teardown. An exchange that was in flight when the app stopped keeps
# its own reference to the buffer it was filling, so its writes land somewhere
# harmless; the session check stops it publishing them. Nothing has to be joined,
# which is what lets on_stop() finish without awaiting the network.
session = 0


# ---------------------------------------------------------------------------
# Buffer helpers
# ---------------------------------------------------------------------------
def _fill(buf, pattern):
    """Repeat a CHUNK-sized pattern across a buffer, in memcpy-sized steps.

    Never allocates a full-frame temporary, which is the whole point - a
    150KB block would land on the big-object heap.
    """
    view = memoryview(buf)
    total = len(buf)
    offset = 0
    while offset < total:
        end = offset + CHUNK
        if end > total:
            end = total
        view[offset:end] = pattern[: end - offset]
        offset = end


def _blank(buf):
    """Zero a buffer. 0x0000 is black in RGB565."""
    _fill(buf, _ZEROS)


def _swap_frame():
    """Show the back buffer and hand the old front buffer back for reuse."""
    global canvas_buf, back_buf
    if not (canvas and back_buf):
        return
    canvas.set_buffer(back_buf, FRAME_W, FRAME_H, lv.COLOR_FORMAT.RGB565)
    canvas_buf, back_buf = back_buf, canvas_buf
    canvas.invalidate()


async def _read_frame(reader, buf, total):
    """Stream `total` bytes into `buf`. Returns the number of bytes read.

    Reads straight into a slice of the destination through readinto(), so a whole
    frame allocates nothing but the memoryview slices - a few dozen bytes each.
    That matters more than it looks: read() hands back a fresh bytes object per
    call, which over a 150KB frame is 100-odd transient buffers and 150KB of
    garbage, and a collection on this device costs 200-280ms. One landing
    mid-transfer is worth more than the whole rest of the read.

    readinto() is feature-detected rather than assumed - firmware builds differ on
    what asyncio.Stream exposes, and the read() path stays correct if it is absent.
    """
    view = memoryview(buf)
    readinto = getattr(reader, 'readinto', None)
    started = time.ticks_ms()
    read = 0
    reads = 0

    while read < total:
        want = total - read
        if want > READ_CHUNK:
            want = READ_CHUNK
        reads += 1
        if readinto is None:
            chunk = await reader.read(want)
            if not chunk:
                break
            got = len(chunk)
            view[read : read + got] = chunk
        else:
            got = await readinto(view[read : read + want])
            if not got:
                break
        read += got

    elapsed = time.ticks_diff(time.ticks_ms(), started)
    if elapsed > 0:
        logger.info(
            'Frame: %d bytes in %dms (%d KB/s) over %d reads%s',
            read,
            elapsed,
            (read * 1000) // elapsed // 1024,
            reads,
            '' if readinto is not None else ' (no readinto)',
        )
    return read


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------
def _clear_flag(obj, flag):
    """LVGL 9 renamed clear_flag() to remove_flag(); builds differ on which
    they expose, so try both rather than depend on one."""
    fn = getattr(obj, 'remove_flag', None) or getattr(obj, 'clear_flag', None)
    if fn:
        fn(flag)


def _set_visible(obj, key, visible):
    if not obj or _shown.get(key) == visible:
        return
    if visible:
        _clear_flag(obj, lv.obj.FLAG.HIDDEN)
    else:
        obj.add_flag(lv.obj.FLAG.HIDDEN)
    _shown[key] = visible


def _set_label(label, key, text):
    """Write to a label only when the text actually changed.

    lv_label_set_text() restarts a circular scroll animation from the start, and
    the client pushes a metadata update on every playback event - several per
    second while a track plays. Rewriting identical text was what made long
    titles stutter: the scroll never got more than a fraction of the way across
    before being reset.
    """
    if not label or _shown.get(key) == text:
        return
    label.set_text(text)
    _shown[key] = text


def _set_status(text):
    """Centred overlay text, for errors and the pre-network wait.

    Ordinary 'nothing playing' states are carried by the idle view instead, so
    this is only shown when there is something the user has to act on.
    """
    if not status_label:
        return
    try:
        _set_label(status_label, 'status', text)
        _set_visible(status_label, 'status_shown', True)
        _set_visible(placeholder, 'placeholder_shown', False)
    except Exception as exc:
        logger.warning('Status update failed: %s', exc)


def _show_placeholder():
    """Put the app mark on a dark ground where the artwork would be."""
    global art_id, have_art, ground_is_idle
    if back_buf is None:
        return
    # Only actually repaint when something else is on the canvas.
    if have_art or not ground_is_idle:
        _fill(back_buf, _IDLE_PATTERN)
        _swap_frame()
        ground_is_idle = True
    art_id = None
    have_art = False
    _set_visible(placeholder, 'placeholder_shown', True)


def _show_idle(title):
    """The main view, with no track: placeholder art plus where to reach us.

    Deliberately the same layout as playback rather than a bare screen of text,
    so the dock looks like the same app whether or not anything is playing.
    """
    try:
        _set_visible(status_label, 'status_shown', False)
        _show_placeholder()
        _set_label(title_label, 'title', title)
        _set_label(artist_label, 'artist', '{}:{}'.format(_local_ip(), _configured_port()))
        _set_label(state_label, 'state', lv.SYMBOL.STOP)
        _set_visible(info_bar, 'bar_shown', True)
    except Exception as exc:
        logger.warning('Idle view failed: %s', exc)


def _apply_metadata(meta):
    try:
        artist = meta.get('artist') or ''
        album = meta.get('album') or ''
        if artist and album:
            subtitle = '{} - {}'.format(artist, album)
        else:
            subtitle = artist or album or ''

        status = (meta.get('status') or '').lower()
        if status.startswith('play'):
            symbol = lv.SYMBOL.PLAY
        elif status.startswith('paus'):
            symbol = lv.SYMBOL.PAUSE
        else:
            symbol = lv.SYMBOL.STOP

        _set_label(title_label, 'title', meta.get('title') or 'Unknown title')
        _set_label(artist_label, 'artist', subtitle)
        _set_label(state_label, 'state', symbol)

        _set_visible(info_bar, 'bar_shown', True)
        _set_visible(status_label, 'status_shown', False)
        # A track with no artwork gets the app mark rather than a black hole.
        _set_visible(placeholder, 'placeholder_shown', not have_art)
    except Exception as exc:
        logger.warning('Metadata update failed: %s', exc)


def _local_ip():
    try:
        cfg = net.config()
        if isinstance(cfg, dict):
            return cfg.get('IP') or '?'
        return getattr(cfg, 'IP', '?')
    except Exception:
        return '?'


def _device_identity():
    """Best-effort id/model, so a client seeing two docks can tell them apart."""
    identity = {}
    try:
        import device

        identity['device_id'] = str(device.id)
        identity['model'] = str(device.model)
    except Exception:
        pass
    return identity


def _configured_port():
    port = DEFAULT_PORT
    try:
        if app_mgr:
            port = int((app_mgr.config() or {}).get('port', DEFAULT_PORT))
    except Exception:
        port = DEFAULT_PORT
    if not 1 <= port <= 65535:
        port = DEFAULT_PORT
    return port


# ---------------------------------------------------------------------------
# TCP server
# ---------------------------------------------------------------------------
async def _reply(writer, payload):
    try:
        writer.write((json.dumps(payload) + '\n').encode('utf-8'))
        await writer.drain()
        return True
    except Exception as exc:
        logger.warning('Reply failed: %s', exc)
        return False


async def _close(writer):
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass


async def handle_client(reader, writer):
    """One request/response exchange, then the client disconnects.

    Wire format:
        -> {"title","artist","album","status","art_id","image_len","width","height",
            "light"}\\n
        <- {"ok","proto","send_art","w","h"}\\n
        -> <image_len raw RGB565 bytes>        (only when send_art is true)
        <- {"ok"}\\n

    `light` is optional and tri-state, which is what lets one field cover three
    situations that are genuinely different:

        absent            leave the ambient light exactly as it is. A pre-v4
                          client sends this for everything, so upgrading the dock
                          alone never disturbs the light; a v4 client sends it
                          while it is still hunting for the new track's artwork,
                          where the only colour on hand is the last track's.
        null              release the light - nothing is playing, or the user has
                          the feature switched off.
        [r, g, b, level]  own it and show this colour at this brightness.
    """
    global busy, client_task, art_id, have_art, ground_is_idle

    claimed = False
    changed_frame = False
    my_session = session
    try:
        # Always drain the header before any early return. Closing a socket
        # while its receive buffer still holds data is an abortive close, and
        # the peer then loses the reply we just wrote.
        header = await reader.readline()
        if not header:
            return
        if len(header) > MAX_HEADER:
            await _reply(writer, {'ok': False, 'error': 'header too long'})
            return

        if busy:
            # Overlapping pushes would race for back_buf. Refuse rather than
            # corrupt; the client retries on its next media event.
            await _reply(writer, {'ok': False, 'error': 'busy'})
            return

        busy = True
        claimed = True
        # Only used so on_stop can cancel an in-flight transfer; not every
        # MicroPython asyncio build exposes it.
        try:
            client_task = asyncio.current_task()
        except Exception:
            client_task = None

        try:
            meta = json.loads(header.decode('utf-8').strip())
        except Exception as exc:
            logger.warning('Bad header: %s', exc)
            await _reply(writer, {'ok': False, 'error': 'bad header'})
            return

        incoming_art = meta.get('art_id')
        image_len = int(meta.get('image_len') or 0)
        width = int(meta.get('width') or FRAME_W)
        height = int(meta.get('height') or FRAME_H)
        # Proto 3: Windows has no media session at all, as opposed to a track
        # that merely has no artwork.
        idle = (meta.get('status') or '').upper() == 'IDLE'

        send_art = False
        clear_art = False
        geometry_error = None

        if incoming_art is None:
            # Track has no artwork - blank whatever is on screen.
            clear_art = have_art
        elif have_art and incoming_art == art_id:
            # Already displaying this artwork; the body stays on the client.
            pass
        elif image_len != FRAME_SIZE or width != FRAME_W or height != FRAME_H:
            geometry_error = 'expected {}x{} ({} bytes)'.format(FRAME_W, FRAME_H, FRAME_SIZE)
        else:
            send_art = True

        ack = {
            'ok': geometry_error is None,
            'proto': PROTOCOL_VERSION,
            'send_art': send_art,
            'w': FRAME_W,
            'h': FRAME_H,
        }
        if geometry_error:
            ack['error'] = geometry_error
        if not await _reply(writer, ack):
            return

        if send_art:
            # Pin the buffer: if the app stops mid-read we keep filling this one
            # rather than following the global to None and faulting.
            target = back_buf
            if target is None:
                return
            received = await _read_frame(reader, target, FRAME_SIZE)
            if my_session != session:
                return  # app was stopped while we were reading; drop the frame
            if received != FRAME_SIZE:
                # Leave art_id alone so the client resends on the next update.
                _set_status('Short read: {}/{}'.format(received, FRAME_SIZE))
                await _reply(writer, {'ok': False, 'error': 'short read', 'received': received})
                return
            _swap_frame()
            art_id = incoming_art
            have_art = True
            ground_is_idle = False
            changed_frame = True
        elif clear_art:
            if back_buf is None:
                return
            # Back to the placeholder ground rather than black - the same view
            # the app shows before a client ever connects.
            _show_placeholder()
            changed_frame = True

        if idle:
            _show_idle('Nothing playing')
        else:
            _apply_metadata(meta)
        # Deliberately after the frame swap rather than straight off the header:
        # a transfer takes seconds, and changing the light at the top would leave
        # it announcing the next track while the panel still showed the last one.
        _apply_light(meta.get('light', _NO_LIGHT))
        if geometry_error:
            _set_status(geometry_error)

        if send_art:
            await _reply(writer, {'ok': True})

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning('Client handler failed: %s', exc)
        _set_status('Error: {}'.format(exc))
    finally:
        if claimed:
            busy = False
            client_task = None
        await _close(writer)
        # Only after a real frame change. collect() costs ~200-280ms on this
        # device, and the client sends several metadata-only updates per second
        # while a track plays - collecting on those froze the UI mid-scroll.
        if changed_frame:
            gc.collect()


async def run_server():
    global server, server_running

    server_running = True
    port = _configured_port()
    try:
        while not net.connected():
            _set_status('Waiting for network...')
            await asyncio.sleep_ms(1000)

        _show_idle('Waiting for client')
        logger.info('TCP server starting on 0.0.0.0:%d', port)
        server = await asyncio.start_server(handle_client, '0.0.0.0', port)
        await server.wait_closed()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning('Server error: %s', exc)
        _set_status('Server error: {}'.format(exc))
    finally:
        server_running = False
        if server:
            try:
                server.close()
            except Exception:
                pass
        server = None


# ---------------------------------------------------------------------------
# UDP discovery
# ---------------------------------------------------------------------------
async def run_discovery_server():
    """Answer broadcast probes so the client can find this dock by itself.

    MicroPython's asyncio has no datagram transport, so this is a non-blocking
    socket polled from a task. recvfrom() raises EAGAIN when nothing is waiting,
    which is the normal case - hence the bare sleep on OSError rather than
    treating it as a failure.
    """
    global discovery_socket, discovery_running

    import socket

    discovery_running = True
    sock = None
    try:
        while not net.connected():
            await asyncio.sleep_ms(1000)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except Exception:
            pass
        sock.bind(('0.0.0.0', DISCOVERY_PORT))
        sock.setblocking(False)
        discovery_socket = sock
        logger.info('UDP discovery listening on 0.0.0.0:%d', DISCOVERY_PORT)

        while True:
            try:
                data, addr = sock.recvfrom(MAX_PROBE)
            except OSError:
                # Nothing queued (EAGAIN). Poll rate sets discovery latency;
                # the client waits well over a second for replies.
                await asyncio.sleep_ms(150)
                continue

            if not data or DISCOVERY_MAGIC not in data:
                continue

            reply = {
                'magic': DISCOVERY_REPLY_MAGIC,
                'app': NAME,
                'proto': PROTOCOL_VERSION,
                'port': _configured_port(),
                'ip': _local_ip(),
            }
            reply.update(_device_identity())
            try:
                sock.sendto(json.dumps(reply).encode('utf-8'), addr)
                logger.info('Discovery probe from %s answered', addr[0])
            except Exception as exc:
                logger.warning('Discovery reply failed: %s', exc)

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning('Discovery server error: %s', exc)
    finally:
        discovery_running = False
        discovery_socket = None
        if sock:
            try:
                sock.close()
            except Exception:
                pass


async def stop_discovery_server():
    global discovery_task

    if discovery_socket:
        # Closing under the task unblocks it even if cancellation is late.
        try:
            discovery_socket.close()
        except Exception:
            pass
    if discovery_task:
        try:
            discovery_task.cancel()
        except Exception:
            pass
        discovery_task = None

    for _ in range(50):  # up to ~500ms
        if not discovery_running:
            break
        await asyncio.sleep_ms(10)


async def stop_server():
    """Tear down and wait for confirmation the listening socket is released.

    cancel() only *requests* cancellation - returning before the task has run
    its cleanup leaves the port bound and the next on_start() hits EADDRINUSE.
    """
    global server_task, client_task

    if client_task:
        try:
            client_task.cancel()
        except Exception:
            pass
        client_task = None
    if server:
        try:
            server.close()
        except Exception:
            pass
    if server_task:
        try:
            server_task.cancel()
        except Exception:
            pass
        server_task = None

    # Only to free the listening socket before a quick re-entry rebinds it. The
    # UI is already down by now, so this delays nothing the user can see; poll
    # tightly and give up rather than stalling the app transition.
    for _ in range(50):  # up to ~500ms
        if not server_running and not busy:
            break
        await asyncio.sleep_ms(10)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
async def on_boot(apm):
    global app_mgr
    app_mgr = apm


def _make_placeholder(parent):
    """The app mark, standing in for artwork.

    lv.image is LVGL 9 and lv.img is LVGL 8; if neither can show the PNG - no
    decoder in the build, missing file - fall back to the built-in music symbol,
    which is part of the font and cannot fail.
    """
    cls = getattr(lv, 'image', None) or getattr(lv, 'img', None)
    if cls is not None:
        try:
            image = cls(parent)
            image.set_src(ICON)
            image.center()
            return image
        except Exception as exc:
            logger.warning('Placeholder image unavailable (%s); using a symbol', exc)

    try:
        label = lv.label(parent)
        label.set_text(lv.SYMBOL.AUDIO)
        label.set_style_text_color(lv.color_hex(0x8FA6C8), lv.PART.MAIN)
        label.center()
        return label
    except Exception as exc:
        logger.warning('Placeholder unavailable: %s', exc)
        return None


async def on_start():
    global scr, canvas, canvas_buf, back_buf
    global info_bar, title_label, artist_label, state_label, status_label
    global placeholder, server_task, discovery_task, art_id, have_art, ground_is_idle
    global light_owned, light_state

    logger.info('on start')
    art_id = None
    have_art = False
    # on_stop() released the light, so nothing is held and no colour is current.
    # light_available is deliberately not reset: a firmware that has no ambient
    # light will not have grown one since.
    light_owned = False
    light_state = None
    # Both buffers are filled with the idle ground a few lines down.
    ground_is_idle = True
    # Widgets are about to be rebuilt, so nothing is on screen yet.
    _shown.clear()

    scr = lv.obj()
    scr.set_style_bg_color(lv.color_hex(0x000000), lv.PART.MAIN)
    scr.set_style_bg_opa(255, lv.PART.MAIN)
    scr.set_style_pad_all(0, lv.PART.MAIN)
    scr.set_style_border_width(0, lv.PART.MAIN)
    _clear_flag(scr, lv.obj.FLAG.SCROLLABLE)

    # Two full frames so a transfer in progress never touches what is on screen.
    canvas_buf = bytearray(FRAME_SIZE)
    back_buf = bytearray(FRAME_SIZE)
    # Start on the placeholder ground, so the first thing drawn is the idle view
    # rather than a black rectangle.
    _fill(canvas_buf, _IDLE_PATTERN)
    _fill(back_buf, _IDLE_PATTERN)

    canvas = lv.canvas(scr)
    canvas.set_buffer(canvas_buf, FRAME_W, FRAME_H, lv.COLOR_FORMAT.RGB565)
    canvas.align(lv.ALIGN.CENTER, 0, 0)

    # Everything below is created after the canvas so it draws on top of it.
    placeholder = _make_placeholder(scr)

    status_label = lv.label(scr)
    status_label.set_style_text_color(lv.color_hex(0xFFFFFF), lv.PART.MAIN)
    status_label.set_style_text_align(lv.TEXT_ALIGN.CENTER, lv.PART.MAIN)
    status_label.set_width(FRAME_W - 24)
    status_label.center()
    status_label.set_text('Starting...')

    bar_h = 50
    info_bar = lv.obj(scr)
    info_bar.set_size(FRAME_W, bar_h)
    info_bar.align(lv.ALIGN.BOTTOM_MID, 0, 0)
    info_bar.set_style_bg_color(lv.color_hex(0x000000), lv.PART.MAIN)
    info_bar.set_style_bg_opa(170, lv.PART.MAIN)
    info_bar.set_style_border_width(0, lv.PART.MAIN)
    info_bar.set_style_radius(0, lv.PART.MAIN)
    info_bar.set_style_pad_all(6, lv.PART.MAIN)
    _clear_flag(info_bar, lv.obj.FLAG.SCROLLABLE)
    info_bar.add_flag(lv.obj.FLAG.HIDDEN)

    title_label = lv.label(info_bar)
    title_label.set_style_text_color(lv.color_hex(0xFFFFFF), lv.PART.MAIN)
    title_label.set_width(FRAME_W - 24)
    title_label.align(lv.ALIGN.TOP_LEFT, 0, 0)
    title_label.set_text('')

    artist_label = lv.label(info_bar)
    artist_label.set_style_text_color(lv.color_hex(0xB4C4D8), lv.PART.MAIN)
    artist_label.set_width(FRAME_W - 24)
    artist_label.align(lv.ALIGN.TOP_LEFT, 0, 20)
    artist_label.set_text('')

    # Long titles scroll rather than truncate. Guarded because the enum path
    # moved between LVGL 8 and 9 and a mismatch here would abort on_start.
    try:
        title_label.set_long_mode(lv.label.LONG.SCROLL_CIRCULAR)
        artist_label.set_long_mode(lv.label.LONG.SCROLL_CIRCULAR)
    except Exception as exc:
        logger.warning('Scrolling labels unavailable: %s', exc)

    # One font file is one size, so title and artist need separate objects.
    bold = getattr(lv, 'font_ascii_bold_18', None)
    small = getattr(lv, 'font_ascii_14', None)
    if bold:
        title_label.set_style_text_font(bold, lv.PART.MAIN)
    if small:
        artist_label.set_style_text_font(small, lv.PART.MAIN)

    state_label = lv.label(scr)
    state_label.set_style_text_color(lv.color_hex(0xFFFFFF), lv.PART.MAIN)
    state_label.set_style_bg_color(lv.color_hex(0x000000), lv.PART.MAIN)
    state_label.set_style_bg_opa(150, lv.PART.MAIN)
    state_label.set_style_pad_all(5, lv.PART.MAIN)
    state_label.set_style_radius(10, lv.PART.MAIN)
    state_label.align(lv.ALIGN.TOP_RIGHT, -8, 8)
    state_label.set_text(lv.SYMBOL.STOP)

    lv.scr_load(scr)

    server_task = asyncio.create_task(run_server())
    discovery_task = asyncio.create_task(run_discovery_server())


async def on_pause():
    """Backgrounded. The server deliberately stays up so the Windows client
    keeps succeeding and the display is already current on the way back in.

    The ambient light is held for the same reason: it is not part of this app's
    screen, so there is nothing to hand back while another app is merely in
    front. on_stop() is where it is released."""
    logger.info('on pause')


async def on_resume():
    logger.info('on resume')
    # The system may have loaded its own screen while we were paused.
    if scr:
        lv.scr_load(scr)
    # The feed is the app's only page, so ESC should exit the app.
    if app_mgr:
        app_mgr.enter_root_page()


async def on_stop():
    global scr, canvas, canvas_buf, back_buf
    global info_bar, title_label, artist_label, state_label, status_label
    global placeholder, art_id, have_art, ground_is_idle, session

    logger.info('on stop')

    # Retire any exchange still in flight. It holds its own buffer reference and
    # the session check drops its writes, so teardown never has to wait on it.
    session += 1

    # Before the awaits, like the UI teardown below: the system swaps the menu
    # back in as soon as this hook returns, and leaving the light owned would
    # keep whatever the system does with it suppressed for good.
    _release_light()

    art_id = None
    have_art = False
    # The buffers are about to go; nothing holds the idle ground any more.
    ground_is_idle = False
    _shown.clear()

    # UI first, and with no await before it finishes: the system swaps in the
    # app menu once this hook returns, and anything left half-torn-down is a
    # reboot waiting to happen.
    if scr:
        # clean() deletes the children synchronously, so LVGL has released the
        # set_buffer() pointer by the time it returns - no deferred-delete
        # window to sleep through. Do NOT delete scr itself: it is still the
        # loaded screen until the system swaps the menu in, and freeing it here
        # leaves LVGL dereferencing freed memory on its next refresh (-> reboot).
        scr.clean()
    scr = None
    canvas = None
    info_bar = None
    title_label = None
    artist_label = None
    state_label = None
    status_label = None
    placeholder = None
    canvas_buf = None
    back_buf = None

    await stop_server()
    await stop_discovery_server()
    gc.collect()


# ---------------------------------------------------------------------------
# Web settings
# ---------------------------------------------------------------------------
def get_settings_json():
    return {
        'title': 'Windows Now Playing',
        'hint': {
            'url': 'https://github.com/se0siris/vobot-now-playing',
            'label': 'Setup guide',
        },
        'form': [
            {
                'type': 'input',
                'default': str(DEFAULT_PORT),
                'caption': 'Listen port',
                'name': 'port',
                'tip': 'Must match the port in the Windows client, or just use '
                'Discover there - UDP {} always reports the real port. '
                'Restart the app after changing.'.format(DISCOVERY_PORT),
                'attributes': {
                    'placeholder': str(DEFAULT_PORT),
                    'maxLength': 5,
                },
            },
        ],
    }
