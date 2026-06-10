from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import queue
import socket
import tempfile
import threading
import time
import uuid
from typing import Any

from box_editor_view.box_file import BoxFormatError

from .box_assets import store_box_file_by_hash
from .net_protocol import decode_file_base64, encode_file_base64, list_to_cell, send_socket_message
from .world_file import Cell, WorldFormatError, WorldMap, box_path_for_hash, world_paths


UDP_PROBE_INTERVAL = 0.10
UDP_PROBE_SETTLE_SECONDS = 0.50
DEFAULT_STARTUP_ASSET_CHANNELS = 4
PLAYER_STATE_SEND_INTERVAL = 0.05


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
        self.client_uuid = uuid.uuid4().hex
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
        self.startup_assets_pending = False
        self.startup_assets_complete = threading.Event()
        self.startup_assets_complete.set()
        self.startup_assets_total = 0
        self.startup_assets_done = 0
        self.startup_assets_failed = False

        self._sock: socket.socket | None = None
        self._send_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._latest_player_state: dict[str, Any] | None = None
        self._latest_player_state_lock = threading.Lock()
        self._startup_asset_lock = threading.Lock()
        self._startup_asset_generation = 0
        self._prefer_inline_startup_assets = False
        self._stop = threading.Event()
        self._udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp_socket.bind(("", 0))
        self._udp_socket.setblocking(False)
        self._udp_token: str | None = None
        self._startup_token: str | None = None
        self._udp_ack_lock = threading.Lock()
        self._udp_acks: set[int] = set()
        self._runtime_asset_lock = threading.Lock()
        self._runtime_asset_downloads: set[str] = set()

        self._udp_thread = threading.Thread(target=self._udp_read_loop, daemon=True)
        self._udp_thread.start()
        self._send_thread = threading.Thread(target=self._send_loop, daemon=True)
        self._send_thread.start()
        self._thread = threading.Thread(target=self._connect_loop, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self.connected = False
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        try:
            self._udp_socket.close()
        except OSError:
            pass

    def send_place(self, cell: Cell, digest: str, orientation: int, include_asset: bool = False) -> None:
        self._enqueue_tcp(
            {
                "type": "place",
                "cell": list(cell),
                "hash": digest,
                "orientation": orientation,
                "_attach_asset": include_asset,
            }
        )

    def send_set_box(self, cell: Cell, digest: str, orientation: int, include_asset: bool = False) -> None:
        self._enqueue_tcp(
            {
                "type": "set_box",
                "cell": list(cell),
                "hash": digest,
                "orientation": orientation,
                "_attach_asset": include_asset,
            }
        )

    def send_delete(self, cell: Cell) -> None:
        self._enqueue_tcp({"type": "delete", "cell": list(cell)})

    def send_rotate(self, cell: Cell, orientation: int) -> None:
        self._enqueue_tcp({"type": "rotate", "cell": list(cell), "orientation": orientation})

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
        with self._latest_player_state_lock:
            self._latest_player_state = message

    def asset_payload(self, digest: str) -> str | None:
        path = box_path_for_hash(self.paths.boxes_dir, digest)
        if not path.is_file():
            return None
        return encode_file_base64(path)

    def asset_path(self, digest: str) -> Path:
        return box_path_for_hash(self.paths.boxes_dir, digest)

    def ensure_runtime_asset(self, digest: str) -> None:
        if not digest or self.player_id is None:
            return
        try:
            if box_path_for_hash(self.paths.boxes_dir, digest).is_file():
                return
        except WorldFormatError:
            return
        token = self._startup_token
        if not token:
            return
        with self._runtime_asset_lock:
            if digest in self._runtime_asset_downloads:
                return
            self._runtime_asset_downloads.add(digest)
        player_id = self.player_id
        client_uuid = self.client_uuid

        def worker() -> None:
            try:
                self._download_startup_asset_bucket([digest], token, client_uuid, self._startup_asset_generation)
            finally:
                with self._runtime_asset_lock:
                    self._runtime_asset_downloads.discard(digest)

        threading.Thread(target=worker, daemon=True).start()

    def poll(self, max_messages: int | None = None) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        while max_messages is None or len(messages) < max_messages:
            try:
                messages.append(self.incoming.get_nowait())
            except queue.Empty:
                break
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
                send_socket_message(
                    sock,
                    {
                        "type": "hello",
                        "protocol": 2,
                        "client_uuid": self.client_uuid,
                        "include_assets": self._prefer_inline_startup_assets,
                    },
                )
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
        self._startup_asset_generation += 1
        self.player_id = None
        self.udp_host = self.host
        self.udp_port = None
        self.udp_enabled = False
        self.udp_status = "disabled"
        with self._startup_asset_lock:
            self.startup_assets_pending = False
            self.startup_assets_total = 0
            self.startup_assets_done = 0
            self.startup_assets_failed = False
            self.startup_assets_complete.set()
        self._udp_token = None
        self._startup_token = None
        with self._udp_ack_lock:
            self._udp_acks.clear()
        with self._runtime_asset_lock:
            self._runtime_asset_downloads.clear()

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

    def _send_loop(self) -> None:
        next_player_state_at = 0.0
        while not self._stop.is_set():
            sent_work = False
            try:
                message = self._send_queue.get(timeout=0.01)
            except queue.Empty:
                message = None
            if message is not None:
                self._send_tcp_now(message)
                sent_work = True

            now = time.monotonic()
            if now >= next_player_state_at:
                next_player_state_at = now + PLAYER_STATE_SEND_INTERVAL
                player_state = self._take_latest_player_state()
                if player_state is not None:
                    self._send_player_state_now(player_state)
                    sent_work = True
            if not sent_work:
                time.sleep(0.001)

    def _handle_incoming(self, message: dict[str, Any]) -> None:
        message_type = message.get("type")
        if message_type == "welcome":
            self._apply_welcome(message)
            self.incoming.put({"type": "welcome"})
            return
        elif message_type == "udp_status":
            self.udp_enabled = bool(message.get("enabled"))
            self.udp_status = "enabled" if self.udp_enabled else "fallback"
        elif message_type == "asset":
            digest = str(message.get("hash", ""))
            encoded = message.get("data")
            if isinstance(encoded, str) and encoded:
                self._store_asset(digest, encoded)
            self.incoming.put({"type": "asset", "hash": digest})
            return
        elif message_type == "box_set":
            pass
        elif message_type == "box_removed":
            pass
        elif message_type == "player_state":
            self._apply_player_state(message)
            return
        elif message_type == "player_left":
            try:
                with self._remote_lock:
                    self.remote_players.pop(int(message.get("player_id")), None)
            except (TypeError, ValueError):
                pass
            return
        self.incoming.put(message)

    def _apply_welcome(self, message: dict[str, Any]) -> None:
        self.player_id = int(message["player_id"])
        self.default_hash = str(message["default_hash"])
        for asset in message.get("assets", []):
            self._store_asset(str(asset.get("hash", "")), str(asset.get("data", "")))
        if message.get("assets"):
            self._prefer_inline_startup_assets = False

        manifest = message.get("asset_manifest", [])
        missing_assets = self._missing_manifest_hashes(manifest)
        token = str(message.get("startup_token", "")) or None
        self._startup_token = token
        if missing_assets:
            client_uuid = str(message.get("client_uuid", self.client_uuid)) or self.client_uuid
            channels = int(message.get("startup_asset_channels", DEFAULT_STARTUP_ASSET_CHANNELS))
            self._start_parallel_startup_assets(missing_assets, token or "", client_uuid, channels)
        else:
            with self._startup_asset_lock:
                self.startup_assets_pending = False
                self.startup_assets_total = 0
                self.startup_assets_done = 0
                self.startup_assets_failed = False
                self.startup_assets_complete.set()
            self._prefer_inline_startup_assets = False

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
            self._enqueue_tcp({"type": "udp_probe_result", "sent": 0, "received": 0})

    def _missing_manifest_hashes(self, manifest: object) -> list[str]:
        if not isinstance(manifest, list):
            return []
        missing: list[str] = []
        for item in manifest:
            if not isinstance(item, dict):
                continue
            digest = str(item.get("hash", ""))
            if not digest:
                continue
            try:
                path = box_path_for_hash(self.paths.boxes_dir, digest)
            except WorldFormatError:
                continue
            if not path.is_file():
                missing.append(digest)
        return missing

    def _start_parallel_startup_assets(
        self,
        hashes: list[str],
        token: str,
        client_uuid: str,
        channels: int,
    ) -> None:
        if not token or self.player_id is None:
            with self._startup_asset_lock:
                self.startup_assets_pending = False
                self.startup_assets_failed = True
                self.startup_assets_complete.set()
            return
        self._startup_asset_generation += 1
        generation = self._startup_asset_generation
        with self._runtime_asset_lock:
            self._runtime_asset_downloads.clear()
        with self._startup_asset_lock:
            self.startup_assets_pending = True
            self.startup_assets_total = len(hashes)
            self.startup_assets_done = 0
            self.startup_assets_failed = False
            self.startup_assets_complete.clear()
        channels = max(1, min(channels, max(1, len(hashes))))
        buckets = [hashes[index::channels] for index in range(channels)]
        remaining = len([bucket for bucket in buckets if bucket])
        remaining_lock = threading.Lock()

        def worker(bucket: list[str]) -> None:
            nonlocal remaining
            success = False
            try:
                success = self._download_startup_asset_bucket(bucket, token, client_uuid, generation)
            finally:
                if not success:
                    with self._startup_asset_lock:
                        if generation == self._startup_asset_generation:
                            self.startup_assets_failed = True
                with remaining_lock:
                    remaining -= 1
                    if remaining <= 0 and generation == self._startup_asset_generation:
                        failed = False
                        with self._startup_asset_lock:
                            failed = self.startup_assets_failed
                            self.startup_assets_pending = False
                            self.startup_assets_complete.set()
                        if failed:
                            self._prefer_inline_startup_assets = True
                            self._force_reconnect()

        for bucket in buckets:
            if bucket:
                threading.Thread(target=worker, args=(bucket,), daemon=True).start()

    def _download_startup_asset_bucket(
        self,
        hashes: list[str],
        token: str,
        client_uuid: str,
        generation: int,
    ) -> bool:
        if self.player_id is None:
            return False
        try:
            sock = socket.create_connection((self.host, self.port), timeout=3.0)
        except OSError:
            return False
        try:
            send_socket_message(
                sock,
                {
                    "type": "asset_stream",
                    "protocol": 2,
                    "player_id": self.player_id,
                    "client_uuid": client_uuid,
                    "startup_token": token,
                    "hashes": hashes,
                    "raw": True,
                },
            )
            buffer = bytearray()
            while not self._stop.is_set() and generation == self._startup_asset_generation:
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
                    if not isinstance(message, dict):
                        continue
                    if message.get("type") == "asset":
                        digest = str(message.get("hash", ""))
                        self._store_asset(digest, str(message.get("data", "")))
                        with self._startup_asset_lock:
                            if generation == self._startup_asset_generation:
                                self.startup_assets_done = min(self.startup_assets_total, self.startup_assets_done + 1)
                        self.incoming.put({"type": "asset", "hash": digest})
                    elif message.get("type") == "asset_raw":
                        digest = str(message.get("hash", ""))
                        try:
                            size = int(message.get("size", 0))
                        except (TypeError, ValueError):
                            return False
                        if size < 0 or not self._receive_raw_asset(sock, buffer, digest, size):
                            return False
                        with self._startup_asset_lock:
                            if generation == self._startup_asset_generation:
                                self.startup_assets_done = min(self.startup_assets_total, self.startup_assets_done + 1)
                        self.incoming.put({"type": "asset", "hash": digest})
                    elif message.get("type") == "asset_stream_done":
                        return all(box_path_for_hash(self.paths.boxes_dir, digest).is_file() for digest in hashes)
        except (OSError, ValueError):
            return False
        finally:
            try:
                sock.close()
            except OSError:
                pass
        return False

    def _receive_raw_asset(self, sock: socket.socket, buffer: bytearray, digest: str, size: int) -> bool:
        if not digest:
            return False
        temp_path: Path | None = None
        remaining = size
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".box") as temp_file:
                temp_path = Path(temp_file.name)
                if buffer:
                    chunk_size = min(len(buffer), remaining)
                    temp_file.write(buffer[:chunk_size])
                    del buffer[:chunk_size]
                    remaining -= chunk_size
                while remaining > 0:
                    data = sock.recv(min(65536, remaining))
                    if not data:
                        return False
                    temp_file.write(data)
                    remaining -= len(data)
            actual = store_box_file_by_hash(self.paths.boxes_dir, temp_path)
            if actual != digest:
                box_path_for_hash(self.paths.boxes_dir, actual).unlink(missing_ok=True)
                return False
            return True
        except (BoxFormatError, WorldFormatError, OSError, ValueError):
            return False
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def startup_asset_progress(self) -> tuple[int, int, bool]:
        with self._startup_asset_lock:
            return self.startup_assets_done, self.startup_assets_total, self.startup_assets_failed

    def _force_reconnect(self) -> None:
        sock = self._sock
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

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
            self._enqueue_tcp({"type": "udp_probe_result", "token": token, "sent": count, "received": received})

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

    def _enqueue_tcp(self, message: dict[str, Any]) -> None:
        if self._stop.is_set():
            return
        self._send_queue.put(message)

    def _take_latest_player_state(self) -> dict[str, Any] | None:
        with self._latest_player_state_lock:
            state = self._latest_player_state
            self._latest_player_state = None
            return state

    def _send_player_state_now(self, message: dict[str, Any]) -> None:
        if self.udp_enabled and self.udp_port is not None:
            try:
                payload = json.dumps(message, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
                self._udp_socket.sendto(payload, (self.udp_host, self.udp_port))
                return
            except OSError:
                self.udp_enabled = False
                self.udp_status = "fallback"
        self._send_tcp_now(message)

    def _send_tcp_now(self, message: dict[str, Any]) -> None:
        sock = self._sock
        if sock is None or not self.connected:
            return
        if message.pop("_attach_asset", False):
            digest = str(message.get("hash", ""))
            message["asset"] = self.asset_payload(digest)
        try:
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
