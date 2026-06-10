from __future__ import annotations

import ctypes
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Callable

from direct.gui.DirectGui import DirectButton, DirectEntry, DirectFrame, DirectLabel
from direct.gui.OnscreenText import OnscreenText
from direct.showbase.ShowBase import ShowBase
from direct.showbase.ShowBaseGlobal import globalClock
from panda3d.core import (
    AmbientLight,
    AntialiasAttrib,
    DirectionalLight,
    Filename,
    Geom,
    GeomNode,
    GeomTriangles,
    GeomVertexData,
    GeomVertexFormat,
    GeomVertexWriter,
    LineSegs,
    NodePath,
    Point2,
    Point3,
    TransparencyAttrib,
    Vec3,
    WindowProperties,
    loadPrcFileData,
)

from box_editor_view.audio import ensure_sound_files
from box_editor_view.box_file import BoxFormatError
from box_editor_view.geometry import make_cube_outline, make_cuboid
from box_editor_view.gpu import GpuProfile, detect_gpu_profile
from box_editor_view.platform_window import disable_ime_for_window, maximize_window

from .box_assets import copy_box_for_editing, ensure_default_box, store_box_file_by_hash
from .box_mesh import (
    BoxSurfaceCache,
    ChunkKey,
    WorldChunkMesh,
    build_world_chunk_mesh,
    chunk_key_for_cell,
    chunk_keys_for_cell_and_neighbors,
)
from .client_net import NetworkWorldClient, RemotePlayer
from .collision import CollisionShapeCache
from .net_protocol import list_to_cell
from .orientation import IDENTITY_ORIENTATION, nearest_axis, turn_orientation_around_axis
from .world_file import Cell, LoadedWorld, WorldMap, save_world


loadPrcFileData(
    "",
    "\n".join(
        [
            "window-title Neko Mouse World",
            "sync-video false",
            "show-frame-rate-meter true",
            "textures-power-2 none",
            "framebuffer-multisample true",
            "multisamples 4",
        ]
    ),
)


PLAYER_WIDTH = 0.96
PLAYER_HEIGHT = 1.8
EYE_HEIGHT = 1.70
CAMERA_NEAR = 0.03
CAMERA_FOV = 82.0
MOVE_SPEED = 5.2
FLY_VERTICAL_SPEED = 4.4
MOUSE_SENSITIVITY = 0.055
GRAVITY = 18.0
JUMP_HEIGHT = 1.1
JUMP_SPEED = math.sqrt(2.0 * GRAVITY * JUMP_HEIGHT)
STANDING_TOLERANCE = 0.10
STEP_HEIGHT = 0.5
FOOT_PROBE_FORWARD = PLAYER_WIDTH * 0.25
MAX_INTERACTION_DISTANCE = 10.0
GROUND_SIZE = 80
GROUND_CHUNK = 16
SOUND_VOLUME = 0.5
CLOSE_REQUEST_EVENT = "neko-mouse-world-close-request"
STARTUP_MAXIMIZE_FRAMES = 30


class _NullSound:
    def setVolume(self, _volume: float) -> None:
        pass

    def play(self) -> None:
        pass


class NekoMouseWorldApp(ShowBase):
    def __init__(
        self,
        loaded_world: LoadedWorld,
        network_client: NetworkWorldClient | None = None,
        show_connect_dialog: bool = False,
        default_connect_host: str = "127.0.0.1",
        default_connect_port: int = 5678,
    ) -> None:
        super().__init__()
        self.disableMouse()
        self.camLens.setNear(CAMERA_NEAR)
        self.camLens.setFov(CAMERA_FOV)
        self._sync_camera_aspect()

        self.paths = loaded_world.paths
        self.network_client = network_client
        self.connect_required = show_connect_dialog and network_client is None
        self.default_connect_host = default_connect_host
        self.default_connect_port = default_connect_port
        self.world_map: WorldMap = loaded_world.world_map
        self.default_hash = network_client.default_hash if network_client and network_client.default_hash else ensure_default_box(self.paths.boxes_dir)
        self.selected_hash = self.default_hash
        self.saved_snapshot = self._current_world_snapshot()
        self.gpu_profile: GpuProfile = detect_gpu_profile(self.win.getGsg() if self.win else None)
        self.window_maximized = maximize_window(self.win)
        self.ime_disabled = disable_ime_for_window(self.win)

        self.world = self.render.attachNewNode("world")
        self.blocks_root = self.world.attachNewNode("world-boxes")
        self.ground_node: NodePath | None = None
        self.ground_origin: tuple[int, int] | None = None
        self.surface_cache = BoxSurfaceCache(self.paths.boxes_dir)
        self.collision_cache = CollisionShapeCache(self.paths.boxes_dir)
        self.chunk_meshes: dict[ChunkKey, WorldChunkMesh] = {}
        self.chunk_index: dict[ChunkKey, set[Cell]] = {}
        self.hover_outline = make_cube_outline()
        self.hover_outline.reparentTo(self.world)
        self.hover_outline.hide()
        self.hovered_cell: Cell | None = None

        self.player_pos = Vec3(0.5, -4.0, 0.0)
        self.heading = 0.0
        self.pitch = -10.0
        self.view_mode = "first"
        self.move_mode = "walk"
        self.vertical_velocity = 0.0
        self.grounded = True
        self.mouse_captured = False
        self.ui_open = False
        self.modal_mode: str | None = None
        self.help_panel: DirectFrame | None = None
        self.quit_panel: DirectFrame | None = None
        self.disconnect_panel: DirectFrame | None = None
        self.disconnect_message: DirectLabel | None = None
        self.connect_panel: DirectFrame | None = None
        self.connect_host_entry: DirectEntry | None = None
        self.connect_port_entry: DirectEntry | None = None
        self.connect_error_label: DirectLabel | None = None
        self.editor_wait_panel: DirectFrame | None = None
        self.editor_process: subprocess.Popen[bytes] | None = None
        self.editor_tempdir: tempfile.TemporaryDirectory[str] | None = None
        self.editor_edit_path: Path | None = None
        self.editor_cell: Cell | None = None
        self.editor_original_digest: str | None = None
        self.editor_restore_mouse_capture = False
        self.quit_restore_mouse_capture = False
        self.quit_button_frames: dict[str, DirectFrame] = {}
        self.quit_buttons: dict[str, DirectButton] = {}
        self.active_quit_choice = "cancel"
        self.startup_maximize_frames = STARTUP_MAXIMIZE_FRAMES
        self.removed_missing_refs = loaded_world.removed_missing_refs
        self.remote_player_nodes: dict[int, NodePath] = {}
        self.last_udp_player_state = 0.0

        self.key_state = {
            "forward": False,
            "back": False,
            "left": False,
            "right": False,
            "up": False,
            "down": False,
        }

        self._setup_lights()
        self._setup_player_model()
        self._setup_hud()
        self._setup_audio()
        self._bind_events()
        self._rebuild_all_cells()
        self._update_ground(force=True)
        self._lift_player_out_of_blocks()

        self.set_mouse_capture(True)
        self.taskMgr.add(self._update, "neko-mouse-world-update")
        if self.removed_missing_refs:
            self._set_status(f"Removed {self.removed_missing_refs} missing box references")
        else:
            self._set_status("Ready")
        if show_connect_dialog and self.network_client is None:
            self._open_connect_dialog()
        elif self.network_client and not self.network_client.connected:
            self._show_disconnect_panel("Connecting to server...")

    def _setup_lights(self) -> None:
        ambient = AmbientLight("ambient")
        ambient.setColor((0.34, 0.36, 0.39, 1.0))
        self.render.setLight(self.render.attachNewNode(ambient))

        sun = DirectionalLight("sun")
        sun.setColor((1.0, 0.94, 0.82, 1.0))
        sun_path = self.render.attachNewNode(sun)
        sun_path.setHpr(-38, -56, 0)
        self.render.setLight(sun_path)

        fill = DirectionalLight("fill")
        fill.setColor((0.18, 0.22, 0.30, 1.0))
        fill_path = self.render.attachNewNode(fill)
        fill_path.setHpr(135, -18, 0)
        self.render.setLight(fill_path)

        if self.gpu_profile.shader_auto_enabled:
            self.render.setShaderAuto()
        if self.gpu_profile.antialias_enabled:
            self.render.setAntialias(AntialiasAttrib.MMultisample)
        self.setBackgroundColor(0.60, 0.72, 0.86, 1.0)

    def _setup_player_model(self) -> None:
        self.player_model = self.render.attachNewNode("player")
        self._add_player_part("body", (0.52, 0.28, 0.82), (0, 0, 1.08), (0.10, 0.34, 0.88, 1))
        self._add_player_part("head", (0.48, 0.48, 0.48), (0, 0, 1.74), (0.86, 0.70, 0.52, 1))
        self._add_player_part("left-arm", (0.18, 0.22, 0.72), (-0.37, 0, 1.08), (0.10, 0.34, 0.88, 1))
        self._add_player_part("right-arm", (0.18, 0.22, 0.72), (0.37, 0, 1.08), (0.10, 0.34, 0.88, 1))
        self._add_player_part("left-leg", (0.20, 0.24, 0.78), (-0.13, 0, 0.39), (0.12, 0.18, 0.42, 1))
        self._add_player_part("right-leg", (0.20, 0.24, 0.78), (0.13, 0, 0.39), (0.12, 0.18, 0.42, 1))
        self.player_model.setTransparency(TransparencyAttrib.MAlpha)
        self.player_model.hide()

    def _add_player_part(
        self,
        name: str,
        size: tuple[float, float, float],
        pos: tuple[float, float, float],
        color: tuple[float, float, float, float],
    ) -> None:
        part = make_cuboid(name, size)
        part.reparentTo(self.player_model)
        part.setPos(*pos)
        part.setColor(*color)

    def _make_remote_player_node(self, player_id: int) -> NodePath:
        root = self.render.attachNewNode(f"remote-player-{player_id}")
        palette = (
            (0.92, 0.32, 0.28, 1),
            (0.16, 0.62, 0.56, 1),
            (0.95, 0.68, 0.18, 1),
            (0.46, 0.42, 0.88, 1),
        )
        accent = palette[player_id % len(palette)]
        skin = (0.86, 0.70, 0.52, 1)
        for name, size, pos, color in (
            ("body", (0.52, 0.28, 0.82), (0, 0, 1.08), accent),
            ("head", (0.48, 0.48, 0.48), (0, 0, 1.74), skin),
            ("left-arm", (0.18, 0.22, 0.72), (-0.37, 0, 1.08), accent),
            ("right-arm", (0.18, 0.22, 0.72), (0.37, 0, 1.08), accent),
            ("left-leg", (0.20, 0.24, 0.78), (-0.13, 0, 0.39), (0.15, 0.18, 0.24, 1)),
            ("right-leg", (0.20, 0.24, 0.78), (0.13, 0, 0.39), (0.15, 0.18, 0.24, 1)),
        ):
            part = make_cuboid(name, size)
            part.reparentTo(root)
            part.setPos(*pos)
            part.setColor(*color)
        root.setTransparency(TransparencyAttrib.MAlpha)
        return root

    def _setup_hud(self) -> None:
        self.status = OnscreenText(
            text="",
            pos=(-1.31, 0.94),
            align=0,
            scale=0.038,
            fg=(1, 1, 1, 1),
            mayChange=True,
            shadow=(0, 0, 0, 0.7),
        )
        self.detail = OnscreenText(
            text="",
            pos=(-1.31, 0.89),
            align=0,
            scale=0.031,
            fg=(1, 1, 1, 0.92),
            mayChange=True,
            shadow=(0, 0, 0, 0.7),
        )
        self.help_hint = OnscreenText(
            text="Press H for help",
            pos=(1.30, 0.80),
            align=1,
            scale=0.033,
            fg=(1, 1, 1, 0.92),
            mayChange=False,
            shadow=(0, 0, 0, 0.7),
        )
        self.crosshair = self._make_crosshair()
        self.crosshair.reparentTo(self.aspect2d)

    def _make_crosshair(self) -> NodePath:
        lines = LineSegs()
        lines.setThickness(2.0)
        lines.setColor(1, 1, 1, 0.92)
        lines.moveTo(-0.018, 0, 0)
        lines.drawTo(0.018, 0, 0)
        lines.moveTo(0, 0, -0.018)
        lines.drawTo(0, 0, 0.018)
        return NodePath(lines.create())

    def _setup_audio(self) -> None:
        sound_paths = ensure_sound_files()
        self.place_sound = self._load_sound(sound_paths["place"])
        self.break_sound = self._load_sound(sound_paths["break"])

    def _load_sound(self, path: Path):
        panda_path = Filename.fromOsSpecific(str(path)).getFullpath()
        sound = self.loader.loadSfx(panda_path)
        sound = sound if sound is not None else _NullSound()
        sound.setVolume(SOUND_VOLUME)
        return sound

    def _bind_events(self) -> None:
        self._setup_close_request_event()
        binds: dict[str, tuple[str, bool]] = {
            "w": ("forward", True),
            "w-up": ("forward", False),
            "s": ("back", True),
            "s-up": ("back", False),
            "a": ("left", True),
            "a-up": ("left", False),
            "d": ("right", True),
            "d-up": ("right", False),
            "shift": ("down", True),
            "shift-up": ("down", False),
        }
        for event, (name, value) in binds.items():
            self.accept(event, self._set_key, [name, value])

        self.accept("space", self._space_pressed)
        self.accept("space-up", self._set_key, ["up", False])
        self.accept("mouse1", self._delete_clicked_box)
        self.accept("mouse2", self._pick_clicked_box)
        self.accept("mouse3", self._right_click)
        self.accept("mouse1-up", self._focus_editor_if_waiting)
        self.accept("mouse2-up", self._focus_editor_if_waiting)
        self.accept("mouse3-up", self._focus_editor_if_waiting)
        self.accept("escape", self._release_mouse_capture)
        self.accept("window-event", self._handle_window_event)
        self.accept("f", self._toggle_move_mode)
        self.accept("f2", self.save_current)
        self.accept("control-s", self.save_current)
        self.accept("f5", self._toggle_view)
        self.accept("e", self._edit_target_box)
        self.accept("h", self._open_help)
        self.accept("c", self._look_at_world_focus)
        for event_name, command in {
            "4": "left",
            "6": "right",
            "8": "up",
            "2": "down",
            "num_4": "left",
            "num_6": "right",
            "num_8": "up",
            "num_2": "down",
            "num4": "left",
            "num6": "right",
            "num8": "up",
            "num2": "down",
            "numpad4": "left",
            "numpad6": "right",
            "numpad8": "up",
            "numpad2": "down",
            "numpad_4": "left",
            "numpad_6": "right",
            "numpad_8": "up",
            "numpad_2": "down",
            "keypad4": "left",
            "keypad6": "right",
            "keypad8": "up",
            "keypad2": "down",
            "keypad_4": "left",
            "keypad_6": "right",
            "keypad_8": "up",
            "keypad_2": "down",
            "kp4": "left",
            "kp6": "right",
            "kp8": "up",
            "kp2": "down",
            "kp_4": "left",
            "kp_6": "right",
            "kp_8": "up",
            "kp_2": "down",
        }.items():
            self.accept(event_name, self._rotate_key_pressed, [command])
        self.accept("tab", self._focus_next_quit_choice, [1])
        self.accept("shift-tab", self._focus_next_quit_choice, [-1])
        self.accept("shift_tab", self._focus_next_quit_choice, [-1])
        self.accept("arrow_right", self._directional_key_pressed, ["right"])
        self.accept("arrow_left", self._directional_key_pressed, ["left"])
        self.accept("arrow_up", self._directional_key_pressed, ["up"])
        self.accept("arrow_down", self._directional_key_pressed, ["down"])
        self.accept("enter", self._submit_modal)

    def _setup_close_request_event(self) -> None:
        if hasattr(self.win, "setCloseRequestEvent"):
            self.win.setCloseRequestEvent(CLOSE_REQUEST_EVENT)
            self.accept(CLOSE_REQUEST_EVENT, self._request_quit)

    def _set_key(self, name: str, value: bool) -> None:
        if self.ui_open:
            self.key_state[name] = False
            return
        self.key_state[name] = value

    def _space_pressed(self) -> None:
        if self.ui_open:
            return
        self.key_state["up"] = True
        if self.move_mode == "walk" and self.grounded:
            self.vertical_velocity = JUMP_SPEED
            self.grounded = False

    def _update(self, task):
        dt = min(globalClock.getDt(), 0.05)
        self._keep_startup_window_maximized()
        self._process_network()
        if self.mouse_captured and not self.ui_open:
            self._update_mouse_look()
        if not self.ui_open:
            self._update_player(dt)
        self._send_network_player_state()
        self._sync_remote_players()
        self._update_ground()
        self._update_camera()
        self._update_hover_outline()
        self._update_hud()
        return task.cont

    def _process_network(self) -> None:
        if self.network_client is None:
            return
        if self.network_client.connected:
            self._hide_disconnect_panel()
        else:
            self._show_disconnect_panel("Disconnected. Reconnecting...")
        for message in self.network_client.poll():
            message_type = message.get("type")
            if message_type == "welcome":
                self.default_hash = self.network_client.default_hash or self.default_hash
                self.selected_hash = self.default_hash
                self.world_map = self.network_client.world_map
                self._rebuild_all_cells()
                self.saved_snapshot = self._current_world_snapshot()
                self._set_status("Connected to server")
                self._hide_disconnect_panel()
            elif message_type == "asset":
                digest = str(message.get("hash", ""))
                self.surface_cache.invalidate(digest)
                self.collision_cache.invalidate(digest)
            elif message_type == "box_set":
                cell = list_to_cell(message.get("cell"))
                digest = str(message.get("hash", ""))
                orientation = int(message.get("orientation", IDENTITY_ORIENTATION))
                self._apply_world_box(cell, digest, orientation)
                self.saved_snapshot = self._current_world_snapshot()
            elif message_type == "box_removed":
                self._apply_remove_world_box(list_to_cell(message.get("cell")), play_sound=True)
                self.saved_snapshot = self._current_world_snapshot()
            elif message_type == "udp_status":
                if message.get("enabled"):
                    self._set_status("Connected; player positions use UDP")
                else:
                    received = message.get("received", 0)
                    sent = message.get("sent", 0)
                    self._set_status(f"Connected; UDP probe {received}/{sent}, positions use TCP")
            elif message_type == "disconnect":
                self._show_disconnect_panel("Disconnected. Reconnecting...")

    def _send_network_player_state(self) -> None:
        if self.network_client is None or not self.network_client.connected:
            return
        now = globalClock.getFrameTime()
        if now - self.last_udp_player_state < 0.05:
            return
        self.last_udp_player_state = now
        self.network_client.send_player_state(
            (self.player_pos.x, self.player_pos.y, self.player_pos.z),
            self.heading,
            self.pitch,
            self.move_mode,
        )

    def _sync_remote_players(self) -> None:
        if self.network_client is None:
            return
        remote_players = self.network_client.remote_players_snapshot()
        for player_id in list(self.remote_player_nodes):
            if player_id not in remote_players:
                self.remote_player_nodes.pop(player_id).removeNode()
        for player_id, player in remote_players.items():
            node = self.remote_player_nodes.get(player_id)
            if node is None:
                node = self._make_remote_player_node(player_id)
                self.remote_player_nodes[player_id] = node
            node.setPos(*player.pos)
            node.setH(player.heading)

    def _keep_startup_window_maximized(self) -> None:
        if self.startup_maximize_frames <= 0:
            return
        self.window_maximized = maximize_window(self.win) or self.window_maximized
        self.startup_maximize_frames -= 1

    def _update_mouse_look(self) -> None:
        if not self.win or not hasattr(self.win, "getPointer"):
            return
        pointer = self.win.getPointer(0)
        center_x = self.win.getXSize() // 2
        center_y = self.win.getYSize() // 2
        dx = pointer.getX() - center_x
        dy = pointer.getY() - center_y
        if dx or dy:
            self.heading -= dx * MOUSE_SENSITIVITY
            self.pitch = max(-89.0, min(89.0, self.pitch - dy * MOUSE_SENSITIVITY))
            self.win.movePointer(0, center_x, center_y)

    def _update_player(self, dt: float) -> None:
        heading_rad = math.radians(self.heading)
        forward = Vec3(-math.sin(heading_rad), math.cos(heading_rad), 0)
        right = Vec3(math.cos(heading_rad), math.sin(heading_rad), 0)
        desired = Vec3(0, 0, 0)

        if self.key_state["forward"]:
            desired += forward
        if self.key_state["back"]:
            desired -= forward
        if self.key_state["right"]:
            desired += right
        if self.key_state["left"]:
            desired -= right
        if desired.lengthSquared() > 0:
            desired.normalize()
            desired *= MOVE_SPEED * dt

        if self.move_mode == "fly":
            if self.key_state["up"]:
                desired.z += FLY_VERTICAL_SPEED * dt
            if self.key_state["down"]:
                desired.z -= FLY_VERTICAL_SPEED * dt
            self.vertical_velocity = 0.0
            self.grounded = False
        else:
            if self.grounded and self.vertical_velocity <= 0.0:
                self.vertical_velocity = 0.0
            else:
                self.vertical_velocity -= GRAVITY * dt
            desired.z += self.vertical_velocity * dt

        self._move_player_with_collision(desired)
        if self.move_mode == "walk":
            self.grounded = self._support_height_below(self.player_pos, STANDING_TOLERANCE) is not None
            if self.grounded and self.vertical_velocity < 0.0:
                self.vertical_velocity = 0.0
        self.player_model.setPos(self.player_pos)
        self.player_model.setH(self.heading)

    def _move_player_with_collision(self, movement: Vec3) -> None:
        for component in (Vec3(movement.x, 0, 0), Vec3(0, movement.y, 0), Vec3(0, 0, movement.z)):
            if component.lengthSquared() == 0:
                continue
            if component.z == 0.0 and self.move_mode == "walk" and self.grounded:
                if self._try_walk_horizontal(component):
                    continue
            candidate = self.player_pos + component
            if candidate.z < 0.0:
                self.player_pos.z = 0.0
                if component.z < 0.0:
                    self.vertical_velocity = 0.0
                    self.grounded = True
                continue
            collision_top = self._blocking_top_for_player(candidate)
            if collision_top is None:
                if (component.x != 0.0 or component.y != 0.0) and self.move_mode == "walk" and self.grounded:
                    support = self._support_height_below(candidate, STEP_HEIGHT + STANDING_TOLERANCE)
                    if support is not None and support <= self.player_pos.z + STEP_HEIGHT + STANDING_TOLERANCE:
                        candidate.z = support
                self.player_pos = candidate
                continue

            if component.x != 0.0 or component.y != 0.0:
                climb_limit = self.player_pos.z + STEP_HEIGHT + STANDING_TOLERANCE
                if collision_top <= climb_limit:
                    stepped = Vec3(candidate.x, candidate.y, collision_top)
                    if self._blocking_top_for_player(stepped) is None:
                        self.player_pos = stepped
                        self.vertical_velocity = 0.0
                        self.grounded = True
                continue

            if component.z < 0.0:
                support = self._support_height_below(self.player_pos, abs(component.z) + STANDING_TOLERANCE)
                if support is not None:
                    self.player_pos.z = support
                    self.vertical_velocity = 0.0
                    self.grounded = True
            elif component.z > 0.0:
                self.vertical_velocity = 0.0

    def _try_walk_horizontal(self, component: Vec3) -> bool:
        candidate = self.player_pos + component
        support = self._walk_support_height(candidate, component)
        if support is not None:
            candidate.z = support
            ignored_top = support + STEP_HEIGHT + STANDING_TOLERANCE
            if self._blocking_top_for_player(candidate, ignore_top_at_or_below=ignored_top) is None:
                self.player_pos = candidate
                self.vertical_velocity = 0.0
                self.grounded = True
            return True
        return False

    def _player_collides_blocks(self, pos: Vec3) -> bool:
        return self._blocking_top_for_player(pos) is not None

    def _blocking_top_for_player(self, pos: Vec3, ignore_top_at_or_below: float | None = None) -> float | None:
        min_corner, max_corner = self._player_aabb(pos)
        min_x = math.floor(min_corner.x)
        max_x = math.floor(max_corner.x)
        min_y = math.floor(min_corner.y)
        max_y = math.floor(max_corner.y)
        min_z = math.floor(min_corner.z)
        max_z = math.floor(max_corner.z)

        for x in range(min_x, max_x + 1):
            for y in range(min_y, max_y + 1):
                for z in range(min_z, max_z + 1):
                    height_range = self._shape_height_range_for_world_cell(
                        (x, y, z),
                        min_corner.x,
                        max_corner.x,
                        min_corner.y,
                        max_corner.y,
                    )
                    if height_range is None:
                        continue
                    world_min = z + height_range.minimum
                    world_top = z + height_range.top
                    if min_corner.z < world_top and max_corner.z > world_min:
                        if (
                            ignore_top_at_or_below is not None
                            and world_min <= min_corner.z + STANDING_TOLERANCE
                            and world_top <= ignore_top_at_or_below + 1e-6
                        ):
                            continue
                        return world_top
        return None

    def _walk_support_height(self, pos: Vec3, component: Vec3) -> float | None:
        horizontal = Vec3(component.x, component.y, 0)
        samples = self._foot_support_samples(pos)
        if horizontal.lengthSquared() > 1e-8:
            horizontal.normalize()
            samples.append((pos.x + horizontal.x * FOOT_PROBE_FORWARD, pos.y + horizontal.y * FOOT_PROBE_FORWARD))
        return self._support_height_at_points(
            pos.z,
            samples,
            STEP_HEIGHT + STANDING_TOLERANCE,
            STEP_HEIGHT + STANDING_TOLERANCE,
        )

    def _support_height_below(self, pos: Vec3, tolerance: float) -> float | None:
        support = self._support_height_at_points(pos.z, self._foot_support_samples(pos), tolerance, tolerance)
        if support is not None:
            return support

        min_corner, max_corner = self._player_aabb(pos)
        if pos.z <= tolerance:
            return 0.0

        support: float | None = None
        min_x = math.floor(min_corner.x)
        max_x = math.floor(max_corner.x)
        min_y = math.floor(min_corner.y)
        max_y = math.floor(max_corner.y)
        max_top = pos.z + tolerance
        min_top = pos.z - tolerance
        for x in range(min_x, max_x + 1):
            for y in range(min_y, max_y + 1):
                for z in range(math.floor(min_top) - 1, math.floor(max_top) + 1):
                    height_range = self._shape_height_range_for_world_cell(
                        (x, y, z),
                        min_corner.x,
                        max_corner.x,
                        min_corner.y,
                        max_corner.y,
                    )
                    if height_range is None:
                        continue
                    top = z + height_range.top
                    if min_top <= top <= max_top:
                        support = top if support is None else max(support, top)
        return support

    def _foot_support_samples(self, pos: Vec3) -> list[tuple[float, float]]:
        return [
            (pos.x, pos.y),
            (pos.x + FOOT_PROBE_FORWARD, pos.y),
            (pos.x - FOOT_PROBE_FORWARD, pos.y),
            (pos.x, pos.y + FOOT_PROBE_FORWARD),
            (pos.x, pos.y - FOOT_PROBE_FORWARD),
        ]

    def _support_height_at_points(
        self,
        foot_z: float,
        samples: list[tuple[float, float]],
        up_tolerance: float,
        down_tolerance: float,
    ) -> float | None:
        min_top = foot_z - down_tolerance
        max_top = foot_z + up_tolerance
        support: float | None = 0.0 if min_top <= 0.0 <= max_top else None
        min_z = math.floor(min_top) - 1
        max_z = math.floor(max_top) + 1

        for sample_x, sample_y in samples:
            base_x = math.floor(sample_x)
            base_y = math.floor(sample_y)
            for x in (base_x - 1, base_x, base_x + 1):
                for y in (base_y - 1, base_y, base_y + 1):
                    for z in range(min_z, max_z + 1):
                        digest = self.world_map.get_box((x, y, z))
                        if digest is None:
                            continue
                        orientation = self.world_map.get_orientation((x, y, z))
                        shape = self.collision_cache.get(digest, orientation)
                        top = shape.top_height_at(sample_x - x, sample_y - y)
                        if top is None:
                            continue
                        world_top = z + top
                        if min_top <= world_top <= max_top:
                            support = world_top if support is None else max(support, world_top)
        return support

    def _shape_height_range_for_world_cell(
        self,
        cell: Cell,
        min_x: float,
        max_x: float,
        min_y: float,
        max_y: float,
    ):
        digest = self.world_map.get_box(cell)
        if digest is None:
            return None
        orientation = self.world_map.get_orientation(cell)
        shape = self.collision_cache.get(digest, orientation)
        local_min_x = min_x - cell[0]
        local_max_x = max_x - cell[0]
        local_min_y = min_y - cell[1]
        local_max_y = max_y - cell[1]
        return shape.height_range_for_aabb(local_min_x, local_max_x, local_min_y, local_max_y)

    def _lift_player_out_of_blocks(self) -> None:
        position = Vec3(self.player_pos)
        while position.z < 256.0:
            if not self._player_collides_blocks(position):
                self.player_pos = position
                return
            position.z += 1.0
        self.player_pos = position

    def _player_aabb(self, pos: Vec3) -> tuple[Vec3, Vec3]:
        half = PLAYER_WIDTH * 0.5
        return Vec3(pos.x - half, pos.y - half, pos.z), Vec3(pos.x + half, pos.y + half, pos.z + PLAYER_HEIGHT)

    def _update_camera(self) -> None:
        eye = self.player_pos + Vec3(0, 0, EYE_HEIGHT)
        if self.view_mode == "first":
            self.player_model.hide()
            self.camera.setPos(eye)
            self.camera.setHpr(self.heading, self.pitch, 0)
        else:
            self.player_model.show()
            heading_rad = math.radians(self.heading)
            back = Vec3(math.sin(heading_rad), -math.cos(heading_rad), 0) * 4.8
            cam_pos = eye + back + Vec3(0, 0, 1.55)
            self.camera.setPos(cam_pos)
            self.camera.lookAt(eye + Vec3(0, 0, 0.25))

    def _update_hud(self) -> None:
        selected = self.selected_hash[:12]
        gpu_mode = "GPU" if self.gpu_profile.hardware_accelerated else "software"
        chunk_count = len(self.chunk_meshes)
        quad_count = sum(mesh.quads for mesh in self.chunk_meshes.values())
        net_mode = ""
        if self.network_client is not None:
            net_mode = f"  net={self.network_client.udp_status}"
        self.detail.setText(
            f"{self.paths.root.name}  boxes={len(self.world_map.boxes)}  "
            f"chunks={chunk_count}  quads={quad_count}  selected={selected}  "
            f"move={self.move_mode}  view={self.view_mode}  {gpu_mode}{net_mode}"
        )

    def _set_status(self, text: str) -> None:
        self.status.setText(text)

    def _can_edit_world(self) -> bool:
        if self.connect_required and self.network_client is None:
            self._open_connect_dialog()
            return False
        if self.network_client is None or self.network_client.connected:
            return True
        self._show_disconnect_panel("Disconnected. Reconnecting...")
        self._set_status("Cannot edit while disconnected")
        return False

    def _right_click(self) -> None:
        if self.modal_mode == "editor_wait":
            self._focus_editor_if_waiting()
            return
        if self.ui_open:
            return
        if not self.mouse_captured:
            self.set_mouse_capture(True)
            self._set_status("Mouse captured")
            return
        hit = self._pick()
        if hit is None:
            return
        hit_type, cell, normal, point = hit
        if self.move_mode == "fly" and self.key_state["down"]:
            return

        target = self._placement_cell(hit_type, cell, normal, point)
        if target is None or target in self.world_map.boxes:
            return
        if not self._can_edit_world():
            return
        if self._block_intersects_player(target):
            return
        self._set_world_box(target, self.selected_hash)
        if self.network_client is None:
            self.place_sound.play()
            self._set_status(f"Placed {target}")

    def _delete_clicked_box(self) -> None:
        if self.modal_mode == "editor_wait":
            self._focus_editor_if_waiting()
            return
        if self.ui_open:
            return
        if not self.mouse_captured:
            self.set_mouse_capture(True)
            self._set_status("Mouse captured")
            return
        hit = self._pick()
        if hit is None:
            return
        hit_type, cell, _normal, _point = hit
        if hit_type == "block" and cell is not None:
            if not self._can_edit_world():
                return
            self._remove_world_box(cell)

    def _pick_clicked_box(self) -> None:
        if self.modal_mode == "editor_wait":
            self._focus_editor_if_waiting()
            return
        if self.ui_open:
            return
        if not self.mouse_captured:
            self.set_mouse_capture(True)
            self._set_status("Mouse captured")
            return
        hit = self._pick()
        if hit is None:
            return
        hit_type, cell, _normal, _point = hit
        if hit_type != "block" or cell is None:
            return

        digest = self.world_map.get_box(cell)
        if digest is None:
            return
        self.selected_hash = digest
        self._set_status(f"Selected {digest[:12]}")

    def _rotate_target_box(self, command: str) -> None:
        if self.ui_open:
            return
        if not self.mouse_captured:
            self.set_mouse_capture(True)
            self._set_status("Mouse captured")
            return
        hit = self._pick()
        if hit is None:
            return
        hit_type, cell, _normal, _point = hit
        if hit_type != "block" or cell is None or cell not in self.world_map.boxes:
            return
        if not self._can_edit_world():
            return

        current = self.world_map.get_orientation(cell)
        axis, turns = self._view_rotation_axis(command)
        new_orientation = turn_orientation_around_axis(current, axis, turns)
        if self.network_client is not None:
            self.network_client.send_rotate(cell, new_orientation)
            self._set_status(f"Rotate requested {cell}")
            return
        self.world_map.set_orientation(cell, new_orientation)
        self._rebuild_chunks_for_cell(cell)
        self._set_status(f"Rotated {cell} orientation={new_orientation}")

    def _rotate_key_pressed(self, command: str) -> None:
        if self.modal_mode == "editor_wait":
            self._focus_editor_if_waiting()
            return
        self._rotate_target_box(command)

    def _directional_key_pressed(self, direction: str) -> None:
        if self.modal_mode == "quit":
            if direction == "right":
                self._focus_next_quit_choice(1)
            elif direction == "left":
                self._focus_next_quit_choice(-1)
            return
        if direction in {"left", "right", "up", "down"}:
            self._rotate_key_pressed(direction)

    def _view_rotation_axis(self, command: str) -> tuple[Cell, int]:
        if command in {"left", "right"}:
            up = self.camera.getQuat(self.render).getUp()
            axis = nearest_axis((up.x, up.y, up.z))
            turns = 1 if command == "left" else -1
            return axis, turns
        forward = self.camera.getQuat(self.render).getForward()
        axis = nearest_axis((forward.x, forward.y, forward.z))
        turns = 1 if command == "up" else -1
        return axis, turns

    def _edit_target_box(self) -> None:
        if self.ui_open:
            return
        if not self.mouse_captured:
            self.set_mouse_capture(True)
            self._set_status("Mouse captured")
            return
        hit = self._pick()
        if hit is None:
            return
        hit_type, cell, _normal, _point = hit
        if hit_type != "block" or cell is None:
            return
        if not self._can_edit_world():
            return
        self._open_box_editor_for_cell(cell)

    def _open_box_editor_for_cell(self, cell: Cell) -> None:
        digest = self.world_map.get_box(cell)
        if digest is None:
            return

        self._clear_movement_keys()
        self.editor_restore_mouse_capture = self.mouse_captured
        self._set_status("Opening box editor...")
        try:
            tempdir = tempfile.TemporaryDirectory(prefix="neko-mouse-world-")
            edit_path = Path(tempdir.name) / "edit.box"
            copy_box_for_editing(self.paths.boxes_dir, digest, edit_path)
            process = subprocess.Popen([sys.executable, "-m", "box_editor_view", str(edit_path)])
        except (OSError, BoxFormatError) as exc:
            self._set_status(f"Cannot open box editor: {exc}")
            self.set_mouse_capture(self.editor_restore_mouse_capture)
            return

        self.editor_process = process
        self.editor_tempdir = tempdir
        self.editor_edit_path = edit_path
        self.editor_cell = cell
        self.editor_original_digest = digest
        self._open_editor_wait_modal()
        self._focus_editor_if_waiting()
        self.taskMgr.add(self._poll_box_editor_process, "neko-mouse-world-editor-wait")

    def _open_editor_wait_modal(self) -> None:
        self.ui_open = True
        self.modal_mode = "editor_wait"
        self.set_mouse_capture(False)
        self.crosshair.hide()
        self.editor_wait_panel = DirectFrame(
            frameColor=(0.04, 0.045, 0.052, 0.96),
            frameSize=(-0.78, 0.78, -0.22, 0.22),
            pos=(0, 0, 0.08),
        )
        DirectLabel(
            parent=self.editor_wait_panel,
            text="waiting for editor to exit ....",
            text_fg=(1, 1, 1, 1),
            text_scale=0.046,
            frameColor=(0, 0, 0, 0),
            pos=(0, 0, 0.04),
        )
        DirectLabel(
            parent=self.editor_wait_panel,
            text="Finish editing in the box-editor-view window.",
            text_fg=(0.82, 0.86, 0.90, 1),
            text_scale=0.030,
            frameColor=(0, 0, 0, 0),
            pos=(0, 0, -0.08),
        )
        self._set_status("Waiting for box editor")

    def _poll_box_editor_process(self, task):
        process = self.editor_process
        if process is None or process.poll() is None:
            return task.cont
        self._finish_box_editor_process(process.returncode)
        return task.done

    def _finish_box_editor_process(self, return_code: int | None) -> None:
        cell = self.editor_cell
        digest = self.editor_original_digest
        edit_path = self.editor_edit_path
        tempdir = self.editor_tempdir
        self.editor_process = None
        self.editor_tempdir = None
        self.editor_edit_path = None
        self.editor_cell = None
        self.editor_original_digest = None
        if self.editor_wait_panel is not None:
            self.editor_wait_panel.destroy()
        self.editor_wait_panel = None
        self.ui_open = False
        self.modal_mode = None

        try:
            if return_code != 0:
                self._set_status("Box editor closed without changes")
                return
            if cell is None or digest is None or edit_path is None:
                self._set_status("Box editor state was lost")
                return
            try:
                new_digest = store_box_file_by_hash(self.paths.boxes_dir, edit_path)
            except BoxFormatError as exc:
                self._set_status(f"Edited box format error: {exc}")
                return
            self.surface_cache.invalidate(new_digest)
            self.collision_cache.invalidate(new_digest)
            self.selected_hash = new_digest
            if new_digest != digest:
                self._set_world_box(cell, new_digest)
                self._set_status(f"Updated {cell} -> {new_digest[:12]}")
            else:
                self._set_status(f"Selected edited box {new_digest[:12]}")
        finally:
            if tempdir is not None:
                tempdir.cleanup()
            self.set_mouse_capture(self.editor_restore_mouse_capture)

    def _look_at_world_focus(self) -> None:
        if self.ui_open:
            return
        target = self._world_focus_target()
        self._look_at_point(target)
        if self.world_map.boxes:
            self._set_status("Looking at world centroid")
        else:
            self._set_status("Looking at world origin")

    def _world_focus_target(self) -> Point3:
        if not self.world_map.boxes:
            return Point3(0, 0, 0)
        total = Vec3(0, 0, 0)
        for x, y, z in self.world_map.boxes:
            total += Vec3(x + 0.5, y + 0.5, z + 0.5)
        count = len(self.world_map.boxes)
        return Point3(total.x / count, total.y / count, total.z / count)

    def _look_at_point(self, target: Point3) -> None:
        eye = self.player_pos + Vec3(0, 0, EYE_HEIGHT)
        direction = Vec3(target.x - eye.x, target.y - eye.y, target.z - eye.z)
        if direction.lengthSquared() == 0:
            return

        horizontal = math.hypot(direction.x, direction.y)
        if horizontal > 0.0001:
            self.heading = math.degrees(math.atan2(-direction.x, direction.y))
        self.pitch = max(-89.0, min(89.0, math.degrees(math.atan2(direction.z, horizontal))))

    def _toggle_move_mode(self) -> None:
        if self.ui_open:
            return
        self.move_mode = "fly" if self.move_mode == "walk" else "walk"
        self.vertical_velocity = 0.0
        self.grounded = self.move_mode == "walk" and self._support_height_below(self.player_pos, STANDING_TOLERANCE) is not None
        self._set_status(f"Movement: {self.move_mode}")

    def _toggle_view(self) -> None:
        if self.ui_open:
            return
        self.view_mode = "third" if self.view_mode == "first" else "first"
        self._set_status(f"View: {self.view_mode}")

    def _update_hover_outline(self) -> None:
        if self.ui_open:
            self.hovered_cell = None
            self.hover_outline.hide()
            return

        hit = self._pick()
        if hit is None:
            self.hovered_cell = None
            self.hover_outline.hide()
            return

        hit_type, cell, _normal, _point = hit
        if hit_type == "block" and cell is not None:
            self.hovered_cell = cell
            self.hover_outline.setPos(cell[0] + 0.5, cell[1] + 0.5, cell[2] + 0.5)
            self.hover_outline.show()
        else:
            self.hovered_cell = None
            self.hover_outline.hide()

    def _pick(self) -> tuple[str, Cell | None, Vec3, Point3] | None:
        ray = self._mouse_ray()
        if ray is None:
            return None
        origin, direction = ray

        hits: list[tuple[float, str, Cell | None, Vec3, Point3]] = []
        block_hit = self._raycast_blocks(origin, direction)
        if block_hit is not None:
            distance, cell, normal, point = block_hit
            hits.append((distance, "block", cell, normal, point))

        ground_hit = self._raycast_ground(origin, direction)
        if ground_hit is not None:
            distance, point = ground_hit
            hits.append((distance, "ground", None, Vec3(0, 0, 1), point))

        if not hits:
            return None
        _distance, hit_type, cell, normal, point = min(hits, key=lambda item: item[0])
        return hit_type, cell, normal, point

    def _mouse_ray(self) -> tuple[Point3, Vec3] | None:
        if self.mouseWatcherNode is None:
            mouse_x, mouse_y = 0.0, 0.0
        elif self.mouse_captured or not self.mouseWatcherNode.hasMouse():
            mouse_x, mouse_y = 0.0, 0.0
        else:
            mouse = self.mouseWatcherNode.getMouse()
            mouse_x, mouse_y = mouse.x, mouse.y

        near_point = Point3()
        far_point = Point3()
        if not self.camLens.extrude(Point2(mouse_x, mouse_y), near_point, far_point):
            return None
        origin = self.render.getRelativePoint(self.camera, near_point)
        far = self.render.getRelativePoint(self.camera, far_point)
        direction = far - origin
        if direction.lengthSquared() == 0:
            return None
        direction.normalize()
        return origin, direction

    def _raycast_ground(self, origin: Point3, direction: Vec3) -> tuple[float, Point3] | None:
        if abs(direction.z) < 1e-8:
            return None
        distance = -origin.z / direction.z
        if distance < 0 or distance > MAX_INTERACTION_DISTANCE:
            return None
        return distance, Point3(origin + direction * distance)

    def _raycast_blocks(self, origin: Point3, direction: Vec3) -> tuple[float, Cell, Vec3, Point3] | None:
        distance = 0.0
        cell = [
            math.floor(origin.x),
            math.floor(origin.y),
            math.floor(origin.z),
        ]
        steps: list[int] = []
        next_distances: list[float] = []
        delta_distances: list[float] = []
        for axis in range(3):
            component = direction[axis]
            origin_value = origin[axis]
            if component > 0:
                steps.append(1)
                next_boundary = cell[axis] + 1.0
                next_distances.append((next_boundary - origin_value) / component)
                delta_distances.append(1.0 / component)
            elif component < 0:
                steps.append(-1)
                next_boundary = float(cell[axis])
                next_distances.append((next_boundary - origin_value) / component)
                delta_distances.append(-1.0 / component)
            else:
                steps.append(0)
                next_distances.append(math.inf)
                delta_distances.append(math.inf)

        normal = Vec3(0, 0, 0)
        max_steps = int(MAX_INTERACTION_DISTANCE * 3) + 3
        for _ in range(max_steps):
            current = (cell[0], cell[1], cell[2])
            if current in self.world_map.boxes and distance <= MAX_INTERACTION_DISTANCE:
                point = Point3(origin + direction * distance)
                return distance, current, normal, point

            axis = min(range(3), key=lambda index: next_distances[index])
            distance = next_distances[axis]
            if distance > MAX_INTERACTION_DISTANCE:
                return None
            cell[axis] += steps[axis]
            normal = Vec3(0, 0, 0)
            normal[axis] = -steps[axis]
            next_distances[axis] += delta_distances[axis]
        return None

    def _placement_cell(
        self,
        hit_type: str,
        cell: Cell | None,
        normal: Vec3,
        point: Point3,
    ) -> Cell | None:
        if hit_type == "ground":
            return (math.floor(point.x), math.floor(point.y), 0)

        if hit_type != "block" or cell is None:
            return None
        axis = max(range(3), key=lambda index: abs(normal[index]))
        if abs(normal[axis]) < 0.5:
            return None
        offset = [0, 0, 0]
        offset[axis] = 1 if normal[axis] >= 0 else -1
        return (cell[0] + offset[0], cell[1] + offset[1], cell[2] + offset[2])

    def _rebuild_all_cells(self) -> None:
        for mesh in self.chunk_meshes.values():
            self._remove_chunk_mesh(mesh)
        self.chunk_meshes.clear()
        self.chunk_index = self._build_chunk_index()
        self.surface_cache.invalidate()
        for key in sorted(self.chunk_index):
            self._rebuild_chunk(key)

    def _set_world_box(self, cell: Cell, digest: str) -> None:
        existing = cell in self.world_map.boxes
        existing_orientation = self.world_map.get_orientation(cell) if existing else IDENTITY_ORIENTATION
        if self.network_client is not None:
            asset = self.network_client.asset_payload(digest)
            if existing:
                self.network_client.send_set_box(cell, digest, existing_orientation, asset)
                self._set_status(f"Update requested {cell}")
            else:
                self.network_client.send_place(cell, digest, IDENTITY_ORIENTATION, asset)
                self._set_status(f"Place requested {cell}")
            return
        self._apply_world_box(cell, digest, existing_orientation)

    def _apply_world_box(self, cell: Cell, digest: str, orientation: int) -> None:
        old_key = chunk_key_for_cell(cell) if cell in self.world_map.boxes else None
        self.world_map.set_box(cell, digest, orientation)
        key = chunk_key_for_cell(cell)
        self.chunk_index.setdefault(key, set()).add(cell)
        if old_key is not None and old_key != key:
            old_cells = self.chunk_index.get(old_key)
            if old_cells is not None:
                old_cells.discard(cell)
                if not old_cells:
                    self.chunk_index.pop(old_key, None)
        self._rebuild_chunks_for_cell(cell)

    def _remove_world_box(self, cell: Cell) -> None:
        if self.network_client is not None:
            self.network_client.send_delete(cell)
            self._set_status(f"Delete requested {cell}")
            return
        self._apply_remove_world_box(cell, play_sound=True)

    def _apply_remove_world_box(self, cell: Cell, play_sound: bool = False) -> None:
        if not self.world_map.remove_box(cell):
            return
        key = chunk_key_for_cell(cell)
        cells = self.chunk_index.get(key)
        if cells is not None:
            cells.discard(cell)
            if not cells:
                self.chunk_index.pop(key, None)
        self._rebuild_chunks_for_cell(cell)
        if self.hovered_cell == cell:
            self.hovered_cell = None
            self.hover_outline.hide()
        if play_sound:
            self.break_sound.play()
        self._set_status(f"Deleted {cell}")

    def _build_chunk_index(self) -> dict[ChunkKey, set[Cell]]:
        index: dict[ChunkKey, set[Cell]] = {}
        for cell in self.world_map.boxes:
            index.setdefault(chunk_key_for_cell(cell), set()).add(cell)
        return index

    def _rebuild_chunks_for_cell(self, cell: Cell) -> None:
        for key in chunk_keys_for_cell_and_neighbors(cell):
            self._rebuild_chunk(key)

    def _rebuild_chunk(self, key: ChunkKey) -> None:
        old_mesh = self.chunk_meshes.pop(key, None)
        if old_mesh is not None:
            self._remove_chunk_mesh(old_mesh)
        chunk_cells = self.chunk_index.get(key)
        if not chunk_cells:
            return
        try:
            mesh = build_world_chunk_mesh(
                self.world_map.boxes,
                self.world_map.orientations,
                self.surface_cache,
                key,
                chunk_cells,
            )
        except BoxFormatError as exc:
            raise SystemExit(f"Cannot build world chunk {key}: {exc}") from exc
        if mesh.cells == 0:
            return
        if mesh.opaque:
            mesh.opaque.reparentTo(self.blocks_root)
        if mesh.transparent:
            mesh.transparent.reparentTo(self.blocks_root)
        self.chunk_meshes[key] = mesh

    def _remove_chunk_mesh(self, mesh: WorldChunkMesh) -> None:
        if mesh.opaque:
            mesh.opaque.removeNode()
        if mesh.transparent:
            mesh.transparent.removeNode()

    def _update_ground(self, force: bool = False) -> None:
        origin_x = math.floor(self.player_pos.x / GROUND_CHUNK) * GROUND_CHUNK - GROUND_SIZE // 2
        origin_y = math.floor(self.player_pos.y / GROUND_CHUNK) * GROUND_CHUNK - GROUND_SIZE // 2
        origin = (origin_x, origin_y)
        if not force and origin == self.ground_origin:
            return
        if self.ground_node is not None:
            self.ground_node.removeNode()
        self.ground_node = make_checker_ground_patch(origin_x, origin_y, GROUND_SIZE)
        self.ground_node.reparentTo(self.world)
        self.ground_origin = origin

    def _open_help(self) -> None:
        if self.ui_open:
            return
        self.ui_open = True
        self.modal_mode = "help"
        self._clear_movement_keys()
        self.set_mouse_capture(False)
        self.crosshair.hide()

        self.help_panel = DirectFrame(
            frameColor=(0.04, 0.05, 0.06, 0.96),
            frameSize=(-0.78, 0.78, -0.64, 0.64),
            pos=(0, 0, 0),
        )
        DirectLabel(
            parent=self.help_panel,
            text="Neko Mouse World",
            text_fg=(1, 1, 1, 1),
            text_scale=0.060,
            frameColor=(0, 0, 0, 0),
            pos=(0, 0, 0.52),
        )
        DirectLabel(
            parent=self.help_panel,
            text="\n".join(
                [
                    "Click window: capture mouse",
                    "Mouse look: look around",
                    "WASD: move",
                    "F: switch walk / fly",
                    "Walk Space: jump 1.1 units",
                    "Fly Space / Shift: move up / down",
                    "Right click: place selected box",
                    "Left click: delete target box",
                    "Middle click: select target box type",
                    "E: edit target box with box-editor-view",
                    "F2 or Ctrl+S: save",
                    "F5: switch first / third person",
                    "C: look at box centroid / origin if empty",
                    "Esc: release mouse and show exit choices",
                ]
            ),
            text_fg=(0.92, 0.94, 0.96, 1),
            text_align=0,
            text_scale=0.033,
            frameColor=(0, 0, 0, 0),
            pos=(-0.58, 0, 0.36),
        )
        self._panel_button("OK", (0, 0, -0.52), self._close_help)
        self._set_status("Help")

    def _panel_button(self, text: str, pos: tuple[float, float, float], command: Callable[[], None]) -> DirectButton:
        return DirectButton(
            parent=self.help_panel,
            text=text,
            text_scale=0.038,
            frameSize=(-0.16, 0.16, -0.055, 0.055),
            frameColor=(0.24, 0.29, 0.34, 1),
            text_fg=(1, 1, 1, 1),
            pos=pos,
            command=command,
        )

    def _show_disconnect_panel(self, text: str) -> None:
        if self.disconnect_panel is None:
            self.disconnect_panel = DirectFrame(
                frameColor=(0.02, 0.025, 0.03, 0.82),
                frameSize=(-1.34, 1.34, -0.18, 0.18),
                pos=(0, 0, 0.18),
            )
            DirectLabel(
                parent=self.disconnect_panel,
                text="Disconnected",
                text_fg=(1.0, 0.86, 0.26, 1),
                text_scale=0.060,
                frameColor=(0, 0, 0, 0),
                pos=(0, 0, 0.055),
            )
            self.disconnect_message = DirectLabel(
                parent=self.disconnect_panel,
                text=text,
                text_fg=(0.94, 0.96, 0.98, 1),
                text_scale=0.034,
                frameColor=(0, 0, 0, 0),
                pos=(0, 0, -0.055),
            )
        else:
            if self.disconnect_message is not None:
                self.disconnect_message["text"] = text
            self.disconnect_panel.show()

    def _hide_disconnect_panel(self) -> None:
        if self.disconnect_panel is not None:
            self.disconnect_panel.hide()

    def _focus_editor_if_waiting(self) -> None:
        if self.modal_mode != "editor_wait" or self.editor_process is None:
            return
        focus_process_window(self.editor_process.pid)

    def _open_connect_dialog(self) -> None:
        self.ui_open = True
        self.modal_mode = "connect"
        self._clear_movement_keys()
        self.set_mouse_capture(False)
        self.crosshair.hide()
        self._hide_disconnect_panel()

        self.connect_panel = DirectFrame(
            frameColor=(0.05, 0.06, 0.07, 0.97),
            frameSize=(-0.72, 0.72, -0.42, 0.42),
            pos=(0, 0, 0),
        )
        DirectLabel(
            parent=self.connect_panel,
            text="Connect To Server",
            text_fg=(1, 1, 1, 1),
            text_scale=0.056,
            frameColor=(0, 0, 0, 0),
            pos=(0, 0, 0.30),
        )
        DirectLabel(
            parent=self.connect_panel,
            text="Host",
            text_fg=(0.92, 0.94, 0.96, 1),
            text_align=1,
            text_scale=0.036,
            frameColor=(0, 0, 0, 0),
            pos=(-0.42, 0, 0.13),
        )
        self.connect_host_entry = DirectEntry(
            parent=self.connect_panel,
            initialText=self.default_connect_host,
            text_scale=0.034,
            frameColor=(0.16, 0.18, 0.21, 1),
            text_fg=(1, 1, 1, 1),
            frameSize=(-0.02, 0.46, -0.052, 0.052),
            pos=(-0.20, 0, 0.13),
            numLines=1,
            focus=1,
            command=lambda _text: self._submit_connect_dialog(),
        )
        DirectLabel(
            parent=self.connect_panel,
            text="Port",
            text_fg=(0.92, 0.94, 0.96, 1),
            text_align=1,
            text_scale=0.036,
            frameColor=(0, 0, 0, 0),
            pos=(-0.42, 0, -0.02),
        )
        self.connect_port_entry = DirectEntry(
            parent=self.connect_panel,
            initialText=str(self.default_connect_port),
            text_scale=0.034,
            frameColor=(0.16, 0.18, 0.21, 1),
            text_fg=(1, 1, 1, 1),
            frameSize=(-0.02, 0.46, -0.052, 0.052),
            pos=(-0.20, 0, -0.02),
            numLines=1,
            command=lambda _text: self._submit_connect_dialog(),
        )
        self.connect_error_label = DirectLabel(
            parent=self.connect_panel,
            text="",
            text_fg=(1.0, 0.62, 0.46, 1),
            text_scale=0.030,
            frameColor=(0, 0, 0, 0),
            pos=(0, 0, -0.14),
        )
        DirectButton(
            parent=self.connect_panel,
            text="OK",
            text_scale=0.036,
            frameSize=(-0.16, 0.16, -0.055, 0.055),
            frameColor=(0.24, 0.29, 0.34, 1),
            text_fg=(1, 1, 1, 1),
            pos=(-0.18, 0, -0.28),
            command=self._submit_connect_dialog,
        )
        DirectButton(
            parent=self.connect_panel,
            text="Quit",
            text_scale=0.036,
            frameSize=(-0.16, 0.16, -0.055, 0.055),
            frameColor=(0.20, 0.22, 0.25, 1),
            text_fg=(1, 1, 1, 1),
            pos=(0.18, 0, -0.28),
            command=self._quit_now,
        )
        self._set_status("Choose server")

    def _submit_connect_dialog(self) -> None:
        if self.modal_mode != "connect" or self.connect_host_entry is None or self.connect_port_entry is None:
            return
        host = self.connect_host_entry.get().strip()
        port_text = self.connect_port_entry.get().strip()
        if not host:
            self._set_connect_error("Host is required")
            return
        try:
            port = int(port_text)
        except ValueError:
            self._set_connect_error("Port must be a number")
            return
        if port <= 0 or port > 65535:
            self._set_connect_error("Port must be 1..65535")
            return

        if self.connect_panel is not None:
            self.connect_panel.destroy()
        self.connect_panel = None
        self.connect_host_entry = None
        self.connect_port_entry = None
        self.connect_error_label = None
        self.connect_required = False
        self.ui_open = False
        self.modal_mode = None
        self.crosshair.show()
        self.network_client = NetworkWorldClient(host, port)
        self.paths = self.network_client.paths
        self.surface_cache = BoxSurfaceCache(self.paths.boxes_dir)
        self.collision_cache = CollisionShapeCache(self.paths.boxes_dir)
        self.world_map = self.network_client.world_map
        self._rebuild_all_cells()
        self.saved_snapshot = self._current_world_snapshot()
        self.set_mouse_capture(True)
        self._show_disconnect_panel("Connecting to server...")
        self._set_status(f"Connecting to {host}:{port}")

    def _set_connect_error(self, text: str) -> None:
        if self.connect_error_label is not None:
            self.connect_error_label["text"] = text
        self._set_status(text)

    def _close_help(self) -> None:
        if self.help_panel:
            self.help_panel.destroy()
        self.help_panel = None
        self.ui_open = False
        self.modal_mode = None
        self.crosshair.show()
        self.set_mouse_capture(True)
        self._set_status("Ready")

    def _release_mouse_capture(self) -> None:
        if self.modal_mode == "editor_wait":
            self._focus_editor_if_waiting()
            return
        if self.modal_mode == "quit":
            self._close_quit_confirm()
            return
        if self.modal_mode == "help":
            self._close_help()
            return
        if self.modal_mode == "connect":
            self._request_quit()
            return
        self.set_mouse_capture(False)
        self._open_quit_confirm()

    def set_mouse_capture(self, captured: bool) -> None:
        self.mouse_captured = captured
        if captured:
            self.ime_disabled = disable_ime_for_window(self.win) or self.ime_disabled
        props = WindowProperties()
        props.setCursorHidden(captured)
        if hasattr(self.win, "requestProperties"):
            self.win.requestProperties(props)
        if captured:
            self.window_maximized = maximize_window(self.win) or self.window_maximized
            self._center_pointer()
            self.crosshair.show()
        else:
            self.crosshair.hide()

    def _center_pointer(self) -> None:
        if self.win and hasattr(self.win, "movePointer"):
            self.win.movePointer(0, self.win.getXSize() // 2, self.win.getYSize() // 2)

    def _handle_window_event(self, window) -> None:
        if window is None:
            return
        if hasattr(window, "isClosed") and window.isClosed():
            if self.modal_mode == "editor_wait":
                self._focus_editor_if_waiting()
                return
            self._request_quit()
            return
        if window == self.win:
            self._sync_camera_aspect()
            self.ime_disabled = disable_ime_for_window(self.win) or self.ime_disabled

    def _sync_camera_aspect(self) -> None:
        if not self.win or not hasattr(self.win, "getXSize") or not hasattr(self.win, "getYSize"):
            return
        width = max(1, self.win.getXSize())
        height = max(1, self.win.getYSize())
        self.camLens.setAspectRatio(width / height)

    def userExit(self) -> None:
        self._request_quit()

    def _request_quit(self) -> None:
        if self.modal_mode == "editor_wait":
            self._focus_editor_if_waiting()
            return
        if self.modal_mode == "quit":
            return
        if self.modal_mode == "help":
            self._close_help()
        if self.modal_mode == "connect":
            self.ui_open = False
            self.modal_mode = None
            if self.connect_panel:
                self.connect_panel.destroy()
            self.connect_panel = None
            self.connect_host_entry = None
            self.connect_port_entry = None
            self.connect_error_label = None
        self._open_quit_confirm()

    def _open_quit_confirm(self) -> None:
        self.ui_open = True
        self.modal_mode = "quit"
        self._clear_movement_keys()
        self.quit_restore_mouse_capture = self.mouse_captured
        self.set_mouse_capture(False)
        self.crosshair.hide()
        self.active_quit_choice = "cancel"
        self.quit_button_frames = {}
        self.quit_buttons = {}

        self.quit_panel = DirectFrame(
            frameColor=(0.05, 0.06, 0.07, 0.97),
            frameSize=(-0.86, 0.86, -0.34, 0.34),
            pos=(0, 0, 0),
        )
        DirectLabel(
            parent=self.quit_panel,
            text="Exit World",
            text_fg=(1, 1, 1, 1),
            text_scale=0.058,
            frameColor=(0, 0, 0, 0),
            pos=(0, 0, 0.22),
        )
        DirectLabel(
            parent=self.quit_panel,
            text=self._quit_confirm_message(),
            text_fg=(0.92, 0.94, 0.96, 1),
            text_scale=0.036,
            frameColor=(0, 0, 0, 0),
            pos=(0, 0, 0.08),
        )

        self._quit_button("save", "Save and Quit", (-0.52, 0, -0.17), self._save_and_quit)
        self._quit_button("discard", "Quit without Saving", (0, 0, -0.17), self._quit_now)
        self._quit_button("cancel", "Cancel", (0.52, 0, -0.17), self._close_quit_confirm)
        self._sync_quit_button_highlight()
        self._set_status("Exit world")

    def _quit_confirm_message(self) -> str:
        if self._has_unsaved_changes():
            return "You have unsaved changes. Do you want to quit?"
        return "Do you want to leave the world?"

    def _quit_button(
        self,
        choice: str,
        text: str,
        pos: tuple[float, float, float],
        command: Callable[[], None],
    ) -> DirectButton:
        frame = DirectFrame(
            parent=self.quit_panel,
            frameColor=(0.18, 0.21, 0.25, 1),
            frameSize=(-0.205, 0.205, -0.075, 0.075),
            pos=pos,
        )
        button = DirectButton(
            parent=frame,
            text=text,
            text_scale=0.034,
            frameSize=(-0.19, 0.19, -0.058, 0.058),
            frameColor=(0.24, 0.29, 0.34, 1),
            text_fg=(1, 1, 1, 1),
            pos=(0, 0, 0),
            command=command,
        )
        self.quit_button_frames[choice] = frame
        self.quit_buttons[choice] = button
        return button

    def _focus_next_quit_choice(self, direction: int) -> None:
        if self.modal_mode != "quit":
            return
        order = ["save", "discard", "cancel"]
        index = order.index(self.active_quit_choice) if self.active_quit_choice in order else 2
        self.active_quit_choice = order[(index + direction) % len(order)]
        self._sync_quit_button_highlight()

    def _sync_quit_button_highlight(self) -> None:
        for choice, frame in self.quit_button_frames.items():
            frame["frameColor"] = (1.0, 0.88, 0.18, 1.0) if choice == self.active_quit_choice else (0.18, 0.21, 0.25, 1)

    def _submit_modal(self) -> None:
        if self.modal_mode == "help":
            self._close_help()
        elif self.modal_mode == "quit":
            self._activate_quit_choice()
        elif self.modal_mode == "connect":
            self._submit_connect_dialog()

    def _activate_quit_choice(self) -> None:
        if self.active_quit_choice == "save":
            self._save_and_quit()
        elif self.active_quit_choice == "discard":
            self._quit_now()
        else:
            self._close_quit_confirm()

    def _save_and_quit(self) -> None:
        if self.network_client is not None:
            self.saved_snapshot = self._current_world_snapshot()
            self._quit_now()
            return
        save_world(self.paths.info_file, self.world_map)
        self.saved_snapshot = self._current_world_snapshot()
        self._quit_now()

    def _close_quit_confirm(self) -> None:
        if self.quit_panel:
            self.quit_panel.destroy()
        self.quit_panel = None
        self.quit_button_frames = {}
        self.quit_buttons = {}
        self.active_quit_choice = "cancel"
        if self.connect_required and self.network_client is None:
            self.ui_open = False
            self.modal_mode = None
            self._open_connect_dialog()
            return
        self.ui_open = False
        self.modal_mode = None
        self.crosshair.show()
        self.set_mouse_capture(self.quit_restore_mouse_capture)
        self._set_status("Ready")

    def _quit_now(self) -> None:
        if self.modal_mode == "editor_wait":
            self._focus_editor_if_waiting()
            return
        if self.network_client is not None:
            self.network_client.close()
        raise SystemExit

    def _clear_movement_keys(self) -> None:
        for key in self.key_state:
            self.key_state[key] = False

    def _block_intersects_player(self, cell: Cell) -> bool:
        digest = self.world_map.get_box(cell)
        temporary = False
        if digest is None:
            digest = self.selected_hash
            self.world_map.set_box(cell, digest, IDENTITY_ORIENTATION)
            temporary = True
        try:
            return self._blocking_top_for_player(self.player_pos) is not None
        finally:
            if temporary:
                self.world_map.remove_box(cell)

    def _current_world_snapshot(self) -> str:
        return repr(
            tuple(
                (cell, digest, self.world_map.get_orientation(cell))
                for cell, digest in sorted(self.world_map.boxes.items())
            )
        )

    def _has_unsaved_changes(self) -> bool:
        return self.saved_snapshot != self._current_world_snapshot()

    def save_current(self, quiet: bool = False) -> None:
        if self.ui_open:
            return
        if self.network_client is not None:
            self.saved_snapshot = self._current_world_snapshot()
            if not quiet:
                self._set_status("Server saves world changes automatically")
            return
        save_world(self.paths.info_file, self.world_map)
        self.saved_snapshot = self._current_world_snapshot()
        if not quiet:
            self._set_status(f"Saved {self.paths.info_file}")


def make_checker_ground_patch(origin_x: int, origin_y: int, size: int) -> NodePath:
    name = f"checker-ground-{origin_x}-{origin_y}"
    vertex_data = GeomVertexData(name, GeomVertexFormat.getV3c4(), Geom.UHStatic)
    vertex_writer = GeomVertexWriter(vertex_data, "vertex")
    color_writer = GeomVertexWriter(vertex_data, "color")
    triangles = GeomTriangles(Geom.UHStatic)

    vertex_index = 0
    for x in range(origin_x, origin_x + size):
        for y in range(origin_y, origin_y + size):
            color = (0.92, 0.92, 0.92, 1.0) if (x + y) % 2 == 0 else (0.06, 0.06, 0.06, 1.0)
            for vertex in ((x, y, -0.006), (x + 1, y, -0.006), (x + 1, y + 1, -0.006), (x, y + 1, -0.006)):
                vertex_writer.addData3f(*vertex)
                color_writer.addData4f(*color)
            triangles.addVertices(vertex_index, vertex_index + 1, vertex_index + 2)
            triangles.addVertices(vertex_index, vertex_index + 2, vertex_index + 3)
            vertex_index += 4

    geom = Geom(vertex_data)
    geom.addPrimitive(triangles)
    node = GeomNode(name)
    node.addGeom(geom)
    path = NodePath(node)
    path.setLightOff()
    return path


def focus_process_window(pid: int) -> None:
    if os.name != "nt":
        return
    try:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        hwnds: list[int] = []

        enum_proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def enum_proc(hwnd, _lparam):
            process_id = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
            if process_id.value == pid and user32.IsWindowVisible(hwnd):
                hwnds.append(int(hwnd))
                return False
            return True

        user32.EnumWindows(enum_proc_type(enum_proc), 0)
        if not hwnds:
            return
        hwnd = hwnds[0]
        user32.ShowWindow(hwnd, 9)
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002)
        user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0001 | 0x0002)
        user32.SetForegroundWindow(hwnd)
    except Exception:
        return
