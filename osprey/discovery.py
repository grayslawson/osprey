"""WS-Discovery — how ONVIF clients find us without being told an address.

A client multicasts a Probe to 239.255.255.250:3702; we answer with a ProbeMatch
naming our device service. A Hello goes out at startup and a Bye at shutdown, so a
client that is already listening notices us without probing.

A Probe that asks for a device type we are not is ignored rather than answered
wrongly — answering everything makes us appear as devices we cannot serve.

Why this module is not optional
-------------------------------
Without correct WS-Discovery a client cannot find the camera **by itself**. A user
can still add it by typing the address, but Home Assistant's "it just appeared"
onboarding — the thing that makes an ONVIF camera pleasant to use — depends
entirely on this. So this is a must-have, not a nicety, and it has a failure mode
that hides in plain sight (see below).

The one detail everything hinges on: the WS-Addressing namespace
--------------------------------------------------------------------
ONVIF's WS-Discovery profile pairs the **2005/04** discovery draft
(`DISCOVERY_NS`) with **WS-Addressing 2004/08** (`ADDRESSING_NS`,
`http://schemas.xmlsoap.org/ws/2004/08/addressing`) — *not* the later 2005/08
addressing spec. A real ONVIF client (Home Assistant uses `python-ws-discovery`)
sends the Probe's `MessageID` under 2004/08 and expects the `ProbeMatch` back in
the same namespace.

Get this wrong and the failure is **silent and total**: a device that looks for the
client's `MessageID` under 2005/08 simply does not find it, sends no reply, and
never appears in discovery. Nothing errors. It is invisible to a hand-written probe
that also uses the wrong namespace, because that round-trips against itself just
fine — only a real client, or a probe captured off the wire, exposes it. (This
happened here; the fix and the regression test that pins it are below and in
`tests/test_discovery.py`.)

Two more things a real client does that a naive one does not, both handled here:

* it declares the ONVIF type in `<d:Types>` under a **random namespace prefix**
  (`xmlns:Zx="…/network/wsdl"` … `<d:Types>Zx:NetworkVideoTransmitter</d:Types>`),
  so type matching must compare *local* names, never prefixes;
* it correlates the reply by `RelatesTo` = the Probe's `MessageID`, so the reply
  must echo it back.

How it works, end to end
------------------------
1. `DiscoveryServer` binds :3702, joins the multicast group, and (optionally)
   multicasts a Hello so already-listening clients see us immediately.
2. `probe_message_id()` parses an incoming Probe: it must be a Probe, its Types (if
   any) must name a type we are, and it must carry a MessageID — returned so the
   reply can relate to it. Anything else returns None and is ignored.
3. `probe_match()` builds the ProbeMatch — our stable endpoint UUID, our Types and
   Scopes, the `XAddrs` pointing at our ONVIF device service, and `RelatesTo` set
   to the Probe's MessageID — and it is unicast straight back to the prober.

More than one device
--------------------
A controller may serve the same camera twice: once with PTZ for the tracker and
once without it for a recorder.  Those are **two ONVIF devices**, so discovery
answers with one ProbeMatch per device — each with its own endpoint UUID, its
own XAddr and its own scopes — and a client can tell them apart.

One datagram, please
--------------------
A 1500-byte Ethernet MTU leaves 1472 bytes for the UDP payload.  A larger
discovery message is split into IP fragments, which some networks drop and
some clients reassemble badly, so every message Osprey sends stays inside one
datagram.  The optional parts of the message — the metadata version, the
`To` header, the device type — are given up in that order to make room.  A
device that serves one camera still sends the full, standard message.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import struct
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Final, Iterable, Sequence
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree

WS_DISCOVERY_PORT: Final = 3702
MULTICAST_GROUP: Final = "239.255.255.250"

DISCOVERY_NS: Final = "http://schemas.xmlsoap.org/ws/2005/04/discovery"
# ONVIF pairs the 2005/04 WS-Discovery draft with the **2004/08** WS-Addressing
# namespace, and that is what real ONVIF clients (Home Assistant's included) send
# and expect back. The later 2005/08 addressing namespace is a different spec; a
# device that answers in it, or looks for the client's MessageID under it, is
# silently ignored — the probe's MessageID simply is not found, so no reply goes
# out and the camera never appears in discovery.
ADDRESSING_NS: Final = "http://schemas.xmlsoap.org/ws/2004/08/addressing"
ADDRESSING_NS_2005: Final = "http://www.w3.org/2005/08/addressing"
SOAP_NS: Final = "http://www.w3.org/2003/05/soap-envelope"
NVT_TYPE: Final = "dn:NetworkVideoTransmitter"
DEVICE_TYPE: Final = "tds:Device"
ANONYMOUS_URI: Final = "http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous"
DISCOVERY_URI: Final = "urn:schemas-xmlsoap-org:ws:2005:04:discovery"
MATCH_RFC3986: Final = f"{DISCOVERY_NS}/rfc3986"
MATCH_STRCMP0: Final = f"{DISCOVERY_NS}/strcmp0"
# Hello/Bye stay on the local link (WS-Discovery recommends a TTL of 1).
MULTICAST_TTL: Final = 1
# The largest discovery message we send, in bytes: a 1500-byte Ethernet MTU
# minus a 20-byte IPv4 header and an 8-byte UDP header.  Anything bigger is
# fragmented, and fragments get dropped.
MAX_MESSAGE_BYTES: Final = 1472

log = logging.getLogger("osprey.discovery")

_oversized_warned = False


def _envelope(body: str, header: str, *, device_namespace: bool = True) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<s:Envelope xmlns:s="{SOAP_NS}" xmlns:a="{ADDRESSING_NS}" xmlns:d="{DISCOVERY_NS}" '
        'xmlns:dn="http://www.onvif.org/ver10/network/wsdl" '
        + ('xmlns:tds="http://www.onvif.org/ver10/device/wsdl"' if device_namespace else "")
        + ">"
        f"<s:Header>{header}</s:Header><s:Body>{body}</s:Body></s:Envelope>"
    )


@dataclass(frozen=True)
class Identity:
    """What we say we are when asked."""

    endpoint: str  # a stable urn:uuid for this device
    xaddr: str  # where the device service actually is
    scopes: tuple[str, ...]

    @property
    def scope_text(self) -> str:
        return " ".join(self.scopes)


def scope_token(value: str) -> str:
    """One scope path segment: spaces become '_', anything else unsafe is %-encoded."""
    return quote(value.strip().replace(" ", "_"), safe="-._~") or "unknown"


def scopes_for(
    name: str, ptz: bool, hardware: str | None = None, extra: Iterable[str] = ()
) -> tuple[str, ...]:
    """The device scopes, shared by WS-Discovery and the Device GetScopes call."""
    scopes = [
        "onvif://www.onvif.org/Profile/Streaming",
        "onvif://www.onvif.org/type/video_encoder",
    ]
    if ptz:
        scopes.append("onvif://www.onvif.org/type/ptz")
    if hardware:
        scopes.append(f"onvif://www.onvif.org/hardware/{scope_token(hardware)}")
    scopes.append(f"onvif://www.onvif.org/name/{scope_token(name)}")
    scopes.extend(scope for scope in extra if scope not in scopes)
    return tuple(scopes)


def validate_scope(scope: object) -> str:
    if not isinstance(scope, str) or not scope or len(scope) > 256 or any(c.isspace() for c in scope):
        raise ValueError("discovery scopes must be URIs without whitespace (max 256 characters)")
    if not scope.startswith("onvif://www.onvif.org/"):
        raise ValueError("discovery scopes must start with onvif://www.onvif.org/")
    return scope


def endpoint_for(host: str, port: int) -> str:
    """A stable urn:uuid for this device service, derived from its address."""
    # The historical seed stays: changing it would give NVRs a new device UUID.
    return f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'cuckoo://{host}:{port}')}"


def identity_for(
    host: str,
    port: int,
    path: str,
    name: str,
    ptz: bool,
    hardware: str | None = None,
    extra_scopes: Iterable[str] = (),
) -> Identity:
    # Derived from the address so a restart keeps the same identity.
    return Identity(
        endpoint=endpoint_for(host, port),
        xaddr=f"http://{host}:{port}{path}",
        scopes=scopes_for(name, ptz, hardware, extra_scopes),
    )


def _scope_parts(scope: str) -> tuple[str, str, list[str]]:
    parts = urlsplit(scope)
    segments = [segment for segment in parts.path.split("/") if segment]
    return parts.scheme.lower(), parts.netloc.lower(), segments


def scope_matches(wanted: str, offered: str, match_by: str = MATCH_RFC3986) -> bool:
    """WS-Discovery 2005/04 scope matching for one probe scope against one of ours."""
    if match_by == MATCH_STRCMP0:
        return wanted == offered
    if match_by != MATCH_RFC3986:
        return False
    w_scheme, w_authority, w_path = _scope_parts(wanted)
    o_scheme, o_authority, o_path = _scope_parts(offered)
    return (
        (w_scheme, w_authority) == (o_scheme, o_authority)
        and len(w_path) <= len(o_path)
        and o_path[: len(w_path)] == w_path
    )


def _fit(build: Callable[[frozenset[str]], str], order: Sequence[str]) -> str:
    """Render a discovery message that fits in one datagram.

    ``build`` renders the message with the named optional parts omitted.
    Optional parts are given up in ``order`` until the message fits inside
    :data:`MAX_MESSAGE_BYTES`; if even that is not enough the message is
    returned whole and the caller says so.
    """
    omitted: frozenset[str] = frozenset()
    message = build(omitted)
    for part in order:
        if len(message.encode()) <= MAX_MESSAGE_BYTES:
            break
        omitted = omitted | {part}
        message = build(omitted)
    return message


def _report_size(message: str, what: str) -> None:
    """Say once that a message did not fit in one datagram."""
    global _oversized_warned
    size = len(message.encode())
    if size <= MAX_MESSAGE_BYTES or _oversized_warned:
        return
    _oversized_warned = True
    log.warning(
        "ws-discovery: the %s message is %d bytes, over the %d-byte datagram budget; "
        "it will be fragmented. Shorten the camera name or the advertised host.",
        what, size, MAX_MESSAGE_BYTES,
    )


def _types(omit: frozenset[str]) -> str:
    """The device types we claim, minus the device type when it is given up."""
    return NVT_TYPE if "device_type" in omit else f"{NVT_TYPE} {DEVICE_TYPE}"


def probe_match(identities: Sequence[Identity], relates_to: str) -> str:
    """A ProbeMatches answer naming every identity we answer for.

    A controller that serves the same camera twice answers with one
    ProbeMatch per view: each has its own endpoint UUID, its own XAddr and
    its own scopes, so a client can tell them apart.

    The message is kept inside one datagram: the metadata version, the device
    type and then the ``To`` header are given up, in that order, if the
    personas would not otherwise fit.
    """
    message_id = f"urn:uuid:{uuid.uuid4()}"

    def build(omit: frozenset[str]) -> str:
        types = _types(omit)
        header = (
            f"<a:MessageID>{message_id}</a:MessageID>"
            f"<a:RelatesTo>{relates_to}</a:RelatesTo>"
            + ("" if "to" in omit else f"<a:To>{ANONYMOUS_URI}</a:To>")
            + f"<a:Action>{DISCOVERY_NS}/ProbeMatches</a:Action>"
        )
        matches = "".join(
            "<d:ProbeMatch>"
            f"<a:EndpointReference><a:Address>{identity.endpoint}</a:Address></a:EndpointReference>"
            f"<d:Types>{types}</d:Types>"
            f"<d:Scopes>{identity.scope_text}</d:Scopes>"
            f"<d:XAddrs>{identity.xaddr}</d:XAddrs>"
            + ("" if "metadata" in omit else "<d:MetadataVersion>1</d:MetadataVersion>")
            + "</d:ProbeMatch>"
            for identity in identities
        )
        body = f"<d:ProbeMatches>{matches}</d:ProbeMatches>"
        return _envelope(body, header, device_namespace="device_type" not in omit)

    message = _fit(build, ("metadata", "device_type", "to"))
    _report_size(message, "ProbeMatches")
    return message


def announcement(identities: Sequence[Identity], leaving: bool = False) -> str:
    """A Hello (or Bye) naming every identity we announce."""
    action = "Bye" if leaving else "Hello"
    message_id = f"urn:uuid:{uuid.uuid4()}"

    def build(omit: frozenset[str]) -> str:
        types = _types(omit)
        header = (
            f"<a:MessageID>{message_id}</a:MessageID>"
            f"<a:To>{DISCOVERY_URI}</a:To>"
            f"<a:Action>{DISCOVERY_NS}/{action}</a:Action>"
        )
        bodies = "".join(
            f"<d:{action}>"
            f"<a:EndpointReference><a:Address>{identity.endpoint}</a:Address></a:EndpointReference>"
            f"<d:Types>{types}</d:Types>"
            f"<d:Scopes>{identity.scope_text}</d:Scopes>"
            f"<d:XAddrs>{identity.xaddr}</d:XAddrs>"
            + ("" if "metadata" in omit else "<d:MetadataVersion>1</d:MetadataVersion>")
            + f"</d:{action}>"
            for identity in identities
        )
        return _envelope(bodies, header, device_namespace="device_type" not in omit)

    message = _fit(build, ("metadata", "device_type"))
    _report_size(message, action)
    return message


def _probe_element(payload: bytes) -> ElementTree.Element | None:
    """The Probe element of a payload, or None when it is not a Probe."""
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError:
        return None
    body = root.find(f"{{{SOAP_NS}}}Body")
    if body is None:
        return None
    probe = body.find(f"{{{DISCOVERY_NS}}}Probe")
    if probe is None:
        return None
    types = probe.find(f"{{{DISCOVERY_NS}}}Types")
    if types is not None and types.text:
        wanted = types.text.split()
        # Compare local names: the client's prefixes are its own business.
        locals_wanted = {name.rpartition(":")[2] for name in wanted}
        if not locals_wanted & {"NetworkVideoTransmitter", "Device", "NetworkVideoDisplay"}:
            return None
        if "NetworkVideoDisplay" in locals_wanted and len(locals_wanted) == 1:
            return None
    return probe


def probe_message_id(payload: bytes) -> str | None:
    """The MessageID of a Probe we should answer, or None if it is not for us.

    Which of our personas answer is decided by :func:`matching_identities`.
    """
    probe = _probe_element(payload)
    if probe is None:
        return None
    root = ElementTree.fromstring(payload)
    header = root.find(f"{{{SOAP_NS}}}Header")
    if header is None:
        return None
    # Accept the MessageID under either WS-Addressing namespace: ONVIF clients use
    # 2004/08, but tolerate a client that used 2005/08 rather than dropping it.
    message_id = header.find(f"{{{ADDRESSING_NS}}}MessageID")
    if message_id is None:
        message_id = header.find(f"{{{ADDRESSING_NS_2005}}}MessageID")
    if message_id is None or not message_id.text:
        return None
    return message_id.text.strip()


def matching_identities(payload: bytes, identities: Sequence[Identity]) -> tuple[Identity, ...]:
    """The personas that answer this Probe.

    A Probe that names scopes is answered by the personas that satisfy every
    requested scope, so a client that wants a PTZ device is offered the PTZ
    persona alone and never the media-only one.  A Probe that names no scopes
    is answered by all of them.
    """
    probe = _probe_element(payload)
    if probe is None:
        return ()
    wanted = probe.find(f"{{{DISCOVERY_NS}}}Scopes")
    if wanted is None or not wanted.text or not wanted.text.strip():
        return tuple(identities)
    match_by = wanted.attrib.get("MatchBy", MATCH_RFC3986)
    requested = wanted.text.split()
    matched = []
    for identity in identities:
        if all(
            any(scope_matches(scope, offered, match_by) for offered in identity.scopes)
            for scope in requested
        ):
            matched.append(identity)
    return tuple(matched)


def interface_ipv4(name: str) -> str | None:
    """The primary IPv4 address of a local interface (Linux), or None."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX
        return None
    if not name or len(name) > 15:
        return None
    probe_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        request = struct.pack("256s", name.encode()[:15])
        reply = fcntl.ioctl(probe_sock.fileno(), 0x8915, request)  # SIOCGIFADDR
        return socket.inet_ntoa(reply[20:24])
    except OSError:
        return None
    finally:
        probe_sock.close()


def multicast_address(
    address: str | None, interface: str | None, bind: str | None, host: str | None
) -> str | None:
    """Choose the IPv4 address WS-Discovery multicast should use.

    Precedence: explicit ``discovery.address``; the address of
    ``discovery.interface``; the ``bind`` address when it is specific; the
    advertised ``host`` when it is an IPv4 literal. ``None`` means the kernel's
    default multicast interface.
    """
    if address:
        return address
    if interface:
        found = interface_ipv4(interface)
        if found:
            return found
        log.warning("ws-discovery: interface %s has no IPv4 address; falling back", interface)
    for candidate in (bind, host):
        if not candidate:
            continue
        try:
            parsed = ipaddress.IPv4Address(candidate)
        except ValueError:
            continue
        if not parsed.is_unspecified and not parsed.is_loopback:
            return str(parsed)
    return None


class DiscoveryServer:
    """Answers Probes. Multicast is optional so this can be driven over unicast.

    ``interface_address`` pins multicast to one LAN interface: the group is
    joined on that interface only and Hello/Bye leave through it
    (``IP_MULTICAST_IF``) with a TTL of one. ``interface_name`` additionally
    tries ``SO_BINDTODEVICE`` so probes arriving on other interfaces are not
    seen at all; that needs ``CAP_NET_RAW`` and is skipped (with a log line)
    when not permitted. The socket stays bound to the wildcard address so that
    both multicast and unicast Probes are received.

    ``hello_interval`` (seconds, 0 disables) repeats the Hello so clients that
    missed the startup announcement still learn about the device.
    ``announce_to`` overrides the Hello/Bye destination (tests use a local
    socket instead of the multicast group).
    """

    def __init__(
        self,
        identity: Identity | Sequence[Identity],
        port: int = WS_DISCOVERY_PORT,
        multicast: bool = True,
        *,
        interface_address: str | None = None,
        interface_name: str | None = None,
        hello_interval: float = 0.0,
        announce_to: tuple[str, int] | None = None,
    ) -> None:
        self.identities: tuple[Identity, ...] = (identity,) if isinstance(identity, Identity) else tuple(identity)
        self.multicast = multicast
        self.hello_interval = max(0.0, hello_interval)
        self.announce_to = announce_to or (MULTICAST_GROUP, WS_DISCOVERY_PORT)
        self.interface_address: str | None = None
        self.bound_device: str | None = None
        self._stop = threading.Event()
        self._next_hello = 0.0
        self.hellos = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if interface_name:
            self._bind_device(interface_name)
        self.sock.bind(("", port))
        if interface_address:
            self._pin_interface(interface_address)
        if multicast:
            local = socket.inet_aton(self.interface_address) if self.interface_address else struct.pack("=I", socket.INADDR_ANY)
            membership = struct.pack("4s4s", socket.inet_aton(MULTICAST_GROUP), local)
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, MULTICAST_TTL)
        self.sock.settimeout(0.5)
        self.answered = 0
        self._thread: threading.Thread | None = None

    def _bind_device(self, name: str) -> None:
        option = getattr(socket, "SO_BINDTODEVICE", None)
        if option is None:
            log.info("ws-discovery: SO_BINDTODEVICE unsupported; relying on IP_MULTICAST_IF")
            return
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, option, name.encode() + b"\0")
            self.bound_device = name
        except OSError as exc:
            log.info("ws-discovery: cannot bind to device %s (%s); relying on IP_MULTICAST_IF", name, exc.strerror or exc)

    def _pin_interface(self, address: str) -> None:
        try:
            parsed = ipaddress.IPv4Address(address)
        except ValueError:
            log.warning("ws-discovery: %r is not an IPv4 address; using the default interface", address)
            return
        try:
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(str(parsed)))
        except OSError as exc:
            log.warning(
                "ws-discovery: %s is not a local interface address (%s); using the default interface",
                parsed, exc.strerror or exc,
            )
            return
        self.interface_address = str(parsed)

    @property
    def port(self) -> int:
        return int(self.sock.getsockname()[1])

    @property
    def identity(self) -> Identity:
        """The primary identity; the first one we answer for."""
        return self.identities[0]

    def start(self) -> None:
        log.info(
            "ws-discovery answering on :%d%s", self.port,
            f" (multicast on {self.interface_address})" if self.interface_address else "",
        )
        if self.multicast:
            self.say_hello()
        self._next_hello = time.monotonic() + self.hello_interval
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def say_hello(self) -> None:
        self._announce(leaving=False)

    def _announce(self, leaving: bool) -> None:
        try:
            self.sock.sendto(announcement(self.identities, leaving).encode(), self.announce_to)
            if not leaving:
                self.hellos += 1
        except OSError as exc:
            log.debug("could not announce: %s", exc)

    def _periodic_hello(self) -> None:
        if not self.hello_interval or not (self.multicast or self.announce_to != (MULTICAST_GROUP, WS_DISCOVERY_PORT)):
            return
        now = time.monotonic()
        if now >= self._next_hello:
            self._next_hello = now + self.hello_interval
            self.say_hello()

    def _serve(self) -> None:
        while not self._stop.is_set():
            self._periodic_hello()
            try:
                payload, peer = self.sock.recvfrom(65536)
            except (TimeoutError, OSError):
                continue
            matched = matching_identities(payload, self.identities)
            if not matched:
                continue
            message_id = probe_message_id(payload)
            if message_id is None:
                continue
            try:
                self.sock.sendto(probe_match(matched, message_id).encode(), peer)
                self.answered += 1
                log.debug("answered probe from %s", peer[0])
            except OSError as exc:  # pragma: no cover - transient network failure
                log.debug("could not answer probe: %s", exc)

    def stop(self) -> None:
        if self.multicast:
            self._announce(leaving=True)
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.sock.close()
