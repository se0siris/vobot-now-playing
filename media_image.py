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


# How many distinct images to remember per track. Sources alternate between a
# cover and an icon or two, so this only needs to be big enough to cover that -
# it exists to bound the set, not to be reached.
SEEN_LIMIT = 8


class ArtworkPicker:
    """Keeps the best artwork seen so far for the track currently playing.

    A single track produces several media_properties_changed events, and the
    thumbnail attached to them is not always the album art - sources also
    publish a small placeholder (typically the player's own icon, identical
    across every track). Whichever arrived last used to win, so good art was
    replaced by the placeholder a few seconds in.

    Ranking by pixel area handles either arrival order, and only ever upgrades
    within a track, so it does not churn the device.

    Ranking alone is not enough at a track change, though: Windows does not
    swap the metadata and the thumbnail stream in one step, so the first read
    after a new title appears routinely still returns whatever the source was
    serving for the track that just ended. Ranking would then keep it for the
    whole song, because the replacement is rarely bigger - and when both are
    the same small placeholder size, the tiebreak on byte length is decided by
    JPEG entropy, which says nothing about which one belongs here.

    So every image seen during a track is remembered, not just the one that
    won: what leaks across a boundary is whatever was being served at that
    instant, which is as often the loser as the winner. Artwork the previous
    track served is held only until this track serves something of its own,
    and nothing is considered settled until two reads have agreed on it.
    """

    def __init__(self):
        self._track_key: tuple | None = None
        self._best_rank: tuple[int, int] | None = None
        self._best_raw: bytes | None = None
        self._best_id: str | None = None
        # Reads that have agreed with the artwork currently held.
        self._agreements: int = 0
        # Every image this track has served, and the same for the one before.
        self._seen: set[str] = set()
        self._previous_seen: frozenset[str] = frozenset()

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

    @property
    def settled(self) -> bool:
        """True once two separate reads have agreed on the artwork held.

        The first read of a track is not trustworthy on its own - it may be
        the previous track's cover, or a placeholder the source replaces a
        moment later. Confirming it costs one extra thumbnail read per track
        and is what makes it safe for a caller to stop looking.
        """
        return self._agreements >= 2

    def reset(self):
        """Forget the held artwork - nothing is playing at all."""
        self._track_key = None
        self._best_rank = None
        self._best_raw = None
        self._best_id = None
        self._agreements = 0
        self._seen = set()
        self._previous_seen = frozenset()

    def best_for(self, track_key, thumbnail_bytes):
        art_id = art_id_for(thumbnail_bytes)

        if track_key != self._track_key:
            self._previous_seen = frozenset(self._seen)
            self._seen = set()
            self._track_key = track_key
            self._best_rank = None
            self._best_raw = None
            self._best_id = None
            self._agreements = 0

        if len(self._seen) < SEEN_LIMIT:
            self._seen.add(art_id)

        rank = thumbnail_rank(thumbnail_bytes)

        # Artwork the previous track was serving loses to anything this track
        # has not served before, however small. Ranking it instead would let a
        # leftover sit out the whole song, since what replaces it is rarely
        # bigger and is often the very same placeholder size.
        #
        # Two tracks that genuinely share a cover are unaffected: that read is
        # the one already held, so it falls through to the ranking below.
        if (self._best_id is not None
                and self._best_id in self._previous_seen
                and art_id != self._best_id
                and art_id not in self._previous_seen):
            logger.debug('Artwork %s is this track\'s own; replacing the previous '
                         'track\'s, held at %s', rank, self._best_rank)
            # One swap per track: past here the held artwork is this track's,
            # and the ordinary ranking protects it from the placeholder.
            self._previous_seen = frozenset()
            self._best_rank = rank
            self._best_raw = thumbnail_bytes
            self._best_id = art_id
            self._agreements = 1
            return thumbnail_bytes

        if self._best_rank is not None and rank <= self._best_rank:
            # Equal ranks land here too, which is deliberate: re-sending the
            # same artwork would restart the device's scroll animation.
            how = 'no better than' if rank == self._best_rank else 'worse than'
            logger.debug('Incoming artwork %s is %s held %s; keeping it',
                         rank, how, self._best_rank)
            if art_id == self._best_id:
                # The source served the same image again, which is the
                # confirmation settled() waits for.
                self._agreements += 1
            return self._best_raw

        self._best_rank = rank
        self._best_raw = thumbnail_bytes
        self._best_id = art_id
        self._agreements = 1
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
