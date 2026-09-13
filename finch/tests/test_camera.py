"""finch's identity, handshake and answers. No controller, no network.

The strongest tests here are the ones that run finch's output through cuckoo's
input: the two halves have to agree byte for byte, and neither is allowed to be
the definition of correct on its own.
"""

from __future__ import annotations

from typing import Any

import adopt
import camera as camera_module
import identity
import replies
from unifiwire import ws
from unifiwire import wsclient
from unifiwire.envelope import CAMERA, CONTROLLER, HELLO, PARAM_AGREEMENT, TIME_SYNC, Envelope, Ids, decode


def a_camera(token: str = "tok") -> camera_module.Camera:
    return camera_module.Camera(who=identity.Identity(), token=token)


def from_controller(name: str, payload: dict[str, Any], mid: int = 5) -> Envelope:
    return Envelope(
        function_name=name,
        payload=dict(payload),
        message_id=mid,
        sender=CONTROLLER,
        recipient=CAMERA,
    )


# ---------------------------------------------------------------------- identity


def test_we_are_a_g5_ptz_with_the_real_devices_numbers() -> None:
    who = identity.Identity()
    hello = who.hello("tok", "10.0.0.1")
    assert hello["model"] == "UVC G5 PTZ"
    assert hello["platform"] == "sav530q"
    assert hello["fwVersion"] == identity.FIRMWARE
    features = hello["features"]
    assert features["pan"]["steps"] == {"min": 500, "max": 35500, "step": 9}
    assert features["tilt"]["steps"]["max"] == 18000
    assert features["zoom"]["steps"]["max"] == 730


def test_our_mac_looks_like_a_camera_and_can_be_moved_out_of_the_way() -> None:
    """It has to be a Ubiquiti OUI to be plausible, and overridable so it never
    collides with a real device on the same network."""
    assert identity.DEFAULT_MAC.startswith(identity.UBIQUITI_OUI)
    assert len(identity.DEFAULT_MAC) == 12
    assert identity.Identity(mac="AABBCCDDEEFF").mac_colons == "aa:bb:cc:dd:ee:ff"


def test_hello_quotes_the_adoption_token() -> None:
    hello = identity.Identity().hello("token-here", "10.0.0.1")
    assert hello["adoptionCode"] == "token-here"


def test_channels_match_the_real_camera() -> None:
    assert [c.track for c in identity.CHANNELS] == ["video1", "video2", "video3"]
    high = identity.channel("video1")
    assert high is not None and (high.width, high.height, high.fps) == (2688, 1512, 30)
    assert high.channel_id == 0 and high.stream_id == 1


# --------------------------------------------------------------------- handshake


def test_upgrade_request_carries_token_subprotocol_and_mac() -> None:
    raw = wsclient.upgrade_request(
        "10.0.0.1", 7442, ws.CONTROL_PATH, "a-key", "AABBCCDDEEFF", token="tok", model="UVC G5 PTZ"
    ).decode()
    assert raw.startswith(f"GET {ws.CONTROL_PATH}?token=tok HTTP/1.1")
    assert "Sec-WebSocket-Protocol: secure_transfer" in raw
    assert "camera-mac: AABBCCDDEEFF" in raw
    assert "camera-model: UVC G5 PTZ" in raw


def test_cuckoo_can_read_our_upgrade_request() -> None:
    """The two halves have to agree: finch writes it, cuckoo parses it."""
    raw = wsclient.upgrade_request(
        "10.0.0.1", 7442, ws.CONTROL_PATH, wsclient.client_key(), "AABBCCDDEEFF",
        token="tok", model="UVC G5 PTZ", firmware="5.3.95",
    )
    upgrade = ws.parse_upgrade(raw)
    assert upgrade.camera_mac == "AABBCCDDEEFF"
    assert upgrade.camera_model == "UVC G5 PTZ"
    assert upgrade.camera_firmware == "5.3.95"
    assert upgrade.subprotocol == wsclient.SUBPROTOCOL
    assert not upgrade.already_adopted


def test_we_accept_the_answer_cuckoo_would_give() -> None:
    key = wsclient.client_key()
    raw = wsclient.upgrade_request("h", 1, ws.CONTROL_PATH, key, "AABBCCDDEEFF")
    response = ws.handshake_response(ws.parse_upgrade(raw))
    accepted = wsclient.read_response(response, key)
    assert accepted.status == 101 and accepted.subprotocol == wsclient.SUBPROTOCOL


def test_a_wrong_accept_key_is_refused() -> None:
    key = wsclient.client_key()
    raw = wsclient.upgrade_request("h", 1, ws.CONTROL_PATH, "someone-elses-key", "AABBCCDDEEFF")
    response = ws.handshake_response(ws.parse_upgrade(raw))
    try:
        wsclient.read_response(response, key)
        raise AssertionError("should not have accepted a mismatched key")
    except wsclient.HandshakeError:
        pass


def test_a_refusal_is_reported_with_its_status() -> None:
    try:
        wsclient.read_response(b"HTTP/1.1 403 Forbidden\r\n\r\n", "k")
        raise AssertionError("403 should raise")
    except wsclient.HandshakeError as exc:
        assert "403" in str(exc)


def test_our_frames_are_masked_and_cuckoo_can_read_them() -> None:
    """A client that does not mask is disconnected by a strict server."""
    payload = b'{"hello":true}'
    frame = ws.encode_frame(payload, ws.Opcode.BINARY, mask=True)
    assert frame[1] & 0x80, "mask bit must be set"
    assert payload not in frame, "the body must actually be masked"
    reader = ws.FrameReader()
    assert [f.payload for f in reader.feed(frame)] == [payload]


# ------------------------------------------------------------------- the answers


def test_hello_reply_marks_us_adopted_and_needs_no_answer() -> None:
    cam = a_camera()
    out = cam.handle(
        from_controller(HELLO, {"controllerName": "emu", "controllerVersion": "7.1.77",
                                "controllerUuid": None, "overrideUuid": True})
    )
    assert out == [], "a reply to our own hello is not answered again"
    assert cam.adopted and cam.controller_version == "7.1.77"


def test_param_agreement_hands_the_token_back() -> None:
    cam = a_camera(token="tok")
    out = cam.handle(from_controller(PARAM_AGREEMENT, {"enableStatusCodes": True}, mid=9))
    assert len(out) == 1
    assert out[0].in_response_to == 9
    assert out[0].payload["authToken"] == "tok"


def test_every_settings_message_is_acknowledged_by_id() -> None:
    cam = a_camera()
    for name in (
        camera_module.CHANGE_ISP, camera_module.CHANGE_OSD, camera_module.CHANGE_DEVICE,
        camera_module.CHANGE_SOUND_LED, camera_module.NETWORK_STATUS,
    ):
        out = cam.handle(from_controller(name, {}, mid=42))
        assert len(out) == 1 and out[0].in_response_to == 42, name
        assert out[0].function_name == name, "a reply echoes the verb"
        assert out[0].sender == CAMERA


def test_an_unknown_verb_is_still_acknowledged() -> None:
    """Silence stalls the controller's suite; an echo keeps it moving."""
    cam = a_camera()
    out = cam.handle(from_controller("ChangeSomethingNew", {"a": 1}, mid=7))
    assert len(out) == 1 and out[0].payload == {"a": 1}


def test_isp_reply_echoes_what_was_set() -> None:
    cam = a_camera()
    out = cam.handle(from_controller(camera_module.CHANGE_ISP, {"brightness": 12, "wdr": 0}))
    assert out[0].payload["brightness"] == 12 and out[0].payload["wdr"] == 0
    assert "aeMode" in out[0].payload, "unasked fields are still reported"


def test_video_settings_arm_a_track_and_report_the_destination() -> None:
    cam = a_camera()
    started: list[camera_module.Stream] = []
    cam.on_stream_start = started.append
    out = cam.handle(
        from_controller(camera_module.CHANGE_VIDEO, {
            "video": {"video1": {"avSerializer": {
                "type": "extendedFlv",
                "parameters": {"streamName": "video1"},
                "destinations": ["tcp://10.0.0.1:7550?retryInterval=1"],
            }}},
        })
    )
    assert [s.track for s in started] == ["video1"]
    assert started[0].host_port == ("10.0.0.1", 7550)
    assert out[0].payload["video"]["video1"]["streaming"] is True
    assert out[0].payload["video"]["video1"]["width"] == 2688


def test_an_empty_destination_list_stops_the_track() -> None:
    cam = a_camera()
    stopped: list[str] = []
    cam.on_stream_stop = stopped.append
    cam.handle(from_controller(camera_module.CHANGE_VIDEO, {
        "video": {"video1": {"avSerializer": {"destinations": ["tcp://h:7550"]}}}}))
    cam.handle(from_controller(camera_module.CHANGE_VIDEO, {
        "video": {"video1": {"avSerializer": {"destinations": []}}}}))
    assert stopped == ["video1"] and cam.streams == {}


def test_video_reply_reports_every_channel_not_only_the_armed_one() -> None:
    cam = a_camera()
    out = cam.handle(from_controller(camera_module.CHANGE_VIDEO, {"video": {}}))
    assert set(out[0].payload["video"]) >= {"video1", "video2", "video3"}


def test_snapshot_request_hands_over_the_upload_url() -> None:
    cam = a_camera()
    asked: list[tuple[str, str]] = []
    cam.on_snapshot = lambda what, uri: asked.append((what, uri))
    out = cam.handle(from_controller(camera_module.GET_REQUEST, {
        "what": "snapshot", "uri": "https://10.0.0.1:7444/internal/camera-upload/xyz"}))
    assert asked == [("snapshot", "https://10.0.0.1:7444/internal/camera-upload/xyz")]
    assert len(out) == 1, "the request is acknowledged as well as acted on"


def test_ptz_enable_records_the_callback_url() -> None:
    cam = a_camera()
    seen: list[str] = []
    cam.on_ptz_requested = seen.append
    cam.handle(from_controller(camera_module.ENABLE_PTZ, {"uri": "wss://10.0.0.1/camera/1.0/ws"}))
    assert seen == ["wss://10.0.0.1/camera/1.0/ws"]
    assert cam.ptz_uri.endswith("/camera/1.0/ws")


def test_time_sync_takes_an_offset_rather_than_setting_a_clock() -> None:
    cam = a_camera()
    out = cam.handle(from_controller(TIME_SYNC, {"t1": 4_000_000_000_000, "t2": 4_000_000_000_000}))
    assert out[0].function_name == TIME_SYNC
    assert cam.time_offset_ms > 0, "the controller is ahead of us in this fixture"


def test_firmware_updates_are_acknowledged_but_never_acted_on() -> None:
    cam = a_camera()
    out = cam.handle(from_controller(camera_module.UPDATE_FIRMWARE, {"uri": "http://x/fw.bin"}))
    assert len(out) == 1 and out[0].payload == {}


def test_our_own_messages_are_ignored() -> None:
    cam = a_camera()
    mine = Envelope(function_name=HELLO, payload={}, message_id=1, sender=CAMERA)
    assert cam.handle_bytes(mine.to_json()) == []


def test_a_malformed_frame_does_not_end_the_session() -> None:
    cam = a_camera()
    assert cam.handle_bytes(b"\xff not json") == []
    assert cam.handle_bytes(from_controller(PARAM_AGREEMENT, {}).to_json()) != []


def test_what_we_send_is_what_cuckoo_reads() -> None:
    """finch encodes, cuckoo decodes — the round trip has to survive intact."""
    cam = a_camera(token="tok")
    hello = cam.hello("10.0.0.1")
    parsed = decode(hello.to_json())
    assert parsed.function_name == HELLO
    assert parsed.sender == CAMERA and parsed.recipient == CONTROLLER
    assert parsed.payload["features"]["pan"]["steps"]["max"] == 35500


def test_message_ids_do_not_repeat() -> None:
    cam = a_camera()
    ids = [cam.message("X", {}).message_id for _ in range(5)]
    assert len(set(ids)) == 5 and ids == sorted(ids)


# ------------------------------------------------------------------- the token


def test_management_block_is_read_from_the_nested_mgmt_key() -> None:
    """The token is under `mgmt`, not at the top level, which is easy to get wrong."""
    management = adopt.parse_management(
        {"wifi": {}, "mgmt": {"protocol": "wss", "hosts": ["10.0.0.1:7442"], "token": "abc"}}
    )
    assert management.token == "abc"
    assert management.first_host == ("10.0.0.1", 7442)


def test_a_payload_without_a_token_is_an_error_not_an_empty_string() -> None:
    bodies: list[dict[str, Any]] = [{}, {"mgmt": {}}, {"mgmt": {"token": ""}}]
    for body in bodies:
        try:
            adopt.parse_management(body)
            raise AssertionError(f"should have refused {body}")
        except adopt.AdoptionError:
            pass


def test_replies_module_reports_sixteen_kilohertz_aac() -> None:
    """What we claim in settings has to match what we actually push."""
    reported = replies.video_settings({}, {})
    assert reported["audio"]["sampleRate"] == 16_000
    assert reported["audio"]["type"] == "aac"


def test_ids_start_apart_from_the_controllers() -> None:
    assert Ids().next() >= 10_000
