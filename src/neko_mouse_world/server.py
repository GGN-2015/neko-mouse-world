from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import binascii
from collections import deque
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
DEFAULT_STARTUP_ASSET_CHANNELS = 4
SERVER_LOG_LIMIT = 300


@dataclass
class ClientConnection:
    player_id: int
    client_uuid: str
    writer: BinaryIO
    lock: threading.Lock = field(default_factory=threading.Lock)
    startup_token: str = field(default_factory=lambda: secrets.token_hex(16))
    startup_asset_hashes: set[str] = field(default_factory=set)
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
        self.startup_asset_channels = DEFAULT_STARTUP_ASSET_CHANNELS
        self.lock = threading.RLock()
        self.next_player_id = 1
        self.log_lines: deque[str] = deque(maxlen=SERVER_LOG_LIMIT)

    def configure_udp(self, host: str, port: int) -> None:
        with self.lock:
            self.udp_host = host
            self.udp_port = port

    def configure_startup_asset_channels(self, channels: int) -> None:
        with self.lock:
            self.startup_asset_channels = max(1, min(int(channels), 16))

    def register(self, writer: BinaryIO, client_uuid: str) -> ClientConnection:
        with self.lock:
            player_id = self.next_player_id
            self.next_player_id += 1
            client = ClientConnection(player_id=player_id, client_uuid=client_uuid, writer=writer)
            self.clients[player_id] = client
            return client

    def unregister(self, client: ClientConnection) -> None:
        should_cleanup = False
        with self.lock:
            self.clients.pop(client.player_id, None)
            self.player_states.pop(client.player_id, None)
            should_cleanup = not self.clients
        try:
            client.writer.close()
        except OSError:
            pass
        self.broadcast({"type": "player_left", "player_id": client.player_id}, exclude=client.player_id)
        if should_cleanup:
            self._cleanup_unused_boxes()

    def welcome_message(self, player_id: int, include_assets: bool = False) -> dict[str, Any]:
        with self.lock:
            client = self.clients[player_id]
            hashes = sorted({self.default_hash, *self.world_map.boxes.values()})
            existing_hashes = [
                digest
                for digest in hashes
                if box_path_for_hash(self.paths.boxes_dir, digest).is_file()
            ]
            client.startup_asset_hashes = set(existing_hashes)
            asset_manifest = [
                {
                    "hash": digest,
                    "size": box_path_for_hash(self.paths.boxes_dir, digest).stat().st_size,
                }
                for digest in existing_hashes
            ]
            assets = []
            if include_assets:
                assets = [
                    {"hash": digest, "data": encode_file_base64(box_path_for_hash(self.paths.boxes_dir, digest))}
                    for digest in existing_hashes
                ]
            return {
                "type": "welcome",
                "protocol": 2,
                "player_id": player_id,
                "default_hash": self.default_hash,
                "client_uuid": client.client_uuid,
                "startup_token": client.startup_token,
                "startup_asset_channels": client.startup_asset_hashes and self.startup_asset_channels or 1,
                "asset_manifest": asset_manifest,
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
                "logs": list(self.log_lines),
            }

    def handle_asset_stream(self, message: dict[str, Any], writer: BinaryIO) -> None:
        try:
            player_id = int(message.get("player_id"))
            client_uuid = str(message.get("client_uuid", ""))
            token = str(message.get("startup_token", ""))
            raw_hashes = message.get("hashes", [])
            if not isinstance(raw_hashes, list):
                raise ValueError("hashes must be a list")
            hashes = [str(digest) for digest in raw_hashes]
            raw_stream = bool(message.get("raw"))
        except (TypeError, ValueError):
            return

        with self.lock:
            client = self.clients.get(player_id)
            if client is None or client.client_uuid != client_uuid or client.startup_token != token:
                return
            allowed_hashes = {self.default_hash, *self.world_map.boxes.values(), *client.startup_asset_hashes}

        for digest in hashes:
            if digest not in allowed_hashes:
                self._send_asset_stream_error(writer, f"asset {digest} is not available for this startup stream")
                return
            path = box_path_for_hash(self.paths.boxes_dir, digest)
            if not path.is_file():
                self._send_asset_stream_error(writer, f"asset {digest} is missing")
                return
            with self.lock:
                current = self.clients.get(player_id)
                if current is None or current.client_uuid != client_uuid or current.startup_token != token:
                    return
            if raw_stream:
                if not self._send_asset_stream_raw(writer, digest, path):
                    return
            else:
                try:
                    send_message(writer, {"type": "asset", "hash": digest, "data": encode_file_base64(path)})
                except OSError:
                    return
        try:
            send_message(writer, {"type": "asset_stream_done"})
        except OSError:
            return

    def _send_asset_stream_raw(self, writer: BinaryIO, digest: str, path: Path) -> bool:
        try:
            send_message(writer, {"type": "asset_raw", "hash": digest, "size": path.stat().st_size})
            with path.open("rb") as source:
                while True:
                    chunk = source.read(1024 * 256)
                    if not chunk:
                        break
                    writer.write(chunk)
            writer.flush()
            return True
        except OSError:
            return False

    def _send_asset_stream_error(self, writer: BinaryIO, message: str) -> None:
        try:
            send_message(writer, {"type": "error", "message": message})
        except OSError:
            return

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
            elif message_type == "server_command":
                self._handle_server_command(client, message)
            else:
                client.send({"type": "error", "message": f"unknown message type {message_type!r}"})
        except (TypeError, ValueError, WorldFormatError) as exc:
            client.send({"type": "error", "message": str(exc)})

    def log(self, text: str) -> None:
        line = str(text)
        with self.lock:
            self.log_lines.append(line)
        print(line, flush=True)
        self.broadcast({"type": "server_log", "line": line})

    def _handle_server_command(self, client: ClientConnection, message: dict[str, Any]) -> None:
        command = str(message.get("command", "")).strip()
        if not command:
            return
        self.log(f"{client.player_id}: {command}")

    def _handle_place(self, client: ClientConnection, message: dict[str, Any]) -> None:
        cell = list_to_cell(message.get("cell"))
        digest = str(message.get("hash", ""))
        orientation = validate_orientation(message.get("orientation", IDENTITY_ORIENTATION))
        with self.lock:
            if cell in self.world_map.boxes:
                self._send_cell_state(client, cell)
                return
            if not self._ensure_asset(digest, message.get("asset")):
                client.send({"type": "error", "message": f"missing asset {digest}"})
                self._send_cell_state(client, cell)
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
                self._send_cell_state(client, cell)
                return
            orientation = validate_orientation(message.get("orientation", self.world_map.get_orientation(cell)))
            if not self._ensure_asset(digest, message.get("asset")):
                client.send({"type": "error", "message": f"missing asset {digest}"})
                self._send_cell_state(client, cell)
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

    def _handle_rotate(self, client: ClientConnection, message: dict[str, Any]) -> None:
        cell = list_to_cell(message.get("cell"))
        orientation = validate_orientation(message.get("orientation", IDENTITY_ORIENTATION))
        with self.lock:
            if cell not in self.world_map.boxes:
                self._send_cell_state(client, cell)
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
            self.broadcast({"type": "asset", "hash": digest, "size": path.stat().st_size})

    def _box_set_message(self, cell: Cell, digest: str, orientation: int) -> dict[str, Any]:
        return {
            "type": "box_set",
            "cell": cell_to_list(cell),
            "hash": digest,
            "orientation": orientation,
        }

    def _send_cell_state(self, client: ClientConnection, cell: Cell) -> None:
        digest = self.world_map.get_box(cell)
        try:
            if digest is None:
                client.send({"type": "box_removed", "cell": cell_to_list(cell)})
            else:
                client.send(self._box_set_message(cell, digest, self.world_map.get_orientation(cell)))
        except OSError:
            pass

    def _save(self) -> None:
        save_world(self.paths.info_file, self.world_map)

    def _cleanup_unused_boxes(self) -> None:
        with self.lock:
            used = {self.default_hash, *self.world_map.boxes.values()}
            self._save()
        removed = 0
        for path in self.paths.boxes_dir.glob("*.box"):
            if path.stem not in used:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
        if removed:
            self.log(f"Removed {removed} unused .box files")

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
        if not isinstance(raw_pos, (list, tuple)):
            raw_pos = [0.0, 0.0, 0.0]
        pos_values = list(raw_pos[:3])
        while len(pos_values) < 3:
            pos_values.append(0.0)
        raw_velocity = message.get("velocity", [0.0, 0.0, 0.0])
        if not isinstance(raw_velocity, (list, tuple)):
            raw_velocity = [0.0, 0.0, 0.0]
        velocity_values = list(raw_velocity[:3])
        while len(velocity_values) < 3:
            velocity_values.append(0.0)
        try:
            pos = [float(value) for value in pos_values]
            velocity = [float(value) for value in velocity_values]
            heading = float(message.get("heading", 0.0))
            pitch = float(message.get("pitch", 0.0))
        except (TypeError, ValueError):
            pos = [0.0, 0.0, 0.0]
            velocity = [0.0, 0.0, 0.0]
            heading = 0.0
            pitch = 0.0
        return {
            "pos": pos,
            "velocity": velocity,
            "heading": heading,
            "pitch": pitch,
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
        client: ClientConnection | None = None
        try:
            first = read_message(self.rfile)
            if first is not None and first.get("type") == "asset_stream":
                state.handle_asset_stream(first, self.wfile)
                return

            client_uuid = str((first or {}).get("client_uuid", "")) or secrets.token_hex(16)
            client = state.register(self.wfile, client_uuid)
            include_assets = bool((first or {}).get("include_assets", False))
            client.send(state.welcome_message(client.player_id, include_assets=include_assets))
            state.broadcast({"type": "player_join", "player_id": client.player_id}, exclude=client.player_id)
            while True:
                message = read_message(self.rfile)
                if message is None:
                    break
                state.handle_message(client, message)
        except (ConnectionError, OSError, ValueError):
            pass
        finally:
            if client is not None:
                state.unregister(client)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Neko Mouse World multiplayer server")
    parser.add_argument("world", nargs="?", default=".", help="world save folder")
    parser.add_argument("--host", default=DEFAULT_HOST, help="host/interface to bind")
    parser.add_argument("--port", default=DEFAULT_PORT, type=int, help="TCP port to bind")
    parser.add_argument("--udp-host", default=None, help="UDP host/interface to bind; defaults to --host")
    parser.add_argument("--udp-port", default=0, type=int, help="UDP port to bind; 0 auto-allocates")
    parser.add_argument(
        "--startup-asset-channels",
        default=DEFAULT_STARTUP_ASSET_CHANNELS,
        type=int,
        help="temporary TCP channels per client for startup .box asset transfer",
    )
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
    state.configure_startup_asset_channels(args.startup_asset_channels)

    udp_host = args.udp_host or args.host
    with _ThreadingServer((args.host, args.port), _Handler, state) as server:
        udp_loop = _UdpLoop(udp_host, args.udp_port, state)
        udp_loop.start()
        actual_host = str(server.server_address[0])
        actual_port = int(server.server_address[1])
        state.log(f"Neko Mouse World server listening on {actual_host}:{actual_port}")
        state.log(f"World TCP sync on {actual_host}:{actual_port}")
        state.log(f"Player UDP candidate on {udp_host}:{udp_loop.port} (negotiated over TCP)")
        state.log(f"World: {state.paths.root}")
        try:
            if args.with_client:
                return _run_with_main_client(server, udp_loop, args.host, actual_port)
            server.serve_forever()
        except KeyboardInterrupt:
            state.log("Shutting down")
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
    server.state.log(f"Launching main client on {client_host}:{port}")
    try:
        client_process = subprocess.Popen(command)
        return_code = client_process.wait()
    except KeyboardInterrupt:
        server.state.log("Shutting down")
        return_code = 0
    finally:
        server.shutdown()
        udp_loop.stop()
        server_thread.join(timeout=3.0)
    return int(return_code)


if __name__ == "__main__":
    raise SystemExit(main())
