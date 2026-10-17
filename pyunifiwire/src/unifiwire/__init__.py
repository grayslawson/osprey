"""The wire UniFi cameras and controllers speak to each other.

One protocol, two ends. This package is the part that is identical whichever end
you are writing: the message envelope, the WebSocket framing, the `extendedFlv`
container, the HEVC bitstream, and the AMF0 metadata that identifies a stream.

    from unifiwire import envelope, flv, hevc, avc, ws

Nothing here decides policy. There is no notion of adoption, of settings, or of
what a camera should do when asked to move — those belong to whichever end you are
building. What is here is what the bytes mean, and every value in it was measured
against a real UniFi Protect controller and a real UVC G5 PTZ rather than guessed.

The two codecs the camera can push, HEVC and H.264, live in `hevc` and `avc`; they
share a parameter-set surface so a consumer can hold either without special-casing.

See `SPEC.md` for the format itself, written out.
"""

from __future__ import annotations

from . import amf, annexb, avc, certs, discovery, envelope, flv, hevc, ws, wsclient

__all__ = [
    "amf",
    "annexb",
    "avc",
    "certs",
    "discovery",
    "envelope",
    "flv",
    "hevc",
    "ws",
    "wsclient",
]
__version__ = "0.1.0"
