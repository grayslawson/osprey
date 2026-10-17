# pyunifiwire

**The wire protocol UniFi Protect cameras and controllers speak to each other.**
Measured off a real controller and a real camera, not guessed.

```python
from unifiwire import envelope, flv, hevc, ws

message = envelope.decode(frame_payload)      # what the camera just said
message.function_name                          # 'ubnt_avclient_hello'

for tag in flv.Deframer().feed(chunk):         # the push stream, tag by tag
    if tag.kind is flv.TagType.VIDEO and tag.is_keyframe:
        units = hevc.split_nalus(hevc.video_packet(tag.body).payload)
```

This is the part that is identical whichever end you are building. It has no
opinion about adoption, settings, or what a camera should do when told to move —
those belong to the thing you are writing. What is here is what the bytes mean.

## Why it exists

UniFi cameras do not speak ONVIF or RTSP to their controller. They speak a private
protocol: a JSON envelope over a TLS WebSocket, and video pushed over plain TCP in
an FLV variant with **20 bytes between tags instead of 4** and **HEVC under codec
id 8**, which no standard demuxer maps. Every project that has needed to talk to
one has re-derived that from scratch.

Two implementations use this package as their only shared code — a controller that
isn't there and a camera that isn't there — and their test suites read each
other's output through it. Anything wrong here fails on both sides at once.

## Install

```sh
pip install pyunifiwire
```

Python 3.13+. **No dependencies** — standard library only, and typed throughout
(`py.typed`, `mypy --strict` clean).

## What's in it

| Module | What it covers |
|---|---|
| `envelope` | The JSON envelope every control message rides in: `functionName`, `messageId`, `inResponseTo`, sender and recipient |
| `ws` | RFC 6455 framing and the server side of the upgrade — enough to accept a camera without a WebSocket library |
| `wsclient` | The dialling side: the handshake a camera sends, header for header, and masked frames |
| `flv` | The `extendedFlv` container — flags `0x07`, the 16-byte inter-tag trailer, tag parsing that survives split reads |
| `hevc` | `hvcC` parameter sets, length-prefixed NAL units, and the AAC `AudioSpecificConfig` |
| `annexb` | Annex B ↔ `hvcC`, for turning an ordinary `.h265` file into something a controller accepts |
| `amf` | Enough AMF0 to write the metadata tag that identifies a stream |
| `discovery` | The UDP 10001 probe and the TLV answer a device gives before adoption |
| `certs` | A self-signed certificate, which both ends need and neither validates |

[`SPEC.md`](SPEC.md) writes out the format itself: the envelope, the framing, the
container, and the metadata — with the parts that trip people up called out.

## What it is not

- Not a UniFi Protect **API** client. For the controller's HTTP API see
  [`uiprotect`](https://github.com/uilibs/uiprotect) or
  [`pyunifi`](https://github.com/finish06/pyunifi) — different protocol entirely.
- Not a way to get a third-party camera into Protect. See
  [`unifi-cam-proxy`](https://github.com/keshavdv/unifi-cam-proxy).
- Not affiliated with or endorsed by Ubiquiti Inc.

## Testing

```sh
./test.sh          # mypy --strict, then pytest. No network, no hardware.
```

## Licence

MIT — see [`LICENSE`](LICENSE).
