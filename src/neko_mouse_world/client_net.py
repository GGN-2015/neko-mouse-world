from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import queue
import socket
import tempfile
import threading
import time
from typing import Any

from .box_assets import store_box_file_by_hash
from .net_protocol import decode_file_base64, encode_file_base64, list_to_cell, send_socket_message
from .world_file import Cell, WorldMap, box_path_for_hash, world_paths


UDP_PROBE_INTERVAL = 0.10
UDP_PROBE_SETTLE_SECONDS = 0.50


@dataclass(frozen=True)
class RemotePlayer:
    player_id: int
    pos: tuple[float, float, float]
    heading: float
    pitch: float
    move_mode: str


class NetworkWorldClient:
    def __init__(self, host: str, port: int, cache_dir: Path | None = None) -> None:
        self.host = host
        self.port = port
        cache_root = cache_dir or (Path(tempfile.gettempdir()) / "neko_mouse_world_client_cache" / f"{host}_{port}")
        self.paths = world_paths(cache_root)
        self.paths.root.mkdir(parents=True, exist_ok=True)
        self.paths.boxes_dir.mkdir(parents=True, exist_ok=True)

        self.incoming: queue.Queue[dict[str, Any]] = queue.Queue()
        self.connected = False
        self.connecting = True
        self.player_id: int | None = None
        self.default_hash: str | None = None
        self.world_map = WorldMap()
        self.remote_players: dict[int, RemotePlayer] = {}
        self._remote_lock = threading.RLock()
        self.udp_host = host
        self.udp_port: int | None = None
        self.udp_enabled = False
        self.udp_status = "disabled"

        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp_socket.bind(("", 0))
        self._udp_socket.setblocking(False)
        self._udp_token: str | None = None
        self._udp_ack_lock = threading.Lock()
        self._udp_acks: set[int] = set()

        self._udp_thread = threading.Thread(target=self._udp_read_loop, daemon=True)
        self._udp_thread.start()
        self._thread = threading.Thread(target=self._connect_loop, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self.connected = False
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        try:
            self._udp_socket.close()
        except OSError:
            pass

    def send_place(self, cell: Cell, digest: str, orientation: int, asset: str | None) -> None:
        self._send_tcp({"type": "place", "cell": list(cell), "hash": digest, "orientation": orientation, "asset": asset})

    def send_set_box(self, cell: Cell, digest: str, orientation: int, asset: str | None) -> None:
        self._send_tcp({"type": "set_box", "cell": list(cell), "hash": digest, "orientation": orientation, "asset": asset})

    def send_delete(self, cell: Cell) -> None:
        self._send_tcp({"type": "delete", "cell": list(cell)})

    def send_rotate(self, cell: Cell, orientation: int) -> None:
        self._send_tcp({"type": "rotate", "cell": list(cell), "orientation": orientation})

    def send_player_state(
        self,
        pos: tuple[float, float, float],
        heading: float,
        pitch: float,
        move_mode: str,
    ) -> None:
        if self.player_id is None:
            return
        message = {
            "type": "player_state",
            "player_id": self.player_id,
            "pos": [pos[0], pos[1], pos[2]],
            "heading": heading,
            "pitch": pitch,
            "move_mode": move_mode,
        }
        if self.udp_enabled and self.udp_port is not None:
            try:
                payload = json.dumps(message, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
                self._udp_socket.sendto(payload, (self.udp_host, self.udp_port))
                return
            except OSError:
                self.udp_enabled = False
                self.udp_status = "fallback"
        self._send_tcp(message)

    def asset_payload(self, digest: str) -> str | None:
        path = box_path_for_hash(self.paths.boxes_dir, digest)
        if not path.is_file():
            return None
        return encode_file_base64(path)

    def poll(self) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        while True:
            try:
                messages.append(self.incoming.get_nowait())
            except queue.Empty:
                return messages

    def _connect_loop(self) -> None:
        while not self._stop.is_set():
            self._mark_disconnected()
            try:
                sock = socket.create_connection((self.host, self.port), timeout=3.0)
                sock.settimeout(None)
                self._sock = sock
                self.connected = True
                self.connecting = False
                send_socket_message(sock, {"type": "hello", "protocol": 1})
                self._read_loop(sock)
            except (OSError, ValueError, ConnectionError):
                pass
            finally:
                self._mark_disconnected()
                sock = self._sock
                self._sock = None
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                if not self._stop.is_set():
                    self.incoming.put({"type": "disconnect"})
            time.sleep(1.0)

    def _mark_disconnected(self) -> None:
        self.connected = False
        self.connecting = True
        self.player_id = None
        self.udp_host = self.host
        self.udp_port = None
        self.udp_enabled = False
        self.udp_status = "disabled"
        self._udp_token = None
        with self._udp_ack_lock:
            self._udp_acks.clear()

    def _read_loop(self, sock: socket.socket) -> None:
        buffer = bytearray()
        while not self._stop.is_set():
            data = sock.recv(65536)
            if not data:
                break
            buffer.extend(data)
            while b"\n" in buffer:
                line, _, rest = buffer.partition(b"\n")
                buffer = bytearray(rest)
                if not line:
                    continue
                message = json.loads(line.decode("utf-8"))
                if isinstance(message, dict):
                    self._handle_incoming(message)

    def _udp_read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, _address = self._udp_socket.recvfrom(65535)
            except BlockingIOError:
                time.sleep(0.02)
                continue
            except OSError:
                break
            try:
                message = json.loads(data.decode("utf-8"))
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("type") == "udp_probe_ack":
                if str(message.get("token", "")) == self._udp_token:
                    try:
                        seq = int(message.get("seq"))
                    except (TypeError, ValueError):
                        continue
                    with self._udp_ack_lock:
                        self._udp_acks.add(seq)
                continue
            if message.get("type") == "player_state":
                self._handle_incoming(message)

    def _handle_incoming(self, message: dict[str, Any]) -> None:
        message_type = message.get("type")
        if message_type == "welcome":
            self._apply_welcome(message)
        elif message_type == "udp_status":
            self.udp_enabled = bool(message.get("enabled"))
            self.udp_status = "enabled" if self.udp_enabled else "fallback"
        elif message_type == "asset":
            self._store_asset(str(message.get("hash", "")), str(message.get("data", "")))
        elif message_type == "box_set":
            pass
        elif message_type == "box_removed":
            pass
        elif message_type == "player_state":
            self._apply_player_state(message)
        elif message_type == "player_left":
            try:
                with self._remote_lock:
                    self.remote_players.pop(int(message.get("player_id")), None)
            except (TypeError, ValueError):
                pass
        self.incoming.put(message)

    def _apply_welcome(self, message: dict[str, Any]) -> None:
        self.player_id = int(message["player_id"])
        self.default_hash = str(message["default_hash"])
        for asset in message.get("assets", []):
            self._store_asset(str(asset.get("hash", "")), str(asset.get("data", "")))

        boxes: dict[Cell, str] = {}
        orientations: dict[Cell, int] = {}
        for item in message.get("world", []):
            cell = list_to_cell(item.get("cell"))
            boxes[cell] = str(item.get("hash", ""))
            orientations[cell] = int(item.get("orientation", 0))
        self.world_map = WorldMap(boxes=boxes, orientations=orientations)

        with self._remote_lock:
            self.remote_players.clear()
        for player in message.get("players", []):
            self._apply_player_state({"type": "player_state", **player})

        self.udp_enabled = False
        self.udp_status = "disabled"
        advertised_udp_host = str(message.get("udp_host", self.host)) or self.host
        self.udp_host = self.host if advertised_udp_host in {"0.0.0.0", "::", ""} else advertised_udp_host
        self.udp_port = self._optional_int(message.get("udp_port"))
        self._udp_token = str(message.get("udp_probe_token", "")) or None
        if self.udp_port is not None and self._udp_token:
            self.udp_status = "testing"
            count = int(message.get("udp_probe_count", 5))
            threading.Thread(target=self._run_udp_probe, args=(self.player_id, self._udp_token, count), daemon=True).start()
        else:
            self._send_tcp({"type": "udp_probe_result", "sent": 0, "received": 0})

    def _run_udp_probe(self, player_id: int, token: str, count: int) -> None:
        count = max(1, min(count, 20))
        with self._udp_ack_lock:
            self._udp_acks.clear()
        for seq in range(count):
            if self._stop.is_set() or not self.connected or self.player_id != player_id or self._udp_token != token:
                return
            message = {"type": "udp_probe", "player_id": player_id, "token": token, "seq": seq}
            try:
                payload = json.dumps(message, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
                if self.udp_port is not None:
                    self._udp_socket.sendto(payload, (self.udp_host, self.udp_port))
            except OSError:
                break
            time.sleep(UDP_PROBE_INTERVAL)

        deadline = time.monotonic() + UDP_PROBE_SETTLE_SECONDS
        while time.monotonic() < deadline:
            with self._udp_ack_lock:
                if len(self._udp_acks) >= count:
                    break
            time.sleep(0.02)
        with self._udp_ack_lock:
            received = len(self._udp_acks)
        if self.connected and self.player_id == player_id and self._udp_token == token:
            self._send_tcp({"type": "udp_probe_result", "token": token, "sent": count, "received": received})

    def _store_asset(self, digest: str, encoded: str) -> None:
        if not digest or not encoded:
            return
        target = box_path_for_hash(self.paths.boxes_dir, digest)
        if target.is_file():
            return
        with tempfile.NamedTemporaryFile(delete=False, suffix=".box") as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(decode_file_base64(encoded))
        try:
            actual = store_box_file_by_hash(self.paths.boxes_dir, temp_path)
            if actual != digest:
                box_path_for_hash(self.paths.boxes_dir, actual).unlink(missing_ok=True)
        finally:
            temp_path.unlink(missing_ok=True)

    def _apply_player_state(self, message: dict[str, Any]) -> None:
        try:
            player_id = int(message.get("player_id"))
        except (TypeError, ValueError):
            return
        if player_id == self.player_id:
            return
        pos = message.get("pos", [0.0, 0.0, 0.0])
        if not isinstance(pos, list | tuple) or len(pos) < 3:
            return
        player = RemotePlayer(
            player_id=player_id,
            pos=(float(pos[0]), float(pos[1]), float(pos[2])),
            heading=float(message.get("heading", 0.0)),
            pitch=float(message.get("pitch", 0.0)),
            move_mode=str(message.get("move_mode", "walk")),
        )
        with self._remote_lock:
            self.remote_players[player_id] = player

    def remote_players_snapshot(self) -> dict[int, RemotePlayer]:
        with self._remote_lock:
            return dict(self.remote_players)

    def _send_tcp(self, message: dict[str, Any]) -> None:
        sock = self._sock
        if sock is None or not self.connected:
            return
        try:
            with self._send_lock:
                send_socket_message(sock, message)
        except OSError:
            self.connected = False

    def _optional_int(self, value: object) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
