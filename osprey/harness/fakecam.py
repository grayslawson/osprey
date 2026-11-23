"""A stand-in G5 PTZ for running the setup wizard end to end without hardware.

It does the three things the wizard touches on a real camera:

* answers UniFi discovery on UDP 10001 with a G5 PTZ's fields;
* serves the management API on HTTPS 443 (``/api/1.2/login`` with
  ``ubnt``/``ubnt``, then ``/api/1.2/manage``);
* after the manage call, dials the controller it was given, says hello, acks
  every settings message and pushes the H.264 file to each armed track.

Run it in its own container next to Osprey on an isolated network::

    ffmpeg -f lavfi -i testsrc2=size=640x360:rate=15 -t 4 -c:v libx264 \\
           -bsf:v h264_mp4toannexb -f h264 /tmp/t.h264
    python3 fakecam.py --ip 10.89.0.20 --media /tmp/t.h264

Never point it at a real network: it answers every discovery probe it hears.
Like stubcam.py, it is a development tool and is not shipped.
"""

from __future__ import annotations

import argparse
import json
import socket
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import stubcam
from unifiwire import certs, envelope, ws
from unifiwire import discovery as wire

HELLO: dict[str, Any] = {
    "fwVersion": "UVC.SAV530q.v5.5.13.67.c2f8b94.260907.1843",
    "lensmodel": "UVC G5 PTZ",
    "features": {
        "pan": {"steps": {"min": 500, "max": 35500}},
        "tilt": {"steps": {"min": 8000, "max": 18000}},
        "zoom": {"steps": {"min": 0, "max": 730}},
    },
}


class Camera:
    def __init__(self, mac: str, ip: str, media: Path, managed: bool, password: str) -> None:
        self.mac, self.ip, self.media, self.managed, self.password = mac, ip, media, managed, password
        self.pushing: set[str] = set()

    # ------------------------------------------------------------ discovery

    def answer_discovery(self) -> None:
        reply = wire.announce(self.mac, self.ip, "G5 PTZ", "UVC G5 PTZ", HELLO["fwVersion"], 0xA59B)
        flag = (0 if self.managed else 1).to_bytes(4, "big")
        reply.fields = [wire.Field(f.kind, flag) if f.kind == wire.MGMT_IS_DEFAULT else f for f in reply.fields]
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", wire.PORT))  # all interfaces: broadcasts arrive on the wildcard
        while True:
            datagram, sender = sock.recvfrom(1500)
            if wire.is_probe(datagram):
                print(f"fakecam: discovery probe from {sender[0]}", file=sys.stderr)
                sock.sendto(reply.to_bytes(), sender)

    # ------------------------------------------------------ control channel

    def adopt(self, host: str, port: int) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        for _ in range(30):
            try:
                sock = context.wrap_socket(socket.create_connection((host, port), timeout=5))
                break
            except OSError:
                time.sleep(1)
        else:
            print(f"fakecam: cannot reach the controller at {host}:{port}", file=sys.stderr)
            return
        sock.sendall(
            b"GET /camera/1.0/ws HTTP/1.1\r\nUpgrade: websocket\r\nConnection: close, Upgrade\r\n"
            b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Protocol: secure_transfer\r\n"
            + f"camera-mac: {self.mac.replace(':', '').upper()}\r\n".encode()
            + b"camera-model: UVC G5 PTZ\r\nadopted: false\r\n\r\n"
        )
        if b"101" not in sock.recv(1024):
            print("fakecam: the controller refused the upgrade", file=sys.stderr)
            return
        ids = iter(range(1001, 10**9))

        def send(name: str, payload: dict[str, Any], reply_to: int = 0) -> None:
            message = envelope.Envelope(function_name=name, payload=payload, message_id=next(ids),
                                        in_response_to=reply_to, sender=envelope.CAMERA,
                                        recipient=envelope.CONTROLLER)
            ws.send(sock, message.to_json())

        send(envelope.HELLO, HELLO)
        print(f"fakecam: connected to {host}:{port} and said hello", file=sys.stderr)
        reader = ws.FrameReader()
        sock.settimeout(None)
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                print("fakecam: the controller closed the control channel", file=sys.stderr)
                return
            for frame in reader.feed(chunk):
                if frame.opcode not in (ws.Opcode.BINARY, ws.Opcode.TEXT):
                    continue
                try:
                    message = envelope.decode(frame.payload)
                except envelope.DecodeError:
                    continue
                if message.function_name == envelope.HELLO:
                    continue
                send(message.function_name, {}, reply_to=message.message_id)
                if message.function_name == "ChangeVideoSettings":
                    self.arm(message.payload.get("video") or {})

    def arm(self, video: dict[str, Any]) -> None:
        for name, track in video.items():
            for destination in (track.get("avSerializer") or {}).get("destinations") or []:
                target = urlsplit(destination)
                if name not in self.pushing and target.hostname and target.port:
                    self.pushing.add(name)
                    threading.Thread(target=self.push, args=(name, target.hostname, target.port),
                                     daemon=True).start()

    def push(self, name: str, host: str, port: int) -> None:
        spec = stubcam.CODECS["h264"]
        with socket.create_connection((host, port), timeout=10) as sock:
            sock.sendall(stubcam.opening(self.media, name, spec))
            print(f"fakecam: pushing {name} to {host}:{port}", file=sys.stderr)
            timestamp = 0
            while True:
                for chunk in stubcam.picture_tags(self.media, 15, spec, timestamp):
                    sock.sendall(chunk)
                    timestamp += 67
                    time.sleep(1 / 15)


def management_api(camera: Camera, cert: Path) -> None:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def reply(self, code: int, headers: dict[str, str] | None = None) -> None:
            self.send_response(code)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
            if self.path == "/api/1.2/login":
                ok = body.get("username") == "ubnt" and body.get("password") == camera.password
                print(f"fakecam: login {'accepted' if ok else 'refused'}", file=sys.stderr)
                if ok:
                    self.reply(200, {"Set-Cookie": "TOKEN=fake; Path=/"})
                else:
                    self.reply(401)
            elif self.path == "/api/1.2/manage" and "TOKEN=fake" in self.headers.get("Cookie", ""):
                host, _, port = body["mgmt"]["hosts"][0].rpartition(":")
                print(f"fakecam: told to dial {host}:{port}", file=sys.stderr)
                self.reply(200)
                threading.Thread(target=camera.adopt, args=(host, int(port)), daemon=True).start()
            else:
                self.reply(401)

    server = ThreadingHTTPServer(("", 443), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(certs.ensure(cert)))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ip", required=True, help="this container's address, as discovery reports it")
    parser.add_argument("--mac", default="f4:e2:c6:00:00:01")
    parser.add_argument("--media", type=Path, required=True, help="Annex B H.264 file to push")
    parser.add_argument("--managed", action="store_true", help="report that a controller manages it")
    parser.add_argument("--password", default="ubnt", help="the management password it accepts")
    parser.add_argument("--cert", type=Path, default=Path("/tmp/fakecam.pem"))
    args = parser.parse_args(argv)
    camera = Camera(args.mac, args.ip, args.media, args.managed, args.password)
    threading.Thread(target=camera.answer_discovery, daemon=True).start()
    print(f"fakecam: G5 PTZ {args.mac} at {args.ip}, discovery and HTTPS up", file=sys.stderr)
    management_api(camera, args.cert)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
