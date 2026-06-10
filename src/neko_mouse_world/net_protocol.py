from __future__ import annotations

import base64
import json
from pathlib import Path
import socket
from typing import Any, BinaryIO


Message = dict[str, Any]


def send_message(file: BinaryIO, message: Message) -> None:
    payload = json.dumps(message, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    file.write(payload + b"\n")
    file.flush()


def read_message(file: BinaryIO) -> Message | None:
    line = file.readline()
    if not line:
        return None
    message = json.loads(line.decode("utf-8"))
    if not isinstance(message, dict):
        raise ValueError("network message must be a JSON object")
    return message


def send_socket_message(sock: socket.socket, message: Message) -> None:
    payload = json.dumps(message, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    sock.sendall(payload + b"\n")


def encode_file_base64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def decode_file_base64(data: str) -> bytes:
    return base64.b64decode(data.encode("ascii"))


def cell_to_list(cell: tuple[int, int, int]) -> list[int]:
    return [int(cell[0]), int(cell[1]), int(cell[2])]


def list_to_cell(value: object) -> tuple[int, int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("cell must be a list of three integers")
    return (int(value[0]), int(value[1]), int(value[2]))
