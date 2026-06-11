from __future__ import annotations

import argparse
from dataclasses import dataclass
import getpass
import json
import math
import os
from pathlib import Path
import queue
import sys
import threading
import time
import traceback
from typing import Any, Callable
import urllib.error
import urllib.request


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from box_editor_view.box_file import BoxFormatError, BoxMap  # noqa: E402

from neko_mouse_world.box_assets import store_box_map  # noqa: E402
from neko_mouse_world.client import create_client_app  # noqa: E402
from neko_mouse_world.world_file import Cell, WorldFormatError, validate_cell  # noqa: E402


DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
DEFAULT_USER_ID = "ai-agent"
SYSTEM_PROMPT = """You are an AI agent driving a visible Neko Mouse World client.

The human player cannot use the client's keyboard or mouse. You must use tools
to observe, move, look around, create reusable .box assets, and place or delete
world boxes. Chat with the user in the same language they use.

New user instructions can interrupt your current policy at any time. Keep
movement bounded, stop movement when a motion is complete, and prefer direct
world-editing tools for building structures. Use fill_region for bulk cuboids
instead of many single-cell placements. Use create_box_asset when a new color
or reusable block style is needed. Coordinates are integer world-grid cells.
Report what you changed and ask a short follow-up only when the request is
ambiguous enough to risk building the wrong thing.
"""


JsonObject = dict[str, Any]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI-controlled Neko Mouse World client agent")
    parser.add_argument("--host", required=True, help="server TCP host")
    parser.add_argument("--port", required=True, type=int, help="server TCP port")
    parser.add_argument("--user-id", default=DEFAULT_USER_ID, help="requested server user ID")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="OpenAI-compatible chat model")
    parser.add_argument("--temperature", default=0.2, type=float, help="chat completion temperature")
    parser.add_argument("--request-timeout", default=120.0, type=float, help="OpenAI request timeout in seconds")
    parser.add_argument("--max-tool-steps", default=16, type=int, help="maximum tool-call rounds per instruction")
    parser.add_argument("--max-fill-volume", default=2048, type=int, help="maximum boxes changed by one fill_region call")
    return parser


def read_openai_config() -> tuple[str, str]:
    endpoint = input("OpenAI-compatible endpoint: ").strip()
    while not endpoint:
        endpoint = input("OpenAI-compatible endpoint: ").strip()
    api_key = getpass.getpass("SK key: ").strip()
    while not api_key:
        api_key = getpass.getpass("SK key: ").strip()
    return endpoint, api_key


def chat_completions_url(endpoint: str) -> str:
    base = endpoint.strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


class OpenAIChatClient:
    def __init__(
        self,
        endpoint: str,
        api_key: str,
        model: str,
        temperature: float,
        timeout: float,
    ) -> None:
        self.url = chat_completions_url(endpoint)
        self.api_key = api_key
        self.model = model
        self.temperature = float(temperature)
        self.timeout = float(timeout)

    def complete(self, messages: list[JsonObject], tools: list[JsonObject]) -> JsonObject:
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": self.temperature,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=data,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenAI request failed with HTTP {exc.code}: {detail[:1200]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OpenAI request failed: {exc}") from exc

        try:
            parsed = json.loads(body)
            choices = parsed["choices"]
            message = choices[0]["message"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"OpenAI response did not contain choices[0].message: {body[:1200]}") from exc
        if not isinstance(message, dict):
            raise RuntimeError("OpenAI response message is not an object")
        return message


@dataclass
class _ScheduledCall:
    fn: Callable[[], Any]
    done: threading.Event | None
    result: dict[str, Any]


class MainThreadCallGate:
    def __init__(self, app: Any) -> None:
        self.app = app
        self._main_thread_id = threading.get_ident()
        self._queue: queue.Queue[_ScheduledCall] = queue.Queue()
        self.app.taskMgr.add(self._drain, "ai-agent-main-thread-call-gate")

    def call(self, fn: Callable[[], Any], timeout: float = 10.0) -> Any:
        if threading.get_ident() == self._main_thread_id:
            return fn()
        done = threading.Event()
        scheduled = _ScheduledCall(fn=fn, done=done, result={})
        self._queue.put(scheduled)
        if not done.wait(timeout):
            raise TimeoutError("timed out waiting for client main thread")
        if "exception" in scheduled.result:
            raise scheduled.result["exception"]
        return scheduled.result.get("value")

    def call_async(self, fn: Callable[[], Any]) -> None:
        if threading.get_ident() == self._main_thread_id:
            fn()
            return
        self._queue.put(_ScheduledCall(fn=fn, done=None, result={}))

    def _drain(self, task: Any) -> Any:
        for _ in range(128):
            try:
                scheduled = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                scheduled.result["value"] = scheduled.fn()
            except BaseException as exc:  # noqa: BLE001 - propagate to waiting worker.
                scheduled.result["exception"] = exc
                if scheduled.done is None:
                    traceback.print_exception(type(exc), exc, exc.__traceback__)
            finally:
                if scheduled.done is not None:
                    scheduled.done.set()
        return task.cont


class ClientTools:
    def __init__(self, app: Any, gate: MainThreadCallGate, max_fill_volume: int) -> None:
        self.app = app
        self.gate = gate
        self.max_fill_volume = max(1, int(max_fill_volume))

    def stop_motion_async(self) -> None:
        self.gate.call_async(lambda: self.app.stop_automation_movement())

    def execute(
        self,
        name: str,
        arguments: JsonObject,
        generation: int,
        is_current: Callable[[int], bool],
    ) -> JsonObject:
        if not is_current(generation):
            return {"ok": False, "interrupted": True}
        try:
            if name == "observe":
                return self.observe(arguments, generation, is_current)
            if name == "create_box_asset":
                return self.create_box_asset(arguments, generation, is_current)
            if name == "select_box":
                return self.select_box(arguments, generation, is_current)
            if name == "place_box":
                return self.place_box(arguments, generation, is_current)
            if name == "delete_box":
                return self.delete_box(arguments, generation, is_current)
            if name == "fill_region":
                return self.fill_region(arguments, generation, is_current)
            if name == "set_move_mode":
                return self.set_move_mode(arguments, generation, is_current)
            if name == "set_look":
                return self.set_look(arguments, generation, is_current)
            if name == "set_movement":
                return self.set_movement(arguments, generation, is_current)
            if name == "move_for":
                return self.move_for(arguments, generation, is_current)
            if name == "stop_movement":
                return self.stop_movement(generation, is_current)
            if name == "jump":
                return self.jump(generation, is_current)
            if name == "capture_screenshot":
                return self.capture_screenshot(arguments, generation, is_current)
        except Exception as exc:  # noqa: BLE001 - return tool errors to the model.
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": False, "error": f"unknown tool {name!r}"}

    def _call_current(
        self,
        generation: int,
        is_current: Callable[[int], bool],
        op: Callable[[], JsonObject],
        timeout: float = 10.0,
    ) -> JsonObject:
        if not is_current(generation):
            return {"ok": False, "interrupted": True}

        def guarded() -> JsonObject:
            if not is_current(generation):
                return {"ok": False, "interrupted": True}
            return op()

        return self.gate.call(guarded, timeout=timeout)

    def observe(
        self,
        arguments: JsonObject,
        generation: int,
        is_current: Callable[[int], bool],
    ) -> JsonObject:
        radius = int(arguments.get("radius", 8))
        limit = int(arguments.get("limit", 80))

        def op() -> JsonObject:
            network_client = self.app.network_client
            selected_hash, selected_orientation = self.app.get_selected_box()
            remote_players = []
            if network_client is not None:
                for player in network_client.remote_players_snapshot().values():
                    remote_players.append(
                        {
                            "player_id": player.player_id,
                            "user_id": player.user_id,
                            "pos": player.pos,
                            "move_mode": player.move_mode,
                        }
                    )
            return {
                "ok": True,
                "connected": bool(network_client.connected) if network_client is not None else True,
                "connecting": bool(network_client.connecting) if network_client is not None else False,
                "loading": self.app.world_load_job is not None,
                "user_id": network_client.user_id if network_client is not None else "local",
                "permissions": dict(self.app.permissions),
                "pose": self.app.get_player_pose(),
                "selected_box": {"hash": selected_hash, "orientation": selected_orientation},
                "box_count": len(self.app.world_map.boxes),
                "nearby_boxes": self.app.get_world_boxes_near_player(radius=radius, limit=limit),
                "remote_players": remote_players[:20],
                "status": self.app.last_status_text,
            }

        return self._call_current(generation, is_current, op)

    def create_box_asset(
        self,
        arguments: JsonObject,
        generation: int,
        is_current: Callable[[int], bool],
    ) -> JsonObject:
        n = int(arguments.get("n", 0))
        color = self._rgba(arguments.get("color", [140, 140, 140, 255]))
        voxels = arguments.get("voxels")
        select = bool(arguments.get("select", True))
        orientation = int(arguments.get("orientation", 0)) % 24

        def op() -> JsonObject:
            size = 2**n
            if isinstance(voxels, list) and voxels:
                boxes: dict[Cell, tuple[float, float, float, float]] = {}
                for item in voxels:
                    if not isinstance(item, dict):
                        raise ValueError("each voxel must be an object")
                    cell = self._box_cell(item.get("cell"), size)
                    voxel_color = self._rgba(item.get("color", color))
                    boxes[cell] = voxel_color
            else:
                boxes = {
                    (x, y, z): color
                    for x in range(size)
                    for y in range(size)
                    for z in range(size)
                }
            box_map = BoxMap(n=n, boxes=boxes)
            digest = store_box_map(self.app.paths.boxes_dir, box_map)
            self.app.surface_cache.invalidate(digest)
            self.app.collision_cache.invalidate(digest)
            if select:
                self.app.set_selected_box(digest=digest, orientation=orientation)
            return {
                "ok": True,
                "hash": digest,
                "n": n,
                "size": size,
                "voxels": len(boxes),
                "selected": select,
                "orientation": orientation,
            }

        return self._call_current(generation, is_current, op)

    def select_box(
        self,
        arguments: JsonObject,
        generation: int,
        is_current: Callable[[int], bool],
    ) -> JsonObject:
        digest = str(arguments.get("hash", "")).strip()
        orientation = int(arguments.get("orientation", 0)) % 24

        def op() -> JsonObject:
            selected_hash, selected_orientation = self.app.set_selected_box(digest=digest, orientation=orientation)
            return {"ok": True, "hash": selected_hash, "orientation": selected_orientation}

        return self._call_current(generation, is_current, op)

    def place_box(
        self,
        arguments: JsonObject,
        generation: int,
        is_current: Callable[[int], bool],
    ) -> JsonObject:
        cell = self._world_cell(arguments.get("cell"))
        digest = arguments.get("hash")
        orientation = arguments.get("orientation")

        def op() -> JsonObject:
            ok = self.app.set_world_box_at(
                cell,
                digest=str(digest).strip() if digest else None,
                orientation=int(orientation) if orientation is not None else None,
            )
            return {
                "ok": bool(ok),
                "cell": cell,
                "box": self.app.get_box_at(cell),
                "status": self.app.last_status_text,
            }

        return self._call_current(generation, is_current, op)

    def delete_box(
        self,
        arguments: JsonObject,
        generation: int,
        is_current: Callable[[int], bool],
    ) -> JsonObject:
        cell = self._world_cell(arguments.get("cell"))

        def op() -> JsonObject:
            ok = self.app.delete_world_box_at(cell)
            return {"ok": bool(ok), "cell": cell, "status": self.app.last_status_text}

        return self._call_current(generation, is_current, op)

    def fill_region(
        self,
        arguments: JsonObject,
        generation: int,
        is_current: Callable[[int], bool],
    ) -> JsonObject:
        min_cell = self._world_cell(arguments.get("min_cell"))
        max_cell = self._world_cell(arguments.get("max_cell"))
        mode = str(arguments.get("mode", "place")).strip().lower()
        if mode not in {"place", "delete"}:
            raise ValueError("mode must be 'place' or 'delete'")
        orientation = int(arguments.get("orientation", 0)) % 24
        digest_arg = arguments.get("hash")
        x0, x1 = sorted((min_cell[0], max_cell[0]))
        y0, y1 = sorted((min_cell[1], max_cell[1]))
        z0, z1 = sorted((min_cell[2], max_cell[2]))
        volume = (x1 - x0 + 1) * (y1 - y0 + 1) * (z1 - z0 + 1)
        if volume > self.max_fill_volume:
            return {
                "ok": False,
                "error": f"region volume {volume} exceeds --max-fill-volume {self.max_fill_volume}",
            }

        def op() -> JsonObject:
            changed = 0
            if mode == "place":
                digest = str(digest_arg).strip() if digest_arg else self.app.selected_hash
                self.app.surface_cache.get(digest)
                self.app.collision_cache.get(digest, orientation)
                for x in range(x0, x1 + 1):
                    for y in range(y0, y1 + 1):
                        for z in range(z0, z1 + 1):
                            if not is_current(generation):
                                return {
                                    "ok": False,
                                    "interrupted": True,
                                    "mode": mode,
                                    "changed": changed,
                                }
                            if self.app.set_world_box_at((x, y, z), digest=digest, orientation=orientation):
                                changed += 1
            else:
                for x in range(x0, x1 + 1):
                    for y in range(y0, y1 + 1):
                        for z in range(z0, z1 + 1):
                            if not is_current(generation):
                                return {
                                    "ok": False,
                                    "interrupted": True,
                                    "mode": mode,
                                    "changed": changed,
                                }
                            if self.app.delete_world_box_at((x, y, z)):
                                changed += 1
            return {
                "ok": True,
                "mode": mode,
                "min_cell": (x0, y0, z0),
                "max_cell": (x1, y1, z1),
                "volume": volume,
                "changed": changed,
                "status": self.app.last_status_text,
            }

        return self._call_current(
            generation,
            is_current,
            op,
            timeout=max(10.0, volume * 0.05),
        )

    def set_move_mode(
        self,
        arguments: JsonObject,
        generation: int,
        is_current: Callable[[int], bool],
    ) -> JsonObject:
        mode = str(arguments.get("mode", "walk")).strip().lower()
        if mode not in {"walk", "fly"}:
            raise ValueError("mode must be 'walk' or 'fly'")

        def op() -> JsonObject:
            if mode == "fly" and not self.app.permissions.get("allow_fly", True):
                return {"ok": False, "error": "server does not allow fly mode"}
            self.app.move_mode = mode
            self.app.vertical_velocity = 0.0
            self.app.grounded = mode == "walk" and self.app._has_walk_contact(self.app.player_pos)
            self.app._set_status(f"Movement: {self.app.move_mode}")
            return {"ok": True, "move_mode": self.app.move_mode}

        return self._call_current(generation, is_current, op)

    def set_look(
        self,
        arguments: JsonObject,
        generation: int,
        is_current: Callable[[int], bool],
    ) -> JsonObject:
        heading = arguments.get("heading")
        pitch = arguments.get("pitch")
        heading_delta = float(arguments.get("heading_delta", 0.0))
        pitch_delta = float(arguments.get("pitch_delta", 0.0))

        def op() -> JsonObject:
            target_heading = float(heading) if heading is not None else self.app.heading
            target_pitch = float(pitch) if pitch is not None else self.app.pitch
            self.app.set_automation_look(target_heading + heading_delta, target_pitch + pitch_delta)
            return {"ok": True, "pose": self.app.get_player_pose()}

        return self._call_current(generation, is_current, op)

    def set_movement(
        self,
        arguments: JsonObject,
        generation: int | None = None,
        is_current: Callable[[int], bool] | None = None,
    ) -> JsonObject:
        forward = float(arguments.get("forward", 0.0))
        right = float(arguments.get("right", 0.0))
        vertical = float(arguments.get("vertical", 0.0))

        def op() -> JsonObject:
            if generation is not None and is_current is not None and not is_current(generation):
                return {"ok": False, "interrupted": True}
            self.app.set_automation_movement(forward=forward, right=right, vertical=vertical)
            return {
                "ok": True,
                "movement": {
                    "forward": self.app.automation_forward,
                    "right": self.app.automation_right,
                    "vertical": self.app.automation_vertical,
                },
            }

        if generation is None or is_current is None:
            return self.gate.call(op)
        return self._call_current(generation, is_current, op)

    def move_for(
        self,
        arguments: JsonObject,
        generation: int,
        is_current: Callable[[int], bool],
    ) -> JsonObject:
        seconds = max(0.0, min(30.0, float(arguments.get("seconds", 1.0))))
        if not is_current(generation):
            return {"ok": False, "interrupted": True}
        self.set_movement(arguments, generation, is_current)
        deadline = time.monotonic() + seconds
        interrupted = False
        while time.monotonic() < deadline:
            if not is_current(generation):
                interrupted = True
                break
            time.sleep(min(0.10, max(0.0, deadline - time.monotonic())))
        if is_current(generation):
            self.stop_movement(generation, is_current)
        pose = self.observe({"radius": 4, "limit": 20}, generation, is_current).get("pose")
        return {"ok": not interrupted, "interrupted": interrupted, "seconds": seconds, "pose": pose}

    def stop_movement(
        self,
        generation: int | None = None,
        is_current: Callable[[int], bool] | None = None,
    ) -> JsonObject:
        def op() -> JsonObject:
            if generation is not None and is_current is not None and not is_current(generation):
                return {"ok": False, "interrupted": True}
            self.app.stop_automation_movement()
            return {"ok": True, "movement": {"forward": 0.0, "right": 0.0, "vertical": 0.0}}

        if generation is None or is_current is None:
            return self.gate.call(op)
        return self._call_current(generation, is_current, op)

    def jump(self, generation: int, is_current: Callable[[int], bool]) -> JsonObject:
        def op() -> JsonObject:
            self.app.automation_jump()
            return {"ok": True, "pose": self.app.get_player_pose()}

        return self._call_current(generation, is_current, op)

    def capture_screenshot(
        self,
        arguments: JsonObject,
        generation: int,
        is_current: Callable[[int], bool],
    ) -> JsonObject:
        path = arguments.get("path")

        def op() -> JsonObject:
            saved = self.app.capture_screenshot(str(path)) if path else self.app.capture_screenshot()
            return {"ok": saved is not None, "path": str(saved) if saved is not None else None}

        return self._call_current(generation, is_current, op)

    def _world_cell(self, value: Any) -> Cell:
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            raise WorldFormatError("cell must be a list of three integers")
        return validate_cell((value[0], value[1], value[2]))

    def _box_cell(self, value: Any, size: int) -> Cell:
        cell = self._world_cell(value)
        if any(part < 0 or part >= size for part in cell):
            raise BoxFormatError(f"box voxel cell {cell} is outside 0..{size - 1}")
        return cell

    def _rgba(self, value: Any) -> tuple[float, float, float, float]:
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            raise ValueError("color must be [r, g, b, a]")
        channels = [float(part) for part in value]
        if not all(math.isfinite(part) for part in channels):
            raise ValueError("color channels must be finite")
        return tuple(channels)  # type: ignore[return-value]


def tool_schemas() -> list[JsonObject]:
    color_schema = {
        "type": "array",
        "items": {"type": "number"},
        "minItems": 4,
        "maxItems": 4,
        "description": "RGBA color, either 0..1 floats or 0..255 channel values.",
    }
    cell_schema = {
        "type": "array",
        "items": {"type": "integer"},
        "minItems": 3,
        "maxItems": 3,
        "description": "Integer world cell [x, y, z].",
    }
    return [
        {
            "type": "function",
            "function": {
                "name": "observe",
                "description": "Inspect client state, player pose, permissions, selected box, and nearby boxes.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "radius": {"type": "integer", "default": 8},
                        "limit": {"type": "integer", "default": 80},
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "create_box_asset",
                "description": "Create a reusable .box asset in the client cache and optionally select it.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "n": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 5,
                            "default": 0,
                            "description": "Box resolution exponent; side length is 2**n.",
                        },
                        "color": color_schema,
                        "voxels": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "cell": cell_schema,
                                    "color": color_schema,
                                },
                                "required": ["cell"],
                                "additionalProperties": False,
                            },
                            "description": "Optional sparse voxel list. Omit it to make a solid box.",
                        },
                        "select": {"type": "boolean", "default": True},
                        "orientation": {"type": "integer", "minimum": 0, "maximum": 23, "default": 0},
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "select_box",
                "description": "Select an existing reusable box hash for future placement.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "hash": {"type": "string"},
                        "orientation": {"type": "integer", "minimum": 0, "maximum": 23, "default": 0},
                    },
                    "required": ["hash"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "place_box",
                "description": "Place or replace one world box at an integer cell.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "cell": cell_schema,
                        "hash": {"type": "string", "description": "Optional .box hash; default is selected box."},
                        "orientation": {"type": "integer", "minimum": 0, "maximum": 23},
                    },
                    "required": ["cell"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "delete_box",
                "description": "Delete one world box at an integer cell.",
                "parameters": {
                    "type": "object",
                    "properties": {"cell": cell_schema},
                    "required": ["cell"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fill_region",
                "description": "Place or delete boxes in an inclusive cuboid world region.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "min_cell": cell_schema,
                        "max_cell": cell_schema,
                        "mode": {"type": "string", "enum": ["place", "delete"], "default": "place"},
                        "hash": {"type": "string", "description": "Optional .box hash; default is selected box."},
                        "orientation": {"type": "integer", "minimum": 0, "maximum": 23, "default": 0},
                    },
                    "required": ["min_cell", "max_cell"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "set_move_mode",
                "description": "Set player movement mode to walk or fly.",
                "parameters": {
                    "type": "object",
                    "properties": {"mode": {"type": "string", "enum": ["walk", "fly"]}},
                    "required": ["mode"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "set_look",
                "description": "Set or adjust camera heading/pitch in degrees.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "heading": {"type": "number"},
                        "pitch": {"type": "number"},
                        "heading_delta": {"type": "number", "default": 0},
                        "pitch_delta": {"type": "number", "default": 0},
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "set_movement",
                "description": "Set continuous automation movement axes, each clamped to -1..1.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "forward": {"type": "number", "default": 0},
                        "right": {"type": "number", "default": 0},
                        "vertical": {"type": "number", "default": 0},
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "move_for",
                "description": "Move with automation axes for a bounded number of seconds, then stop.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "forward": {"type": "number", "default": 0},
                        "right": {"type": "number", "default": 0},
                        "vertical": {"type": "number", "default": 0},
                        "seconds": {"type": "number", "minimum": 0, "maximum": 30, "default": 1},
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "stop_movement",
                "description": "Stop all automation movement immediately.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "jump",
                "description": "Request one walk-mode jump.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "capture_screenshot",
                "description": "Capture the visible client window and return the saved PNG path.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "additionalProperties": False,
                },
            },
        },
    ]


class AgentCoordinator:
    def __init__(
        self,
        chat_client: OpenAIChatClient,
        tools: ClientTools,
        max_tool_steps: int,
    ) -> None:
        self.chat_client = chat_client
        self.tools = tools
        self.max_tool_steps = max(1, int(max_tool_steps))
        self._tool_schemas = tool_schemas()
        self._lock = threading.RLock()
        self._generation = 0
        self._history: list[JsonObject] = []
        self.shutdown_requested = threading.Event()

    def submit(self, text: str) -> None:
        stripped = text.strip()
        if not stripped:
            return
        if stripped.lower() in {"exit", "quit", "/exit", "/quit"}:
            self.request_shutdown()
            return
        self.tools.stop_motion_async()
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._history.append({"role": "user", "content": stripped})
            history = list(self._history[-24:])
        print("[agent] accepted instruction; current movement was interrupted.")
        thread = threading.Thread(target=self._run_turn, args=(generation, history), daemon=True)
        thread.start()

    def request_shutdown(self) -> None:
        with self._lock:
            self._generation += 1
        self.tools.stop_motion_async()
        self.shutdown_requested.set()

    def is_current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation and not self.shutdown_requested.is_set()

    def _run_turn(self, generation: int, history: list[JsonObject]) -> None:
        messages: list[JsonObject] = [{"role": "system", "content": SYSTEM_PROMPT}, *history]
        print("[agent] thinking...")
        try:
            for _step in range(self.max_tool_steps):
                if not self.is_current(generation):
                    return
                message = self.chat_client.complete(messages, self._tool_schemas)
                if not self.is_current(generation):
                    return
                tool_calls = message.get("tool_calls") or []
                if tool_calls:
                    messages.append(
                        {
                            "role": "assistant",
                            "content": message.get("content") or "",
                            "tool_calls": tool_calls,
                        }
                    )
                    for call in tool_calls:
                        if not self.is_current(generation):
                            return
                        tool_name, tool_args, tool_call_id = self._parse_tool_call(call)
                        result = self.tools.execute(tool_name, tool_args, generation, self.is_current)
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "content": json.dumps(result, ensure_ascii=False),
                            }
                        )
                    continue

                content = str(message.get("content") or "").strip() or "Done."
                if self.is_current(generation):
                    with self._lock:
                        if generation == self._generation:
                            self._history.append({"role": "assistant", "content": content})
                    print(f"AI: {content}")
                return
            if self.is_current(generation):
                print("AI: I stopped because the tool-call limit was reached.")
        except Exception as exc:  # noqa: BLE001 - keep the console loop alive.
            if self.is_current(generation):
                print(f"[agent] error: {type(exc).__name__}: {exc}")

    def _parse_tool_call(self, call: JsonObject) -> tuple[str, JsonObject, str]:
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict):
            raise ValueError(f"invalid tool call: {call!r}")
        name = str(function.get("name", ""))
        raw_arguments = function.get("arguments") or "{}"
        if isinstance(raw_arguments, str):
            arguments = json.loads(raw_arguments)
        elif isinstance(raw_arguments, dict):
            arguments = raw_arguments
        else:
            raise ValueError(f"invalid arguments for tool {name!r}")
        if not isinstance(arguments, dict):
            raise ValueError(f"arguments for tool {name!r} must be an object")
        tool_call_id = str(call.get("id", f"tool-{time.time_ns()}"))
        return name, arguments, tool_call_id


def stdin_loop(coordinator: AgentCoordinator) -> None:
    while not coordinator.shutdown_requested.is_set():
        try:
            text = input("> ")
        except EOFError:
            coordinator.request_shutdown()
            return
        coordinator.submit(text)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    endpoint, api_key = read_openai_config()

    app = create_client_app(args.host, args.port, user_id=args.user_id)
    app.set_human_input_enabled(False)
    gate = MainThreadCallGate(app)
    tools = ClientTools(app, gate, max_fill_volume=args.max_fill_volume)
    chat_client = OpenAIChatClient(
        endpoint=endpoint,
        api_key=api_key,
        model=args.model,
        temperature=args.temperature,
        timeout=args.request_timeout,
    )
    coordinator = AgentCoordinator(chat_client, tools, max_tool_steps=args.max_tool_steps)

    def shutdown_task(task: Any) -> Any:
        if coordinator.shutdown_requested.is_set():
            if app.network_client is not None:
                app.network_client.close()
            raise SystemExit
        return task.cont

    app.taskMgr.add(shutdown_task, "ai-agent-shutdown")
    threading.Thread(target=stdin_loop, args=(coordinator,), daemon=True).start()

    print(f"[agent] connected client starting for {args.host}:{args.port} as {args.user_id!r}.")
    print("[agent] type instructions for the AI agent; type 'exit' to stop.")
    try:
        app.run()
    finally:
        if app.network_client is not None:
            app.network_client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
