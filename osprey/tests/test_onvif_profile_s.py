"""Profile S surface, the auth/role matrix, principals, snapshot tokens and Stop.

Covers the multi-principal ONVIF work: pre-auth operations, per-role authorization
through ``Services.dispatch``, the users-file parser, discovery interface choice,
bounded pull-point subscriptions and a real PTZ Stop. No camera, no sockets.
"""

from __future__ import annotations

import base64
import ipaddress
import hashlib
import secrets
import time
from http import HTTPStatus
from xml.etree import ElementTree

import discovery
import onvif
import pytest
from principals import Principal, UserDirectory
from tests_support import a_camera

PASSWORDS = {"frigate": "frigate-pass", "protect": "protect-pass", "admin": "admin-pass"}
PROTECT_PEER = "198.51.100.7"  # documentation range, not a real address

USERS = UserDirectory.parse(
    {
        "users": [
            {"username": "frigate", "password": PASSWORDS["frigate"], "roles": ["media", "events", "ptz"]},
            {"username": "protect", "password": PASSWORDS["protect"], "roles": ["media", "events"],
             "allow_from": ["198.51.100.0/24"]},
            {"username": "admin", "password": PASSWORDS["admin"], "roles": ["admin"]},
        ]
    }
)

# Pre-auth operations ONVIF Core lets any client call before it authenticates.
EXPECTED_PRE_AUTH = {
    "GetSystemDateAndTime", "GetCapabilities", "GetServices", "GetWsdlUrl",
    "GetEndpointReference", "GetServiceCapabilities",
}


class Recorder:
    def __init__(self) -> None:
        self.camera = a_camera()
        self.stops: list[tuple[bool, bool]] = []

    def backend(self) -> onvif.Backend:
        return onvif.Backend(
            camera=lambda: self.camera,
            stream_uri=lambda token: f"rtsp://192.0.2.10:8554/{token}",
            snapshot_uri=lambda token: f"http://192.0.2.10:8000/snapshot/{token}",
            snapshot=lambda: b"\xff\xd8jpeg",
            stop=self._stop,
        )

    def _stop(self, pan_tilt: bool, zoom: bool) -> bool:
        self.stops.append((pan_tilt, zoom))
        return True


def service(*, configured: bool = True, snapshot_interval: float = 0.0) -> tuple[onvif.Services, Recorder]:
    recorder = Recorder()
    auth = onvif.OnvifAuth(users=USERS) if configured else onvif.OnvifAuth()
    return (
        onvif.Services(recorder.backend(), host="192.0.2.10", port=8000, onvif_auth=auth,
                       snapshot_interval=snapshot_interval),
        recorder,
    )


def signed(username: str, password: str, body: str) -> bytes:
    """A SOAP request carrying a WS-Security UsernameToken with a fresh nonce."""
    nonce = secrets.token_bytes(16)
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
    nonce_b64 = base64.b64encode(nonce).decode()
    header = (
        "<s:Header>"
        '<wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
        "<wsse:UsernameToken>"
        f"<wsse:Username>{username}</wsse:Username>"
        f'<wsse:Password Type="...#PasswordDigest">{digest}</wsse:Password>'
        f'<wsse:Nonce EncodingType="...#Base64Binary">{nonce_b64}</wsse:Nonce>'
        '<wsu:Created xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
        f"{created}</wsu:Created>"
        "</wsse:UsernameToken></wsse:Security></s:Header>"
    )
    # The Header sits beside the Body, as in a real envelope, not inside it.
    return onvif.envelope(body).replace("<s:Body>", header + "<s:Body>", 1).encode()


def unsigned(body: str) -> bytes:
    return onvif.envelope(body).encode()


def call_as(svc: onvif.Services, username: str | None, body: str, peer: str = PROTECT_PEER) -> tuple[HTTPStatus, str]:
    payload = unsigned(body) if username is None else signed(username, PASSWORDS[username], body)
    return svc.dispatch(payload, peer=peer)


GET_PROFILES = '<trt:GetProfiles xmlns:trt="http://www.onvif.org/ver10/media/wsdl"/>'
GET_DEVICE_INFO = '<tds:GetDeviceInformation xmlns:tds="http://www.onvif.org/ver10/device/wsdl"/>'
RELATIVE_MOVE = (
    '<tptz:RelativeMove xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl">'
    '<tptz:ProfileToken>video1</tptz:ProfileToken><tptz:Translation>'
    '<tt:PanTilt xmlns:tt="http://www.onvif.org/ver10/schema" x="0.1" y="0"/></tptz:Translation>'
    '</tptz:RelativeMove>'
)
STOP = (
    '<tptz:Stop xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl">'
    '<tptz:ProfileToken>video1</tptz:ProfileToken>'
    '<tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>false</tptz:Zoom></tptz:Stop>'
)
GET_STATUS = (
    '<tptz:GetStatus xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl">'
    '<tptz:ProfileToken>video1</tptz:ProfileToken></tptz:GetStatus>'
)


# ------------------------------------------------------------- allow-list

def test_only_the_onvif_core_operations_are_pre_auth() -> None:
    pre_auth = {op.action for op in onvif.OPERATIONS if op.role is onvif.PRE_AUTH}
    assert pre_auth <= EXPECTED_PRE_AUTH


def test_every_allow_listed_operation_has_a_handler() -> None:
    for operation in onvif.OPERATIONS:
        assert callable(getattr(onvif.Services, operation.handler, None)), operation


def test_unlisted_private_methods_are_not_reachable_by_name() -> None:
    svc, _ = service(configured=False)
    status, body = svc.dispatch(
        unsigned('<tds:_device_capabilities_xml xmlns:tds="http://www.onvif.org/ver10/device/wsdl"/>'),
    )
    assert status != HTTPStatus.OK
    assert "not implemented" in body


# ------------------------------------------------------------- auth matrix

def test_pre_auth_operations_answer_without_credentials() -> None:
    svc, _ = service()
    status, body = svc.dispatch(
        unsigned('<tds:GetSystemDateAndTime xmlns:tds="http://www.onvif.org/ver10/device/wsdl"/>')
    )
    assert status == HTTPStatus.OK
    assert "Fault" not in body


def test_media_calls_need_credentials() -> None:
    svc, _ = service()
    status, body = svc.dispatch(unsigned(GET_PROFILES))
    assert status == HTTPStatus.UNAUTHORIZED
    assert "NotAuthorized" in body


def test_a_wrong_password_is_refused() -> None:
    svc, _ = service()
    status, _ = svc.dispatch(signed("protect", "not-the-password", GET_PROFILES))
    assert status == HTTPStatus.UNAUTHORIZED


def test_media_role_can_read_media_but_not_move_the_head() -> None:
    svc, recorder = service()
    status, _ = call_as(svc, "protect", GET_PROFILES)
    assert status == HTTPStatus.OK
    status, body = call_as(svc, "protect", RELATIVE_MOVE)
    assert status == HTTPStatus.BAD_REQUEST
    assert "NotAuthorized" in body
    assert recorder.stops == []


def test_ptz_role_can_read_status_and_stop() -> None:
    svc, recorder = service()
    status, _ = call_as(svc, "frigate", GET_STATUS)
    assert status == HTTPStatus.OK
    status, body = call_as(svc, "frigate", STOP)
    assert status == HTTPStatus.OK
    assert "StopResponse" in body
    assert recorder.stops == [(True, False)]


def test_only_admin_may_write_encoder_configuration() -> None:
    svc, _ = service()
    write = '<trt:SetVideoEncoderConfiguration xmlns:trt="http://www.onvif.org/ver10/media/wsdl"/>'
    for user in ("frigate", "protect"):
        status, body = call_as(svc, user, write)
        assert status == HTTPStatus.BAD_REQUEST, user
        assert "NotAuthorized" in body


def test_any_authenticated_principal_reads_device_information() -> None:
    svc, _ = service()
    status, body = call_as(svc, "protect", GET_DEVICE_INFO)
    assert status == HTTPStatus.OK
    assert "GetDeviceInformationResponse" in body


def test_an_unconfigured_endpoint_stays_anonymous() -> None:
    svc, _ = service(configured=False)
    status, _ = svc.dispatch(unsigned(GET_PROFILES))
    assert status == HTTPStatus.OK


def test_allow_from_refuses_a_principal_on_another_network() -> None:
    svc, _ = service()
    status, _ = call_as(svc, "protect", GET_PROFILES, peer="203.0.113.9")
    assert status == HTTPStatus.UNAUTHORIZED


# ------------------------------------------------------------- profile S surface

PROFILE_S_READS = [
    ("tds", "GetHostname", ""),
    ("tds", "GetDNS", ""),
    ("tds", "GetNTP", ""),
    ("tds", "GetNetworkInterfaces", ""),
    ("tds", "GetNetworkProtocols", ""),
    ("tds", "GetNetworkDefaultGateway", ""),
    ("tds", "GetDiscoveryMode", ""),
    ("tds", "GetScopes", ""),
    ("trt", "GetVideoSources", ""),
    ("trt", "GetVideoSourceConfigurations", ""),
    ("trt", "GetVideoEncoderConfigurations", ""),
    ("trt", "GetVideoEncoderConfigurationOptions", "<trt:ConfigurationToken>video1</trt:ConfigurationToken>"),
    ("trt", "GetSnapshotUri", "<trt:ProfileToken>video1</trt:ProfileToken>"),
]
NAMESPACE_URIS = {
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
}


@pytest.mark.parametrize(("prefix", "action", "inner"), PROFILE_S_READS)
def test_profile_s_reads_return_well_formed_xml(prefix: str, action: str, inner: str) -> None:
    svc, _ = service()
    body = f'<{prefix}:{action} xmlns:{prefix}="{NAMESPACE_URIS[prefix]}">{inner}</{prefix}:{action}>'
    status, text = call_as(svc, "admin", body)
    assert status == HTTPStatus.OK, text
    root = ElementTree.fromstring(text)  # raises on malformed XML
    assert root is not None


def test_network_interfaces_do_not_report_the_camera_mac() -> None:
    svc, _ = service()
    body = '<tds:GetNetworkInterfaces xmlns:tds="http://www.onvif.org/ver10/device/wsdl"/>'
    _, text = call_as(svc, "admin", body)
    assert "AABBCCDDEEFF" not in text.upper().replace(":", "").replace("-", "")


def test_video_encoder_options_include_rate_control_and_h264() -> None:
    svc, _ = service()
    body = ('<trt:GetVideoEncoderConfigurations xmlns:trt="http://www.onvif.org/ver10/media/wsdl"/>')
    _, text = call_as(svc, "admin", body)
    assert "RateControl" in text
    assert "H264" in text


# ------------------------------------------------------------- snapshots

def test_snapshot_refuses_anonymous_requests_when_auth_is_configured() -> None:
    svc, _ = service()
    status, body, _ = svc.snapshot_response("video1")
    assert status == HTTPStatus.UNAUTHORIZED
    assert body == b"authentication required"


def test_snapshot_token_from_get_snapshot_uri_is_accepted() -> None:
    svc, _ = service()
    _, text = call_as(svc, "protect", '<trt:GetSnapshotUri xmlns:trt="http://www.onvif.org/ver10/media/wsdl">'
                                       "<trt:ProfileToken>video1</trt:ProfileToken></trt:GetSnapshotUri>")
    assert "auth=" in text
    token = svc.snapshot_token("protect", "video1")
    status, image, headers = svc.snapshot_response("video1", query_token=token)
    assert status == HTTPStatus.OK
    assert image.startswith(b"\xff\xd8")
    assert headers["Cache-Control"] == "no-store"


def test_snapshot_token_is_bound_to_its_profile() -> None:
    svc, _ = service()
    token = svc.snapshot_token("protect", "video1")
    status, _, _ = svc.snapshot_response("video2", query_token=token)
    assert status == HTTPStatus.UNAUTHORIZED


def test_snapshot_token_from_one_user_does_not_authenticate_another() -> None:
    svc, _ = service()
    token = svc.snapshot_token("frigate", "video1").replace("frigate.", "protect.", 1)
    status, _, _ = svc.snapshot_response("video1", query_token=token)
    assert status == HTTPStatus.UNAUTHORIZED


def test_snapshots_stay_open_when_auth_is_not_configured() -> None:
    svc, _ = service(configured=False)
    status, _, _ = svc.snapshot_response("video1")
    assert status == HTTPStatus.OK


# ------------------------------------------------------------- principals

def test_a_user_without_roles_is_an_admin() -> None:
    directory = UserDirectory.parse({"users": [{"username": "solo", "password": "pw-solo-1"}]})
    user = directory.get("solo")
    assert user is not None
    assert user.has("ptz") and user.has("media") and user.has("admin")


def test_roles_limit_what_a_principal_may_do() -> None:
    user = USERS.get("protect")
    assert user is not None
    assert user.has("media") and user.has("events")
    assert not user.has("ptz")
    assert not user.has("admin")
    assert user.has("user")  # ANY_USER: every authenticated principal


def test_unknown_roles_and_keys_are_rejected() -> None:
    with pytest.raises(ValueError, match="unknown roles"):
        UserDirectory.parse({"users": [{"username": "x", "password": "pw-x-123", "roles": ["root"]}]})
    with pytest.raises(ValueError, match="unknown keys"):
        UserDirectory.parse({"users": [{"username": "x", "password": "pw-x-123", "scope": "all"}]})


def test_duplicate_usernames_are_rejected() -> None:
    entry = {"username": "dup", "password": "pw-dup-123"}
    with pytest.raises(ValueError, match="duplicates"):
        UserDirectory.parse({"users": [entry, dict(entry)]})


def test_usernames_are_restricted_to_safe_characters() -> None:
    with pytest.raises(ValueError, match="username"):
        UserDirectory.parse({"users": [{"username": "bad user", "password": "pw-bad-123"}]})


def test_password_must_come_from_exactly_one_source() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        UserDirectory.parse({"users": [{"username": "x", "password": "a", "password_file": "/p"}]})


def test_allow_from_matches_networks_only() -> None:
    principal = Principal(
        "p", "pw", frozenset({"media"}), allow_from=(ipaddress.ip_network("198.51.100.0/24"),),
    )
    assert principal.permits_peer("198.51.100.20")
    assert not principal.permits_peer("203.0.113.20")
    assert Principal("open", "pw").permits_peer("203.0.113.20")


def test_single_credential_form_keeps_working() -> None:
    auth = onvif.OnvifAuth("frigate", "correct horse")
    result = auth.authenticate(signed("frigate", "correct horse", GET_PROFILES))
    assert result.ok
    assert result.principal is not None and result.principal.has("ptz")


def test_replay_protection_can_be_switched_off_per_principal() -> None:
    directory = UserDirectory.parse(
        {"users": [{"username": "legacy", "password": "pw-legacy-1", "replay_protection": False}]}
    )
    auth = onvif.OnvifAuth(users=directory)
    payload = signed("legacy", "pw-legacy-1", GET_PROFILES)
    assert auth.authenticate(payload).ok
    assert auth.authenticate(payload).ok


# ------------------------------------------------------------- discovery binding

def test_discovery_uses_an_explicit_address_first() -> None:
    assert discovery.multicast_address("198.51.100.5", None, "0.0.0.0", "192.0.2.10") == "198.51.100.5"


def test_discovery_falls_back_to_the_advertised_host() -> None:
    assert discovery.multicast_address(None, None, "0.0.0.0", "192.0.2.10") == "192.0.2.10"


def test_discovery_ignores_wildcard_and_loopback_binds() -> None:
    assert discovery.multicast_address(None, None, "0.0.0.0", "0.0.0.0") is None
    assert discovery.multicast_address(None, None, "127.0.0.1", None) is None


def test_discovery_with_an_unknown_interface_falls_back_quietly() -> None:
    assert discovery.multicast_address(None, "osprey-no-such-if", None, "192.0.2.10") == "192.0.2.10"


# ------------------------------------------------------------- subscriptions

def test_subscription_ttl_is_clamped() -> None:
    assert onvif.Subscriptions.clamp_ttl(1) == onvif.Subscriptions.MIN_TTL
    assert onvif.Subscriptions.clamp_ttl(10**9) == onvif.Subscriptions.MAX_TTL
    assert onvif.Subscriptions.clamp_ttl(None) == onvif.Subscriptions.DEFAULT_TTL


def test_one_owner_cannot_hold_more_than_its_share() -> None:
    subs = onvif.Subscriptions(clock=lambda: 1000.0)
    made = [subs.create("user:protect") for _ in range(onvif.Subscriptions.MAX_PER_OWNER + 2)]
    assert all(made)
    assert len(subs.owned_by("user:protect")) == onvif.Subscriptions.MAX_PER_OWNER


def test_the_table_is_capped_globally() -> None:
    subs = onvif.Subscriptions(clock=lambda: 1000.0)
    results = [subs.create(f"peer:{index}") for index in range(onvif.Subscriptions.MAX_TOTAL + 1)]
    assert results[-1] is None
    assert subs.count == onvif.Subscriptions.MAX_TOTAL
