"""The read-only ONVIF persona must never advertise or execute PTZ."""

from __future__ import annotations

from xml.etree import ElementTree

import onvif

from test_onvif import ask, request, services


def test_read_only_persona_advertises_media_without_ptz() -> None:
    _, recorder = services()
    readonly = onvif.Services(recorder.backend(), host="10.0.0.1", port=8001, read_only=True)

    capabilities = ask(readonly, "GetCapabilities")
    assert "<tt:PTZ>" not in capabilities
    assert "<tt:Media>" in capabilities
    service_list = ask(readonly, "GetServices")
    namespaces = [
        (element.text or "").strip()
        for element in ElementTree.fromstring(service_list).iter()
        if onvif.local_name(element.tag) == "Namespace"
    ]
    assert "http://www.onvif.org/ver20/ptz/wsdl" not in namespaces
    profiles = readonly.handle(
        request("GetProfiles", namespace="trt", namespace_uri=onvif.NAMESPACES["trt"])
    )
    assert "PTZConfiguration" not in profiles

    move = readonly.handle(
        request("AbsoluteMove", namespace="tptz", namespace_uri=onvif.NAMESPACES["tptz"])
    )
    assert "PTZ is disabled" in move
