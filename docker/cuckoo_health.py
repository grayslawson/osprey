from __future__ import annotations

import socket


def main() -> int:
    with socket.create_connection(("127.0.0.1", 8000), timeout=2.0):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
