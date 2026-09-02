"""Minecraft RCON client.

Synchronous and deliberately simple: one connection per command, reconnecting
each time. The server tolerates this fine at human command rates, and it means
a restart of Minecraft under us cannot leave a half-dead socket behind.
"""
from __future__ import annotations

import socket
import struct

TYPE_AUTH = 3
TYPE_EXEC = 2


class RconError(Exception):
    """Any RCON failure: connect, auth, protocol, or timeout."""


class RconAuthError(RconError):
    """The server rejected the password."""


def _packet(req_id: int, req_type: int, body: str) -> bytes:
    payload = struct.pack("<ii", req_id, req_type) + body.encode("utf-8") + b"\x00\x00"
    return struct.pack("<i", len(payload)) + payload


def _read_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise RconError("connection closed by server")
        buf += chunk
    return bytes(buf)


def _read_packet(sock: socket.socket) -> tuple[int, int, str]:
    (length,) = struct.unpack("<i", _read_exact(sock, 4))
    # A sane response is 10 bytes of framing plus body. Anything wild means we
    # have lost sync with the stream and should not try to allocate for it.
    if not 10 <= length <= 4_200_000:
        raise RconError(f"implausible packet length {length}")
    payload = _read_exact(sock, length)
    req_id, req_type = struct.unpack("<ii", payload[:8])
    return req_id, req_type, payload[8:-2].decode("utf-8", "replace")


def execute(command: str, host: str, port: int, password: str,
            timeout: float = 10.0) -> str:
    """Run one console command and return the server's response text."""
    if not password:
        raise RconAuthError("no RCON password configured")
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(_packet(1, TYPE_AUTH, password))
            req_id, _, _ = _read_packet(sock)
            # Minecraft signals a bad password with request id -1.
            if req_id == -1:
                raise RconAuthError("RCON authentication failed")
            sock.sendall(_packet(2, TYPE_EXEC, command))
            _, _, body = _read_packet(sock)
            return body
    except (socket.timeout, TimeoutError) as exc:
        raise RconError(f"RCON timed out after {timeout}s") from exc
    except OSError as exc:
        raise RconError(f"RCON unreachable: {exc}") from exc
