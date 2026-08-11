"""Finding Mini Docks on the local network.

The dock answers a UDP broadcast probe with its address and the TCP port it is
actually listening on. The discovery port is fixed rather than following the
configured TCP port - a client that already knew the port would have nothing to
discover.
"""
import json
import logging
import select
import socket
import time

from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Must match DISCOVERY_PORT / DISCOVERY_MAGIC in the Mini Dock app.
DISCOVERY_PORT = 32151
PROBE = b'VOBOT-NOW-PLAYING-DISCOVER'
REPLY_MAGIC = 'VOBOT-NOW-PLAYING'

DEFAULT_TIMEOUT = 1.5
MAX_REPLY = 1024


@dataclass(frozen=True)
class Device:
    """A dock that answered a probe."""
    host: str
    port: int
    app: str = ''
    model: str = ''
    device_id: str = ''

    @property
    def label(self) -> str:
        """One line for a chooser: what it is, and where."""
        name = self.model or self.app or 'Mini Dock'
        if self.device_id:
            name = f'{name} ({self.device_id})'
        return f'{name}  -  {self.host}:{self.port}'


def discover(timeout: float = DEFAULT_TIMEOUT) -> list[Device]:
    """Broadcast a probe and collect replies for `timeout` seconds.

    Blocking - call it off the GUI thread.
    """
    # Resolved once and threaded through: it is a host name lookup, and both the
    # sockets and the reply preference are derived from it.
    local_addresses = _local_addresses()

    bound = _broadcast_sockets(local_addresses)
    if not bound:
        logger.warning('No usable network interface to search from')
        return []

    sockets = [sock for sock, _ in bound]
    try:
        for sock, address in bound:
            for target in _targets_for(address):
                try:
                    sock.sendto(PROBE, (target, DISCOVERY_PORT))
                except OSError as exc:
                    # A down or restricted interface is normal; others may work.
                    logger.debug('Probe from %s to %s failed: %s',
                                 address, target, exc)

        found: dict[str, Device] = {}
        local_prefixes = {_subnet(address) for address in local_addresses}
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select(sockets, [], [], remaining)
            for sock in ready:
                try:
                    data, addr = sock.recvfrom(MAX_REPLY)
                except OSError:
                    continue
                device = _parse_reply(data, addr)
                if device is None:
                    continue
                # One dock answers once per interface the probe went out on, so
                # the same device can arrive under several addresses. Collapse
                # on its id and keep the address we are most likely to reach.
                key = _identity_key(device)
                existing = found.get(key)
                found[key] = _preferred(existing, device, local_prefixes)

        devices = sorted(found.values(), key=lambda d: _sort_key(d.host))
        logger.info('Discovery found %d dock(s)', len(devices))
        return devices
    finally:
        for sock in sockets:
            try:
                sock.close()
            except OSError:
                pass


def _parse_reply(data: bytes, addr) -> Device | None:
    try:
        payload = json.loads(data.decode('utf-8'))
    except Exception:
        logger.debug('Ignoring unparseable reply from %s', addr[0])
        return None

    if not isinstance(payload, dict) or payload.get('magic') != REPLY_MAGIC:
        return None

    try:
        port = int(payload.get('port'))
    except (TypeError, ValueError):
        return None

    # Trust the sender's address over anything it claims: that is the address we
    # can actually reach it on.
    return Device(
        host=addr[0],
        port=port,
        app=str(payload.get('app') or ''),
        model=str(payload.get('model') or ''),
        device_id=str(payload.get('device_id') or ''),
    )


def _broadcast_sockets(local_addresses) -> list[tuple[socket.socket, str]]:
    """One socket per local address, paired with the address it is bound to.

    Binding to 0.0.0.0 alone sends only from whichever route the stack picks,
    which misses the dock when a VPN or a second NIC owns the default route.
    """
    sockets = []
    for address in local_addresses:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind((address, 0))
            sock.setblocking(False)
        except OSError as exc:
            # Close it here: the caller only closes what reaches the list, and
            # a down or restricted interface is a normal, repeatable failure.
            logger.debug('Cannot search from %s: %s', address, exc)
            sock.close()
            continue
        sockets.append((sock, address))
    return sockets


def _local_addresses() -> list[str]:
    addresses = set()
    try:
        _, _, ips = socket.gethostbyname_ex(socket.gethostname())
        addresses.update(ips)
    except OSError as exc:
        logger.debug('Could not list local addresses: %s', exc)

    addresses.discard('127.0.0.1')
    return sorted(addresses) or ['0.0.0.0']


def _targets_for(address: str) -> list[str]:
    """Where a socket bound to `address` should send: its own subnet only.

    Sending another interface's directed broadcast down this one is not merely
    redundant, it cannot work - the stack rejects it outright with
    WSAENETUNREACH. The netmask is not available without platform-specific
    calls, so the directed address assumes /24, which home networks almost
    always are; the limited broadcast covers us either way.
    """
    targets = ['255.255.255.255']
    parts = address.split('.')
    if len(parts) == 4 and address != '0.0.0.0':
        targets.append('.'.join(parts[:3] + ['255']))
    return targets


def _identity_key(device: Device) -> str:
    """What makes two replies the same dock.

    Falls back to the address when the device reports no id - an older app, or
    one whose `device` module is unavailable.
    """
    return device.device_id or f'{device.host}:{device.port}'


def _preferred(existing: Device | None, candidate: Device,
               local_prefixes: set[str]) -> Device:
    """Pick between two addresses for the same dock.

    An address sharing a /24 with one of our own wins: replies that arrive via
    a VPN or virtual adapter are usually the ones we cannot route back to.
    """
    if existing is None:
        return candidate
    if _in_local_subnet(candidate.host, local_prefixes) and \
            not _in_local_subnet(existing.host, local_prefixes):
        return candidate
    return existing


def _in_local_subnet(host: str, local_prefixes: set[str]) -> bool:
    return _subnet(host) in local_prefixes


def _subnet(address: str) -> str:
    """The /24 an address belongs to. See _targets_for() on assuming /24."""
    return '.'.join(address.split('.')[:3])


def _sort_key(host: str):
    try:
        return tuple(int(part) for part in host.split('.'))
    except ValueError:
        return (999, 999, 999, 999)
