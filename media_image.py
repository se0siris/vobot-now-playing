"""Artwork handling: ranking the thumbnails Windows hands out, packing the
chosen one into the RGB565 frame the Mini Dock draws, and reading a colour off
it for the dock's ambient light.
"""
import colorsys
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
        with Image.open(BytesIO(thumbnail_bytes)) as opened:
            image = opened.convert('RGB')
    except Exception:
        logger.warning('Could not decode %d bytes of artwork; treating it as none',
                       len(thumbnail_bytes), exc_info=True)
        return None, 0, 0
    # Scale to fit the panel, enlarging as well as shrinking. Image.thumbnail()
    # only ever shrinks, and sources publish artwork far smaller than the panel
    # often enough to matter: Firefox hands over whichever image the page listed
    # first in its media session metadata, unresized, which for YouTube Music is
    # sometimes the 60x60 player-bar thumbnail. That used to reach the dock as a
    # 60x60 stamp centred on black, filling 5% of the panel.
    #
    # Aspect ratio is preserved, so square art on a 4:3 panel caps out at 240x240
    # and the enlargement never exceeds what the short axis allows.
    scale = min(size[0] / image.width, size[1] / image.height)
    fitted = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    if fitted != image.size:
        # LANCZOS over BICUBIC: 0.12ms more on a 4x enlarge and slightly cleaner
        # on the reductions, against ~4ms to read the thumbnail in the first place.
        image = image.resize(fitted, Resampling.LANCZOS)
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


# Dominant colour extraction, for the dock's ambient light.
#
# Sampled at this size rather than full resolution: the answer is a single
# colour, so detail buys nothing, and it keeps the whole thing to roughly the
# cost of decoding the thumbnail in the first place.
COLOUR_SAMPLE_SIZE = (64, 64)
# Clusters to reduce the cover to. Enough to keep a cover's accent colour
# separate from its background, few enough that each one means something.
COLOUR_CLUSTERS = 8

# Pixels this dark say nothing about a cover's colour - they are its shadows and
# its letterboxing, and on a strip of LEDs they are indistinguishable from off.
COLOUR_MIN_VALUE = 0.15

# What a cluster's share of the image is worth against how colourful it is,
# *among clusters that have a colour at all*. The constant keeps a large muted
# region in the running rather than letting one vivid speck win outright.
COLOUR_SATURATION_WEIGHT = 0.30

# The strip is 14 LEDs behind the dock, bounced off a wall. A faithful colour
# read straight off a dim or washed-out cover arrives there as approximately
# nothing, so the winner is lifted to at least this much before it is sent. This
# is the difference between "the light is the colour of the album" and "the
# light appears to be broken".
COLOUR_MIN_SATURATION = 0.60
COLOUR_MIN_BRIGHTNESS = 0.90

# Below this a cluster has no hue worth having: white, black, grey, and the
# off-white paper that a great many covers are mostly made of. Two jobs, and
# both matter:
#
#   * It splits the clusters into tiers. Anything with a colour wins outright
#     over anything without, however little of the cover it occupies. Scoring
#     them together instead does not work, and measured against real covers is
#     what made half of them come out white: a cover that is 70% off-white at
#     saturation 0.05 scores 0.35x count, where a 10% vivid accent scores only
#     0.12x count. The background won every time, and an ambient light that
#     shows white for half an album collection is not showing anything.
#   * It gates the saturation boost below, because hue survives desaturation in
#     HSV: a black-and-white cover comes back as hue 0, and lifting it to the
#     floor would light the dock bright red for an album with no colour in it.
#
# So a genuinely colourless cover still shows white - it just has to be the only
# thing on offer, rather than merely the largest.
COLOUR_ACHROMATIC = 0.12


def dominant_colour(thumbnail_bytes) -> tuple[int, int, int] | None:
    """The colour a piece of artwork reads as, for the dock's ambient light.

    Not the average - that is grey-brown for any cover with more than one hue in
    it. The image is reduced to a handful of clusters, the ones that carry no
    colour information are dropped, and what is left competes on how much of the
    cover it covers *and* how colourful it is.

    Returns None when the artwork cannot be decoded, which is the same thing
    resize_thumbnail() does with it: one bad thumbnail is not worth failing an
    update over.
    """
    if not thumbnail_bytes:
        return None
    try:
        with Image.open(BytesIO(thumbnail_bytes)) as opened:
            sample = opened.convert('RGB').resize(COLOUR_SAMPLE_SIZE,
                                                  Resampling.BILINEAR)
            # FASTOCTREE over the default median cut: it returns the colours
            # actually present rather than interpolating new ones, which is what
            # is wanted when the answer is "which colour is this cover".
            quantised = sample.quantize(colors=COLOUR_CLUSTERS,
                                        method=Image.Quantize.FASTOCTREE)
            palette = quantised.getpalette()
            counts = quantised.getcolors()
    except Exception:
        logger.warning('Could not read a colour from %d bytes of artwork',
                       len(thumbnail_bytes), exc_info=True)
        return None

    if not counts or not palette:
        return None

    best = None          # best cluster that has a hue - always wins if one exists
    best_score = 0.0
    plain = None         # largest colourless one, for a cover that has no hue
    plain_count = -1

    for count, index in counts:
        red, green, blue = palette[index * 3:index * 3 + 3]
        hue, saturation, value = colorsys.rgb_to_hsv(red / 255, green / 255,
                                                     blue / 255)
        if value < COLOUR_MIN_VALUE:
            continue
        if saturation < COLOUR_ACHROMATIC:
            if count > plain_count:
                plain_count = count
                plain = (hue, saturation, value)
            continue
        score = count * (COLOUR_SATURATION_WEIGHT + saturation)
        if score > best_score:
            best_score = score
            best = (hue, saturation, value)

    if best is None:
        # Nothing on this cover has a colour. Show the largest thing that is at
        # least visible; failing even that - an all-black cover - the largest
        # cluster there is, so something goes out rather than nothing.
        if plain is None:
            index = max(counts)[1]
            red, green, blue = palette[index * 3:index * 3 + 3]
            plain = colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)
        best = plain

    hue, saturation, value = best
    if saturation >= COLOUR_ACHROMATIC:
        saturation = max(saturation, COLOUR_MIN_SATURATION)
    red, green, blue = colorsys.hsv_to_rgb(
        hue, saturation, max(value, COLOUR_MIN_BRIGHTNESS))
    colour = (round(red * 255), round(green * 255), round(blue * 255))
    logger.debug('Artwork reads as %s', colour)
    return colour


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
    def holding_leftover(self) -> bool:
        """True while the artwork held is one the previous track served.

        Windows hands over the new metadata before the new thumbnail, so the
        first read of a track is routinely the last image of the one before it.
        A caller that knows this can keep reading instead of waiting for the
        next event, which is what catches artwork that is only published for a
        fraction of a second.

        Two tracks that genuinely share a cover keep this true for as long as
        they play, so a caller must bound how long it is willing to chase.
        """
        return self._best_id is not None and self._best_id in self._previous_seen

    @property
    def settled(self) -> bool:
        """True once two separate reads have agreed on the artwork held.

        The first read of a track is not trustworthy on its own - it may be
        the previous track's cover, or a placeholder the source replaces a
        moment later. Confirming it costs one extra thumbnail read per track
        and is what makes it safe for a caller to stop looking.
        """
        return self._agreements >= 2

    def keep_as_own(self):
        """Stop treating the held artwork as the previous track's.

        For a caller that has looked for this track's own artwork and not found
        it: an image the track before it also served, and which is still all that
        is on offer, is not a leftover at all - the two tracks share a cover, as
        consecutive tracks from one album do. Saying so ends the chase for good,
        rather than leaving every later read to reach the same dead end.

        Ranking is untouched, so a larger version turning up later still wins.
        """
        if not self._previous_seen:
            return
        logger.debug('Keeping artwork %s as this track\'s own', self._best_rank)
        self._previous_seen = frozenset()

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


class ColourCache:
    """Dominant colour for the current artwork.

    Deliberately not folded into FrameCache, which looks like the obvious home
    for it: that only runs when a frame is actually needed, and the colour is
    needed on *every* push. Most pushes send no frame at all - the art_id
    handshake sees to that - so a colour cached there would be recomputed from
    scratch on the play/pause events where it is most often wanted.
    """

    def __init__(self):
        self._art_id: str | None = None
        self._colour: tuple[int, int, int] | None = None

    def colour_for(self, thumbnail_bytes, art_id) -> tuple[int, int, int] | None:
        if art_id is not None and art_id == self._art_id:
            return self._colour
        self._art_id = art_id
        self._colour = dominant_colour(thumbnail_bytes)
        return self._colour

    def clear(self):
        self._art_id = None
        self._colour = None
