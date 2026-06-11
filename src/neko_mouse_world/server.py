from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass, field
import binascii
from collections import deque
import os
from pathlib import Path
import socketserver
import json
import math
import socket
import secrets
import subprocess
import tempfile
import threading
import sys
import time
import traceback
from typing import Any, BinaryIO, TextIO

from box_editor_view.box_file import BoxFormatError

from .box_assets import ensure_default_box, store_box_file_by_hash
from .net_protocol import (
    SERVER_LOG_PERMISSION_DENIED_LINE,
    cell_to_list,
    decode_file_base64,
    encode_file_base64,
    is_valid_user_id,
    list_to_cell,
    read_message,
    send_message,
)
from .orientation import IDENTITY_ORIENTATION, validate_orientation
from .version import get_neko_mouse_world_version
from .world_file import (
    Cell,
    PlayerPosition,
    WorldFormatError,
    WorldMap,
    box_path_for_hash,
    load_or_create_world,
    save_world,
    validate_player_position,
)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5678
UDP_PROBE_COUNT = 5
UDP_PROBE_THRESHOLD = 3
DEFAULT_STARTUP_ASSET_CHANNELS = 4
SERVER_LOG_LIMIT = 300
PLAYER_SPAWN_HEIGHT = 1.8
STDOUT_ONLY_MARKER = "\x1eNEKO_MOUSE_WORLD_STDOUT_ONLY\x1e"
TCP_HEARTBEAT_INTERVAL = 5.0
UDP_RESET_PROBE_INTERVAL = 1.0
DEFAULT_PERMISSION_ENV = {
    "allow_set": "DEFAULT_SET",
    "allow_fly": "DEFAULT_FLY",
    "allow_break": "DEFAULT_BREAK",
    "allow_cmd": "DEFAULT_CMD",
}
DISPLAY_USER_ID_ENV = "DISPLAY_USER_ID"
ALLOW_CONNECT_ENV = "ALLOW_CONNECT"
ENV_TRUE_VALUES = {"1", "true", "yes", "on", "y", "t"}
ENV_FALSE_VALUES = {"0", "false", "no", "off", "n", "f"}
SERVER_COMMAND_HELP = (
    "Server commands:\n"
    "  Commands are restricted Python expressions. Lists, dicts, and int/float arithmetic are supported.\n"
    "  help(): show this command summary\n"
    "  ls(): list online user IDs\n"
    "  seepri(): print every online user's set/break/fly/cmd permissions\n"
    "  stop(): kick all players, save, clean unused .box files, and stop the server\n"
    "  kick(user_id, reason=''): kick a player by string ID\n"
    "  setenv(name, value): set a non-empty server environment variable\n"
    "  getenv(name): return a server environment variable value\n"
    "  tp(user_id, x, y): teleport a player above the highest occupied grid cell at x,y\n"
    "  tp(user_id, x, y, z): teleport a player to x,y,z\n"
    "  allow_set(user_id, true/false): allow placing, rotating boxes, and editing with E\n"
    "  allow_fly(user_id, true/false): allow switching to fly mode\n"
    "  allow_break(user_id, true/false): allow breaking boxes\n"
    "  allow_cmd(user_id, true/false): allow using server commands and viewing server logs\n"
    "  DEFAULT_SET/DEFAULT_FLY/DEFAULT_BREAK/DEFAULT_CMD: environment defaults for new clients\n"
    "    default true; accepts true/false, 1/0, yes/no, on/off; existing clients are unchanged\n"
    "  DISPLAY_USER_ID: show/hide user ID labels above remote players; setenv updates clients live\n"
    "  ALLOW_CONNECT: when false, new clients are refused during handshake\n"
    "  client --user-id accepts letters, digits, underscores, and hyphens only\n"
    "    empty IDs are assigned as unique integer strings; requested conflicts get _1, _2, ... suffixes\n"
    "  pin(secret): enable commands for yourself when the secret matches the server --pin\n"
    "  ignore(*args): evaluate any number of arguments and return None\n"
    "Non-None command return values are logged with repr()."
)
SERVER_COMMAND_NAMES = {
    "help",
    "ls",
    "seepri",
    "stop",
    "kick",
    "setenv",
    "getenv",
    "tp",
    "allow_set",
    "allow_fly",
    "allow_break",
    "allow_cmd",
    "pin",
    "ignore",
}
SERVER_COMMAND_MAX_AST_NODES = 256
SERVER_COMMAND_MAX_CONTAINER_ITEMS = 256
SERVER_COMMAND_MAX_STRING_LENGTH = 4096
SERVER_COMMAND_MAX_ABS_NUMBER = 1_000_000_000
SERVER_COMMAND_MAX_POWER_EXPONENT = 64
SERVER_COMMAND_NUMERIC_BINOPS = (
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.BitAnd,
    ast.BitOr,
    ast.BitXor,
    ast.LShift,
    ast.RShift,
)
SERVER_COMMAND_COMPARE_OPS = (
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Is,
    ast.IsNot,
    ast.In,
    ast.NotIn,
)


class ServerCommandError(ValueError):
    """Raised when an in-game server command is invalid."""


@dataclass
class ClientPermissions:
    allow_set: bool = True
    allow_fly: bool = True
    allow_break: bool = True
    allow_cmd: bool = True

    def as_dict(self) -> dict[str, bool]:
        return {
            "allow_set": self.allow_set,
            "allow_fly": self.allow_fly,
            "allow_break": self.allow_break,
            "allow_cmd": self.allow_cmd,
        }

    @classmethod
    def from_environment(cls) -> "ClientPermissions":
        return cls(
            allow_set=_environment_bool(DEFAULT_PERMISSION_ENV["allow_set"], True),
            allow_fly=_environment_bool(DEFAULT_PERMISSION_ENV["allow_fly"], True),
            allow_break=_environment_bool(DEFAULT_PERMISSION_ENV["allow_break"], True),
            allow_cmd=_environment_bool(DEFAULT_PERMISSION_ENV["allow_cmd"], True),
        )


def _environment_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in ENV_TRUE_VALUES:
        return True
    if normalized in ENV_FALSE_VALUES:
        return False
    return default


def _print_traceback(context: str) -> None:
    try:
        print(f"[server-error] {context}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
    except BaseException:
        pass


def _generate_random_pin() -> str:
    return f"{secrets.randbelow(900_000_000) + 100_000_000:09d}"


def _print_stdout_only(text: str) -> None:
    try:
        sys.stdout.write(f"{STDOUT_ONLY_MARKER}{text}\n")
        sys.stdout.flush()
    except BaseException:
        pass


def _is_udp_peer_reset_error(exc: OSError) -> bool:
    code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
    return isinstance(exc, ConnectionResetError) or code == 10054


@dataclass
class ClientConnection:
    player_id: int
    user_id: str
    client_uuid: str
    writer: BinaryIO
    endpoint: str = "unknown:0"
    lock: threading.Lock = field(default_factory=threading.Lock)
    startup_token: str = field(default_factory=lambda: secrets.token_hex(16))
    startup_asset_hashes: set[str] = field(default_factory=set)
    udp_token: str = field(default_factory=lambda: secrets.token_hex(16))
    udp_address: tuple[str, int] | None = None
    udp_enabled: bool = False
    udp_probe_seen: set[int] = field(default_factory=set)
    kicked: bool = False
    logout_logged: bool = False
    permissions: ClientPermissions = field(default_factory=ClientPermissions)
    spawn_pos: PlayerPosition = (0.5, -4.0, 0.0)
    loaded: bool = False

    def send(self, message: dict[str, Any]) -> None:
        with self.lock:
            send_message(self.writer, message)


class WorldServerState:
    def __init__(self, world_dir: Path) -> None:
        loaded = load_or_create_world(world_dir)
        self.paths = loaded.paths
        self.world_map: WorldMap = loaded.world_map
        self.player_positions: dict[str, PlayerPosition] = dict(loaded.player_positions)
        self.default_hash = ensure_default_box(self.paths.boxes_dir)
        self.clients: dict[int, ClientConnection] = {}
        self.player_states: dict[int, dict[str, Any]] = {}
        self.udp_host = DEFAULT_HOST
        self.udp_port = 0
        self.startup_asset_channels = DEFAULT_STARTUP_ASSET_CHANNELS
        self.pin_secret = ""
        self.package_version = get_neko_mouse_world_version()
        self.lock = threading.RLock()
        self.next_player_id = 1
        self.log_lines: deque[dict[str, str]] = deque(maxlen=SERVER_LOG_LIMIT)
        self.stdout_capture_active = False
        self.accepting_clients = True
        self.stop_requested = threading.Event()
        self.server: socketserver.BaseServer | None = None
        self.last_udp_reset_probe = 0.0

    def configure_udp(self, host: str, port: int) -> None:
        with self.lock:
            self.udp_host = host
            self.udp_port = port

    def configure_startup_asset_channels(self, channels: int) -> None:
        with self.lock:
            self.startup_asset_channels = max(1, min(int(channels), 16))

    def configure_pin(self, pin_secret: str | None) -> None:
        with self.lock:
            self.pin_secret = str(pin_secret or "")

    def attach_server(self, server: socketserver.BaseServer) -> None:
        with self.lock:
            self.server = server

    def client_config(self) -> dict[str, bool]:
        return {
            "display_user_id": _environment_bool(DISPLAY_USER_ID_ENV, True),
        }

    def client_config_message(self) -> dict[str, Any]:
        return {"type": "client_config", **self.client_config()}

    def register(
        self,
        writer: BinaryIO,
        client_uuid: str,
        desired_user_id: str = "",
        endpoint: str = "unknown:0",
    ) -> ClientConnection:
        with self.lock:
            if not self.accepting_clients:
                raise ConnectionError("server is stopping")
            player_id = self.next_player_id
            self.next_player_id += 1
            user_id = self._unique_user_id(desired_user_id)
            client = ClientConnection(
                player_id=player_id,
                user_id=user_id,
                client_uuid=client_uuid,
                writer=writer,
                endpoint=endpoint,
                permissions=ClientPermissions.from_environment(),
                spawn_pos=self._spawn_position_for_user(user_id),
            )
            self.clients[player_id] = client
            return client

    def _spawn_position_for_user(self, user_id: str) -> PlayerPosition:
        try:
            saved = validate_player_position(self.player_positions.get(user_id, (0.5, -4.0, 0.0)))
        except WorldFormatError:
            saved = (0.5, -4.0, 0.0)
        return (saved[0], saved[1], self._safe_spawn_z(saved[0], saved[1], saved[2]))

    def _safe_spawn_z(self, x: float, y: float, preferred_z: float) -> float:
        preferred_z = max(0.0, preferred_z)
        column_x = math.floor(x)
        column_y = math.floor(y)
        player_bottom = preferred_z
        player_top = preferred_z + PLAYER_SPAWN_HEIGHT
        with self.lock:
            blocked = any(
                cell[0] == column_x
                and cell[1] == column_y
                and cell[2] < player_top
                and cell[2] + 1 > player_bottom
                for cell in self.world_map.boxes
            )
        return self._tp_ground_z(x, y) if blocked else preferred_z

    def _unique_user_id(self, desired_user_id: str) -> str:
        base = str(desired_user_id or "").strip()
        used = {client.user_id for client in self.clients.values()}
        if not base or not is_valid_user_id(base):
            candidate = 1
            while str(candidate) in used:
                candidate += 1
            return str(candidate)
        if base not in used:
            return base
        suffix = 1
        while f"{base}_{suffix}" in used:
            suffix += 1
        return f"{base}_{suffix}"

    def unregister(self, client: ClientConnection) -> None:
        try:
            should_cleanup = False
            log_logout = False
            removed = False
            with self.lock:
                if self.clients.get(client.player_id) is not client:
                    return
                self._persist_client_position(client)
                self.clients.pop(client.player_id, None)
                self.player_states.pop(client.player_id, None)
                self._save()
                removed = True
                if not client.logout_logged:
                    client.logout_logged = True
                    log_logout = True
                should_cleanup = not self.clients and not self.stop_requested.is_set()
            if log_logout:
                self.log(f"CLIENT LOGOUT: {client.endpoint}:{client.user_id}")
            try:
                client.writer.close()
            except OSError:
                pass
            except Exception:
                _print_traceback(f"failed to close writer for player {client.player_id}")
            if removed:
                self.broadcast({"type": "player_left", "player_id": client.player_id}, exclude=client.player_id)
            if should_cleanup:
                self._cleanup_unused_boxes()
        except Exception:
            _print_traceback(f"failed to unregister player {client.player_id}")

    def start_client_heartbeat(self, client: ClientConnection, stop_event: threading.Event) -> threading.Thread:
        thread = threading.Thread(
            target=self._client_heartbeat_loop,
            args=(client, stop_event),
            daemon=True,
        )
        thread.start()
        return thread

    def _client_heartbeat_loop(self, client: ClientConnection, stop_event: threading.Event) -> None:
        while not stop_event.wait(TCP_HEARTBEAT_INTERVAL):
            if not self._send_client_heartbeat(client):
                return

    def _send_client_heartbeat(self, client: ClientConnection) -> bool:
        with self.lock:
            if self.clients.get(client.player_id) is not client or self.stop_requested.is_set():
                return False
        try:
            client.send({"type": "server_ping"})
            return True
        except OSError:
            self.unregister(client)
            return False
        except Exception:
            _print_traceback(f"failed to heartbeat player {client.player_id}")
            self.unregister(client)
            return False

    def probe_clients_after_udp_reset(self) -> None:
        now = time.monotonic()
        with self.lock:
            if now - self.last_udp_reset_probe < UDP_RESET_PROBE_INTERVAL:
                return
            self.last_udp_reset_probe = now
            clients = list(self.clients.values())
        threading.Thread(target=self._probe_clients_after_udp_reset, args=(clients,), daemon=True).start()

    def _probe_clients_after_udp_reset(self, clients: list[ClientConnection]) -> None:
        for client in clients:
            self._send_client_heartbeat(client)

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
                "server_version": self.package_version,
                "player_id": player_id,
                "user_id": client.user_id,
                "spawn_pos": list(client.spawn_pos),
                "default_hash": self.default_hash,
                "client_uuid": client.client_uuid,
                "startup_token": client.startup_token,
                "startup_asset_channels": client.startup_asset_hashes and self.startup_asset_channels or 1,
                "permissions": client.permissions.as_dict(),
                "client_config": self.client_config(),
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
                    {
                        "player_id": other_player_id,
                        "user_id": self.clients[other_player_id].user_id,
                        **state,
                    }
                    for other_player_id, state in self.player_states.items()
                    if other_player_id != player_id
                    and other_player_id in self.clients
                    and self.clients[other_player_id].loaded
                ],
                "logs": list(self.log_lines) if client.permissions.allow_cmd else [],
                "server_log_allowed": client.permissions.allow_cmd,
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
        except Exception:
            _print_traceback("failed to parse asset stream request")
            return

        try:
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
        except Exception:
            _print_traceback("asset stream failed")
            self._send_asset_stream_error(writer, "asset stream failed")

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
        except Exception:
            _print_traceback(f"failed to stream asset {digest}")
            return False

    def _send_asset_stream_error(self, writer: BinaryIO, message: str) -> None:
        try:
            send_message(writer, {"type": "error", "message": message})
        except OSError:
            return
        except Exception:
            _print_traceback("failed to send asset stream error")
            return

    def handle_message(self, client: ClientConnection, message: dict[str, Any]) -> None:
        try:
            message_type = message.get("type")
            if client.kicked and message_type != "kick_ack":
                return
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
            elif message_type == "client_loaded":
                self._handle_client_loaded(client, message)
            elif message_type == "held_item":
                self._handle_held_item(client, message)
            elif message_type == "server_command":
                self._handle_server_command(client, message)
            elif message_type == "kick_ack":
                self._handle_kick_ack(client)
            else:
                client.send({"type": "error", "message": f"unknown message type {message_type!r}"})
        except (TypeError, ValueError, WorldFormatError) as exc:
            try:
                client.send({"type": "error", "message": str(exc)})
            except OSError:
                pass
        except Exception:
            _print_traceback("failed to handle client message")
            try:
                client.send({"type": "error", "message": "internal server error"})
            except OSError:
                pass

    def log(self, text: str) -> None:
        line = self._redact_pin_secret_text(str(text))
        if self.stdout_capture_active:
            print(line, flush=True)
            return
        self._record_log_line(line, "stdout")
        print(line, flush=True)

    def _record_log_line(self, line: str, stream: str = "stdout") -> None:
        try:
            line = self._redact_pin_secret_text(str(line)).rstrip("\r")
            if "\n" in line:
                for part in line.splitlines():
                    self._record_log_line(part, stream)
                return
            if not line:
                return
            stream = "stderr" if stream == "stderr" else "stdout"
            entry = {"line": line, "stream": stream}
            with self.lock:
                self.log_lines.append(entry)
                clients = [client for client in self.clients.values() if client.permissions.allow_cmd]
            message = {"type": "server_log", **entry}
            for client in clients:
                try:
                    client.send(message)
                except BaseException:
                    pass
        except BaseException:
            pass

    def _log_error(self, text: str) -> None:
        text = self._redact_pin_secret_text(str(text))
        if self.stdout_capture_active:
            print(text, file=sys.stderr, flush=True)
            return
        self._record_log_line(text, "stderr")
        print(text, file=sys.stderr, flush=True)

    def _redact_pin_secret_text(self, text: str) -> str:
        with self.lock:
            pin_secret = self.pin_secret
        if pin_secret:
            return text.replace(pin_secret, "<redacted-pin>")
        return text

    def _handle_server_command(self, client: ClientConnection, message: dict[str, Any]) -> None:
        command = str(message.get("command", "")).strip()
        if not command:
            return
        sanitized_command = self._sanitize_server_command_for_log(command)
        pin_attempt = self._is_pin_command(command)
        if not client.permissions.allow_cmd and not pin_attempt:
            self._log_error(f"COMMAND DENIED: player {client.player_id} is not allowed to use commands")
            return
        self.log(f"{client.user_id}: {sanitized_command}")
        try:
            result = self._execute_server_command(client, command)
        except ServerCommandError as exc:
            self._log_error(f"COMMAND ERROR: {exc}")
            return
        except Exception:
            _print_traceback("server command failed")
            return
        if result is not None:
            self.log(repr(result))

    def _is_pin_command(self, command: str) -> bool:
        try:
            parsed = ast.parse(command, mode="eval")
        except SyntaxError:
            return False
        body = parsed.body
        return isinstance(body, ast.Call) and isinstance(body.func, ast.Name) and body.func.id == "pin"

    def _sanitize_server_command_for_log(self, command: str) -> str:
        try:
            parsed = ast.parse(command, mode="eval")
        except SyntaxError:
            return command
        redacted = self._redact_pin_calls(parsed.body)
        try:
            return ast.unparse(redacted)
        except Exception:
            return "pin(<redacted>)" if self._contains_pin_call(parsed.body) else command

    def _contains_pin_call(self, node: ast.AST) -> bool:
        for child in ast.walk(node):
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == "pin":
                return True
        return False

    def _redact_pin_calls(self, node: ast.AST) -> ast.AST:
        class PinRedactor(ast.NodeTransformer):
            def visit_Call(self, call_node: ast.Call) -> ast.AST:  # noqa: N802
                if isinstance(call_node.func, ast.Name) and call_node.func.id == "pin":
                    return ast.copy_location(
                        ast.Call(
                            func=ast.Name(id="pin", ctx=ast.Load()),
                            args=[ast.Constant(value="<redacted>")],
                            keywords=[],
                        ),
                        call_node,
                    )
                return self.generic_visit(call_node)

        redacted = PinRedactor().visit(ast.fix_missing_locations(ast.Expression(body=node))).body
        return ast.fix_missing_locations(redacted)

    def _execute_server_command(self, client: ClientConnection, command: str) -> object:
        try:
            parsed = ast.parse(command, mode="eval")
        except SyntaxError as exc:
            raise ServerCommandError(exc.msg) from exc
        self._validate_server_command_ast(parsed)
        environment = {
            "help": self._command_help,
            "ls": self._command_ls,
            "seepri": self._command_seepri,
            "stop": lambda: self._command_stop(client),
            "kick": lambda user_id, reason="": self._command_kick(client, user_id, reason),
            "setenv": self._command_setenv,
            "getenv": self._command_getenv,
            "tp": self._command_tp,
            "allow_set": lambda user_id, allowed: self._command_allow("allow_set", user_id, allowed),
            "allow_fly": lambda user_id, allowed: self._command_allow("allow_fly", user_id, allowed),
            "allow_break": lambda user_id, allowed: self._command_allow("allow_break", user_id, allowed),
            "allow_cmd": lambda user_id, allowed: self._command_allow("allow_cmd", user_id, allowed),
            "pin": lambda secret: self._command_pin(client, secret),
            "ignore": self._command_ignore,
            "true": True,
            "false": False,
        }
        try:
            return eval(compile(parsed, "<server-command>", "eval"), {"__builtins__": {}}, environment)
        except (TypeError, ValueError, ArithmeticError, KeyError, IndexError, NameError, MemoryError, RecursionError) as exc:
            raise ServerCommandError(str(exc)) from exc

    def _validate_server_command_ast(self, parsed: ast.Expression) -> None:
        if sum(1 for _node in ast.walk(parsed)) > SERVER_COMMAND_MAX_AST_NODES:
            raise ServerCommandError("command expression is too large")
        self._validate_server_command_expr(parsed.body)

    def _validate_server_command_expr(self, node: ast.AST) -> None:
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ServerCommandError("only documented command function calls are allowed")
            if node.func.id not in SERVER_COMMAND_NAMES:
                raise ServerCommandError(f"unknown command {node.func.id!r}; try help()")
            for argument in node.args:
                self._validate_server_command_expr(argument)
            for keyword in node.keywords:
                if keyword.arg is None:
                    raise ServerCommandError("**kwargs are not supported")
                self._validate_server_command_expr(keyword.value)
            return
        if isinstance(node, ast.BinOp):
            self._validate_server_command_numeric_expr(node)
            return
        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, (ast.UAdd, ast.USub)):
                self._validate_server_command_numeric_expr(node)
                return
            if isinstance(node.op, ast.Not):
                self._validate_server_command_expr(node.operand)
                return
            raise ServerCommandError("unsupported unary operator in command expression")
        if isinstance(node, ast.BoolOp):
            if not isinstance(node.op, (ast.And, ast.Or)):
                raise ServerCommandError("unsupported boolean operator in command expression")
            if len(node.values) > SERVER_COMMAND_MAX_CONTAINER_ITEMS:
                raise ServerCommandError("command expression has too many boolean terms")
            for value in node.values:
                self._validate_server_command_expr(value)
            return
        if isinstance(node, ast.Compare):
            self._validate_server_command_expr(node.left)
            for operator in node.ops:
                if not isinstance(operator, SERVER_COMMAND_COMPARE_OPS):
                    raise ServerCommandError("unsupported comparison operator in command expression")
            for comparator in node.comparators:
                self._validate_server_command_expr(comparator)
            return
        if isinstance(node, ast.IfExp):
            self._validate_server_command_expr(node.test)
            self._validate_server_command_expr(node.body)
            self._validate_server_command_expr(node.orelse)
            return
        if isinstance(node, (ast.List, ast.Tuple)):
            if len(node.elts) > SERVER_COMMAND_MAX_CONTAINER_ITEMS:
                raise ServerCommandError("command list is too large")
            for element in node.elts:
                self._validate_server_command_expr(element)
            return
        if isinstance(node, ast.Dict):
            if len(node.keys) > SERVER_COMMAND_MAX_CONTAINER_ITEMS:
                raise ServerCommandError("command dict is too large")
            for key, value in zip(node.keys, node.values):
                if key is None:
                    raise ServerCommandError("dict unpacking is not supported")
                self._validate_server_command_expr(key)
                self._validate_server_command_expr(value)
            return
        if isinstance(node, ast.Subscript):
            self._validate_server_command_expr(node.value)
            self._validate_server_command_slice(node.slice)
            return
        if isinstance(node, ast.Name) and node.id in {"true", "false"}:
            return
        if isinstance(node, ast.Constant):
            self._validate_server_command_constant(node.value)
            return
        raise ServerCommandError(
            "command must be a restricted Python expression using literals, lists, dicts, arithmetic, or documented commands"
        )

    def _validate_server_command_slice(self, node: ast.AST) -> None:
        if isinstance(node, ast.Slice):
            for part in (node.lower, node.upper, node.step):
                if part is not None:
                    self._validate_server_command_expr(part)
            return
        self._validate_server_command_expr(node)

    def _validate_server_command_numeric_expr(self, node: ast.AST) -> None:
        if isinstance(node, ast.Constant):
            self._validate_server_command_number(node.value)
            return
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            self._validate_server_command_numeric_expr(node.operand)
            return
        if isinstance(node, ast.BinOp):
            if not isinstance(node.op, SERVER_COMMAND_NUMERIC_BINOPS):
                raise ServerCommandError("unsupported numeric operator in command expression")
            self._validate_server_command_numeric_expr(node.left)
            self._validate_server_command_numeric_expr(node.right)
            if isinstance(node.op, ast.Pow):
                exponent = self._server_command_literal_number(node.right)
                if exponent is None or abs(exponent) > SERVER_COMMAND_MAX_POWER_EXPONENT:
                    raise ServerCommandError("power exponents must be numeric literals between -64 and 64")
            if isinstance(node.op, (ast.LShift, ast.RShift)):
                shift = self._server_command_literal_number(node.right)
                if not isinstance(shift, int) or shift < 0 or shift > SERVER_COMMAND_MAX_POWER_EXPONENT:
                    raise ServerCommandError("shift counts must be integer literals between 0 and 64")
            return
        raise ServerCommandError("numeric operations can only use int and float expressions")

    def _validate_server_command_constant(self, value: object) -> None:
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, str):
            if len(value) > SERVER_COMMAND_MAX_STRING_LENGTH:
                raise ServerCommandError("command string literal is too long")
            return
        self._validate_server_command_number(value)

    def _validate_server_command_number(self, value: object) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ServerCommandError("command literal type is not supported")
        if isinstance(value, float) and not math.isfinite(value):
            raise ServerCommandError("numeric literals must be finite")
        if abs(value) > SERVER_COMMAND_MAX_ABS_NUMBER:
            raise ServerCommandError("numeric literal is too large")

    def _server_command_literal_number(self, node: ast.AST) -> int | float | None:
        sign = 1
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            if isinstance(node.op, ast.USub):
                sign = -1
            node = node.operand
        if not isinstance(node, ast.Constant):
            return None
        value = node.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return sign * value

    def _command_help(self) -> None:
        self.log(SERVER_COMMAND_HELP)
        return None

    def _command_ls(self) -> list[str]:
        with self.lock:
            ids = sorted(client.user_id for client in self.clients.values())
        return ids

    def _command_seepri(self) -> None:
        with self.lock:
            rows = [
                (
                    client.user_id,
                    client.permissions.allow_set,
                    client.permissions.allow_break,
                    client.permissions.allow_fly,
                    client.permissions.allow_cmd,
                )
                for client in self.clients.values()
            ]
        for user_id, can_set, can_break, can_fly, can_cmd in sorted(rows, key=lambda row: row[0]):
            self.log(f"{user_id}: set: {can_set}, break: {can_break}, fly: {can_fly}, cmd: {can_cmd}")
        return None

    def _command_stop(self, requester: ClientConnection) -> None:
        reason = f"server stopped by command (triggered by {requester.user_id})"
        self.log(f"Server stop requested by {requester.user_id}")
        threading.Thread(
            target=self.request_shutdown,
            args=(reason, "Server stopped by command"),
            daemon=True,
        ).start()
        return None

    def request_shutdown(self, reason: str, log_message: str = "Server stopped") -> None:
        try:
            with self.lock:
                if self.stop_requested.is_set():
                    return
                self.stop_requested.set()
                self.accepting_clients = False
                clients = list(self.clients.values())
                server = self.server
            for client in clients:
                client.kicked = True
                try:
                    client.send({"type": "kicked", "reason": reason, "by": "server"})
                except OSError:
                    pass
                except Exception:
                    _print_traceback(f"failed to send stop kick to player {client.player_id}")
            for client in clients:
                try:
                    client.writer.close()
                except OSError:
                    pass
                except Exception:
                    _print_traceback(f"failed to close player {client.player_id} during stop")
            with self.lock:
                for client in clients:
                    self._persist_client_position(client)
                self.clients.clear()
                self.player_states.clear()
                self._save()
            self._cleanup_unused_boxes()
            self.log(log_message)
            if server is not None:
                try:
                    server.shutdown()
                except Exception:
                    _print_traceback("failed to shutdown tcp server")
        except Exception:
            _print_traceback("server shutdown failed")

    def _command_ignore(self, *args: object) -> None:
        return None

    def _command_pin(self, client: ClientConnection, secret: object) -> None:
        with self.lock:
            expected = self.pin_secret
        if not expected:
            raise ServerCommandError("pin() is not enabled on this server")
        if not secrets.compare_digest(str(secret), expected):
            raise ServerCommandError("pin() secret did not match")
        changed = not client.permissions.allow_cmd
        client.permissions.allow_cmd = True
        self._send_permissions(client, log_access_changed=changed)
        self.log(f"PIN ACCEPTED: {client.user_id} can use commands")
        return None

    def _command_kick(self, requester: ClientConnection, user_id: object, reason: object = "") -> None:
        target = self._client_for_user_id(user_id, "kick()")
        reason = str(reason)
        target.kicked = True
        payload = {"type": "kicked", "reason": reason, "by": requester.user_id}
        try:
            target.send(payload)
        except Exception:
            _print_traceback(f"failed to send kick message to player {target.player_id}")
        reason_text = f": {reason}" if reason else ""
        self.log(f"Kicked {target.user_id}{reason_text}")
        return None

    def _command_setenv(self, name: object, value: object) -> None:
        name = str(name)
        if not name:
            raise ServerCommandError("setenv() variable name cannot be empty")
        os.environ[name] = str(value)
        if name == DISPLAY_USER_ID_ENV:
            self.broadcast(self.client_config_message())

    def _command_getenv(self, name: object) -> str | None:
        name = str(name)
        if not name:
            raise ServerCommandError("getenv() variable name cannot be empty")
        return os.environ.get(name)

    def _command_tp(self, user_id: object, x: object, y: object, z: object | None = None) -> None:
        target = self._client_for_user_id(user_id, "tp()")
        player_id = target.player_id
        try:
            target_x = float(x)
            target_y = float(y)
            target_z = self._tp_ground_z(target_x, target_y) if z is None else float(z)
        except (TypeError, ValueError) as exc:
            raise ServerCommandError("tp() coordinates must be numbers") from exc
        try:
            target.send({"type": "teleport", "pos": [target_x, target_y, target_z]})
        except Exception:
            _print_traceback(f"failed to send teleport to player {target.player_id}")
            raise ServerCommandError(f"failed to teleport player {target.player_id}")
        with self.lock:
            state = dict(self.player_states.get(player_id, {}))
            state["pos"] = [target_x, target_y, target_z]
            state["velocity"] = [0.0, 0.0, 0.0]
            self.player_states[player_id] = state
            self._persist_client_position(target, state)
            self._save()
        self.log(f"Teleported {target.user_id} to ({target_x}, {target_y}, {target_z})")
        return None

    def _command_allow(self, permission_name: str, user_id: object, allowed: object) -> None:
        target = self._client_for_user_id(user_id, f"{permission_name}()")
        allowed_bool = self._parse_command_bool(allowed, f"{permission_name}()")
        with self.lock:
            previous_bool = bool(getattr(target.permissions, permission_name))
            setattr(target.permissions, permission_name, allowed_bool)
        self._send_permissions(target, log_access_changed=permission_name == "allow_cmd" and previous_bool != allowed_bool)
        if permission_name == "allow_fly" and not allowed_bool:
            self._force_player_walk(target)
        self.log(f"Set {permission_name} for {target.user_id} to {allowed_bool}")
        return None

    def _client_for_user_id(self, value: object, command_name: str) -> ClientConnection:
        user_id = str(value).strip()
        if not user_id:
            raise ServerCommandError(f"{command_name} requires a non-empty user_id")
        with self.lock:
            for client in self.clients.values():
                if client.user_id == user_id:
                    return client
            try:
                numeric_player_id = int(user_id)
            except ValueError:
                numeric_player_id = None
            if numeric_player_id is not None:
                client = self.clients.get(numeric_player_id)
                if client is not None:
                    return client
        raise ServerCommandError(f"{command_name} user_id {user_id!r} is not online")

    def _parse_command_bool(self, value: object, command_name: str) -> bool:
        if isinstance(value, bool):
            return value
        raise ServerCommandError(f"{command_name} expects true/false or True/False as a boolean literal")

    def _send_permissions(self, client: ClientConnection, log_access_changed: bool = False) -> None:
        try:
            if not client.permissions.allow_set:
                self._clear_held_item_for_client(client)
            client.send({"type": "permissions", "permissions": client.permissions.as_dict()})
            if log_access_changed:
                self._send_server_log_access(client)
        except OSError:
            pass
        except Exception:
            _print_traceback(f"failed to send permissions to player {client.player_id}")

    def _clear_held_item_for_client(self, client: ClientConnection) -> None:
        with self.lock:
            state = self.player_states.get(client.player_id)
            if not state or not state.get("held_visible"):
                return
            state = dict(state)
            state["held_hash"] = ""
            state["held_orientation"] = IDENTITY_ORIENTATION
            state["held_visible"] = False
            self.player_states[client.player_id] = state
        self._broadcast_player_state(client.player_id, state, udp_socket=None)

    def _send_server_log_access(self, client: ClientConnection) -> None:
        try:
            with self.lock:
                allowed = client.permissions.allow_cmd
                logs = list(self.log_lines) if allowed else []
            message: dict[str, Any] = {
                "type": "server_log_permission",
                "allowed": allowed,
                "message": SERVER_LOG_PERMISSION_DENIED_LINE,
            }
            if allowed:
                message["logs"] = logs
            client.send(message)
        except OSError:
            pass
        except Exception:
            _print_traceback(f"failed to send server log access to player {client.player_id}")

    def _force_player_walk(self, client: ClientConnection) -> None:
        with self.lock:
            state = dict(self.player_states.get(client.player_id, {}))
            state["move_mode"] = "walk"
            self.player_states[client.player_id] = state
        try:
            client.send({"type": "force_move_mode", "move_mode": "walk"})
        except OSError:
            pass
        except Exception:
            _print_traceback(f"failed to force walk mode for player {client.player_id}")

    def _tp_ground_z(self, x: float, y: float) -> float:
        column_x = math.floor(x)
        column_y = math.floor(y)
        with self.lock:
            tops = [cell[2] + 1 for cell in self.world_map.boxes if cell[0] == column_x and cell[1] == column_y]
        return float(max(tops, default=0))

    def _handle_kick_ack(self, client: ClientConnection) -> None:
        self.log(f"KICK ACK: {client.player_id}")
        try:
            client.writer.close()
        except OSError:
            pass
        except Exception:
            _print_traceback(f"failed to close kicked player {client.player_id} after ack")

    def _require_permission(self, client: ClientConnection, permission_name: str, action: str) -> bool:
        allowed = bool(getattr(client.permissions, permission_name))
        if allowed:
            return True
        try:
            client.send({"type": "error", "message": f"not allowed to {action}"})
        except OSError:
            pass
        except Exception:
            _print_traceback(f"failed to send permission error to player {client.player_id}")
        self._log_error(f"PERMISSION DENIED: player {client.player_id} cannot {action}")
        return False

    def _handle_place(self, client: ClientConnection, message: dict[str, Any]) -> None:
        if not self._require_permission(client, "allow_set", "place boxes"):
            return
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
        if not self._require_permission(client, "allow_set", "set boxes"):
            return
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

    def _handle_delete(self, client: ClientConnection, message: dict[str, Any]) -> None:
        if not self._require_permission(client, "allow_break", "break boxes"):
            return
        cell = list_to_cell(message.get("cell"))
        with self.lock:
            removed = self.world_map.remove_box(cell)
            if removed:
                self._save()
        if removed:
            self.broadcast({"type": "box_removed", "cell": cell_to_list(cell)})

    def _handle_rotate(self, client: ClientConnection, message: dict[str, Any]) -> None:
        if not self._require_permission(client, "allow_set", "rotate boxes"):
            return
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

    def _handle_held_item(self, client: ClientConnection, message: dict[str, Any]) -> None:
        digest = str(message.get("hash", ""))
        visible = bool(message.get("visible")) and bool(digest) and client.permissions.allow_set
        try:
            orientation = validate_orientation(message.get("orientation", IDENTITY_ORIENTATION))
        except ValueError:
            orientation = IDENTITY_ORIENTATION
        if visible and not self._ensure_asset(digest, message.get("asset")):
            visible = False
            digest = ""
            orientation = IDENTITY_ORIENTATION
        state: dict[str, Any]
        with self.lock:
            current = self.clients.get(client.player_id)
            if current is not client:
                return
            state = dict(self.player_states.get(client.player_id, self._default_player_state()))
            state["held_hash"] = digest if visible else ""
            state["held_orientation"] = orientation
            state["held_visible"] = visible
            self.player_states[client.player_id] = state
        if visible:
            self._broadcast_asset_if_present(digest)
        self._broadcast_player_state(client.player_id, state, udp_socket=None)

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
        except Exception:
            _print_traceback(f"failed to store asset {digest}")
            return False
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
                except Exception:
                    _print_traceback(f"failed to remove temp asset {temp_path}")

    def _broadcast_asset_if_present(self, digest: str) -> None:
        try:
            path = box_path_for_hash(self.paths.boxes_dir, digest)
            if path.is_file():
                self.broadcast({"type": "asset", "hash": digest, "size": path.stat().st_size})
        except Exception:
            _print_traceback(f"failed to broadcast asset {digest}")

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

    def _persist_client_position(self, client: ClientConnection, state: dict[str, Any] | None = None) -> None:
        if not client.loaded:
            return
        if state is None:
            state = self.player_states.get(client.player_id)
        if not state:
            return
        try:
            pos = validate_player_position(state.get("pos"))
        except WorldFormatError:
            return
        self.player_positions[client.user_id] = pos

    def _save(self) -> None:
        try:
            save_world(self.paths.info_file, self.world_map, self.player_positions)
        except Exception:
            _print_traceback("failed to save world")

    def _cleanup_unused_boxes(self) -> None:
        try:
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
                    except Exception:
                        _print_traceback(f"failed to remove unused box {path}")
            if removed:
                self.log(f"Removed {removed} unused .box files")
        except Exception:
            _print_traceback("unused box cleanup failed")

    def broadcast(self, message: dict[str, Any], exclude: int | None = None) -> None:
        try:
            with self.lock:
                clients = [client for client in self.clients.values() if client.player_id != exclude]
            for client in clients:
                try:
                    client.send(message)
                except OSError:
                    pass
                except Exception:
                    _print_traceback(f"failed to broadcast to player {client.player_id}")
        except Exception:
            _print_traceback("broadcast failed")

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
        try:
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
        except OSError:
            pass
        except Exception:
            _print_traceback("failed to send udp status")

    def _handle_tcp_player_state(self, client: ClientConnection, message: dict[str, Any]) -> None:
        if not client.loaded:
            return
        state = self._normalize_player_state(message)
        with self.lock:
            if self.clients.get(client.player_id) is not client:
                return
            state = self._apply_player_state_permissions(client, state)
            self.player_states[client.player_id] = state
            self._persist_client_position(client, state)
        self._broadcast_player_state(client.player_id, state, udp_socket=None)

    def _handle_client_loaded(self, client: ClientConnection, message: dict[str, Any]) -> None:
        state = self._normalize_player_state(message)
        with self.lock:
            if self.clients.get(client.player_id) is not client:
                return
            client.loaded = True
            state = self._apply_player_state_permissions(client, state)
            self.player_states[client.player_id] = state
            self._persist_client_position(client, state)
            self._save()
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
            if client is None or not client.loaded or not client.udp_enabled or client.udp_address != address:
                return
            state = self._apply_player_state_permissions(client, state)
            self.player_states[player_id] = state
            self._persist_client_position(client, state)
        self._broadcast_player_state(player_id, state, udp_socket)

    def _apply_player_state_permissions(self, client: ClientConnection, state: dict[str, Any]) -> dict[str, Any]:
        state = self._merge_held_item_state(client, state)
        if state.get("move_mode") == "fly" and not client.permissions.allow_fly:
            state = dict(state)
            state["move_mode"] = "walk"
            self._send_permissions(client)
            try:
                client.send({"type": "force_move_mode", "move_mode": "walk"})
            except OSError:
                pass
            except Exception:
                _print_traceback(f"failed to force unauthorized flyer {client.player_id} to walk")
        return state

    def _merge_held_item_state(self, client: ClientConnection, state: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            previous = self.player_states.get(client.player_id, {})
            held_hash = str(previous.get("held_hash", ""))
            held_orientation = previous.get("held_orientation", IDENTITY_ORIENTATION)
            held_visible = bool(previous.get("held_visible")) and client.permissions.allow_set and bool(held_hash)
        merged = dict(state)
        merged["held_hash"] = held_hash if held_visible else ""
        try:
            merged["held_orientation"] = validate_orientation(held_orientation)
        except ValueError:
            merged["held_orientation"] = IDENTITY_ORIENTATION
        merged["held_visible"] = held_visible
        return merged

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
        except Exception:
            _print_traceback("failed to send udp probe ack")

    def _broadcast_player_state(
        self,
        player_id: int,
        state: dict[str, Any],
        udp_socket: socket.socket | None,
    ) -> None:
        try:
            with self.lock:
                source = self.clients.get(player_id)
                if source is None or not source.loaded:
                    return
                user_id = source.user_id if source is not None else str(player_id)
                message = {"type": "player_state", "player_id": player_id, "user_id": user_id, **state}
                udp_payload = json.dumps(message, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
                recipients = [
                    client
                    for client in self.clients.values()
                    if client.player_id != player_id and client.loaded
                ]
            for client in recipients:
                if udp_socket is not None and client.udp_enabled and client.udp_address is not None:
                    try:
                        udp_socket.sendto(udp_payload, client.udp_address)
                        continue
                    except OSError:
                        pass
                    except Exception:
                        _print_traceback(f"failed to send udp player state to player {client.player_id}")
                try:
                    client.send(message)
                except OSError:
                    pass
                except Exception:
                    _print_traceback(f"failed to send tcp player state to player {client.player_id}")
        except Exception:
            _print_traceback("failed to broadcast player state")

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

    def _default_player_state(self) -> dict[str, Any]:
        return {
            "pos": [0.0, 0.0, 0.0],
            "velocity": [0.0, 0.0, 0.0],
            "heading": 0.0,
            "pitch": 0.0,
            "move_mode": "walk",
            "held_hash": "",
            "held_orientation": IDENTITY_ORIENTATION,
            "held_visible": False,
        }


class _ServerStreamTee:
    def __init__(self, state: WorldServerState, wrapped: TextIO, stream: str) -> None:
        self.state = state
        self.wrapped = wrapped
        self.stream = "stderr" if stream == "stderr" else "stdout"
        self._buffer = ""
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        with self._lock:
            text = str(text)
            written = len(text)
            if self.stream == "stdout" and STDOUT_ONLY_MARKER in text:
                combined = self._buffer + text
                self._buffer = ""
                self._write_stdout_only_split(combined)
                return written
            written = self.wrapped.write(text)
            self.wrapped.flush()
            self._buffer += text
            while "\n" in self._buffer:
                line, _, rest = self._buffer.partition("\n")
                self._buffer = rest
                self.state._record_log_line(line, self.stream)
            return written

    def _write_stdout_only_split(self, text: str) -> None:
        while STDOUT_ONLY_MARKER in text:
            before, _, after = text.partition(STDOUT_ONLY_MARKER)
            if before:
                self._write_and_capture(before)
            line, newline, rest = after.partition("\n")
            self.wrapped.write(line + newline)
            self.wrapped.flush()
            text = rest
        if text:
            self._buffer += text
            while "\n" in self._buffer:
                line, _, rest = self._buffer.partition("\n")
                self._buffer = rest
                self._write_and_capture(line + "\n")

    def _write_and_capture(self, text: str) -> None:
        self.wrapped.write(text)
        self.wrapped.flush()
        self._buffer += text
        while "\n" in self._buffer:
            line, _, rest = self._buffer.partition("\n")
            self._buffer = rest
            self.state._record_log_line(line, self.stream)

    def flush(self) -> None:
        with self._lock:
            if self._buffer:
                self.state._record_log_line(self._buffer, self.stream)
                self._buffer = ""
            self.wrapped.flush()

    def isatty(self) -> bool:
        return self.wrapped.isatty()

    @property
    def encoding(self) -> str | None:
        return self.wrapped.encoding


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
            except OSError as exc:
                if self._stopped.is_set():
                    break
                if _is_udp_peer_reset_error(exc):
                    self.state.probe_clients_after_udp_reset()
                    continue
                _print_traceback("udp socket error")
                time.sleep(0.1)
            except ValueError:
                continue
            except Exception:
                _print_traceback("udp loop failed while handling datagram")
                time.sleep(0.05)

    def stop(self) -> None:
        self._stopped.set()
        try:
            self.socket.close()
        except OSError:
            pass
        except Exception:
            _print_traceback("failed to close udp socket")


class _ThreadingServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], handler_class, state: WorldServerState) -> None:
        super().__init__(server_address, handler_class)
        self.state = state
        self.state.attach_server(self)

    def handle_error(self, request, client_address) -> None:  # type: ignore[override]
        _print_traceback(f"tcp handler error from {client_address}")


class _Handler(socketserver.StreamRequestHandler):
    def _client_endpoint(self) -> str:
        try:
            host, port = self.client_address[:2]
            return f"{host}:{port}"
        except Exception:
            return "unknown:0"

    def handle(self) -> None:
        state: WorldServerState = self.server.state  # type: ignore[attr-defined]
        client: ClientConnection | None = None
        endpoint = self._client_endpoint()
        heartbeat_stop = threading.Event()
        heartbeat_thread: threading.Thread | None = None
        try:
            first = read_message(self.rfile)
            if first is not None and first.get("type") == "asset_stream":
                state.handle_asset_stream(first, self.wfile)
                return

            client_uuid = str((first or {}).get("client_uuid", "")) or secrets.token_hex(16)
            desired_user_id = str((first or {}).get("desired_user_id", ""))
            if not _environment_bool(ALLOW_CONNECT_ENV, True):
                send_message(
                    self.wfile,
                    {
                        "type": "connect_refused",
                        "reason": "Server do not allow connect. (ALLOW_CONNECT = False)",
                    },
                )
                return
            if not state.accepting_clients or state.stop_requested.is_set():
                send_message(
                    self.wfile,
                    {
                        "type": "kicked",
                        "reason": "server stopped by command",
                        "by": "server",
                    },
                )
                return
            client = state.register(self.wfile, client_uuid, desired_user_id, endpoint)
            state.log(f"CLIENT LOGIN: {endpoint}:{client.user_id}")
            include_assets = bool((first or {}).get("include_assets", False))
            client.send(state.welcome_message(client.player_id, include_assets=include_assets))
            heartbeat_thread = state.start_client_heartbeat(client, heartbeat_stop)
            state.broadcast({"type": "player_join", "player_id": client.player_id}, exclude=client.player_id)
            while True:
                message = read_message(self.rfile)
                if message is None:
                    break
                state.handle_message(client, message)
        except (ConnectionError, OSError, ValueError):
            pass
        except Exception:
            _print_traceback("tcp client handler failed")
        finally:
            heartbeat_stop.set()
            if client is not None:
                try:
                    state.unregister(client)
                except Exception:
                    _print_traceback("failed during client unregister")
            if heartbeat_thread is not None:
                try:
                    heartbeat_thread.join(timeout=0.2)
                except Exception:
                    _print_traceback("failed joining client heartbeat")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Neko Mouse World multiplayer server")
    parser.add_argument("world", nargs="?", default=".", help="world save folder")
    parser.add_argument("--host", default=DEFAULT_HOST, help="host/interface to bind")
    parser.add_argument("--port", default=DEFAULT_PORT, type=int, help="TCP port to bind")
    parser.add_argument("--udp-host", default=None, help="UDP host/interface to bind; defaults to --host")
    parser.add_argument("--udp-port", default=0, type=int, help="UDP port to bind; 0 auto-allocates")
    parser.add_argument("--pin", default=None, help="server command unlock secret; never written to server logs")
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
        _print_traceback(f"FormatError: {exc}")
        return 1
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    sys.stdout = _ServerStreamTee(state, original_stdout, "stdout")  # type: ignore[assignment]
    sys.stderr = _ServerStreamTee(state, original_stderr, "stderr")  # type: ignore[assignment]
    state.stdout_capture_active = True
    state.configure_startup_asset_channels(args.startup_asset_channels)
    if args.pin is None:
        generated_pin = _generate_random_pin()
        state.configure_pin(generated_pin)
        _print_stdout_only(f"Random Pin: {generated_pin}")
    else:
        state.configure_pin(args.pin)

    try:
        udp_host = args.udp_host or args.host
        try:
            server = _ThreadingServer((args.host, args.port), _Handler, state)
        except BaseException:
            _print_traceback("failed to start tcp server")
            return 1
        with server:
            try:
                udp_loop = _UdpLoop(udp_host, args.udp_port, state)
            except BaseException:
                _print_traceback("failed to start udp loop")
                return 1
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
                while not state.stop_requested.is_set():
                    try:
                        server.serve_forever()
                        if state.stop_requested.is_set():
                            break
                    except KeyboardInterrupt:
                        raise
                    except BaseException:
                        _print_traceback("server serve_forever crashed; restarting accept loop")
                        time.sleep(0.5)
            except KeyboardInterrupt:
                state.log("Shutting down")
                state.request_shutdown("server terminated.", "Server terminated.")
            finally:
                udp_loop.stop()
    finally:
        try:
            sys.stdout.flush()
        except BaseException:
            pass
        try:
            sys.stderr.flush()
        except BaseException:
            pass
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        state.stdout_capture_active = False
    return 0


def _run_with_main_client(
    server: _ThreadingServer,
    udp_loop: _UdpLoop,
    host: str,
    port: int,
) -> int:
    def serve_until_shutdown() -> None:
        while not server.state.stop_requested.is_set():
            try:
                server.serve_forever()
                return
            except BaseException:
                if server.state.stop_requested.is_set():
                    return
                _print_traceback("with-client server thread crashed; restarting accept loop")
                time.sleep(0.5)

    server_thread = threading.Thread(target=serve_until_shutdown, daemon=True)
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
        "--user-id",
        "root",
    ]
    server.state.log(f"Launching main client on {client_host}:{port}")
    try:
        try:
            client_process = subprocess.Popen(command)
            return_code = client_process.wait()
        except KeyboardInterrupt:
            raise
        except Exception:
            _print_traceback("main client process failed")
            return_code = 1
    except KeyboardInterrupt:
        server.state.log("Shutting down")
        server.state.request_shutdown("server terminated.", "Server terminated.")
        return_code = 0
    finally:
        if not server.state.stop_requested.is_set():
            server.state.request_shutdown("main client exited.", "Server stopped because main client exited")
        try:
            server.shutdown()
        except BaseException:
            _print_traceback("server shutdown failed")
        try:
            udp_loop.stop()
        except BaseException:
            _print_traceback("udp shutdown failed")
        try:
            server_thread.join(timeout=3.0)
        except BaseException:
            _print_traceback("server thread join failed")
    return int(return_code)


if __name__ == "__main__":
    raise SystemExit(main())
