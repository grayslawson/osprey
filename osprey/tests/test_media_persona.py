"""The media persona: a second ONVIF device for a recorder, with no PTZ.

One physical camera, two ONVIF devices.  The primary persona carries PTZ for
the tracker; the media persona carries media only, for an NVR such as UniFi
Protect.  Two rules make that safe, and both are pinned here:

* the two personas must be **different devices** — their own serial, MAC,
  hostname and friendly name — or the NVR merges the two records into one;
* the media persona must be **unable to move the camera**, not merely
  uninvited to.
"""

from __future__ import annotations

import socket
from pathlib import Path
from xml.etree import ElementTree

import discovery
import identity
import main
import onvif
from unifiwire import envelope

from test_discovery import probe
from test_onvif import ask, request, services, text_of
from test_stack import FakeCamera, HELLO_PAYLOAD, control_port


def a_media_persona() -> tuple[onvif.Services, onvif.Services, object]:
    """The primary persona and the media persona, on the same camera."""
    _, recorder = services()
    primary = onvif.Services(recorder.backend(), host="10.0.0.1", port=8000)
    media = onvif.Services(
        recorder.backend(), host="10.0.0.1", port=8001, read_only=True, persona="media"
    )
    return primary, media, recorder


# --------------------------------------------------------------------- identity


def test_the_two_personas_are_different_devices() -> None:
    """An NVR keys on the identifiers, so the personas must not share any."""
    primary, media, _ = a_media_persona()
    one, two = primary.device_identity(), media.device_identity()
    assert one.serial != two.serial
    assert one.mac != two.mac
    assert one.hostname != two.hostname


def test_the_media_persona_keeps_its_own_friendly_name() -> None:
    primary, media, _ = a_media_persona()
    assert primary.display_name == "front gate"
    assert media.display_name == "front gate (media)"


def test_neither_persona_reports_the_physical_camera() -> None:
    """The G5 has one native controller. Osprey must never look like it."""
    primary, media, _ = a_media_persona()
    for persona in (primary, media):
        ident = persona.device_identity()
        assert ident.mac.replace(":", "") != "AABBCCDDEEFF"
        assert "AABBCCDDEEFF" not in ident.serial


def test_the_media_persona_never_inherits_the_operator_overrides() -> None:
    """A pinned identity describes one device; sharing it would merge the two."""
    _, recorder = services()
    pinned = identity.IdentityConfig(
        mac="02:11:22:33:44:55", serial="OSP123456789012", hostname="osprey-box"
    )
    primary = onvif.Services(
        recorder.backend(), host="10.0.0.1", port=8000, identity=pinned, name="osprey"
    )
    media = onvif.Services(
        recorder.backend(), host="10.0.0.1", port=8001, read_only=True,
        persona="media", identity=pinned, name="osprey",
    )
    assert primary.device_identity().mac == "02:11:22:33:44:55"
    assert media.device_identity().mac != "02:11:22:33:44:55"
    assert media.device_identity().serial != "OSP123456789012"
    assert media.device_identity().hostname != "osprey-box"


def test_the_media_persona_never_borrows_the_interface_mac() -> None:
    """The interface MAC belongs to the host, not to one persona."""
    _, recorder = services()
    primary = onvif.Services(
        recorder.backend(), host="10.0.0.1", port=8000, name="osprey",
        interface_mac="02:aa:bb:cc:dd:ee",
    )
    media = onvif.Services(
        recorder.backend(), host="10.0.0.1", port=8001, read_only=True, persona="media",
        name="osprey", interface_mac="02:aa:bb:cc:dd:ee",
    )
    assert primary.device_identity().mac == "02:aa:bb:cc:dd:ee"
    assert media.device_identity().mac != "02:aa:bb:cc:dd:ee"


def test_the_derived_mac_is_locally_administered_and_unicast() -> None:
    _, media, _ = a_media_persona()
    first = int(media.device_identity().mac[:2], 16)
    assert first & 0b10, "a locally administered address"
    assert not first & 0b1, "a unicast address"


# ----------------------------------------------------------------- media surface


def test_the_media_persona_advertises_no_ptz_scope() -> None:
    primary, media, _ = a_media_persona()
    assert any("type/ptz" in scope for scope in primary.scopes())
    assert not any("type/ptz" in scope for scope in media.scopes())


def test_the_media_persona_reports_its_own_identifiers() -> None:
    """Protect checks the identity before it adopts; it must see a new device."""
    _, media, _ = a_media_persona()
    ident = media.device_identity()
    assert text_of(ask(media, "GetDeviceInformation"), "SerialNumber") == ident.serial
    assert text_of(ask(media, "GetNetworkInterfaces"), "HwAddress") == ident.mac


def test_the_media_persona_cannot_move_the_camera() -> None:
    """No PTZ service, no PTZ configuration, and no movement."""
    _, media, recorder = a_media_persona()
    service_list = ask(media, "GetServices")
    namespaces = [
        (element.text or "").strip()
        for element in ElementTree.fromstring(service_list).iter()
        if onvif.local_name(element.tag) == "Namespace"
    ]
    assert "http://www.onvif.org/ver20/ptz/wsdl" not in namespaces
    profiles = media.handle(
        request("GetProfiles", namespace="trt", namespace_uri=onvif.NAMESPACES["trt"])
    )
    assert "PTZConfiguration" not in profiles
    move = media.handle(
        request("AbsoluteMove", namespace="tptz", namespace_uri=onvif.NAMESPACES["tptz"])
    )
    assert "PTZ is disabled" in move
    assert recorder.absolute == []  # type: ignore[attr-defined]


def test_the_media_persona_has_its_own_endpoint_reference() -> None:
    """A device is identified by its endpoint address as much as by its serial."""
    primary, media, _ = a_media_persona()
    assert (
        discovery.endpoint_for(primary.host, primary.port)
        != discovery.endpoint_for(media.host, media.port)
    )


def test_a_persona_must_be_a_boring_token() -> None:
    _, recorder = services()
    try:
        onvif.Services(recorder.backend(), host="10.0.0.1", port=8001, persona="a b")
    except ValueError as exc:
        assert "persona" in str(exc)
    else:  # pragma: no cover - the constructor must refuse this
        raise AssertionError("an unsafe persona was accepted")


# -------------------------------------------------------------------- discovery


def test_the_two_personas_are_announced_in_one_datagram() -> None:
    """Two ProbeMatches must fit a 1500-byte MTU, or they get fragmented."""
    primary = discovery.identity_for(
        host="192.0.2.2", port=8000, path=onvif.DEVICE_PATH, name="front gate", ptz=True
    )
    media = discovery.identity_for(
        host="192.0.2.2", port=8001, path=onvif.DEVICE_PATH,
        name="front gate (media)", ptz=False,
    )
    reply = discovery.probe_match(
        (primary, media), "urn:uuid:12345678-1234-1234-1234-123456789abc"
    )
    assert len(reply.encode()) <= discovery.MAX_MESSAGE_BYTES
    assert reply.count("<d:ProbeMatch>") == 2


def test_one_persona_still_gets_the_full_standard_message() -> None:
    """Nothing is given up unless it has to be."""
    primary = discovery.identity_for(
        host="192.0.2.2", port=8000, path=onvif.DEVICE_PATH, name="front gate", ptz=True
    )
    reply = discovery.probe_match(
        (primary,), "urn:uuid:12345678-1234-1234-1234-123456789abc"
    )
    assert "MetadataVersion" in reply
    assert "role/anonymous" in reply
    assert "tds:Device" in reply


def test_the_hello_of_two_personas_also_fits_one_datagram() -> None:
    primary = discovery.identity_for(
        host="192.0.2.2", port=8000, path=onvif.DEVICE_PATH, name="front gate", ptz=True
    )
    media = discovery.identity_for(
        host="192.0.2.2", port=8001, path=onvif.DEVICE_PATH,
        name="front gate (media)", ptz=False,
    )
    hello = discovery.announcement((primary, media))
    assert len(hello.encode()) <= discovery.MAX_MESSAGE_BYTES
    assert hello.count("<d:Hello>") == 2


def test_a_long_camera_name_still_fits_one_datagram() -> None:
    """A long name must not push the reply into IP fragments."""
    primary = discovery.identity_for(
        host="192.0.2.2", port=8000, path=onvif.DEVICE_PATH,
        name="Front Yard Driveway Camera 4K", ptz=True, hardware="Osprey UniFi G5 PTZ Camera",
    )
    media = discovery.identity_for(
        host="192.0.2.2", port=8001, path=onvif.DEVICE_PATH,
        name="Front Yard Driveway Camera 4K (media)", ptz=False,
        hardware="Osprey UniFi G5 PTZ Camera",
    )
    reply = discovery.probe_match(
        (primary, media), "urn:uuid:12345678-1234-1234-1234-123456789abc"
    )
    assert len(reply.encode()) <= discovery.MAX_MESSAGE_BYTES
    assert reply.count("<d:ProbeMatch>") == 2


def test_the_hardware_scope_is_given_up_before_the_message_is_fragmented() -> None:
    """The hardware scope only repeats the model, which the device reports itself."""
    primary = discovery.identity_for(
        host="192.0.2.2", port=8000, path=onvif.DEVICE_PATH,
        name="a very long camera name indeed", ptz=True,
        hardware="Osprey UniFi G5 PTZ Camera with a long model name",
    )
    media = discovery.identity_for(
        host="192.0.2.2", port=8001, path=onvif.DEVICE_PATH,
        name="a very long camera name indeed (media)", ptz=False,
        hardware="Osprey UniFi G5 PTZ Camera with a long model name",
    )
    reply = discovery.probe_match(
        (primary, media), "urn:uuid:12345678-1234-1234-1234-123456789abc"
    )
    assert len(reply.encode()) <= discovery.MAX_MESSAGE_BYTES
    assert "onvif://www.onvif.org/hardware/" not in reply
    # The scope is still matched against, so a client that asks for it is answered.
    payload = probe(scopes=primary.scopes[-2])
    matched = discovery.matching_identities(payload, (primary, media))
    assert matched == (primary, media)


# ------------------------------------------------------------------------ stack


def a_stack(tmp_path: Path) -> main.Stack:
    options = main.Options(
        host="127.0.0.1",
        cert=tmp_path / "stack.pem",
        name="osprey test",
        tracks=("video1",),
        control_port=0,
        ingest_port=0,
        snapshot_port=0,
        rtsp_port=0,
        onvif_port=0,
        onvif_read_only_port=0,
        discovery_port=0,
        announce=False,
    )
    stack = main.build(options)
    stack.start_servers()
    return stack


def test_the_stack_announces_the_media_persona_first(tmp_path: Path) -> None:
    """A client that takes the first match must get the one that cannot move."""
    stack = a_stack(tmp_path)
    try:
        media = stack.read_only_north
        assert media is not None, "the media persona should be serving"
        identities = stack.finder.identities
        assert len(identities) == 2
        assert identities[0].xaddr.endswith(f":{media.port}{onvif.DEVICE_PATH}")
        assert identities[1].xaddr.endswith(f":{stack.north.port}{onvif.DEVICE_PATH}")
        assert "onvif://www.onvif.org/type/ptz" not in identities[0].scope_text
    finally:
        stack.stop()


def test_the_primary_claims_ptz_once_a_camera_is_adopted(tmp_path: Path) -> None:
    """The tracker keeps the head; the recorder's view never gets one."""
    stack = a_stack(tmp_path)
    camera: FakeCamera | None = None
    try:
        port = control_port(stack)
        camera = FakeCamera(port)
        camera.send(envelope.HELLO, HELLO_PAYLOAD)
        camera.pump(3.0)
        identities = stack.finder.identities
        assert "onvif://www.onvif.org/type/ptz" in identities[1].scope_text
        assert "onvif://www.onvif.org/type/ptz" not in identities[0].scope_text
        assert identities[0].endpoint != identities[1].endpoint
    finally:
        if camera is not None:
            camera.close()
        stack.stop()


def test_the_stack_answers_a_probe_with_both_personas(tmp_path: Path) -> None:
    stack = a_stack(tmp_path)
    media = stack.read_only_north
    assert media is not None, "the media persona should be serving"
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(5)
    try:
        client.sendto(probe(), ("127.0.0.1", stack.finder.port))
        payload, _ = client.recvfrom(65536)
        body = payload.decode()
        assert body.count("<d:ProbeMatch>") == 2
        assert f":{stack.north.port}{onvif.DEVICE_PATH}" in body
        assert f":{media.port}{onvif.DEVICE_PATH}" in body
    finally:
        client.close()
        stack.stop()
