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
PROTOCOL_VERSION = 2

DEFAULT_PORT = 32150

# Anything longer than this is not a header we sent for.
MAX_HEADER = 1024
# Body copy granularity. Small transient allocations the GC handles trivially,
# versus the ~150KB temporaries that repeated `buf += chunk` would produce.
CHUNK = 2048
# Reused source for blanking a buffer without allocating a full-size zero block.
_ZEROS = bytes(CHUNK)



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
# Global state
# ---------------------------------------------------------------------------
app_mgr = None

scr = None            # root screen
canvas = None         # artwork canvas, created once and rebuffered per frame
info_bar = None       # translucent strip carrying title/artist
title_label = None
artist_label = None
state_label = None    # play/pause glyph badge
status_label = None   # centred idle/error text

canvas_buf = None     # front buffer - what LVGL is displaying
back_buf = None       # back buffer - what the socket streams into

server = None
server_task = None
server_running = False
client_task = None
busy = False          # one client exchange at a time

art_id = None         # id of the artwork currently in canvas_buf
have_art = False

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
def _blank(buf):
    """Zero a buffer in CHUNK-sized memcpys. 0x0000 is black in RGB565."""
    view = memoryview(buf)
    total = len(buf)
    offset = 0
    while offset < total:
        end = offset + CHUNK
        if end > total:
            end = total
        view[offset:end] = _ZEROS[:end - offset]
        offset = end


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

    Copies through a memoryview so nothing larger than CHUNK is ever allocated,
    which is what keeps a track change off the big-object heap entirely.
    """
    view = memoryview(buf)
    read = 0
    while read < total:
        want = total - read
        if want > CHUNK:
            want = CHUNK
        chunk = await reader.read(want)
        if not chunk:
            break
        end = read + len(chunk)
        view[read:end] = chunk
        read = end
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
    """Centred overlay text. Only shown while there is no artwork behind it."""
    if not status_label:
        return
    try:
        _set_label(status_label, 'status', text)
        _set_visible(status_label, 'status_shown', not have_art)
    except Exception as exc:
        logger.warning('Status update failed: %s', exc)


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
        if have_art:
            _set_visible(status_label, 'status_shown', False)
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
        -> {"title","artist","album","status","art_id","image_len","width","height"}\\n
        <- {"ok","proto","send_art","w","h"}\\n
        -> <image_len raw RGB565 bytes>        (only when send_art is true)
        <- {"ok"}\\n
    """
    global busy, client_task, art_id, have_art

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
            geometry_error = 'expected {}x{} ({} bytes)'.format(
                FRAME_W, FRAME_H, FRAME_SIZE)
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
                await _reply(writer, {'ok': False, 'error': 'short read',
                                      'received': received})
                return
            _swap_frame()
            art_id = incoming_art
            have_art = True
            changed_frame = True
        elif clear_art:
            if back_buf is None:
                return
            _blank(back_buf)
            _swap_frame()
            art_id = None
            have_art = False
            changed_frame = True

        _apply_metadata(meta)
        if geometry_error:
            _set_status(geometry_error)
        elif not have_art:
            _set_status('No artwork')

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

        _set_status('Waiting for client\n{}:{}'.format(_local_ip(), port))
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


async def on_start():
    global scr, canvas, canvas_buf, back_buf
    global info_bar, title_label, artist_label, state_label, status_label
    global server_task, art_id, have_art

    logger.info('on start')
    art_id = None
    have_art = False
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
    _blank(canvas_buf)
    _blank(back_buf)

    canvas = lv.canvas(scr)
    canvas.set_buffer(canvas_buf, FRAME_W, FRAME_H, lv.COLOR_FORMAT.RGB565)
    canvas.align(lv.ALIGN.CENTER, 0, 0)

    # Everything below is created after the canvas so it draws on top of it.
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


async def on_pause():
    """Backgrounded. The server deliberately stays up so the Windows client
    keeps succeeding and the display is already current on the way back in."""
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
    global art_id, have_art, session

    logger.info('on stop')

    # Retire any exchange still in flight. It holds its own buffer reference and
    # the session check drops its writes, so teardown never has to wait on it.
    session += 1

    art_id = None
    have_art = False
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
    canvas_buf = None
    back_buf = None

    await stop_server()
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
                'tip': 'Must match TCP_PORT in the Windows client. '
                       'Restart the app after changing.',
                'attributes': {
                    'placeholder': str(DEFAULT_PORT),
                    'maxLength': 5,
                },
            },
        ],
    }
