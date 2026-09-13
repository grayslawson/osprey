"""Forward only the expected G5's LAN TCP channels to loopback Docker ports.

Docker Desktop on this Windows host reserves the published LAN ports but does not
complete inbound connections from the camera. This host-side relay leaves the
container published only on loopback and does not inspect or log payloads.
"""

import argparse
import asyncio
import ipaddress
import logging
from pathlib import Path


PORTS = {7442: 17442, 7444: 17444, 7550: 17550}
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 3600
WRITE_TIMEOUT = 30
MAX_CONNECTIONS = 16
logger = logging.getLogger("physical_lan_proxy")


def read_env(path: Path) -> tuple[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in {"CUCKOO_PHYSICAL_HOST", "G5_PTZ_IP"}:
            values[key] = value.strip().strip('"').strip("'")
    host = str(ipaddress.IPv4Address(values["CUCKOO_PHYSICAL_HOST"]))
    camera = str(ipaddress.IPv4Address(values["G5_PTZ_IP"]))
    if host == camera or ipaddress.IPv4Address(host).is_loopback:
        raise ValueError("physical host must be a non-loopback address distinct from camera")
    return host, camera


async def relay(source: asyncio.StreamReader, destination: asyncio.StreamWriter) -> None:
    while True:
        chunk = await asyncio.wait_for(source.read(65536), READ_TIMEOUT)
        if not chunk:
            return
        destination.write(chunk)
        await asyncio.wait_for(destination.drain(), WRITE_TIMEOUT)


async def serve(host: str, camera: str) -> None:
    active = 0

    async def handle(
        incoming: asyncio.StreamReader, outgoing: asyncio.StreamWriter, target_port: int
    ) -> None:
        nonlocal active
        peer = outgoing.get_extra_info("peername")
        if not peer or peer[0] != camera or active >= MAX_CONNECTIONS:
            outgoing.close()
            await outgoing.wait_closed()
            return
        active += 1
        upstream = None
        tasks: set[asyncio.Task[None]] = set()
        try:
            logger.info("accepted connection from %s:%s to channel %s", peer[0], peer[1], target_port)
            upstream_reader, upstream = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", target_port), CONNECT_TIMEOUT
            )
            logger.info("connected upstream to 127.0.0.1:%s", target_port)
            tasks = {
                asyncio.create_task(relay(incoming, upstream)),
                asyncio.create_task(relay(upstream_reader, outgoing)),
            }
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                try:
                    task.result()
                except Exception as ex:
                    logger.debug("channel %s task ended: %s", target_port, ex)
            await asyncio.gather(*pending, return_exceptions=True)
        except Exception as error:
            logger.warning("channel %s closed: %s", target_port, error)
        finally:
            for task in tasks:
                task.cancel()
            if upstream is not None:
                upstream.close()
                await upstream.wait_closed()
            outgoing.close()
            await outgoing.wait_closed()
            active -= 1

    servers = []
    try:
        for lan_port, loopback_port in PORTS.items():
            server = await asyncio.start_server(
                lambda reader, writer, port=loopback_port: handle(reader, writer, port),
                host,
                lan_port,
                backlog=16,
            )
            servers.append(server)
            logger.info("listening on %s:%s", host, lan_port)
        await asyncio.gather(*(server.serve_forever() for server in servers))
    finally:
        for server in servers:
            server.close()
        await asyncio.gather(*(server.wait_closed() for server in servers))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env.physical"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    host, camera = read_env(args.env_file)
    asyncio.run(serve(host, camera))


if __name__ == "__main__":
    main()
