import logging
import lvgl as lv
import uasyncio as asyncio
import json

# App Name
NAME = 'Windows Now Playing'

logger = logging.getLogger(
    NAME.lower().replace(' ', '_')
)

# LVGL widgets
scr = None
label = None
img_widget = None

counter = 0
tcp_server_task = None

async def handle_client(reader, writer):
    global label, img_widget, scr

    if label:
        label.set_text('TCP Client Connected')
    try:
        # Read header line (JSON + \n).
        header = await reader.readline()

        logger.info('Header: %s', header)
        try:
            meta = json.loads(header.decode('utf-8').strip())
        except Exception as e:
            logger.warning('Failed to parse header: %s', str(e))
            return

        image_len = meta.get('image_len', 0)
        image_bytes = b''

        while len(image_bytes) < image_len:
            chunk = await reader.read(image_len - len(image_bytes))
            if not chunk:
                break
            image_bytes += chunk
        logger.info('Received meta: %s, image bytes: %d', meta, len(image_bytes))

        # Show metadata on label.
        if label:
            try:
                label.set_text(f"{meta.get('title','')}\n{meta.get('artist','')}")
            except Exception as e:
                label.set_text(str(meta))

        # Show image if present.
        if image_bytes:
            # Clear previous image and invalidate LVGL image cache.
            if img_widget:
                img_widget.set_src(None)
                img_widget.delete()
                img_widget = None

            try:
                img_dsc = lv.image_dsc_t({
                    'data_size': len(image_bytes),
                    'data': image_bytes,
                    'header': lv.image_header_t({
                        'w': meta.get('width', 320),
                        'h': meta.get('height', 240),
                        'cf': lv.COLOR_FORMAT.RGB565,
                    })
                })
                img_widget = lv.img(scr)
                img_widget.set_src(img_dsc)
                img_widget.set_size(meta.get('width', 320), meta.get('height', 240))
                img_widget.align(lv.ALIGN.CENTER, 0, 0)
            except Exception as e:
                logger.warning('Failed to display RGB565 image: %s', str(e))
                if label:
                    label.set_text(f'Image error: {e}')
    except Exception as e:
        logger.warning('Error in TCP handler: %s', str(e))
        if label:
            label.set_text(str(e))


async def tcp_server():
    global label
    logger.info('TCP server starting on 0.0.0.0:32150')
    server = await asyncio.start_server(handle_client, '0.0.0.0', 32150)
    if label:
        label.set_text('TCP Server Started')
    async with server:
        while True:
            await asyncio.sleep(100)


async def on_stop():
    # User triggered to leave this App. This App is no longer visible. all function should be deactivated
    # This App becomes STOPPED state
    global scr, tcp_server_task, img_widget
    logger.info('on stop')
    if scr:
        scr.clean()
        del scr
        scr = None
    if img_widget:
        img_widget.delete()
        img_widget = None
    # Stop UDP server
    if tcp_server_task:
        tcp_server_task.cancel()
        tcp_server_task = None

async def on_start():
    # User triggered to enter this App for the first time, or from STOPPED state, all function should be initialed
    # Then, this App becomes STARTED state
    global scr, label, img_widget, tcp_server_task
    logger.info('on start')

    # Create the LVGL widgets first
    scr = lv.obj()
    label = lv.label(scr)
    label.center()
    label.set_text('Starting...')
    img_widget = None
    lv.scr_load(scr)

    # Start TCP server after LVGL is ready
    tcp_server_task = asyncio.create_task(tcp_server())
