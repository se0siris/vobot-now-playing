# Changelog

Notable changes to both halves of the project.

The Windows client and the Mini Dock app are versioned separately. The client follows the `vX.Y.Z`
tags on this repository; the dock app carries its own version in
`esp32/apps/win_now_playing/manifest.yml`.

## [1.1.0] - 2026-08-16

Mini Dock app **2.1.0**. Wire protocol **4**.

Both ends stay compatible with the previous release. A 1.0.0 client simply never sends the new
`light` field, which the dock reads as "leave the ambient light alone", and an older dock app
ignores it. The ambient light itself needs both halves updated.

### Added

- **Ambient light.** The dock's LED strip can be driven from the dominant colour of the current
  cover art. Off by default; enable it and set a brightness in **Settings**. The colour is not an
  average, which comes out grey-brown for any cover with more than one hue in it. The image is
  reduced to eight clusters, the ones carrying no colour information are dropped, and what is left
  competes on both how much of the cover it occupies and how colourful it is. Covers with no colour
  at all show white rather than being forced to a hue they do not have.
- **Playback position** in the client window, for sources that report one. Windows publishes the
  position as an occasional timestamped snapshot rather than a running clock, so the bar is
  extrapolated between updates. Measured against Edge, that stays accurate to under half a second
  across a full minute of drift.
- **Transport controls** in the client window: previous, play/pause and next, with each button
  enabled only when the current source says it accepts that command.
- **Taskbar integration**, all four parts off by default and opted into separately in **Settings**:
  a play/pause status badge on the taskbar button, transport buttons on the thumbnail toolbar,
  cover art as the window and taskbar icon, and playback position on the taskbar progress bar.
  There is also an option to keep the taskbar button when the window is hidden, since a hidden
  window has no taskbar button and would otherwise take the other three with it.
- **The tray notification can be turned off.** Clicking the "still running in the notification
  area" balloon now stops it appearing again.

### Changed

- **Artwork selection is much more reliable.** Sources publish artwork in phases rather than all at
  once, and the version worth showing is often on offer for well under a second. The client now
  ranks every image a track serves, so a small player-bar thumbnail cannot displace a full-size
  cover already in hand, and the previous track's artwork cannot squat for a whole song. While the
  only artwork available still belongs to the track that just ended, the dock is left showing what
  it already has instead of being sent a leftover, and the client's own preview dims to mark it as
  provisional.
- **Frame transfers to the dock are several times faster.** The dock now reads each frame straight
  into a slice of its back buffer rather than into fresh objects it then copies. Measured over WiFi
  at -50dBm, this took a frame from 2-5 seconds down to around 430ms. The saving is in garbage
  collection, not the network: the old path produced roughly 150KB of garbage per frame, and a
  collection on this hardware costs 200-280ms.
- Thumbnail reading now uses context managers throughout, so a stream is closed even when a read
  fails partway.
- Added [ruff](https://docs.astral.sh/ruff/) for linting and formatting, and brought the codebase
  in line with it.

### Fixed

- The discovery socket is now closed when a search fails, rather than being left open.

## [1.0.0] - 2026-08-10

First tagged release. Mini Dock app **2.0.0**. Wire protocol **3**.

- Live title, artist, album and playback state from any player Windows knows about.
- Album art converted to RGB565 and drawn on the dock's panel.
- Automatic discovery of the dock over UDP broadcast.
- Artwork sent only once per image, via an artwork ID handshake.
- Runs in the notification area, with optional start-hidden and close-to-tray behaviour.

[1.1.0]: https://github.com/se0siris/vobot-now-playing/releases/tag/v1.1.0
[1.0.0]: https://github.com/se0siris/vobot-now-playing/releases/tag/v1.0.0
