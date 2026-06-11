from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import getpass
import json
import math
import os
from pathlib import Path
import queue
import socket
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
DEFAULT_MAX_FILL_VOLUME = 4096
ZH_NO_RESPONSE = (
    "\u6a21\u578b\u8fd9\u8f6e\u6ca1\u6709\u8fd4\u56de\u6587\u672c\u6216\u5de5\u5177\u8c03\u7528\uff0c"
    "\u6240\u4ee5\u6211\u6ca1\u6709\u6267\u884c\u52a8\u4f5c\u3002"
    "\u8bf7\u6362\u4e00\u53e5\u66f4\u5177\u4f53\u7684\u6307\u4ee4\uff0c"
    "\u6216\u8005\u68c0\u67e5\u5f53\u524d\u6a21\u578b\u662f\u5426\u652f\u6301 Responses "
    "\u5de5\u5177\u8c03\u7528\u3002"
)
ZH_START_ACTING = (
    "\u6211\u5df2\u7ecf\u770b\u5230\u5f53\u524d\u753b\u9762\uff0c"
    "\u5148\u5f00\u59cb\u6267\u884c\u8fd9\u6761\u6307\u4ee4\uff0c"
    "\u7136\u540e\u4f1a\u7528\u622a\u56fe\u68c0\u67e5\u7ed3\u679c\u5e76\u8c03\u6574\u3002"
)
ZH_TURN_FAILED_PREFIX = "\u6211\u8fd9\u8f6e\u6267\u884c\u5931\u8d25\u4e86\uff1a"
SYSTEM_PROMPT = """You are an AI agent controlling a visible Neko Mouse World client.

You are not only a block placer. You are a conversational builder:
- Talk to the user while you work. Explain your design intent before major actions,
  after visual checkpoints, and when you change the plan.
- Chat with the user in the same language they use.
- You can see the client through screenshots included in the Responses input.
  Use that visual feedback to inspect what you built, then adjust.
- The human cannot use this client window's keyboard or mouse. Use tools for
  movement, camera, screenshots, and world editing.
- For building, prefer direct world-editing tools and fill_region for bulk cuboids.
  Use create_box_asset when a color or reusable block style is needed.
- Never ask fill_region to change more than 4096 world boxes in one call. Split
  larger builds into multiple smaller fill_region calls.
- Before every fill_region call, move near the target region and look at it.
  Prefer move_near_region for this; otherwise use move_to and look_at_cell.
  After filling, capture_view and inspect the result from nearby.
- For navigation, prefer move_to or move_for plus set_look/look_at_cell.
- Coordinates are integer world cells. Player positions are floating point.
- If a request is ambiguous, discuss the design choice briefly instead of silently
  guessing. If you are blocked, say exactly what blocked you.

For any build or movement request:
- Before the first non-say tool, call say with your design intent.
- After a visual checkpoint, call say with what you see and what you will adjust.
- Do not finish with only text. Call relevant tools, inspect the screenshot,
  then summarize what changed and what you plan next.
"""


JsonObject = dict[str, Any]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI-controlled Neko Mouse World client agent")
    parser.add_argument("--host", required=True, help="server TCP host")
    parser.add_argument("--port", required=True, type=int, help="server TCP port")
    parser.add_argument("--user-id", default=DEFAULT_USER_ID, help="requested server user ID")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Responses-capable multimodal model")
    parser.add_argument("--temperature", default=0.35, type=float, help="response temperature")
    parser.add_argument("--request-timeout", default=60.0, type=float, help="Responses request timeout in seconds")
    parser.add_argument("--request-retries", default=2, type=int, help="Responses retries after transient failures")
    parser.add_argument(
        "--max-tool-steps",
        default=None,
        type=int,
        help="maximum non-say tool calls per instruction; omitted or 0 means unlimited",
    )
    parser.add_argument(
        "--max-fill-volume",
        default=DEFAULT_MAX_FILL_VOLUME,
        type=int,
        help="maximum boxes changed by one fill_region call",
    )
    parser.add_argument("--screenshot-detail", choices=("low", "high", "auto"), default="high")
    return parser


def read_openai_config() -> tuple[str, str]:
    endpoint = input("OpenAI-compatible Responses endpoint: ").strip()
    while not endpoint:
        endpoint = input("OpenAI-compatible Responses endpoint: ").strip()
    api_key = getpass.getpass("SK key: ").strip()
    while not api_key:
        api_key = getpass.getpass("SK key: ").strip()
    return endpoint, api_key


def responses_url(endpoint: str) -> str:
    base = endpoint.strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return f"{base[: -len('/chat/completions')]}/responses"
    if base.endswith("/responses"):
        return base
    if base.endswith("/v1"):
        return f"{base}/responses"
    return f"{base}/v1/responses"


def json_preview(value: Any, limit: int = 360) -> str:
    sanitized = _sanitize_preview(value)
    text = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"))
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _sanitize_preview(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("<image-data-url>" if key in {"image_url", "_image_url"} else _sanitize_preview(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_preview(item) for item in value]
    return value


class ResponsesClient:
    def __init__(
        self,
        endpoint: str,
        api_key: str,
        model: str,
        temperature: float,
        timeout: float,
        retries: int,
    ) -> None:
        self.url = responses_url(endpoint)
        self.api_key = api_key
        self.model = model
        self.temperature = float(temperature)
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))

    def create(self, input_items: list[JsonObject], tools: list[JsonObject]) -> JsonObject:
        payload: JsonObject = {
            "model": self.model,
            "instructions": SYSTEM_PROMPT,
            "input": input_items,
            "tools": tools,
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "temperature": self.temperature,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = self._post_with_retries(data, headers)

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Responses body was not JSON: {body[:1600]}") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("Responses body was not a JSON object")
        return parsed

    def _post_with_retries(self, data: bytes, headers: JsonObject) -> str:
        last_error: BaseException | None = None
        for attempt in range(self.retries + 1):
            request = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
            attempt_label = f"{attempt + 1}/{self.retries + 1}"
            try:
                print(f"[agent] POST {self.url} attempt {attempt_label}", flush=True)
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read().decode("utf-8")
                print(f"[agent] received {len(body)} response bytes.", flush=True)
                return body
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                if self._should_retry_http(exc.code, attempt):
                    self._retry_sleep(attempt, f"HTTP {exc.code}")
                    last_error = RuntimeError(f"HTTP {exc.code}: {detail[:1600]}")
                    continue
                raise RuntimeError(f"Responses request failed with HTTP {exc.code}: {detail[:1600]}") from exc
            except urllib.error.URLError as exc:
                last_error = exc
                reason = getattr(exc, "reason", None)
                if self._is_timeout(reason) or attempt < self.retries:
                    self._retry_sleep(attempt, str(reason or exc))
                    continue
                raise RuntimeError(f"Responses request failed: {exc}") from exc
            except (TimeoutError, socket.timeout) as exc:
                last_error = exc
                if attempt < self.retries:
                    self._retry_sleep(attempt, "read timeout")
                    continue
                raise RuntimeError(
                    f"Responses request timed out after {self.timeout:.1f}s and {self.retries + 1} attempts"
                ) from exc
        raise RuntimeError(f"Responses request failed after retries: {last_error}")

    def _should_retry_http(self, status_code: int, attempt: int) -> bool:
        if attempt >= self.retries:
            return False
        return status_code in {408, 409, 425, 429} or 500 <= status_code <= 599

    def _retry_sleep(self, attempt: int, reason: str) -> None:
        delay = min(8.0, 1.5 * (2**attempt))
        print(f"[agent] Responses request failed ({reason}); retrying in {delay:.1f}s.", flush=True)
        time.sleep(delay)

    def _is_timeout(self, value: object) -> bool:
        return isinstance(value, (TimeoutError, socket.timeout)) or "timed out" in str(value).lower()


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
            except BaseException as exc:  # noqa: BLE001
                scheduled.result["exception"] = exc
                if scheduled.done is None:
                    traceback.print_exception(type(exc), exc, exc.__traceback__)
            finally:
                if scheduled.done is not None:
                    scheduled.done.set()
        return task.cont


class ClientTools:
    def __init__(
        self,
        app: Any,
        gate: MainThreadCallGate,
        max_fill_volume: int,
        screenshot_detail: str,
    ) -> None:
        self.app = app
        self.gate = gate
        self.max_fill_volume = max(1, int(max_fill_volume))
        self.screenshot_detail = screenshot_detail

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
            dispatch: dict[str, Callable[[JsonObject, int, Callable[[int], bool]], JsonObject]] = {
                "say": self.say,
                "observe": self.observe,
                "capture_view": self.capture_view,
                "create_box_asset": self.create_box_asset,
                "upload_box_asset": self.upload_box_asset,
                "select_box": self.select_box,
                "place_box": self.place_box,
                "delete_box": self.delete_box,
                "fill_region": self.fill_region,
                "set_move_mode": self.set_move_mode,
                "set_look": self.set_look,
                "look_at_cell": self.look_at_cell,
                "set_movement": self.set_movement,
                "move_for": self.move_for,
                "move_to": self.move_to,
                "move_near_region": self.move_near_region,
                "stop_movement": self.stop_movement,
                "jump": self.jump,
            }
            tool = dispatch.get(name)
            if tool is None:
                return {"ok": False, "error": f"unknown tool {name!r}"}
            return tool(arguments, generation, is_current)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

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

    def _flush_network_edits(self, timeout: float = 5.0) -> JsonObject:
        synced = self.app.wait_for_network_sends(timeout=timeout)
        if self.app.network_client is not None:
            # This runs on the agent worker thread. The Panda3D main loop keeps
            # running while we give it a short chance to consume server echoes.
            deadline = time.monotonic() + 0.25
            while time.monotonic() < deadline and self.app.pending_network_sends() == 0:
                time.sleep(0.01)
        return {
            "network_synced": synced,
            "pending_network_sends": self.app.pending_network_sends(),
            "failed_network_sends": self.app.failed_network_sends(),
        }

    def _network_state(self) -> JsonObject:
        return {
            "network_synced": True,
            "pending_network_sends": self.app.pending_network_sends(),
            "failed_network_sends": self.app.failed_network_sends(),
        }

    def say(self, arguments: JsonObject, generation: int, is_current: Callable[[int], bool]) -> JsonObject:
        message = str(arguments.get("message", "")).strip()
        if not message:
            return {"ok": False, "error": "message is required"}
        if is_current(generation):
            print(f"AI: {message}", flush=True)
        return {"ok": True, "said": message}

    def observe(self, arguments: JsonObject, generation: int, is_current: Callable[[int], bool]) -> JsonObject:
        radius = int(arguments.get("radius", 10))
        limit = int(arguments.get("limit", 120))

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
                "pending_network_sends": self.app.pending_network_sends(),
                "failed_network_sends": self.app.failed_network_sends(),
                "nearby_boxes": self.app.get_world_boxes_near_player(radius=radius, limit=limit),
                "remote_players": remote_players[:20],
                "status": self.app.last_status_text,
            }

        return self._call_current(generation, is_current, op)

    def upload_box_asset(self, arguments: JsonObject, generation: int, is_current: Callable[[int], bool]) -> JsonObject:
        digest = str(arguments.get("hash", "")).strip()
        if not digest:
            return {"ok": False, "error": "hash is required"}

        def op() -> JsonObject:
            ok = self.app.send_box_asset_to_server(digest)
            return {
                "ok": bool(ok),
                "hash": digest,
                "status": self.app.last_status_text,
            }

        result = self._call_current(generation, is_current, op)
        network_state = self._flush_network_edits(timeout=5.0) if result.get("ok") else self._network_state()
        return {**result, **network_state, "ok": bool(result.get("ok")) and bool(network_state["network_synced"])}

    def capture_view(self, arguments: JsonObject, generation: int, is_current: Callable[[int], bool]) -> JsonObject:
        include_image = bool(arguments.get("include_image", True))

        def op() -> JsonObject:
            screenshots_dir = self.app.paths.root / "agent_screenshots"
            screenshots_dir.mkdir(parents=True, exist_ok=True)
            target = screenshots_dir / f"agent-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000_000:09d}.png"
            saved = self.app.capture_screenshot(target)
            if saved is None:
                return {"ok": False, "error": "screenshot failed"}
            result: JsonObject = {"ok": True, "path": str(saved)}
            if include_image:
                result["_image_url"] = image_data_url(saved)
                result["_image_detail"] = self.screenshot_detail
            return result

        return self._call_current(generation, is_current, op, timeout=10.0)

    def create_box_asset(self, arguments: JsonObject, generation: int, is_current: Callable[[int], bool]) -> JsonObject:
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
                    boxes[cell] = self._rgba(item.get("color", color))
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

    def select_box(self, arguments: JsonObject, generation: int, is_current: Callable[[int], bool]) -> JsonObject:
        digest = str(arguments.get("hash", "")).strip()
        orientation = int(arguments.get("orientation", 0)) % 24

        def op() -> JsonObject:
            selected_hash, selected_orientation = self.app.set_selected_box(digest=digest, orientation=orientation)
            return {"ok": True, "hash": selected_hash, "orientation": selected_orientation}

        return self._call_current(generation, is_current, op)

    def place_box(self, arguments: JsonObject, generation: int, is_current: Callable[[int], bool]) -> JsonObject:
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

        result = self._call_current(generation, is_current, op)
        network_state = self._flush_network_edits(timeout=5.0) if result.get("ok") else self._network_state()
        return {**result, **network_state, "ok": bool(result.get("ok")) and bool(network_state["network_synced"])}

    def delete_box(self, arguments: JsonObject, generation: int, is_current: Callable[[int], bool]) -> JsonObject:
        cell = self._world_cell(arguments.get("cell"))

        def op() -> JsonObject:
            ok = self.app.delete_world_box_at(cell)
            return {
                "ok": bool(ok),
                "cell": cell,
                "status": self.app.last_status_text,
            }

        result = self._call_current(generation, is_current, op)
        network_state = self._flush_network_edits(timeout=5.0) if result.get("ok") else self._network_state()
        return {**result, **network_state, "ok": bool(result.get("ok")) and bool(network_state["network_synced"])}

    def fill_region(self, arguments: JsonObject, generation: int, is_current: Callable[[int], bool]) -> JsonObject:
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
            return {"ok": False, "error": f"region volume {volume} exceeds max {self.max_fill_volume}"}

        cells = [(x, y, z) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1) for z in range(z0, z1 + 1)]
        digest = str(digest_arg).strip() if digest_arg else ""
        if mode == "place":
            prepare = self._prepare_fill_asset(digest, orientation, generation, is_current)
            if not prepare.get("ok"):
                return prepare
            digest = str(prepare["hash"])

        changed = 0
        last_status = ""
        batch_size = 64
        for start in range(0, len(cells), batch_size):
            if not is_current(generation):
                return {"ok": False, "interrupted": True, "mode": mode, "changed": changed}
            batch = cells[start : start + batch_size]

            def batch_op(batch_cells: list[Cell] = batch) -> JsonObject:
                batch_changed = 0
                if mode == "place":
                    for cell in batch_cells:
                        if self.app.set_world_box_at(cell, digest=digest, orientation=orientation, include_asset=False):
                            batch_changed += 1
                else:
                    for cell in batch_cells:
                        if self.app.delete_world_box_at(cell):
                            batch_changed += 1
                return {"ok": True, "changed": batch_changed, "status": self.app.last_status_text}

            batch_result = self._call_current(generation, is_current, batch_op, timeout=max(5.0, len(batch) * 0.05))
            if not batch_result.get("ok"):
                return batch_result
            changed += int(batch_result.get("changed", 0))
            last_status = str(batch_result.get("status", last_status))
            self._flush_network_edits(timeout=5.0)

        network_state = self._flush_network_edits(timeout=max(5.0, min(30.0, volume * 0.02)))
        return {
            "ok": bool(network_state["network_synced"]),
            "mode": mode,
            "min_cell": (x0, y0, z0),
            "max_cell": (x1, y1, z1),
            "volume": volume,
            "changed": changed,
            "status": last_status,
            **network_state,
        }

    def _prepare_fill_asset(
        self,
        digest: str,
        orientation: int,
        generation: int,
        is_current: Callable[[int], bool],
    ) -> JsonObject:
        def op() -> JsonObject:
            actual_digest = digest or self.app.selected_hash
            self.app.surface_cache.get(actual_digest)
            self.app.collision_cache.get(actual_digest, orientation)
            if self.app.network_client is not None and not self.app.send_box_asset_to_server(actual_digest):
                return {"ok": False, "error": self.app.last_status_text, **self._network_state()}
            return {"ok": True, "hash": actual_digest, "status": self.app.last_status_text}

        result = self._call_current(generation, is_current, op)
        if result.get("ok"):
            network_state = self._flush_network_edits(timeout=5.0)
            result = {**result, **network_state, "ok": bool(network_state["network_synced"])}
        return result

    def set_move_mode(self, arguments: JsonObject, generation: int, is_current: Callable[[int], bool]) -> JsonObject:
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
            return {"ok": True, "move_mode": self.app.move_mode, "pose": self.app.get_player_pose()}

        return self._call_current(generation, is_current, op)

    def set_look(self, arguments: JsonObject, generation: int, is_current: Callable[[int], bool]) -> JsonObject:
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

    def look_at_cell(self, arguments: JsonObject, generation: int, is_current: Callable[[int], bool]) -> JsonObject:
        cell = self._world_cell(arguments.get("cell"))

        def op() -> JsonObject:
            px, py, pz = self.app.get_player_position()
            tx, ty, tz = cell[0] + 0.5, cell[1] + 0.5, cell[2] + 0.5
            dx, dy, dz = tx - px, ty - py, tz - (pz + 1.7)
            horizontal = math.hypot(dx, dy)
            heading = math.degrees(math.atan2(-dx, dy)) if horizontal > 0.0001 else self.app.heading
            pitch = max(-89.0, min(89.0, math.degrees(math.atan2(dz, horizontal))))
            self.app.set_automation_look(heading, pitch)
            return {"ok": True, "cell": cell, "pose": self.app.get_player_pose()}

        return self._call_current(generation, is_current, op)

    def set_movement(self, arguments: JsonObject, generation: int, is_current: Callable[[int], bool]) -> JsonObject:
        forward = float(arguments.get("forward", 0.0))
        right = float(arguments.get("right", 0.0))
        vertical = float(arguments.get("vertical", 0.0))

        def op() -> JsonObject:
            self.app.set_automation_movement(forward=forward, right=right, vertical=vertical)
            return {
                "ok": True,
                "movement": {
                    "forward": self.app.automation_forward,
                    "right": self.app.automation_right,
                    "vertical": self.app.automation_vertical,
                },
                "pose": self.app.get_player_pose(),
            }

        return self._call_current(generation, is_current, op)

    def move_for(self, arguments: JsonObject, generation: int, is_current: Callable[[int], bool]) -> JsonObject:
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
            self.stop_movement({}, generation, is_current)
        pose = self.observe({"radius": 6, "limit": 40}, generation, is_current).get("pose")
        return {"ok": not interrupted, "interrupted": interrupted, "seconds": seconds, "pose": pose}

    def move_to(self, arguments: JsonObject, generation: int, is_current: Callable[[int], bool]) -> JsonObject:
        target = arguments.get("position", arguments.get("cell"))
        if not isinstance(target, (list, tuple)) or len(target) != 3:
            raise ValueError("move_to requires position or cell [x, y, z]")
        target_pos = (float(target[0]) + 0.5, float(target[1]) + 0.5, float(target[2]))
        tolerance = max(0.1, float(arguments.get("tolerance", 0.75)))
        timeout = max(0.5, min(60.0, float(arguments.get("timeout", 12.0))))
        use_fly = bool(arguments.get("fly", False))

        def setup() -> JsonObject:
            if use_fly and self.app.permissions.get("allow_fly", True):
                self.app.move_mode = "fly"
            return {"ok": True, "pose": self.app.get_player_pose()}

        self._call_current(generation, is_current, setup)
        deadline = time.monotonic() + timeout
        interrupted = False
        arrived = False
        while time.monotonic() < deadline:
            if not is_current(generation):
                interrupted = True
                break

            def step() -> JsonObject:
                px, py, pz = self.app.get_player_position()
                dx, dy, dz = target_pos[0] - px, target_pos[1] - py, target_pos[2] - pz
                horizontal = math.hypot(dx, dy)
                distance = math.sqrt(dx * dx + dy * dy + dz * dz)
                heading = math.degrees(math.atan2(-dx, dy)) if horizontal > 0.0001 else self.app.heading
                self.app.set_automation_look(heading, self.app.pitch)
                vertical_axis = 0.0
                if self.app.move_mode == "fly" and abs(dz) > 0.35:
                    vertical_axis = 1.0 if dz > 0 else -1.0
                forward = 0.0 if horizontal <= tolerance else 1.0
                self.app.set_automation_movement(forward=forward, right=0.0, vertical=vertical_axis)
                return {"ok": True, "distance": distance, "horizontal": horizontal, "pose": self.app.get_player_pose()}

            state = self._call_current(generation, is_current, step)
            if float(state.get("distance", 9999.0)) <= tolerance:
                arrived = True
                break
            time.sleep(0.08)
        if is_current(generation):
            self.stop_movement({}, generation, is_current)
        pose = self.observe({"radius": 8, "limit": 60}, generation, is_current).get("pose")
        return {"ok": arrived and not interrupted, "arrived": arrived, "interrupted": interrupted, "target": target_pos, "pose": pose}

    def move_near_region(self, arguments: JsonObject, generation: int, is_current: Callable[[int], bool]) -> JsonObject:
        min_cell = self._world_cell(arguments.get("min_cell"))
        max_cell = self._world_cell(arguments.get("max_cell"))
        distance = max(2.0, min(24.0, float(arguments.get("distance", 8.0))))
        timeout = max(1.0, min(60.0, float(arguments.get("timeout", 18.0))))
        fly = bool(arguments.get("fly", False))
        x0, x1 = sorted((min_cell[0], max_cell[0]))
        y0, y1 = sorted((min_cell[1], max_cell[1]))
        z0, z1 = sorted((min_cell[2], max_cell[2]))
        center = ((x0 + x1) / 2.0 + 0.5, (y0 + y1) / 2.0 + 0.5, (z0 + z1) / 2.0 + 0.5)

        def choose_target() -> JsonObject:
            px, py, _pz = self.app.get_player_position()
            dx = px - center[0]
            dy = py - center[1]
            length = math.hypot(dx, dy)
            if length < 0.001:
                dx, dy, length = 1.0, -1.0, math.sqrt(2.0)
            tx = center[0] + dx / length * distance
            ty = center[1] + dy / length * distance
            tz = max(0.0, center[2])
            return {"ok": True, "target": [tx, ty, tz], "center_cell": [round(center[0] - 0.5), round(center[1] - 0.5), round(center[2] - 0.5)]}

        target_result = self._call_current(generation, is_current, choose_target)
        if not target_result.get("ok"):
            return target_result
        move_result = self.move_to(
            {"position": target_result["target"], "fly": fly, "tolerance": 1.25, "timeout": timeout},
            generation,
            is_current,
        )
        look_result = self.look_at_cell({"cell": target_result["center_cell"]}, generation, is_current)
        return {
            "ok": bool(move_result.get("ok")) and bool(look_result.get("ok")),
            "region": {"min_cell": min_cell, "max_cell": max_cell},
            "target": target_result["target"],
            "look_at": target_result["center_cell"],
            "move": move_result,
            "look": look_result,
        }

    def stop_movement(self, _arguments: JsonObject, generation: int, is_current: Callable[[int], bool]) -> JsonObject:
        def op() -> JsonObject:
            self.app.stop_automation_movement()
            return {"ok": True, "movement": {"forward": 0.0, "right": 0.0, "vertical": 0.0}, "pose": self.app.get_player_pose()}

        return self._call_current(generation, is_current, op)

    def jump(self, _arguments: JsonObject, generation: int, is_current: Callable[[int], bool]) -> JsonObject:
        def op() -> JsonObject:
            self.app.automation_jump()
            return {"ok": True, "pose": self.app.get_player_pose()}

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


def image_data_url(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def response_tools() -> list[JsonObject]:
    cell_schema = {"type": "array", "items": {"type": "integer"}, "minItems": 3, "maxItems": 3}
    color_schema = {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4}

    def tool(name: str, description: str, properties: JsonObject, required: list[str] | None = None) -> JsonObject:
        return {
            "type": "function",
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
                "additionalProperties": False,
            },
        }

    return [
        tool("say", "Speak to the user about the current design plan or result.", {"message": {"type": "string"}}, ["message"]),
        tool("observe", "Inspect client state, player pose, permissions, selected box, and nearby boxes.", {"radius": {"type": "integer"}, "limit": {"type": "integer"}}),
        tool("capture_view", "Capture the current client view. The screenshot is returned to you as visual input.", {"include_image": {"type": "boolean"}}),
        tool(
            "create_box_asset",
            "Create a reusable .box asset in the client cache and optionally select it.",
            {
                "n": {"type": "integer", "minimum": 0, "maximum": 5},
                "color": color_schema,
                "voxels": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"cell": cell_schema, "color": color_schema},
                        "required": ["cell"],
                        "additionalProperties": False,
                    },
                },
                "select": {"type": "boolean"},
                "orientation": {"type": "integer", "minimum": 0, "maximum": 23},
            },
        ),
        tool("upload_box_asset", "Upload an existing .box asset hash to the server without changing world cells.", {"hash": {"type": "string"}}, ["hash"]),
        tool("select_box", "Select an existing reusable box hash for future placement.", {"hash": {"type": "string"}, "orientation": {"type": "integer", "minimum": 0, "maximum": 23}}, ["hash"]),
        tool("place_box", "Place or replace one world box at an integer cell.", {"cell": cell_schema, "hash": {"type": "string"}, "orientation": {"type": "integer", "minimum": 0, "maximum": 23}}, ["cell"]),
        tool("delete_box", "Delete one world box at an integer cell.", {"cell": cell_schema}, ["cell"]),
        tool(
            "fill_region",
            "Place or delete boxes in an inclusive cuboid world region. Before calling this, move near the region and look at it.",
            {"min_cell": cell_schema, "max_cell": cell_schema, "mode": {"type": "string", "enum": ["place", "delete"]}, "hash": {"type": "string"}, "orientation": {"type": "integer", "minimum": 0, "maximum": 23}},
            ["min_cell", "max_cell"],
        ),
        tool("set_move_mode", "Set player movement mode to walk or fly.", {"mode": {"type": "string", "enum": ["walk", "fly"]}}, ["mode"]),
        tool("set_look", "Set or adjust camera heading/pitch in degrees.", {"heading": {"type": "number"}, "pitch": {"type": "number"}, "heading_delta": {"type": "number"}, "pitch_delta": {"type": "number"}}),
        tool("look_at_cell", "Turn the camera to look at a world cell.", {"cell": cell_schema}, ["cell"]),
        tool("set_movement", "Set continuous automation movement axes, each clamped to -1..1.", {"forward": {"type": "number"}, "right": {"type": "number"}, "vertical": {"type": "number"}}),
        tool("move_for", "Move with automation axes for a bounded number of seconds, then stop.", {"forward": {"type": "number"}, "right": {"type": "number"}, "vertical": {"type": "number"}, "seconds": {"type": "number", "minimum": 0, "maximum": 30}}),
        tool("move_to", "Move toward a target world position or cell, optionally using fly mode.", {"position": cell_schema, "cell": cell_schema, "fly": {"type": "boolean"}, "tolerance": {"type": "number"}, "timeout": {"type": "number"}}),
        tool(
            "move_near_region",
            "Move near an inclusive cuboid region and look at its center before inspecting or filling it.",
            {"min_cell": cell_schema, "max_cell": cell_schema, "distance": {"type": "number"}, "fly": {"type": "boolean"}, "timeout": {"type": "number"}},
            ["min_cell", "max_cell"],
        ),
        tool("stop_movement", "Stop all automation movement immediately.", {}),
        tool("jump", "Request one walk-mode jump.", {}),
    ]


class AgentCoordinator:
    def __init__(self, client: ResponsesClient, tools: ClientTools, max_tool_steps: int | None) -> None:
        self.client = client
        self.tools = tools
        if max_tool_steps is None or int(max_tool_steps) <= 0:
            self.max_tool_steps: int | None = None
        else:
            self.max_tool_steps = int(max_tool_steps)
        self._tool_schemas = response_tools()
        self._lock = threading.RLock()
        self._generation = 0
        self._history: list[str] = []
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
            self._history.append(f"User: {stripped}")
            history = list(self._history[-16:])
        print("[agent] accepted instruction; current movement was interrupted.", flush=True)
        threading.Thread(target=self._run_turn, args=(generation, stripped, history), daemon=True).start()

    def request_shutdown(self) -> None:
        with self._lock:
            self._generation += 1
        self.tools.stop_motion_async()
        self.shutdown_requested.set()

    def is_current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation and not self.shutdown_requested.is_set()

    def _run_turn(self, generation: int, instruction: str, history: list[str]) -> None:
        print("[agent] thinking with Responses + screenshot feedback...", flush=True)
        try:
            observation = self.tools.execute("observe", {"radius": 12, "limit": 140}, generation, self.is_current)
            screenshot = self.tools.execute("capture_view", {"include_image": True}, generation, self.is_current)
            if not self.is_current(generation):
                return

            input_items = [
                self._user_message(
                    [
                        {
                            "type": "input_text",
                            "text": (
                                "New user instruction:\n"
                                f"{instruction}\n\n"
                                "Recent conversation:\n"
                                + "\n".join(history[-12:])
                                + "\n\nCurrent client observation:\n"
                                + json.dumps(strip_private_fields(observation), ensure_ascii=False)
                                + "\n\nFirst, speak briefly to the user about your design intent. "
                                "Then use tools to act. Inspect screenshots after changes."
                            ),
                        },
                        *image_content_from_result(screenshot, self.tools.screenshot_detail),
                    ]
                )
            ]
            print(f"[agent] observed client: {json_preview(observation)}", flush=True)
            if screenshot.get("ok"):
                print(f"[agent] screenshot: {screenshot.get('path')}", flush=True)

            last_tool_result: JsonObject | None = None
            executed_tool_steps = 0
            turn_spoke = False
            while True:
                if not self.is_current(generation):
                    return
                print(f"[agent] requesting Responses model ({self.client.model})...", flush=True)
                response = self.client.create(input_items, self._tool_schemas)
                output = response.get("output", [])
                if not isinstance(output, list):
                    raise RuntimeError(f"Responses output was not a list: {json_preview(response)}")
                input_items.extend(output)

                text = extract_response_text(response)
                if text:
                    self._record_assistant_text(generation, text)
                    print(f"AI: {text}", flush=True)
                    turn_spoke = True

                calls = parse_function_calls(output)
                if not calls:
                    if not text:
                        message = localized_status(
                            instruction,
                            ZH_NO_RESPONSE,
                            "The model returned no text or tool calls, so I did not take action. Try a more specific instruction or check that this model supports Responses tool calls.",
                        )
                        print(f"AI: {message}", flush=True)
                        print(f"[agent] no text or tool calls in response: {json_preview(response, 1000)}", flush=True)
                    return

                for call in calls:
                    if not self.is_current(generation):
                        return
                    counts_as_tool_step = call["name"] != "say"
                    if (
                        counts_as_tool_step
                        and self.max_tool_steps is not None
                        and executed_tool_steps >= self.max_tool_steps
                    ):
                        print(
                            "AI: I reached the explicit --max-tool-steps limit for this instruction.",
                            flush=True,
                        )
                        return
                    if call["name"] != "say" and not turn_spoke:
                        message = localized_status(
                            instruction,
                            ZH_START_ACTING,
                            "I can see the current view. I am going to start acting on this instruction, then inspect a screenshot and adjust.",
                        )
                        print(f"AI: {message}", flush=True)
                        self._record_assistant_text(generation, message)
                        turn_spoke = True
                    print(f"[agent] tool {call['name']} {json_preview(call['arguments'])}", flush=True)
                    result = self.tools.execute(call["name"], call["arguments"], generation, self.is_current)
                    last_tool_result = result
                    print(f"[agent] tool result {json_preview(result)}", flush=True)
                    input_items.append(
                        {
                            "type": "function_call_output",
                            "call_id": call["call_id"],
                            "output": json.dumps(strip_private_fields(result), ensure_ascii=False),
                        }
                    )
                    if call["name"] == "say" and result.get("ok"):
                        self._record_assistant_text(generation, str(result.get("said", "")))
                        turn_spoke = True
                    if counts_as_tool_step:
                        executed_tool_steps += 1

                if not self.is_current(generation):
                    return
                observation = self.tools.execute("observe", {"radius": 12, "limit": 140}, generation, self.is_current)
                screenshot = self.tools.execute("capture_view", {"include_image": True}, generation, self.is_current)
                feedback_content = [
                    {
                        "type": "input_text",
                        "text": (
                            "Post-tool feedback. Inspect this before the next step.\n"
                            "Observation:\n"
                            + json.dumps(strip_private_fields(observation), ensure_ascii=False)
                            + "\nLast tool result:\n"
                            + json.dumps(strip_private_fields(last_tool_result or {}), ensure_ascii=False)
                        ),
                    },
                    *image_content_from_result(screenshot, self.tools.screenshot_detail),
                ]
                input_items.append(self._user_message(feedback_content))
                if screenshot.get("ok"):
                    print(f"[agent] visual feedback screenshot: {screenshot.get('path')}", flush=True)
        except Exception as exc:  # noqa: BLE001
            if self.is_current(generation):
                message = localized_status(
                    instruction,
                    f"{ZH_TURN_FAILED_PREFIX}{type(exc).__name__}: {exc}",
                    f"This turn failed: {type(exc).__name__}: {exc}",
                )
                print(f"AI: {message}", flush=True)
                print(f"[agent] error: {type(exc).__name__}: {exc}", flush=True)
                traceback.print_exception(type(exc), exc, exc.__traceback__)

    def _user_message(self, content: list[JsonObject]) -> JsonObject:
        return {"role": "user", "content": content}

    def _record_assistant_text(self, generation: int, text: str) -> None:
        clean = " ".join(text.strip().split())
        if not clean:
            return
        with self._lock:
            if generation == self._generation:
                self._history.append(f"AI: {clean}")
                self._history = self._history[-24:]


def strip_private_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: strip_private_fields(item) for key, item in value.items() if not key.startswith("_")}
    if isinstance(value, list):
        return [strip_private_fields(item) for item in value]
    return value


def localized_status(instruction: str, zh: str, en: str) -> str:
    return zh if contains_cjk(instruction) else en


def contains_cjk(text: str) -> bool:
    return any("\u4e00" <= character <= "\u9fff" for character in text)


def image_content_from_result(result: JsonObject, detail: str) -> list[JsonObject]:
    image_url = result.get("_image_url")
    if not isinstance(image_url, str) or not image_url:
        return []
    return [{"type": "input_image", "image_url": image_url, "detail": result.get("_image_detail", detail)}]


def extract_response_text(response: JsonObject) -> str:
    direct = response.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    parts: list[str] = []
    output = response.get("output", [])
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content", [])
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"output_text", "text"}:
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "\n".join(part.strip() for part in parts if part.strip())


def parse_function_calls(output: list[JsonObject]) -> list[JsonObject]:
    calls: list[JsonObject] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        name = str(item.get("name", "")).strip()
        raw_arguments = item.get("arguments") or "{}"
        if isinstance(raw_arguments, str):
            arguments = json.loads(raw_arguments)
        elif isinstance(raw_arguments, dict):
            arguments = raw_arguments
        else:
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        call_id = str(item.get("call_id") or item.get("id") or f"call-{time.time_ns()}")
        calls.append({"name": name, "arguments": arguments, "call_id": call_id})
    return calls


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
    tools = ClientTools(
        app,
        gate,
        max_fill_volume=args.max_fill_volume,
        screenshot_detail=args.screenshot_detail,
    )
    client = ResponsesClient(
        endpoint=endpoint,
        api_key=api_key,
        model=args.model,
        temperature=args.temperature,
        timeout=args.request_timeout,
        retries=args.request_retries,
    )
    coordinator = AgentCoordinator(client, tools, max_tool_steps=args.max_tool_steps)

    def shutdown_task(task: Any) -> Any:
        if coordinator.shutdown_requested.is_set():
            if app.network_client is not None:
                app.network_client.close()
            raise SystemExit
        return task.cont

    app.taskMgr.add(shutdown_task, "ai-agent-shutdown")
    threading.Thread(target=stdin_loop, args=(coordinator,), daemon=True).start()

    print(f"[agent] visible AI client starting for {args.host}:{args.port} as {args.user_id!r}.")
    print("[agent] type instructions for the AI agent; type 'exit' to stop.")
    try:
        app.run()
    finally:
        if app.network_client is not None:
            app.network_client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
