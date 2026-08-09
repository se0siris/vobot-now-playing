"""Artwork handling: ranking the thumbnails Windows hands out, and packing the
chosen one into the RGB565 frame the Mini Dock draws.
"""
import hashlib
import logging

from io import BytesIO

from PIL import Image, ImageChops
from PIL.Image import Resampling

from constants import FRAME_SIZE_DEFAULT

logger = logging.getLogger(__name__)


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


def resize_thumbnail(thumbnail_bytes, size=FRAME_SIZE_DEFAULT):
    """Fit artwork to the device frame, letterboxed on black, packed to RGB565.

    Returns (None, 0, 0) when the artwork cannot be decoded. A source is free to
    hand us something Pillow does not understand, and one bad thumbnail must not
    take down the whole update - the track's text is still worth showing.
    """
    if thumbnail_bytes is None:
        return None, 0, 0
    try:
        image = Image.open(BytesIO(thumbnail_bytes))
        image = image.convert('RGB')
    except Exception:
        logger.warning('Could not decode %d bytes of artwork; treating it as none',
                       len(thumbnail_bytes), exc_info=True)
        return None, 0, 0
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


class ArtworkPicker:
    """Keeps the best artwork seen so far for the track currently playing.

    A single track produces several media_properties_changed events, and the
    thumbnail attached to them is not always the album art - sources also
    publish a small placeholder (typically the player's own icon, identical
    across every track). Whichever arrived last used to win, so good art was
    replaced by the placeholder a few seconds in.

    Ranking by pixel area handles either arrival order, and only ever upgrades
    within a track, so it does not churn the device.
    """

    def __init__(self):
        self._track_key: tuple | None = None
        self._best_rank: tuple[int, int] | None = None
        self._best_raw: bytes | None = None

    @property
    def key(self) -> tuple | None:
        """Track the held artwork belongs to."""
        return self._track_key

    @property
    def current(self) -> bytes | None:
        """Best artwork held for that track, if any."""
        return self._best_raw

    @property
    def best_area(self) -> int:
        """Pixel area of the held artwork, 0 when there is none."""
        return self._best_rank[0] if self._best_rank else 0

    def reset(self):
        """Forget the held artwork - nothing is playing at all."""
        self._track_key = None
        self._best_rank = None
        self._best_raw = None

    def best_for(self, track_key, thumbnail_bytes):
        if track_key != self._track_key:
            self._track_key = track_key
            self._best_rank = None
            self._best_raw = None

        rank = thumbnail_rank(thumbnail_bytes)
        if self._best_rank is not None and rank <= self._best_rank:
            # Equal ranks land here too, which is deliberate: re-sending the
            # same artwork would restart the device's scroll animation.
            how = 'no better than' if rank == self._best_rank else 'worse than'
            logger.debug('Incoming artwork %s is %s held %s; keeping it',
                         rank, how, self._best_rank)
            return self._best_raw

        self._best_rank = rank
        self._best_raw = thumbnail_bytes
        return thumbnail_bytes


class FrameCache:
    """Encoded RGB565 frame for the current artwork.

    Re-sending after a device restart, or a heartbeat push, would otherwise
    re-run the resize and pack work for artwork that has not changed.
    """

    def __init__(self):
        self._art_id: str | None = None
        self._frame: bytes | None = None
        self._size: tuple[int, int] = (0, 0)

    def frame_for(self, thumbnail_bytes, art_id, target_size):
        if (art_id == self._art_id
                and self._frame is not None
                and self._size == target_size):
            return self._frame, target_size[0], target_size[1]

        frame, width, height = resize_thumbnail(thumbnail_bytes, target_size)
        self._art_id = art_id
        self._frame = frame
        self._size = (width, height)
        return frame, width, height

    def clear(self):
        self._art_id = None
        self._frame = None
        self._size = (0, 0)
