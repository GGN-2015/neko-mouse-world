from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import binascii
from pathlib import Path
import socketserver
import json
import socket
import secrets
import subprocess
import tempfile
import threading
import sys
from typing import Any, BinaryIO

from box_editor_view.box_file import BoxFormatError

from .box_assets import ensure_default_box, store_box_file_by_hash
from .net_protocol import cell_to_list, decode_file_base64, encode_file_base64, list_to_cell, read_message, send_message
from .orientation import IDENTITY_ORIENTATION, validate_orientation
from .world_file import Cell, WorldFormatError, WorldMap, box_path_for_hash, load_or_create_world, save_world


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5678
UDP_PROBE_COUNT = 5
UDP_PROBE_THRESHOLD = 3


@dataclass
class ClientConnection:
    player_id: int
    writer: BinaryIO
    lock: threading.Lock = field(default_factory=threading.Lock)
    udp_token: str = field(default_factory=lambda: secrets.token_hex(16))
    udp_address: tuple[str, int] | None = None
    udp_enabled: bool = False
    udp_probe_seen: set[int] = field(default_factory=set)

    def send(self, message: dict[str, Any]) -> None:
        with self.lock:
            send_message(self.writer, message)


class WorldServerState:
    def __init__(self, world_dir: Path) -> None:
        loaded = load_or_create_world(world_dir)
        self.paths = loaded.paths
        self.world_map: WorldMap = loaded.world_map
        self.default_hash = ensure_default_box(self.paths.boxes_dir)
        self.clients: dict[int, ClientConnection] = {}
        self.player_states: dict[int, dict[str, Any]] = {}
        self.udp_host = DEFAULT_HOST
        self.udp_port = 0
        self.lock = threading.RLock()
        self.next_player_id = 1

    def configure_udp(self, host: str, port: int) -> None:
        with self.lock:
            self.udp_host = host
            self.udp_port = port

    def register(self, writer: BinaryIO) -> ClientConnection:
        with self.lock:
            player_id = self.next_player_id
            self.next_player_id += 1
            client = ClientConnection(player_id=player_id, writer=writer)
            self.clients[player_id] = client
            return client

    def unregister(self, client: ClientConnection) -> None:
        with self.lock:
            self.clients.pop(client.player_id, None)
            self.player_states.pop(client.player_id, None)
        self.broadcast({"type": "player_left", "player_id": client.player_id}, exclude=client.player_id)

    def welcome_message(self, player_id: int) -> dict[str, Any]:
        with self.lock:
            hashes = sorted({self.default_hash, *self.world_map.boxes.values()})
            assets = [
                {"hash": digest, "data": encode_file_base64(box_path_for_hash(self.paths.boxes_dir, digest))}
                for digest in hashes
                if box_path_for_hash(self.paths.boxes_dir, digest).is_file()
            ]
            return {
                "type": "welcome",
                "protocol": 1,
                "player_id": player_id,
                "default_hash": self.default_hash,
                "udp_host": self.udp_host,
                "udp_port": self.udp_port,
                "udp_probe_token": self.clients[player_id].udp_token,
                "udp_probe_count": UDP_PROBE_COUNT,
                "udp_probe_threshold": UDP_PROBE_THRESHOLD,
                "world": [
                    {
                        "cell": cell_to_list(cell),
                        "hash": digest,
                        "orientation": self.world_map.get_orientation(cell),
                    }
                    for cell, digest in sorted(self.world_map.boxes.items())
                ],
                "assets": assets,
                "players": [
                    {"player_id": other_player_id, **state}
                    for other_player_id, state in self.player_states.items()
                    if other_player_id != player_id
                ],
            }

    def handle_message(self, client: ClientConnection, message: dict[str, Any]) -> None:
        try:
            message_type = message.get("type")
            if message_type == "place":
                self._handle_place(client, message)
            elif message_type == "set_box":
                self._handle_set_box(client, message)
            elif message_type == "delete":
                self._handle_delete(client, message)
            elif message_type == "rotate":
                self._handle_rotate(client, message)
            elif message_type == "asset":
                digest = str(message.get("hash", ""))
                self._ensure_asset(digest, message.get("asset"))
            elif message_type == "udp_probe_result":
                self._handle_udp_probe_result(client, message)
            elif message_type == "player_state":
                self._handle_tcp_player_state(client, message)
            else:
                client.send({"type": "error", "message": f"unknown message type {message_type!r}"})
        except (TypeError, ValueError, WorldFormatError) as exc:
            client.send({"type": "error", "message": str(exc)})

    def _handle_place(self, client: ClientConnection, message: dict[str, Any]) -> None:
        cell = list_to_cell(message.get("cell"))
        digest = str(message.get("hash", ""))
        orientation = validate_orientation(message.get("orientation", IDENTITY_ORIENTATION))
        with self.lock:
            if cell in self.world_map.boxes:
                return
            if not self._ensure_asset(digest, message.get("asset")):
                client.send({"type": "error", "message": f"missing asset {digest}"})
                return
            self.world_map.set_box(cell, digest, orientation)
            self._save()
        self._broadcast_asset_if_present(digest)
        self.broadcast(self._box_set_message(cell, digest, orientation))

    def _handle_set_box(self, client: ClientConnection, message: dict[str, Any]) -> None:
        cell = list_to_cell(message.get("cell"))
        digest = str(message.get("hash", ""))
        with self.lock:
            if cell not in self.world_map.boxes:
                return
            orientation = validate_orientation(message.get("orientation", self.world_map.get_orientation(cell)))
            if not self._ensure_asset(digest, message.get("asset")):
                client.send({"type": "error", "message": f"missing asset {digest}"})
                return
            self.world_map.set_box(cell, digest, orientation)
            self._save()
        self._broadcast_asset_if_present(digest)
        self.broadcast(self._box_set_message(cell, digest, orientation))

    def _handle_delete(self, _client: ClientConnection, message: dict[str, Any]) -> None:
        cell = list_to_cell(message.get("cell"))
        with self.lock:
            removed = self.world_map.remove_box(cell)
            if removed:
                self._save()
        if removed:
            self.broadcast({"type": "box_removed", "cell": cell_to_list(cell)})

    def _handle_rotate(self, _client: ClientConnection, message: dict[str, Any]) -> None:
        cell = list_to_cell(message.get("cell"))
        orientation = validate_orientation(message.get("orientation", IDENTITY_ORIENTATION))
        with self.lock:
            if cell not in self.world_map.boxes:
                return
            self.world_map.set_orientation(cell, orientation)
            digest = self.world_map.get_box(cell)
            self._save()
        if digest is not None:
            self.broadcast(self._box_set_message(cell, digest, orientation))

    def _ensure_asset(self, digest: str, encoded_asset: object | None) -> bool:
        try:
            target = box_path_for_hash(self.paths.boxes_dir, digest)
        except WorldFormatError:
            return False
        if target.is_file():
            return True
        if not isinstance(encoded_asset, str):
            return False
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".box") as temp_file:
                temp_path = Path(temp_file.name)
                temp_file.write(decode_file_base64(encoded_asset))
            actual_digest = store_box_file_by_hash(self.paths.boxes_dir, temp_path)
            if actual_digest != digest:
                box_path_for_hash(self.paths.boxes_dir, actual_digest).unlink(missing_ok=True)
                return False
            return True
        except (BoxFormatError, WorldFormatError):
            return False
        except (ValueError, binascii.Error):
            return False
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def _broadcast_asset_if_present(self, digest: str) -> None:
        path = box_path_for_hash(self.paths.boxes_dir, digest)
        if path.is_file():
            self.broadcast({"type": "asset", "hash": digest, "data": encode_file_base64(path)})

    def _box_set_message(self, cell: Cell, digest: str, orientation: int) -> dict[str, Any]:
        return {
            "type": "box_set",
            "cell": cell_to_list(cell),
            "hash": digest,
            "orientation": orientation,
        }

    def _save(self) -> None:
        save_world(self.paths.info_file, self.world_map)

    def broadcast(self, message: dict[str, Any], exclude: int | None = None) -> None:
        with self.lock:
            clients = [client for client in self.clients.values() if client.player_id != exclude]
        for client in clients:
            try:
                client.send(message)
            except OSError:
                pass

    def _handle_udp_probe_result(self, client: ClientConnection, message: dict[str, Any]) -> None:
        token = str(message.get("token", ""))
        try:
            sent = int(message.get("sent", 0))
            received = int(message.get("received", 0))
        except (TypeError, ValueError):
            sent = 0
            received = 0
        with self.lock:
            current = self.clients.get(client.player_id)
            if current is not client:
                return
            server_received = len(client.udp_probe_seen)
            enabled = (
                token == client.udp_token
                and sent >= UDP_PROBE_COUNT
                and received >= UDP_PROBE_THRESHOLD
                and server_received >= UDP_PROBE_THRESHOLD
            )
            client.udp_enabled = enabled
            if not enabled:
                client.udp_address = None
        client.send(
            {
                "type": "udp_status",
                "enabled": enabled,
                "sent": sent,
                "received": received,
                "server_received": server_received,
                "threshold": UDP_PROBE_THRESHOLD,
            }
        )

    def _handle_tcp_player_state(self, client: ClientConnection, message: dict[str, Any]) -> None:
        state = self._normalize_player_state(message)
        with self.lock:
            if self.clients.get(client.player_id) is not client:
                return
            self.player_states[client.player_id] = state
        self._broadcast_player_state(client.player_id, state, udp_socket=None)

    def handle_udp_message(self, message: dict[str, Any], address: tuple[str, int], udp_socket: socket.socket) -> None:
        message_type = message.get("type")
        if message_type == "udp_probe":
            self._handle_udp_probe(message, address, udp_socket)
            return
        if message_type != "player_state":
            return
        try:
            player_id = int(message.get("player_id"))
        except (TypeError, ValueError):
            return
        state = self._normalize_player_state(message)
        with self.lock:
            client = self.clients.get(player_id)
            if client is None or not client.udp_enabled or client.udp_address != address:
                return
            self.player_states[player_id] = state
        self._broadcast_player_state(player_id, state, udp_socket)

    def _handle_udp_probe(
        self,
        message: dict[str, Any],
        address: tuple[str, int],
        udp_socket: socket.socket,
    ) -> None:
        try:
            player_id = int(message.get("player_id"))
            seq = int(message.get("seq"))
        except (TypeError, ValueError):
            return
        token = str(message.get("token", ""))
        with self.lock:
            client = self.clients.get(player_id)
            if client is None or token != client.udp_token:
                return
            client.udp_address = address
            client.udp_probe_seen.add(seq)
        payload = json.dumps(
            {"type": "udp_probe_ack", "token": token, "seq": seq},
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        try:
            udp_socket.sendto(payload, address)
        except OSError:
            pass

    def _broadcast_player_state(
        self,
        player_id: int,
        state: dict[str, Any],
        udp_socket: socket.socket | None,
    ) -> None:
        message = {"type": "player_state", "player_id": player_id, **state}
        udp_payload = json.dumps(message, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        with self.lock:
            recipients = [
                client
                for client in self.clients.values()
                if client.player_id != player_id
            ]
        for client in recipients:
            if udp_socket is not None and client.udp_enabled and client.udp_address is not None:
                try:
                    udp_socket.sendto(udp_payload, client.udp_address)
                    continue
                except OSError:
                    pass
            try:
                client.send(message)
            except OSError:
                pass

    def _normalize_player_state(self, message: dict[str, Any]) -> dict[str, Any]:
        raw_pos = message.get("pos", [0.0, 0.0, 0.0])
        if not isinstance(raw_pos, list | tuple):
            raw_pos = [0.0, 0.0, 0.0]
        pos_values = list(raw_pos[:3])
        while len(pos_values) < 3:
            pos_values.append(0.0)
        return {
            "pos": [float(value) for value in pos_values],
            "heading": float(message.get("heading", 0.0)),
            "pitch": float(message.get("pitch", 0.0)),
            "move_mode": str(message.get("move_mode", "walk")),
        }


class _UdpLoop(threading.Thread):
    def __init__(self, host: str, port: int, state: WorldServerState) -> None:
        super().__init__(daemon=True)
        self.host = host
        self.state = state
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind((host, port))
        self.socket.settimeout(0.5)
        self.port = int(self.socket.getsockname()[1])
        self.state.configure_udp(host, self.port)
        self._stopped = threading.Event()

    def run(self) -> None:
        while not self._stopped.is_set():
            try:
                data, address = self.socket.recvfrom(65535)
                message = json.loads(data.decode("utf-8"))
                if isinstance(message, dict):
                    self.state.handle_udp_message(message, address, self.socket)
            except socket.timeout:
                continue
            except OSError:
                break
            except ValueError:
                continue

    def stop(self) -> None:
        self._stopped.set()
        self.socket.close()


class _ThreadingServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], handler_class, state: WorldServerState) -> None:
        super().__init__(server_address, handler_class)
        self.state = state


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        state: WorldServerState = self.server.state  # type: ignore[attr-defined]
        client = state.register(self.wfile)
        try:
            first = read_message(self.rfile)
            if first and first.get("type") == "hello":
                pass
            client.send(state.welcome_message(client.player_id))
            state.broadcast({"type": "player_join", "player_id": client.player_id}, exclude=client.player_id)
            while True:
                message = read_message(self.rfile)
                if message is None:
                    break
                state.handle_message(client, message)
        except (ConnectionError, OSError, ValueError):
            pass
        finally:
            state.unregister(client)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Neko Mouse World multiplayer server")
    parser.add_argument("world", nargs="?", default=".", help="world save folder")
    parser.add_argument("--host", default=DEFAULT_HOST, help="host/interface to bind")
    parser.add_argument("--port", default=DEFAULT_PORT, type=int, help="TCP port to bind")
    parser.add_argument("--udp-host", default=None, help="UDP host/interface to bind; defaults to --host")
    parser.add_argument("--udp-port", default=0, type=int, help="UDP port to bind; 0 auto-allocates")
    parser.add_argument("--with-client", action="store_true", help="also launch a main local client and stop when it exits")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        state = WorldServerState(Path(args.world))
    except WorldFormatError as exc:
        print(f"FormatError: {exc}")
        return 1

    udp_host = args.udp_host or args.host
    with _ThreadingServer((args.host, args.port), _Handler, state) as server:
        udp_loop = _UdpLoop(udp_host, args.udp_port, state)
        udp_loop.start()
        actual_host = str(server.server_address[0])
        actual_port = int(server.server_address[1])
        print(f"Neko Mouse World server listening on {actual_host}:{actual_port}")
        print(f"World TCP sync on {actual_host}:{actual_port}")
        print(f"Player UDP candidate on {udp_host}:{udp_loop.port} (negotiated over TCP)")
        print(f"World: {state.paths.root}")
        try:
            if args.with_client:
                return _run_with_main_client(server, udp_loop, args.host, actual_port)
            server.serve_forever()
        except KeyboardInterrupt:
            print("Shutting down")
        finally:
            udp_loop.stop()
    return 0


def _run_with_main_client(
    server: _ThreadingServer,
    udp_loop: _UdpLoop,
    host: str,
    port: int,
) -> int:
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    client_host = "127.0.0.1" if host in {"", "0.0.0.0", "::"} else host
    command = [
        sys.executable,
        "-m",
        "neko_mouse_world.client",
        "--host",
        client_host,
        "--port",
        str(port),
    ]
    print(f"Launching main client on {client_host}:{port}")
    try:
        client_process = subprocess.Popen(command)
        return_code = client_process.wait()
    except KeyboardInterrupt:
        print("Shutting down")
        return_code = 0
    finally:
        server.shutdown()
        udp_loop.stop()
        server_thread.join(timeout=3.0)
    return int(return_code)


if __name__ == "__main__":
    raise SystemExit(main())
