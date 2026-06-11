from __future__ import annotations

from collections import deque
import ctypes
from dataclasses import dataclass
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
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
    PointLight,
    TextNode,
    TransparencyAttrib,
    Vec3,
    WindowProperties,
    loadPrcFileData,
)

from box_editor_view.audio import ensure_sound_files
from box_editor_view.box_file import BoxFormatError, RGBA
from box_editor_view.geometry import make_cube_outline, make_cuboid
from box_editor_view.gpu import GpuProfile, detect_gpu_profile
from box_editor_view.platform_window import disable_ime_for_window, maximize_window

from .box_assets import copy_box_for_editing, ensure_default_box, store_box_file_by_hash
from .box_mesh import (
    BoxSurfaceCache,
    ChunkKey,
    WorldChunkMesh,
    build_box_preview_mesh,
    build_world_chunk_mesh,
    chunk_key_for_cell,
    chunk_keys_for_cell_and_neighbors,
)
from .client_net import DEFAULT_PERMISSIONS as CLIENT_DEFAULT_PERMISSIONS, NetworkWorldClient, RemotePlayer
from .collision import CollisionShapeCache
from .net_protocol import SERVER_LOG_PERMISSION_DENIED_LINE, list_to_cell
from .orientation import IDENTITY_ORIENTATION, nearest_axis, rotate_normal, rotate_point, turn_orientation_around_axis
from .world_file import Cell, LoadedWorld, WorldFormatError, WorldMap, save_world


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
POINT_LIGHT_UPDATE_INTERVAL = 0.20
POINT_LIGHT_ACTIVE_UPDATE_INTERVAL = 0.60
POINT_LIGHT_MAX_DISTANCE = 34.0
POINT_LIGHT_SPATIAL_CHUNK = 8
POINT_LIGHT_PREFILTER_LIMIT = 64
POINT_LIGHT_OCCLUSION_TEST_LIMIT = 24
POINT_LIGHT_OCCLUSION_CACHE_SECONDS = 0.75
POINT_LIGHT_OCCLUSION_CACHE_LIMIT = 512
POINT_LIGHT_BLOCKING_FACE_CACHE_LIMIT = 8192
MAX_ACTIVE_POINT_LIGHTS = 8
POINT_LIGHT_OCCLUSION_PRUNE_INTERVAL = 0.25
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
GROUND_SIZE = 160
GROUND_CHUNK = 16
GROUND_CHUNKS_PER_FRAME = 2
HUD_UPDATE_INTERVAL = 0.10
SOUND_VOLUME = 0.5
CLOSE_REQUEST_EVENT = "neko-mouse-world-close-request"
WORLD_LOAD_TASK_NAME = "neko-mouse-world-load"
WORLD_LOAD_FRAME_BUDGET = 0.012
NETWORK_POLL_MESSAGE_BUDGET = 64
ACTIVE_INPUT_NETWORK_POLL_MESSAGE_BUDGET = 0
NETWORK_APPLY_FRAME_BUDGET = 0.003
CHUNK_REBUILD_FRAME_BUDGET = 0.006
PLAYER_INPUT_IDLE_SYNC_DELAY = 0.25
REMOTE_PLAYER_PREDICTION_SECONDS = 1.0
REMOTE_HELD_TEMPLATE_CACHE_LIMIT = 96
REMOTE_HELD_MODEL_BUILDS_PER_FRAME = 1
STARTUP_MAXIMIZE_RETRY_SECONDS = 1.0
STARTUP_MAXIMIZE_RETRY_INTERVAL = 0.10
STARTUP_MAXIMIZE_TASK_NAME = "neko-mouse-world-startup-maximize"
CONSOLE_LOG_LIMIT = 300
CONSOLE_VISIBLE_LINES = 19
CONSOLE_SCROLL_X = 0.99
CONSOLE_SCROLL_TRACK_TOP = 0.50
CONSOLE_SCROLL_TRACK_BOTTOM = -0.46
CONSOLE_SCROLL_THUMB_MIN_HEIGHT = 0.08
CONSOLE_COMMAND_MAX_CHARS = 4096
CONSOLE_INPUT_WRAP_WIDTH = 48.0
CONSOLE_INPUT_WRAP_CHARS = 78
CONSOLE_INPUT_MIN_LINES = 1
CONSOLE_INPUT_MAX_LINES = 3
CONSOLE_INPUT_BASE_HEIGHT = 0.104
CONSOLE_INPUT_LINE_STEP = 0.052
CONSOLE_INPUT_BOTTOM = -0.700
CONSOLE_INPUT_CENTER_Z = -0.620
CONSOLE_INPUT_SCROLL_X = 0.55
CONSOLE_INPUT_SCROLL_THUMB_MIN_HEIGHT = 0.026


@dataclass
class _WorldLoadJob:
    surface_digests: deque[str]
    collision_keys: deque[tuple[str, int]]
    chunk_keys: deque[ChunkKey]
    total: int
    complete_status: str
    lift_player: bool
    show_progress: bool
    waiting_for_assets: bool
    done: int = 0

    @property
    def pending(self) -> int:
        return len(self.surface_digests) + len(self.collision_keys) + len(self.chunk_keys)


@dataclass(frozen=True)
class _BoxLightCandidate:
    position: tuple[float, float, float]
    color: RGBA


@dataclass
class _RemotePlayerRender:
    root: NodePath
    limbs: dict[str, NodePath]
    name_label: NodePath
    held_anchor: NodePath
    held_model: NodePath | None
    held_key: tuple[str, int] | None
    phase: float
    last_update: float


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
        default_user_id: str = "",
    ) -> None:
        super().__init__()
        self.disableMouse()
        self.camLens.setNear(CAMERA_NEAR)
        self.camLens.setFov(CAMERA_FOV)
        self._sync_camera_aspect()

        self.paths = loaded_world.paths
        self.network_client = network_client
        self.permissions = dict(network_client.permissions) if network_client is not None else dict(CLIENT_DEFAULT_PERMISSIONS)
        self.display_user_id = bool(network_client.client_config.get("display_user_id", True)) if network_client is not None else True
        self.connect_required = show_connect_dialog and network_client is None
        self.default_connect_host = default_connect_host
        self.default_connect_port = default_connect_port
        self.default_user_id = default_user_id
        self.world_map: WorldMap = loaded_world.world_map
        self.server_initial_load_progress_used = network_client is None and not show_connect_dialog
        self.default_hash = network_client.default_hash if network_client and network_client.default_hash else ensure_default_box(self.paths.boxes_dir)
        self.selected_hash = self.default_hash
        self.selected_orientation = IDENTITY_ORIENTATION
        self.saved_snapshot = "" if network_client is not None else self._current_world_snapshot()
        self.gpu_profile: GpuProfile = detect_gpu_profile(self.win.getGsg() if self.win else None)
        self._startup_window_maximize_done = True
        self._startup_window_maximize_started = 0.0
        self._startup_window_next_maximize_attempt = 0.0
        self.ime_disabled = disable_ime_for_window(self.win)

        self.world = self.render.attachNewNode("world")
        self.blocks_root = self.world.attachNewNode("world-boxes")
        self.ground_root = self.world.attachNewNode("checker-ground")
        self.ground_chunks: dict[tuple[int, int], NodePath] = {}
        self.pending_ground_chunks: deque[tuple[int, int]] = deque()
        self.pending_ground_chunk_set: set[tuple[int, int]] = set()
        self.ground_center_chunk: tuple[int, int] | None = None
        self.ground_chunk_template = make_checker_ground_patch(0, 0, GROUND_CHUNK)
        self.surface_cache = BoxSurfaceCache(self.paths.boxes_dir)
        self.collision_cache = CollisionShapeCache(self.paths.boxes_dir)
        self.chunk_meshes: dict[ChunkKey, WorldChunkMesh] = {}
        self.chunk_index: dict[ChunkKey, set[Cell]] = {}
        self.digest_index: dict[str, set[Cell]] = {}
        self.box_light_candidates: dict[Cell, tuple[_BoxLightCandidate, ...]] = {}
        self.box_light_spatial_index: dict[tuple[int, int, int], set[tuple[Cell, int]]] = {}
        self.box_light_occlusion_cache: dict[tuple[Cell, int], tuple[tuple[int, int, int], bool, float]] = {}
        self.box_light_blocking_face_cache: dict[Cell, frozenset[Cell]] = {}
        self.active_box_lights: dict[tuple[Cell, int], NodePath] = {}
        self.next_point_light_update = 0.0
        self.next_point_light_prune = 0.0
        self.hover_outline = make_cube_outline()
        self.hover_outline.reparentTo(self.world)
        self.hover_outline.hide()
        self.hovered_cell: Cell | None = None
        self.last_deleted_box: tuple[Cell, str, int] | None = None

        self.player_pos = Vec3(0.5, -4.0, 0.0)
        self.player_velocity = Vec3(0, 0, 0)
        self.heading = 0.0
        self.pitch = -10.0
        self.network_spawn_pos: tuple[float, float, float] | None = None
        self.network_client_loaded_sent = network_client is None
        self.held_item_root: NodePath | None = None
        self.held_item_model: NodePath | None = None
        self.held_item_key: tuple[str, int] | None = None
        self.held_item_visible = False
        self.third_person_held_anchor: NodePath | None = None
        self.third_person_held_model: NodePath | None = None
        self.third_person_held_key: tuple[str, int] | None = None
        self.view_mode = "first"
        self.move_mode = "walk"
        self.vertical_velocity = 0.0
        self.grounded = True
        self.mouse_captured = False
        self.ui_open = False
        self.modal_mode: str | None = None
        self.help_panel: DirectFrame | None = None
        self.quit_panel: DirectFrame | None = None
        self.focus_pause_panel: DirectFrame | None = None
        self.disconnect_panel: DirectFrame | None = None
        self.disconnect_message: DirectLabel | None = None
        self.kicked_panel: DirectFrame | None = None
        self.kicked_reason = ""
        self.connect_refused_panel: DirectFrame | None = None
        self.version_mismatch_panel: DirectFrame | None = None
        self.console_panel: DirectFrame | None = None
        self.console_log_labels: list[DirectLabel] = []
        self.console_entry: DirectEntry | None = None
        self.console_send_button: DirectButton | None = None
        self.console_input_scrollbar: DirectFrame | None = None
        self.console_input_scroll_thumb: DirectFrame | None = None
        self.console_input_scroll_buttons: list[DirectButton] = []
        self.console_entry_event_names: list[str] = []
        self.console_entry_wrapped_line_count = 1
        self.console_scrollbar: DirectFrame | None = None
        self.console_scroll_thumb: DirectFrame | None = None
        self.console_logs: list[dict[str, str]] = []
        self.console_scroll_offset = 0
        self.loading_panel: DirectFrame | None = None
        self.loading_bar_fill: DirectFrame | None = None
        self.loading_message: DirectLabel | None = None
        self.loading_percent: DirectLabel | None = None
        self.world_load_job: _WorldLoadJob | None = None
        self.deferred_world_messages: list[dict] = []
        self.pending_world_messages: deque[dict] = deque()
        self.waiting_asset_world_messages: dict[str, list[dict]] = {}
        self.pending_asset_digests: set[str] = set()
        self.dirty_chunk_keys: set[ChunkKey] = set()
        self.dirty_chunk_queue: deque[ChunkKey] = deque()
        self.total_chunk_quads = 0
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
        self.removed_missing_refs = loaded_world.removed_missing_refs
        self.remote_player_nodes: dict[int, _RemotePlayerRender] = {}
        self.remote_held_model_templates: dict[tuple[str, int], NodePath] = {}
        self.remote_held_pending_builds: deque[tuple[str, int]] = deque()
        self.remote_held_pending_build_set: set[tuple[str, int]] = set()
        self.last_sent_held_item_key: tuple[str, int, bool] | None = None
        self.last_udp_player_state = 0.0
        self.last_player_input_time = 0.0
        self.next_hud_update = 0.0
        self.last_detail_text = ""
        self.last_status_text = ""

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
        self._setup_held_item()
        self._setup_hud()
        self._setup_audio()
        self._bind_events()
        self._update_ground(force=True)
        if self.network_client is None and not self.connect_required:
            self._rebuild_all_cells()
            self._lift_player_out_of_blocks()
        if self.network_client is None and not self.connect_required:
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
        elif self.network_client is None and not self.connect_required:
            self._refresh_held_item(force=True)
            self._begin_startup_maximize_retry()

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
        self.third_person_held_anchor = self.player_model.attachNewNode("third-person-held-anchor")
        self.third_person_held_anchor.setPos(0.48, -0.14, 0.78)
        self.third_person_held_anchor.setHpr(18, -12, -8)
        self.third_person_held_anchor.hide()
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

    def _setup_held_item(self) -> None:
        self.held_item_root = self.camera.attachNewNode("held-item")
        self.held_item_root.setPos(0.76, 1.18, -0.58)
        self.held_item_root.setHpr(-18, -10, 6)
        self.held_item_root.setBin("fixed", 10)
        self.held_item_root.setDepthTest(True)
        self.held_item_root.setDepthWrite(True)
        self.held_item_root.hide()
        hand = make_cuboid("held-right-hand", (0.22, 0.30, 0.20))
        hand.reparentTo(self.held_item_root)
        hand.setPos(0.10, -0.05, -0.09)
        hand.setHpr(-12, 18, -8)
        hand.setColor(0.86, 0.70, 0.52, 1)
        sleeve = make_cuboid("held-sleeve", (0.24, 0.42, 0.22))
        sleeve.reparentTo(self.held_item_root)
        sleeve.setPos(0.19, -0.26, -0.12)
        sleeve.setHpr(-12, 18, -8)
        sleeve.setColor(0.10, 0.34, 0.88, 1)

    def _refresh_held_item(self, force: bool = False) -> None:
        self._refresh_third_person_held_item(force=force)
        if self.held_item_root is None:
            return
        visible = self._local_held_item_visible()
        key = (self.selected_hash, self.selected_orientation) if visible else None
        if not force and visible == self.held_item_visible and key == self.held_item_key:
            return
        self.held_item_visible = bool(visible)
        self.held_item_key = key
        self.last_sent_held_item_key = None
        if self.held_item_model is not None:
            self.held_item_model.removeNode()
            self.held_item_model = None
        if not visible or key is None:
            self.held_item_root.hide()
            return
        try:
            surface = self.surface_cache.get(key[0])
            model = build_box_preview_mesh(surface, key[1], "held-box-preview")
        except (BoxFormatError, ValueError):
            model = None
        if model is None:
            self.held_item_root.hide()
            return
        model.reparentTo(self.held_item_root)
        model.setPos(-0.02, 0.08, 0.12)
        model.setScale(0.42)
        model.setHpr(28, -24, 12)
        model.setDepthTest(True)
        self.held_item_model = model
        self.held_item_root.show()

    def _refresh_third_person_held_item(self, force: bool = False) -> None:
        if self.third_person_held_anchor is None:
            return
        digest, orientation, held = self._current_held_item_state()
        visible = bool(held and self.view_mode == "third")
        key = (digest, orientation) if visible else None
        if not force and key == self.third_person_held_key:
            return
        if self.third_person_held_model is not None:
            self.third_person_held_model.removeNode()
            self.third_person_held_model = None
        self.third_person_held_key = key
        if key is None:
            self.third_person_held_anchor.hide()
            return
        try:
            surface = self.surface_cache.get(key[0])
            model = build_box_preview_mesh(surface, key[1], "third-person-held-box")
        except (BoxFormatError, ValueError):
            model = None
        if model is None:
            self.third_person_held_anchor.hide()
            return
        model.reparentTo(self.third_person_held_anchor)
        model.setPos(0, 0, 0)
        model.setScale(0.30)
        model.setHpr(28, -24, 12)
        model.setDepthTest(True)
        model.show()
        self.third_person_held_model = model
        self.third_person_held_anchor.show()

    def _current_held_item_state(self) -> tuple[str, int, bool]:
        visible = bool(
            self.permissions.get("allow_set", True)
            and self.world_load_job is None
            and self.modal_mode not in {"kicked", "connect_refused", "version_mismatch"}
            and self.selected_hash
        )
        digest = str(self.selected_hash or "") if visible else ""
        return digest, int(self.selected_orientation), visible

    def _local_held_item_visible(self) -> bool:
        return bool(
            self.permissions.get("allow_set", True)
            and self.view_mode == "first"
            and self.world_load_job is None
            and self.modal_mode not in {"kicked", "connect_refused", "version_mismatch"}
            and self.selected_hash
        )

    def _make_remote_player_render(self, player_id: int) -> _RemotePlayerRender:
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
        ):
            part = make_cuboid(name, size)
            part.reparentTo(root)
            part.setPos(*pos)
            part.setColor(*color)
        limbs: dict[str, NodePath] = {}
        limb_specs = (
            ("left-arm", (0.18, 0.22, 0.72), (-0.37, 0, 1.38), (0, 0, -0.30), accent),
            ("right-arm", (0.18, 0.22, 0.72), (0.37, 0, 1.38), (0, 0, -0.30), accent),
            ("left-leg", (0.20, 0.24, 0.78), (-0.13, 0, 0.78), (0, 0, -0.39), (0.15, 0.18, 0.24, 1)),
            ("right-leg", (0.20, 0.24, 0.78), (0.13, 0, 0.78), (0, 0, -0.39), (0.15, 0.18, 0.24, 1)),
        )
        for name, size, pivot_pos, part_pos, color in limb_specs:
            pivot = root.attachNewNode(f"{name}-pivot")
            pivot.setPos(*pivot_pos)
            part = make_cuboid(name, size)
            part.reparentTo(pivot)
            part.setPos(*part_pos)
            part.setColor(*color)
            limbs[name] = pivot
        held_anchor = limbs["right-arm"].attachNewNode("held-item-anchor")
        held_anchor.setPos(0.19, -0.13, -0.71)
        held_anchor.setHpr(18, -12, -8)
        name_node = TextNode(f"remote-player-{player_id}-user-id")
        name_node.setText("")
        name_node.setAlign(TextNode.ACenter)
        name_node.setTextColor(1.0, 1.0, 1.0, 1.0)
        name_label = root.attachNewNode(name_node)
        name_label.setPos(0, 0, PLAYER_HEIGHT + 0.42)
        name_label.setScale(0.22)
        name_label.setBillboardPointEye()
        name_label.setLightOff()
        name_label.setDepthWrite(False)
        name_label.setTransparency(TransparencyAttrib.MAlpha)
        root.setTransparency(TransparencyAttrib.MAlpha)
        return _RemotePlayerRender(
            root=root,
            limbs=limbs,
            name_label=name_label,
            held_anchor=held_anchor,
            held_model=None,
            held_key=None,
            phase=0.0,
            last_update=time.monotonic(),
        )

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
        self.accept("z", self._restore_last_deleted_box)
        for event_name in ("`", "~", "tilde"):
            self.accept(event_name, self._open_console)
        self.accept("wheel_up", self._scroll_console_wheel, [-3])
        self.accept("wheel_down", self._scroll_console_wheel, [3])
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
        if self.modal_mode == "focus_pause":
            self.key_state[name] = False
            return
        if self.world_load_job is not None:
            self.key_state[name] = False
            return
        if self.ui_open:
            self.key_state[name] = False
            return
        old_value = self.key_state[name]
        self.key_state[name] = value
        if value or old_value != value:
            self._mark_player_input_active()

    def _space_pressed(self) -> None:
        if self.modal_mode == "focus_pause":
            return
        if self.world_load_job is not None:
            return
        if self.ui_open:
            return
        self.key_state["up"] = True
        self._mark_player_input_active()
        if self.move_mode == "walk" and self.grounded:
            self.vertical_velocity = JUMP_SPEED
            self.grounded = False

    def _update(self, task):
        if self.modal_mode in {"kicked", "connect_refused", "version_mismatch"}:
            return task.cont
        dt = min(globalClock.getDt(), 0.05)
        previous_player_pos = Vec3(self.player_pos)
        self._check_foreground_pause()
        loading = self.world_load_job is not None
        focus_paused = self.modal_mode == "focus_pause"
        controls_enabled = self._player_controls_enabled(loading)
        self._sync_network_permissions_snapshot()
        if self.mouse_captured and controls_enabled:
            self._update_mouse_look()
        if not loading:
            self._update_player(dt, allow_input=controls_enabled)
        self._update_player_velocity(previous_player_pos, dt)
        self._update_camera()
        self._refresh_held_item()
        self._sync_network_held_item()
        self._send_network_player_state()
        if loading or focus_paused:
            self.hovered_cell = None
            self.hover_outline.hide()
        else:
            self._update_hover_outline()
        self._update_hud()

        self._process_network()
        if not loading and self._can_run_background_world_sync():
            self._apply_pending_asset_updates()
            self._apply_pending_world_messages()
            self._process_remote_held_model_builds()
            self._process_dirty_chunks()
        self._sync_remote_players()
        self._update_ground()
        self._update_visible_box_lights()
        return task.cont

    def _process_network(self) -> None:
        if self.network_client is None:
            return
        if self.network_client.connected:
            if self.world_load_job is None or self.world_load_job.show_progress:
                self._hide_disconnect_panel()
        else:
            self._show_disconnect_panel("Disconnected. Reconnecting...")
        poll_budget = NETWORK_POLL_MESSAGE_BUDGET
        controls_enabled = self._player_controls_enabled(self.world_load_job is not None)
        if controls_enabled and not self._player_input_is_idle():
            poll_budget = ACTIVE_INPUT_NETWORK_POLL_MESSAGE_BUDGET
        for message in self.network_client.poll(poll_budget):
            message_type = message.get("type")
            if message_type == "welcome":
                self.pending_world_messages.clear()
                self.waiting_asset_world_messages.clear()
                self.pending_asset_digests.clear()
                self.dirty_chunk_keys.clear()
                self.dirty_chunk_queue.clear()
                self.default_hash = self.network_client.default_hash or self.default_hash
                self.selected_hash = self.default_hash
                self.selected_orientation = IDENTITY_ORIENTATION
                self.network_client_loaded_sent = False
                self.network_spawn_pos = self.network_client.spawn_pos
                self._apply_network_spawn_pos()
                self.permissions = dict(self.network_client.permissions)
                if not self.permissions["allow_fly"] and self.move_mode == "fly":
                    self._force_walk_mode("Flight disabled by server")
                self._apply_client_config(self.network_client.client_config)
                self._refresh_held_item(force=True)
                self.world_map = self.network_client.world_map
                self.saved_snapshot = ""
                self._hide_disconnect_panel()
                if not self.server_initial_load_progress_used:
                    self._start_server_world_load(show_progress=True)
                else:
                    self._start_server_world_load(show_progress=False)
            elif message_type == "asset":
                digest = str(message.get("hash", ""))
                if self.world_load_job is None:
                    self.pending_asset_digests.add(digest)
                    if digest == self.selected_hash:
                        self._refresh_held_item(force=True)
                else:
                    self.surface_cache.invalidate(digest)
                    self.collision_cache.invalidate(digest)
            elif message_type == "box_set":
                if self.world_load_job is not None:
                    self.deferred_world_messages.append(message)
                    continue
                self.pending_world_messages.append(message)
            elif message_type == "box_removed":
                if self.world_load_job is not None:
                    self.deferred_world_messages.append(message)
                    continue
                self.pending_world_messages.append(message)
            elif message_type == "udp_status":
                if self.world_load_job is not None:
                    continue
                if message.get("enabled"):
                    self._set_status("Connected; player positions use UDP")
                else:
                    received = message.get("received", 0)
                    sent = message.get("sent", 0)
                    self._set_status(f"Connected; UDP probe {received}/{sent}, positions use TCP")
            elif message_type == "server_log":
                self._append_console_log(message)
            elif message_type == "kicked":
                self._open_kicked_modal(str(message.get("reason", "")))
                return
            elif message_type == "connect_refused":
                self._open_connect_refused_modal(str(message.get("reason", "")))
                return
            elif message_type == "version_mismatch":
                self._open_version_mismatch_modal(
                    str(message.get("server_version", "")),
                    str(message.get("client_version", "")),
                )
                return
            elif message_type == "teleport":
                self._apply_teleport(message.get("pos"))
            elif message_type == "permissions":
                self._apply_permissions(message.get("permissions"))
            elif message_type == "server_log_permission":
                self._apply_server_log_permission(bool(message.get("allowed")), str(message.get("message", "")))
            elif message_type == "client_config":
                self._apply_client_config(message.get("client_config"))
            elif message_type == "force_move_mode":
                if str(message.get("move_mode", "walk")) == "walk":
                    self._force_walk_mode("Flight disabled by server")
            elif message_type == "disconnect":
                self._cancel_world_load()
                self._show_disconnect_panel("Disconnected. Reconnecting...")

    def _send_network_player_state(self) -> None:
        if self.network_client is None or not self.network_client.connected:
            return
        if self.world_load_job is not None or not self.network_client_loaded_sent:
            return
        now = globalClock.getFrameTime()
        if now - self.last_udp_player_state < 0.05:
            return
        self.last_udp_player_state = now
        held_hash, held_orientation, held_visible = self._current_held_item_state()
        self.network_client.send_player_state(
            (self.player_pos.x, self.player_pos.y, self.player_pos.z),
            (self.player_velocity.x, self.player_velocity.y, self.player_velocity.z),
            self.heading,
            self.pitch,
            self.move_mode,
            held_hash,
            held_orientation,
            held_visible,
        )

    def _sync_network_held_item(self, force: bool = False) -> None:
        if self.network_client is None or not self.network_client.connected:
            return
        if self.world_load_job is not None or not self.network_client_loaded_sent:
            return
        digest, orientation, visible = self._current_held_item_state()
        key = (digest, orientation, visible)
        if not force and key == self.last_sent_held_item_key:
            return
        self.last_sent_held_item_key = key
        self.network_client.send_held_item(digest, orientation, visible, include_asset=visible)

    def _apply_network_spawn_pos(self) -> None:
        if self.network_spawn_pos is None:
            return
        self.player_pos = Vec3(*self.network_spawn_pos)
        self.player_velocity = Vec3(0, 0, 0)
        self.vertical_velocity = 0.0
        self.grounded = True
        self._clear_movement_keys()
        self.last_udp_player_state = 0.0
        self._update_camera()

    def _notify_network_client_loaded(self) -> None:
        if self.network_client is None or not self.network_client.connected or self.network_client_loaded_sent:
            return
        held_hash, held_orientation, held_visible = self._current_held_item_state()
        self.network_client.send_client_loaded(
            (self.player_pos.x, self.player_pos.y, self.player_pos.z),
            (self.player_velocity.x, self.player_velocity.y, self.player_velocity.z),
            self.heading,
            self.pitch,
            self.move_mode,
            held_hash,
            held_orientation,
            held_visible,
        )
        self.network_client_loaded_sent = True
        self.last_sent_held_item_key = None
        self.last_udp_player_state = 0.0

    def _sync_remote_players(self) -> None:
        if self.network_client is None:
            return
        remote_players = self.network_client.remote_players_snapshot()
        for player_id in list(self.remote_player_nodes):
            if player_id not in remote_players:
                self._remove_remote_player_render(player_id)
        for player_id, player in remote_players.items():
            render = self.remote_player_nodes.get(player_id)
            if render is None:
                render = self._make_remote_player_render(player_id)
                self.remote_player_nodes[player_id] = render
            render.root.setPos(*self._predicted_remote_player_pos(player))
            render.root.setH(player.heading)
            self._update_remote_player_label(render, player)
            self._sync_remote_player_held_item(render, player)
            self._animate_remote_player(render, player)

    def _update_remote_player_label(self, render: _RemotePlayerRender, player: RemotePlayer) -> None:
        if not self.display_user_id:
            render.name_label.hide()
            return
        user_id = player.user_id.strip() or str(player.player_id)
        label_node = render.name_label.node()
        if isinstance(label_node, TextNode) and label_node.getText() != user_id:
            label_node.setText(user_id)
        render.name_label.show()

    def _remove_remote_player_render(self, player_id: int) -> None:
        render = self.remote_player_nodes.pop(player_id, None)
        if render is None:
            return
        if render.held_model is not None:
            render.held_model.removeNode()
            render.held_model = None
        render.root.removeNode()

    def _sync_remote_player_labels(self) -> None:
        if self.network_client is None:
            for render in self.remote_player_nodes.values():
                render.name_label.hide()
            return
        remote_players = self.network_client.remote_players_snapshot()
        for player_id, render in self.remote_player_nodes.items():
            player = remote_players.get(player_id)
            if player is None:
                render.name_label.hide()
            else:
                self._update_remote_player_label(render, player)

    def _predicted_remote_player_pos(self, player: RemotePlayer) -> tuple[float, float, float]:
        elapsed = max(0.0, min(REMOTE_PLAYER_PREDICTION_SECONDS, time.monotonic() - player.received_at))
        return (
            player.pos[0] + player.velocity[0] * elapsed,
            player.pos[1] + player.velocity[1] * elapsed,
            player.pos[2] + player.velocity[2] * elapsed,
        )

    def _animate_remote_player(self, render: _RemotePlayerRender, player: RemotePlayer) -> None:
        now = time.monotonic()
        dt = min(0.10, max(0.0, now - render.last_update))
        render.last_update = now
        prediction_elapsed = now - player.received_at
        horizontal_speed = math.hypot(player.velocity[0], player.velocity[1])
        if prediction_elapsed > REMOTE_PLAYER_PREDICTION_SECONDS or horizontal_speed < 0.05:
            horizontal_speed = 0.0
        if horizontal_speed > 0.0:
            render.phase += dt * (5.0 + min(horizontal_speed, 8.0) * 1.3)
            swing = math.sin(render.phase) * min(34.0, 10.0 + horizontal_speed * 4.5)
            roll = math.sin(render.phase * 2.0) * min(7.0, horizontal_speed * 0.8)
        else:
            swing = 0.0
            roll = 0.0
        for name, angle in (
            ("left-arm", swing),
            ("right-arm", -swing),
            ("left-leg", -swing),
            ("right-leg", swing),
        ):
            limb = render.limbs.get(name)
            if limb is not None:
                limb.setP(angle)
                limb.setR(roll if name.startswith("left") else -roll)

    def _sync_remote_player_held_item(self, render: _RemotePlayerRender, player: RemotePlayer) -> None:
        key = (player.held_hash, int(player.held_orientation)) if player.held_visible and player.held_hash else None
        if key == render.held_key:
            return
        if render.held_model is not None:
            render.held_model.removeNode()
            render.held_model = None
        render.held_key = key
        if key is None:
            render.held_anchor.hide()
            return
        template = self._remote_held_model_template(key)
        if template is None:
            render.held_anchor.hide()
            return
        model = template.copyTo(render.held_anchor)
        model.setPos(0, 0, 0)
        model.setScale(0.30)
        model.setHpr(28, -24, 12)
        model.setDepthTest(True)
        model.show()
        render.held_model = model
        render.held_anchor.show()

    def _remote_held_model_template(self, key: tuple[str, int]) -> NodePath | None:
        template = self.remote_held_model_templates.get(key)
        if template is not None:
            return template
        digest, orientation = key
        try:
            asset_ready = self.network_client is None or self.network_client.asset_path(digest).is_file()
        except WorldFormatError:
            asset_ready = False
        if not asset_ready:
            self.pending_asset_digests.add(digest)
            if self.network_client is not None:
                self.network_client.ensure_runtime_asset(digest)
            return None
        if key not in self.remote_held_pending_build_set:
            self.remote_held_pending_build_set.add(key)
            self.remote_held_pending_builds.append(key)
        return None

    def _process_remote_held_model_builds(self) -> None:
        if not self.remote_held_pending_builds:
            return
        deadline = time.perf_counter() + NETWORK_APPLY_FRAME_BUDGET
        built = 0
        while self.remote_held_pending_builds:
            key = self.remote_held_pending_builds.popleft()
            self.remote_held_pending_build_set.discard(key)
            if key in self.remote_held_model_templates:
                continue
            digest, orientation = key
            try:
                surface = self.surface_cache.get(digest)
                template = build_box_preview_mesh(surface, orientation, f"remote-held-{digest[:12]}-{orientation}")
            except (BoxFormatError, ValueError):
                template = None
            if template is not None:
                template.detachNode()
                template.hide()
                self.remote_held_model_templates[key] = template
                self._trim_remote_held_model_templates()
                built += 1
            for render in self.remote_player_nodes.values():
                if render.held_key == key and render.held_model is None:
                    render.held_key = None
            if built >= REMOTE_HELD_MODEL_BUILDS_PER_FRAME or time.perf_counter() >= deadline:
                break

    def _trim_remote_held_model_templates(self) -> None:
        while len(self.remote_held_model_templates) > REMOTE_HELD_TEMPLATE_CACHE_LIMIT:
            key, template = next(iter(self.remote_held_model_templates.items()))
            self.remote_held_model_templates.pop(key, None)
            template.removeNode()
            for render in self.remote_player_nodes.values():
                if render.held_key == key:
                    if render.held_model is not None:
                        render.held_model.removeNode()
                        render.held_model = None
                    render.held_key = None

    def _update_player_velocity(self, previous_pos: Vec3, dt: float) -> None:
        if dt <= 1e-6:
            self.player_velocity = Vec3(0, 0, 0)
            return
        delta = self.player_pos - previous_pos
        self.player_velocity = delta / dt

    def _startup_maximize_window_task(self, task):
        if self._startup_window_maximize_done:
            return task.done
        now = time.monotonic()
        if now - self._startup_window_maximize_started > STARTUP_MAXIMIZE_RETRY_SECONDS:
            self._startup_window_maximize_done = True
            return task.done
        if now < self._startup_window_next_maximize_attempt:
            return task.cont
        self._startup_window_next_maximize_attempt = now + STARTUP_MAXIMIZE_RETRY_INTERVAL
        if self.win is None or self.win.getXSize() <= 1 or self.win.getYSize() <= 1:
            return task.cont
        try:
            self._request_startup_window_maximize()
        except Exception:
            return task.cont
        return task.cont

    def _begin_startup_maximize_retry(self) -> None:
        self.taskMgr.remove(STARTUP_MAXIMIZE_TASK_NAME)
        self._startup_window_maximize_done = False
        self._startup_window_maximize_started = time.monotonic()
        self._startup_window_next_maximize_attempt = 0.0
        self.taskMgr.add(self._startup_maximize_window_task, STARTUP_MAXIMIZE_TASK_NAME)

    def _request_startup_window_maximize(self) -> bool:
        if sys.platform == "win32":
            hwnd = _panda_window_hwnd(self.win)
            if hwnd is None:
                return False
            return _maximize_windows_hwnd(hwnd)
        return maximize_window(self.win)

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
            self._mark_player_input_active()

    def _mark_player_input_active(self) -> None:
        self.last_player_input_time = globalClock.getFrameTime()

    def _can_run_background_world_sync(self) -> bool:
        if not self._player_controls_enabled(self.world_load_job is not None):
            return True
        if self.network_client is None:
            return True
        return self._player_input_is_idle()

    def _player_controls_enabled(self, loading: bool | None = None) -> bool:
        if loading is None:
            loading = self.world_load_job is not None
        return not loading and not self.ui_open and self.modal_mode is None

    def _sync_network_permissions_snapshot(self) -> None:
        if self.network_client is None:
            return
        permissions = dict(self.network_client.permissions)
        if permissions != self.permissions:
            self._apply_permissions(permissions)
        self._apply_client_config(self.network_client.client_config, show_status=False)

    def _apply_client_config(self, value: object, show_status: bool = True) -> None:
        previous = self.display_user_id
        display_user_id = True
        if isinstance(value, dict) and "display_user_id" in value:
            display_user_id = bool(value["display_user_id"])
        self.display_user_id = display_user_id
        if previous != display_user_id:
            self._sync_remote_player_labels()
            if show_status:
                state = "shown" if display_user_id else "hidden"
                self._set_status(f"User ID labels {state}")

    def _player_input_is_idle(self) -> bool:
        if any(self.key_state.values()):
            return False
        if self.move_mode == "walk" and (not self.grounded or abs(self.vertical_velocity) > 1e-4):
            return False
        return globalClock.getFrameTime() - self.last_player_input_time >= PLAYER_INPUT_IDLE_SYNC_DELAY

    def _update_player(self, dt: float, allow_input: bool = True) -> None:
        heading_rad = math.radians(self.heading)
        forward = Vec3(-math.sin(heading_rad), math.cos(heading_rad), 0)
        right = Vec3(math.cos(heading_rad), math.sin(heading_rad), 0)
        desired = Vec3(0, 0, 0)

        if allow_input and self.key_state["forward"]:
            desired += forward
        if allow_input and self.key_state["back"]:
            desired -= forward
        if allow_input and self.key_state["right"]:
            desired += right
        if allow_input and self.key_state["left"]:
            desired -= right
        if desired.lengthSquared() > 0:
            desired.normalize()
            desired *= MOVE_SPEED * dt

        if self.move_mode == "fly":
            if allow_input and self.key_state["up"]:
                desired.z += FLY_VERTICAL_SPEED * dt
            if allow_input and self.key_state["down"]:
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
            self.grounded = (
                self.vertical_velocity <= 0.0
                and self._support_height_below(self.player_pos, STANDING_TOLERANCE) is not None
            )
            if self.grounded and self.vertical_velocity < 0.0:
                self.vertical_velocity = 0.0
        self.player_model.setPos(self.player_pos)
        self.player_model.setH(self.heading)

    def _move_player_with_collision(self, movement: Vec3) -> None:
        for component in (Vec3(movement.x, 0, 0), Vec3(0, movement.y, 0), Vec3(0, 0, movement.z)):
            if component.lengthSquared() == 0:
                continue
            if component.z == 0.0 and self._can_snap_to_walk_support():
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
                if (component.x != 0.0 or component.y != 0.0) and self._can_snap_to_walk_support():
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

    def _can_snap_to_walk_support(self) -> bool:
        return self.move_mode == "walk" and self.grounded and self.vertical_velocity <= 0.0

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
        now = globalClock.getFrameTime()
        if now < self.next_hud_update:
            return
        self.next_hud_update = now + HUD_UPDATE_INTERVAL
        selected = f"{self.selected_hash[:12]}@{self.selected_orientation}"
        target = "none"
        if self.hovered_cell is not None:
            digest = self.world_map.get_box(self.hovered_cell)
            if digest is not None:
                orientation = self.world_map.get_orientation(self.hovered_cell)
                target = f"{self.hovered_cell}:{digest[:12]}@{orientation}"
        gpu_mode = "GPU" if self.gpu_profile.hardware_accelerated else "software"
        chunk_count = len(self.chunk_meshes)
        quad_count = self.total_chunk_quads
        net_mode = ""
        if self.network_client is not None:
            user_text = self.network_client.user_id or "pending"
            net_mode = f"  user={user_text}  net={self.network_client.udp_status}"
        restricted_items = [
            label
            for label, key in (("set", "allow_set"), ("fly", "allow_fly"), ("break", "allow_break"), ("cmd", "allow_cmd"))
            if not self.permissions.get(key, True)
        ]
        restricted = ",".join(restricted_items)
        permission_text = f"  restricted={restricted}" if restricted else ""
        text = (
            f"{self.paths.root.name}  boxes={len(self.world_map.boxes)}  "
            f"chunks={chunk_count}  quads={quad_count}  target={target}  selected={selected}  "
            f"move={self.move_mode}  view={self.view_mode}  {gpu_mode}{net_mode}{permission_text}"
        )
        if text != self.last_detail_text:
            self.detail.setText(text)
            self.last_detail_text = text

    def _set_status(self, text: str) -> None:
        if text == self.last_status_text:
            return
        self.status.setText(text)
        self.last_status_text = text

    def _can_edit_world(self) -> bool:
        if self.connect_required and self.network_client is None:
            self._open_connect_dialog()
            return False
        if self.network_client is None:
            return True
        if self.network_client.connected:
            return True
        self._show_disconnect_panel("Disconnected. Reconnecting...")
        self._set_status("Cannot edit while disconnected")
        return False

    def _right_click(self) -> None:
        self._mark_player_input_active()
        if self.modal_mode == "focus_pause":
            return
        if self.world_load_job is not None:
            return
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
        if not self._permission_allowed("allow_set", "place boxes"):
            return

        target = self._placement_cell(hit_type, cell, normal, point)
        if target is None or target in self.world_map.boxes:
            return
        if not self._can_edit_world():
            return
        if self._block_intersects_player(target):
            return
        self._set_world_box_with_orientation(target, self.selected_hash, self.selected_orientation)
        self.place_sound.play()
        self._set_status(f"Placed {target} orientation={self.selected_orientation}")

    def _delete_clicked_box(self) -> None:
        self._mark_player_input_active()
        if self.modal_mode == "focus_pause":
            return
        if self.world_load_job is not None:
            return
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
            if not self._permission_allowed("allow_break", "break boxes"):
                return
            if not self._can_edit_world():
                return
            digest = self.world_map.get_box(cell)
            if digest is not None:
                self.last_deleted_box = (cell, digest, self.world_map.get_orientation(cell))
            self._remove_world_box(cell)

    def _pick_clicked_box(self) -> None:
        self._mark_player_input_active()
        if self.modal_mode == "focus_pause":
            return
        if self.world_load_job is not None:
            return
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
        self.selected_orientation = self.world_map.get_orientation(cell)
        self._refresh_held_item(force=True)
        self._set_status(f"Selected {digest[:12]} orientation={self.selected_orientation}")

    def _rotate_target_box(self, command: str) -> None:
        self._mark_player_input_active()
        if self.modal_mode == "focus_pause":
            return
        if self.world_load_job is not None:
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
        if hit_type != "block" or cell is None or cell not in self.world_map.boxes:
            return
        if not self._permission_allowed("allow_set", "rotate boxes"):
            return
        if not self._can_edit_world():
            return

        current = self.world_map.get_orientation(cell)
        axis, turns = self._view_rotation_axis(command)
        new_orientation = turn_orientation_around_axis(current, axis, turns)
        self.world_map.set_orientation(cell, new_orientation)
        self._rebuild_chunks_for_cell(cell)
        if self.network_client is not None:
            self.network_client.send_rotate(cell, new_orientation)
            self._set_status(f"Rotated {cell} orientation={new_orientation}")
            return
        self._set_status(f"Rotated {cell} orientation={new_orientation}")

    def _rotate_key_pressed(self, command: str) -> None:
        if self.modal_mode == "editor_wait":
            self._focus_editor_if_waiting()
            return
        self._rotate_target_box(command)

    def _directional_key_pressed(self, direction: str) -> None:
        if self.modal_mode == "focus_pause":
            return
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
        if self.modal_mode == "focus_pause":
            return
        if self.world_load_job is not None:
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
        if not self._permission_allowed("allow_set", "edit boxes"):
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
            self.selected_orientation = self.world_map.get_orientation(cell)
            self._refresh_held_item(force=True)
            if new_digest != digest:
                self._set_world_box_with_orientation(cell, new_digest, self.selected_orientation)
                self._set_status(f"Updated {cell} -> {new_digest[:12]} orientation={self.selected_orientation}")
            else:
                self._set_status(f"Selected edited box {new_digest[:12]} orientation={self.selected_orientation}")
        finally:
            if tempdir is not None:
                tempdir.cleanup()
            self.set_mouse_capture(self.editor_restore_mouse_capture)

    def _look_at_world_focus(self) -> None:
        if self.modal_mode == "focus_pause":
            return
        if self.world_load_job is not None:
            return
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
        if self.modal_mode == "focus_pause":
            return
        if self.world_load_job is not None:
            return
        if self.ui_open:
            return
        if self.move_mode == "walk" and not self._permission_allowed("allow_fly", "fly"):
            return
        self.move_mode = "fly" if self.move_mode == "walk" else "walk"
        self.vertical_velocity = 0.0
        self.grounded = self.move_mode == "walk" and self._support_height_below(self.player_pos, STANDING_TOLERANCE) is not None
        self._set_status(f"Movement: {self.move_mode}")

    def _toggle_view(self) -> None:
        if self.modal_mode == "focus_pause":
            return
        if self.world_load_job is not None:
            return
        if self.ui_open:
            return
        self.view_mode = "third" if self.view_mode == "first" else "first"
        self._refresh_held_item(force=True)
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

    def _start_server_world_load(self, show_progress: bool) -> None:
        self._clear_movement_keys()
        if self.focus_pause_panel is not None:
            self.focus_pause_panel.destroy()
        self.focus_pause_panel = None
        if self.modal_mode == "focus_pause":
            self.ui_open = False
            self.modal_mode = None
        if show_progress:
            self.set_mouse_capture(False)
            self.crosshair.hide()
        self._clear_world_meshes()
        self.chunk_index = self._build_chunk_index()
        self.digest_index = self._build_digest_index()
        self.surface_cache.invalidate()
        self.collision_cache.invalidate()

        digests = deque(sorted({self.default_hash, *self.world_map.boxes.values()}))
        collision_keys = deque(
            sorted(
                {
                    (digest, self.world_map.get_orientation(cell))
                    for cell, digest in self.world_map.boxes.items()
                }
            )
        )
        chunk_keys = deque(sorted(self.chunk_index))
        total = len(digests) + len(collision_keys) + len(chunk_keys)
        self.world_load_job = _WorldLoadJob(
            surface_digests=digests,
            collision_keys=collision_keys,
            chunk_keys=chunk_keys,
            total=max(1, total),
            complete_status="Connected to server",
            lift_player=True,
            show_progress=show_progress,
            waiting_for_assets=bool(self.network_client and not self.network_client.startup_assets_complete.is_set()),
        )
        if show_progress:
            self._open_loading_panel()
            if self.world_load_job.waiting_for_assets:
                self._update_startup_asset_progress()
            else:
                self._set_loading_progress("Loading world data...", 0.0)
        else:
            self._set_status("Synchronizing world...")
        self.taskMgr.remove(WORLD_LOAD_TASK_NAME)
        self.taskMgr.add(self._advance_world_load, WORLD_LOAD_TASK_NAME)

    def _open_loading_panel(self) -> None:
        if self.loading_panel is not None:
            self.loading_panel.show()
            return
        self.loading_panel = DirectFrame(
            frameColor=(0.025, 0.030, 0.036, 0.92),
            frameSize=(-0.86, 0.86, -0.24, 0.24),
            pos=(0, 0, 0.05),
        )
        DirectLabel(
            parent=self.loading_panel,
            text="Loading World",
            text_fg=(1, 1, 1, 1),
            text_scale=0.052,
            frameColor=(0, 0, 0, 0),
            pos=(0, 0, 0.125),
        )
        self.loading_message = DirectLabel(
            parent=self.loading_panel,
            text="Loading world data...",
            text_fg=(0.86, 0.90, 0.94, 1),
            text_scale=0.030,
            frameColor=(0, 0, 0, 0),
            pos=(0, 0, 0.035),
        )
        DirectFrame(
            parent=self.loading_panel,
            frameColor=(0.12, 0.14, 0.16, 1),
            frameSize=(-0.58, 0.58, -0.026, 0.026),
            pos=(0, 0, -0.055),
        )
        self.loading_bar_fill = DirectFrame(
            parent=self.loading_panel,
            frameColor=(0.18, 0.58, 0.78, 1),
            frameSize=(-0.58, -0.58, -0.026, 0.026),
            pos=(0, 0, -0.055),
        )
        self.loading_percent = DirectLabel(
            parent=self.loading_panel,
            text="0%",
            text_fg=(0.96, 0.98, 1, 1),
            text_scale=0.030,
            frameColor=(0, 0, 0, 0),
            pos=(0, 0, -0.135),
        )

    def _advance_world_load(self, task):
        job = self.world_load_job
        if job is None:
            return task.done

        if job.waiting_for_assets:
            if self.network_client is None:
                self._finish_world_load()
                return task.done
            if not self.network_client.startup_assets_complete.is_set():
                self._update_startup_asset_progress()
                return task.cont
            _done, _total, failed = self.network_client.startup_asset_progress()
            if failed:
                self._cancel_world_load()
                self._show_disconnect_panel("Startup asset transfer failed. Reconnecting...")
                return task.done
            job.waiting_for_assets = False
            if job.show_progress:
                self._set_loading_progress("Preparing world...", 0.0)

        deadline = time.perf_counter() + WORLD_LOAD_FRAME_BUDGET
        while True:
            if job.surface_digests:
                digest = job.surface_digests.popleft()
                if job.show_progress:
                    self._set_loading_progress(f"Preparing box mesh {digest[:12]}", job.done / job.total)
                try:
                    self.surface_cache.get(digest)
                except BoxFormatError as exc:
                    raise SystemExit(f"Cannot preload box {digest}: {exc}") from exc
                job.done += 1
            elif job.collision_keys:
                digest, orientation = job.collision_keys.popleft()
                if job.show_progress:
                    self._set_loading_progress(f"Preparing collision {digest[:12]}", job.done / job.total)
                try:
                    self.collision_cache.get(digest, orientation)
                except BoxFormatError as exc:
                    raise SystemExit(f"Cannot preload collision {digest}: {exc}") from exc
                job.done += 1
            elif job.chunk_keys:
                key = job.chunk_keys.popleft()
                if job.show_progress:
                    self._set_loading_progress(f"Building chunk {key}", job.done / job.total)
                self._rebuild_chunk(key)
                job.done += 1
            else:
                self._finish_world_load()
                return task.done

            if time.perf_counter() >= deadline:
                if job.show_progress:
                    self._set_loading_progress("Loading world...", job.done / job.total)
                return task.cont

    def _finish_world_load(self) -> None:
        job = self.world_load_job
        if job is None:
            return
        self.world_load_job = None
        trigger_startup_maximize = job.show_progress and not self.server_initial_load_progress_used
        if job.show_progress:
            self.server_initial_load_progress_used = True
            self._set_loading_progress("Ready", 1.0)
        if job.lift_player:
            self._lift_player_out_of_blocks()
        self._apply_deferred_world_messages()
        if job.show_progress:
            self._close_loading_panel()
            self.set_mouse_capture(True)
        self._refresh_held_item(force=True)
        self._notify_network_client_loaded()
        if trigger_startup_maximize:
            self._begin_startup_maximize_retry()
        self._set_status(job.complete_status)

    def _cancel_world_load(self) -> None:
        if self.world_load_job is None:
            return
        self.world_load_job = None
        self.taskMgr.remove(WORLD_LOAD_TASK_NAME)
        self._close_loading_panel()
        self.deferred_world_messages.clear()
        self.waiting_asset_world_messages.clear()
        self._clear_movement_keys()
        self.set_mouse_capture(False)
        self._set_status("Disconnected while loading")

    def _set_loading_progress(self, message: str, progress: float) -> None:
        clamped = max(0.0, min(1.0, progress))
        left = -0.58
        right = left + 1.16 * clamped
        if self.loading_message is not None:
            self.loading_message["text"] = message
        if self.loading_bar_fill is not None:
            self.loading_bar_fill["frameSize"] = (left, right, -0.026, 0.026)
        if self.loading_percent is not None:
            self.loading_percent["text"] = f"{round(clamped * 100):d}%"

    def _update_startup_asset_progress(self) -> None:
        if self.network_client is None:
            return
        done, total, _failed = self.network_client.startup_asset_progress()
        if total <= 0:
            self._set_loading_progress("Downloading box assets...", 0.0)
            return
        self._set_loading_progress(f"Downloading box assets {done}/{total}", done / total)

    def _close_loading_panel(self) -> None:
        if self.loading_panel is not None:
            self.loading_panel.destroy()
        self.loading_panel = None
        self.loading_bar_fill = None
        self.loading_message = None
        self.loading_percent = None

    def _clear_world_meshes(self) -> None:
        for mesh in self.chunk_meshes.values():
            self._remove_chunk_mesh(mesh)
        self.chunk_meshes.clear()
        self.total_chunk_quads = 0
        self._clear_box_lights()
        self._clear_remote_held_model_templates()
        self.waiting_asset_world_messages.clear()
        self.dirty_chunk_keys.clear()
        self.dirty_chunk_queue.clear()

    def _clear_remote_held_model_templates(self) -> None:
        for template in self.remote_held_model_templates.values():
            template.removeNode()
        self.remote_held_model_templates.clear()
        self.remote_held_pending_builds.clear()
        self.remote_held_pending_build_set.clear()
        for render in self.remote_player_nodes.values():
            if render.held_model is not None:
                render.held_model.removeNode()
                render.held_model = None
            render.held_key = None
            render.held_anchor.hide()

    def _apply_deferred_world_messages(self) -> None:
        self.pending_world_messages.extend(self.deferred_world_messages)
        self.deferred_world_messages = []
        self._apply_pending_asset_updates()
        self._apply_pending_world_messages()
        self._process_dirty_chunks()

    def _apply_pending_asset_updates(self) -> None:
        if not self.pending_asset_digests:
            return
        deadline = time.perf_counter() + NETWORK_APPLY_FRAME_BUDGET
        for digest in list(self.pending_asset_digests):
            try:
                asset_ready = self.network_client is None or self.network_client.asset_path(digest).is_file()
            except WorldFormatError:
                asset_ready = False
            if not asset_ready:
                if self.network_client is not None:
                    self.network_client.ensure_runtime_asset(digest)
                continue
            self.pending_asset_digests.discard(digest)
            self.surface_cache.invalidate(digest)
            self.collision_cache.invalidate(digest)
            self._invalidate_remote_held_model_templates(digest)
            self._mark_chunks_dirty_for_digest(digest)
            waiting = self.waiting_asset_world_messages.pop(digest, [])
            if waiting:
                self.pending_world_messages.extend(waiting)
            if time.perf_counter() >= deadline:
                break

    def _invalidate_remote_held_model_templates(self, digest: str) -> None:
        stale_keys = [key for key in self.remote_held_model_templates if key[0] == digest]
        for key in stale_keys:
            self.remote_held_model_templates.pop(key).removeNode()
        self.remote_held_pending_builds = deque(key for key in self.remote_held_pending_builds if key[0] != digest)
        self.remote_held_pending_build_set = {key for key in self.remote_held_pending_build_set if key[0] != digest}
        for render in self.remote_player_nodes.values():
            if render.held_key is not None and render.held_key[0] == digest:
                if render.held_model is not None:
                    render.held_model.removeNode()
                    render.held_model = None
                render.held_key = None

    def _apply_pending_world_messages(self) -> None:
        deadline = time.perf_counter() + NETWORK_APPLY_FRAME_BUDGET
        while self.pending_world_messages:
            self._apply_network_world_delta(self.pending_world_messages.popleft())
            if time.perf_counter() >= deadline:
                break

    def _process_dirty_chunks(self, force: bool = False) -> None:
        deadline = time.perf_counter() + CHUNK_REBUILD_FRAME_BUDGET
        while self.dirty_chunk_queue:
            key = self.dirty_chunk_queue.popleft()
            self.dirty_chunk_keys.discard(key)
            self._rebuild_chunk(key)
            if not force and time.perf_counter() >= deadline:
                break

    def _apply_network_world_delta(self, message: dict) -> None:
        message_type = message.get("type")
        if message_type == "box_set":
            cell = list_to_cell(message.get("cell"))
            digest = str(message.get("hash", ""))
            orientation = int(message.get("orientation", IDENTITY_ORIENTATION))
            if self.world_map.get_box(cell) == digest and self.world_map.get_orientation(cell) == orientation:
                return
            if self.network_client is not None:
                try:
                    asset_ready = self.network_client.asset_path(digest).is_file()
                except WorldFormatError:
                    asset_ready = False
                if not asset_ready:
                    self.pending_asset_digests.add(digest)
                    self.network_client.ensure_runtime_asset(digest)
                    waiting = self.waiting_asset_world_messages.setdefault(digest, [])
                    waiting.append(message)
                    return
            try:
                self.surface_cache.get(digest)
                self.collision_cache.get(digest, orientation)
            except BoxFormatError as exc:
                raise SystemExit(f"Cannot preload network box {digest}: {exc}") from exc
            self._apply_world_box(cell, digest, orientation, rebuild_now=False)
            if self.network_client is None:
                self.saved_snapshot = self._current_world_snapshot()
        elif message_type == "box_removed":
            cell = list_to_cell(message.get("cell"))
            if cell not in self.world_map.boxes:
                return
            self._apply_remove_world_box(cell, play_sound=True, rebuild_now=False)
            if self.network_client is None:
                self.saved_snapshot = self._current_world_snapshot()

    def _rebuild_all_cells(self) -> None:
        self._clear_world_meshes()
        self.chunk_index = self._build_chunk_index()
        self.digest_index = self._build_digest_index()
        self.surface_cache.invalidate()
        for key in sorted(self.chunk_index):
            self._rebuild_chunk(key)

    def _set_world_box(self, cell: Cell, digest: str) -> None:
        existing = cell in self.world_map.boxes
        existing_orientation = self.world_map.get_orientation(cell) if existing else IDENTITY_ORIENTATION
        self._set_world_box_with_orientation(cell, digest, existing_orientation)

    def _set_world_box_with_orientation(self, cell: Cell, digest: str, orientation: int) -> None:
        existing = cell in self.world_map.boxes
        self._apply_world_box(cell, digest, orientation)
        if self.network_client is not None:
            if existing:
                self.network_client.send_set_box(cell, digest, orientation, include_asset=True)
            else:
                self.network_client.send_place(cell, digest, orientation, include_asset=True)
            return

    def _apply_world_box(self, cell: Cell, digest: str, orientation: int, rebuild_now: bool = True) -> None:
        old_digest = self.world_map.get_box(cell)
        old_key = chunk_key_for_cell(cell) if cell in self.world_map.boxes else None
        self.world_map.set_box(cell, digest, orientation)
        self.box_light_blocking_face_cache.pop(cell, None)
        key = chunk_key_for_cell(cell)
        self.chunk_index.setdefault(key, set()).add(cell)
        if old_digest is not None and old_digest != digest:
            old_digest_cells = self.digest_index.get(old_digest)
            if old_digest_cells is not None:
                old_digest_cells.discard(cell)
                if not old_digest_cells:
                    self.digest_index.pop(old_digest, None)
        self.digest_index.setdefault(digest, set()).add(cell)
        if old_key is not None and old_key != key:
            old_cells = self.chunk_index.get(old_key)
            if old_cells is not None:
                old_cells.discard(cell)
                if not old_cells:
                    self.chunk_index.pop(old_key, None)
        self._mark_chunks_dirty_for_cell(cell, rebuild_now=rebuild_now)

    def _remove_world_box(self, cell: Cell) -> None:
        removed = cell in self.world_map.boxes
        self._apply_remove_world_box(cell, play_sound=True)
        if self.network_client is not None:
            self.network_client.send_delete(cell)
            return
        if not removed:
            self._set_status(f"No box at {cell}")

    def _apply_remove_world_box(self, cell: Cell, play_sound: bool = False, rebuild_now: bool = True) -> None:
        old_digest = self.world_map.get_box(cell)
        if not self.world_map.remove_box(cell):
            return
        self.box_light_blocking_face_cache.pop(cell, None)
        key = chunk_key_for_cell(cell)
        cells = self.chunk_index.get(key)
        if cells is not None:
            cells.discard(cell)
            if not cells:
                self.chunk_index.pop(key, None)
        if old_digest is not None:
            digest_cells = self.digest_index.get(old_digest)
            if digest_cells is not None:
                digest_cells.discard(cell)
                if not digest_cells:
                    self.digest_index.pop(old_digest, None)
        self._mark_chunks_dirty_for_cell(cell, rebuild_now=rebuild_now)
        if self.hovered_cell == cell:
            self.hovered_cell = None
            self.hover_outline.hide()
        if play_sound:
            self.break_sound.play()
        self._set_status(f"Deleted {cell}")

    def _restore_last_deleted_box(self) -> None:
        if self.modal_mode == "editor_wait":
            self._focus_editor_if_waiting()
            return
        if self.ui_open:
            return
        if not self._permission_allowed("allow_set", "restore boxes"):
            return
        if not self._can_edit_world():
            return
        if self.last_deleted_box is None:
            self._set_status("No deleted box to restore")
            return
        cell, digest, orientation = self.last_deleted_box
        if cell in self.world_map.boxes:
            self._set_status(f"Cannot restore {cell}; cell is occupied")
            return
        if self._block_intersects_player(cell):
            self._set_status(f"Cannot restore {cell}; player is in the way")
            return
        self._set_world_box_with_orientation(cell, digest, orientation)
        self._set_status(f"Restore requested {cell}")

    def _build_chunk_index(self) -> dict[ChunkKey, set[Cell]]:
        index: dict[ChunkKey, set[Cell]] = {}
        for cell in self.world_map.boxes:
            index.setdefault(chunk_key_for_cell(cell), set()).add(cell)
        return index

    def _build_digest_index(self) -> dict[str, set[Cell]]:
        index: dict[str, set[Cell]] = {}
        for cell, digest in self.world_map.boxes.items():
            index.setdefault(digest, set()).add(cell)
        return index

    def _rebuild_chunks_for_cell(self, cell: Cell) -> None:
        for key in chunk_keys_for_cell_and_neighbors(cell):
            self._rebuild_chunk(key)

    def _mark_chunks_dirty_for_cell(self, cell: Cell, rebuild_now: bool = False) -> None:
        self.box_light_occlusion_cache.clear()
        keys = chunk_keys_for_cell_and_neighbors(cell)
        if rebuild_now:
            for key in keys:
                self._rebuild_chunk(key)
            return
        for key in keys:
            if key not in self.dirty_chunk_keys:
                self.dirty_chunk_keys.add(key)
                self.dirty_chunk_queue.append(key)

    def _mark_chunks_dirty_for_digest(self, digest: str) -> None:
        for cell in self.digest_index.get(digest, ()):
            self._mark_chunks_dirty_for_cell(cell, rebuild_now=False)

    def _rebuild_chunk(self, key: ChunkKey) -> None:
        old_mesh = self.chunk_meshes.pop(key, None)
        if old_mesh is not None:
            self.total_chunk_quads = max(0, self.total_chunk_quads - old_mesh.quads)
            self._remove_chunk_mesh(old_mesh)
        self._clear_box_lights_for_chunk(key)
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
        for cell in sorted(chunk_cells):
            self._sync_box_lights_for_cell(cell)
        self.chunk_meshes[key] = mesh
        self.total_chunk_quads += mesh.quads
        if self.world_load_job is None:
            self.next_point_light_update = 0.0

    def _remove_chunk_mesh(self, mesh: WorldChunkMesh) -> None:
        if mesh.opaque:
            mesh.opaque.removeNode()
        if mesh.transparent:
            mesh.transparent.removeNode()

    def _sync_box_lights_for_cell(self, cell: Cell) -> None:
        self._clear_box_lights_for_cell(cell)
        digest = self.world_map.get_box(cell)
        if digest is None:
            return
        surface = self.surface_cache.get(digest)
        if not surface.light_points:
            return
        orientation = self.world_map.get_orientation(cell)
        candidates: list[_BoxLightCandidate] = []
        for local_position, color in surface.light_points:
            rotated = rotate_point(local_position, orientation)
            candidates.append(
                _BoxLightCandidate(
                    (cell[0] + rotated[0], cell[1] + rotated[1], cell[2] + rotated[2]),
                    color,
                )
            )
        self.box_light_candidates[cell] = tuple(candidates)
        for index, _candidate in enumerate(candidates):
            self._add_box_light_to_spatial_index(cell, index)
        self.next_point_light_update = 0.0

    def _clear_box_lights(self) -> None:
        for light in self.active_box_lights.values():
            self._remove_box_light(light)
        self.active_box_lights.clear()
        self.box_light_candidates.clear()
        self.box_light_spatial_index.clear()
        self.box_light_occlusion_cache.clear()
        self.box_light_blocking_face_cache.clear()
        self.next_point_light_update = 0.0
        self.next_point_light_prune = 0.0

    def _clear_box_lights_for_chunk(self, key: ChunkKey) -> None:
        for cell in [cell for cell in self.box_light_candidates if chunk_key_for_cell(cell) == key]:
            self._clear_box_lights_for_cell(cell)

    def _clear_box_lights_for_cell(self, cell: Cell) -> None:
        candidates = self.box_light_candidates.pop(cell, None)
        if candidates is not None:
            for index, candidate in enumerate(candidates):
                self._remove_box_light_from_spatial_index(cell, index, candidate.position)
                self.box_light_occlusion_cache.pop((cell, index), None)
        for key in [key for key in self.active_box_lights if key[0] == cell]:
            self._remove_box_light(self.active_box_lights.pop(key))
        self.next_point_light_update = 0.0

    def _box_light_spatial_key_for_position(self, position: tuple[float, float, float]) -> tuple[int, int, int]:
        return (
            math.floor(position[0] / POINT_LIGHT_SPATIAL_CHUNK),
            math.floor(position[1] / POINT_LIGHT_SPATIAL_CHUNK),
            math.floor(position[2] / POINT_LIGHT_SPATIAL_CHUNK),
        )

    def _add_box_light_to_spatial_index(self, cell: Cell, index: int) -> None:
        candidates = self.box_light_candidates.get(cell, ())
        if index >= len(candidates):
            return
        key = self._box_light_spatial_key_for_position(candidates[index].position)
        self.box_light_spatial_index.setdefault(key, set()).add((cell, index))

    def _remove_box_light_from_spatial_index(
        self,
        cell: Cell,
        index: int,
        position: tuple[float, float, float],
    ) -> None:
        key = self._box_light_spatial_key_for_position(position)
        bucket = self.box_light_spatial_index.get(key)
        if bucket is None:
            return
        bucket.discard((cell, index))
        if not bucket:
            self.box_light_spatial_index.pop(key, None)

    def _update_visible_box_lights(self, force: bool = False) -> None:
        if self.world_load_job is not None:
            return
        now = globalClock.getFrameTime()
        if not force and not self._player_input_is_idle():
            if now >= self.next_point_light_update:
                self.next_point_light_update = now + POINT_LIGHT_ACTIVE_UPDATE_INTERVAL
                self._sync_desired_box_lights()
            self._prune_occluded_active_box_lights(now)
            return
        if not force and now < self.next_point_light_update:
            self._prune_occluded_active_box_lights(now)
            return
        self.next_point_light_update = now + POINT_LIGHT_UPDATE_INTERVAL
        self.next_point_light_prune = now + POINT_LIGHT_OCCLUSION_PRUNE_INTERVAL
        self._sync_desired_box_lights()

    def _sync_desired_box_lights(self) -> None:
        desired = self._desired_box_light_keys()
        for key in [key for key in self.active_box_lights if key not in desired]:
            self._remove_box_light(self.active_box_lights.pop(key))
        for key in desired:
            if key in self.active_box_lights:
                continue
            candidate = self.box_light_candidates[key[0]][key[1]]
            self.active_box_lights[key] = self._create_box_light(key, candidate)

    def _prune_occluded_active_box_lights(self, now: float) -> None:
        if now < self.next_point_light_prune:
            return
        self.next_point_light_prune = now + POINT_LIGHT_OCCLUSION_PRUNE_INTERVAL
        camera_pos = self.camera.getPos(self.render)
        max_distance_sq = POINT_LIGHT_MAX_DISTANCE * POINT_LIGHT_MAX_DISTANCE
        for key in list(self.active_box_lights):
            candidate = self.box_light_candidates.get(key[0], ())
            if key[1] >= len(candidate):
                self._remove_box_light(self.active_box_lights.pop(key))
                continue
            light = candidate[key[1]]
            dx = light.position[0] - camera_pos.x
            dy = light.position[1] - camera_pos.y
            dz = light.position[2] - camera_pos.z
            if dx * dx + dy * dy + dz * dz > max_distance_sq:
                self._remove_box_light(self.active_box_lights.pop(key))
                continue
            if self._is_box_light_occluded_cached(camera_pos, light.position, key[0], key):
                self._remove_box_light(self.active_box_lights.pop(key))

    def _desired_box_light_keys(self) -> set[tuple[Cell, int]]:
        scored: list[tuple[float, Cell, int]] = []
        camera_pos = self.camera.getPos(self.render)
        max_distance_sq = POINT_LIGHT_MAX_DISTANCE * POINT_LIGHT_MAX_DISTANCE
        for cell, index in self._nearby_box_light_keys(camera_pos):
            candidates = self.box_light_candidates.get(cell, ())
            if index >= len(candidates):
                continue
            candidate = candidates[index]
            dx = candidate.position[0] - camera_pos.x
            dy = candidate.position[1] - camera_pos.y
            dz = candidate.position[2] - camera_pos.z
            distance_sq = dx * dx + dy * dy + dz * dz
            if distance_sq > max_distance_sq:
                continue
            distance = math.sqrt(max(distance_sq, 1e-6))
            brightness = max(candidate.color[0], candidate.color[1], candidate.color[2])
            score = brightness * 100.0 - distance
            scored.append((score, cell, index))
        scored.sort(reverse=True)
        desired: set[tuple[Cell, int]] = set()
        tests = 0
        for _score, cell, index in scored[:POINT_LIGHT_PREFILTER_LIMIT]:
            candidates = self.box_light_candidates.get(cell, ())
            if index >= len(candidates):
                continue
            candidate = candidates[index]
            if tests >= POINT_LIGHT_OCCLUSION_TEST_LIMIT:
                break
            tests += 1
            if self._is_box_light_occluded_cached(camera_pos, candidate.position, cell, (cell, index)):
                continue
            desired.add((cell, index))
            if len(desired) >= MAX_ACTIVE_POINT_LIGHTS:
                break
        return desired

    def _nearby_box_light_keys(self, camera_pos: Point3 | Vec3) -> list[tuple[Cell, int]]:
        if not self.box_light_spatial_index:
            return []
        radius = math.ceil(POINT_LIGHT_MAX_DISTANCE / POINT_LIGHT_SPATIAL_CHUNK)
        center = (
            math.floor(camera_pos.x / POINT_LIGHT_SPATIAL_CHUNK),
            math.floor(camera_pos.y / POINT_LIGHT_SPATIAL_CHUNK),
            math.floor(camera_pos.z / POINT_LIGHT_SPATIAL_CHUNK),
        )
        keys: list[tuple[Cell, int]] = []
        for sx in range(center[0] - radius, center[0] + radius + 1):
            for sy in range(center[1] - radius, center[1] + radius + 1):
                for sz in range(center[2] - radius, center[2] + radius + 1):
                    bucket = self.box_light_spatial_index.get((sx, sy, sz))
                    if bucket:
                        keys.extend(bucket)
        return keys

    def _is_box_light_occluded_cached(
        self,
        camera_pos: Point3 | Vec3,
        light_pos: tuple[float, float, float],
        light_cell: Cell,
        key: tuple[Cell, int],
    ) -> bool:
        camera_bucket = (
            math.floor(camera_pos.x / 2.0),
            math.floor(camera_pos.y / 2.0),
            math.floor(camera_pos.z / 2.0),
        )
        now = globalClock.getFrameTime()
        cached = self.box_light_occlusion_cache.get(key)
        if cached is not None:
            cached_bucket, occluded, expires_at = cached
            if cached_bucket == camera_bucket and now < expires_at:
                return occluded
        occluded = self._is_box_light_occluded(camera_pos, light_pos, light_cell)
        if len(self.box_light_occlusion_cache) > POINT_LIGHT_OCCLUSION_CACHE_LIMIT:
            self.box_light_occlusion_cache.clear()
        self.box_light_occlusion_cache[key] = (
            camera_bucket,
            occluded,
            now + POINT_LIGHT_OCCLUSION_CACHE_SECONDS,
        )
        return occluded

    def _is_box_light_occluded(self, camera_pos: Point3 | Vec3, light_pos: tuple[float, float, float], light_cell: Cell) -> bool:
        origin = Point3(camera_pos)
        target = Point3(*light_pos)
        direction = target - origin
        total_distance = direction.length()
        if total_distance <= 1e-5:
            return False
        direction /= total_distance

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

        distance = 0.0
        entry_normal = (0, 0, 0)
        max_steps = int(total_distance * 3.0) + 8
        for _ in range(max_steps):
            current = (cell[0], cell[1], cell[2])
            if distance > total_distance:
                return False
            if current != light_cell and self._cell_blocks_box_light(current, entry_normal):
                return True

            axis = min(range(3), key=lambda index: next_distances[index])
            distance = next_distances[axis]
            if distance > total_distance:
                return False
            cell[axis] += steps[axis]
            normal = [0, 0, 0]
            normal[axis] = -steps[axis]
            entry_normal = (normal[0], normal[1], normal[2])
            next_distances[axis] += delta_distances[axis]
        return False

    def _cell_blocks_box_light(self, cell: Cell, entry_normal: Cell) -> bool:
        blocking_faces = self._box_light_blocking_faces(cell)
        if not blocking_faces:
            return False
        if entry_normal == (0, 0, 0):
            return True
        return entry_normal in blocking_faces

    def _box_light_blocking_faces(self, cell: Cell) -> frozenset[Cell]:
        cached = self.box_light_blocking_face_cache.get(cell)
        if cached is not None:
            return cached
        digest = self.world_map.get_box(cell)
        if digest is None:
            blocking_faces: frozenset[Cell] = frozenset()
        else:
            try:
                surface = self.surface_cache.get(digest)
            except BoxFormatError:
                blocking_faces = frozenset()
            else:
                orientation = self.world_map.get_orientation(cell)
                blocking_faces = frozenset(rotate_normal(face, orientation) for face in surface.opaque_boundary_faces)
        if len(self.box_light_blocking_face_cache) > POINT_LIGHT_BLOCKING_FACE_CACHE_LIMIT:
            self.box_light_blocking_face_cache.clear()
        self.box_light_blocking_face_cache[cell] = blocking_faces
        return blocking_faces

    def _create_box_light(self, key: tuple[Cell, int], candidate: _BoxLightCandidate) -> NodePath:
        cell, index = key
        light = PointLight(f"box-light-{cell[0]}-{cell[1]}-{cell[2]}-{index}")
        light.setColor((candidate.color[0], candidate.color[1], candidate.color[2], 1.0))
        light.setAttenuation((1.0, 0.12, 0.035))
        light_path = self.world.attachNewNode(light)
        light_path.setPos(*candidate.position)
        self.render.setLight(light_path)
        return light_path

    def _remove_box_light(self, light_path: NodePath) -> None:
        self.render.clearLight(light_path)
        light_path.removeNode()

    def _update_ground(self, force: bool = False) -> None:
        if not force and not self._player_input_is_idle():
            return
        center = (
            math.floor(self.player_pos.x / GROUND_CHUNK),
            math.floor(self.player_pos.y / GROUND_CHUNK),
        )
        if force or center != self.ground_center_chunk:
            self.ground_center_chunk = center
            self._sync_ground_chunk_set(center)
        self._build_pending_ground_chunks(force=force)

    def _sync_ground_chunk_set(self, center: tuple[int, int]) -> None:
        radius = math.ceil((GROUND_SIZE / 2) / GROUND_CHUNK)
        desired = {
            (cx, cy)
            for cx in range(center[0] - radius, center[0] + radius + 1)
            for cy in range(center[1] - radius, center[1] + radius + 1)
        }
        for key in [key for key in self.ground_chunks if key not in desired]:
            self.ground_chunks.pop(key).removeNode()
        self.pending_ground_chunks = deque(key for key in self.pending_ground_chunks if key in desired)
        self.pending_ground_chunk_set = set(self.pending_ground_chunks)
        missing = [key for key in desired if key not in self.ground_chunks and key not in self.pending_ground_chunk_set]
        missing.sort(key=lambda key: (abs(key[0] - center[0]) + abs(key[1] - center[1]), key[0], key[1]))
        for key in missing:
            self.pending_ground_chunks.append(key)
            self.pending_ground_chunk_set.add(key)

    def _build_pending_ground_chunks(self, force: bool = False) -> None:
        budget = len(self.pending_ground_chunks) if force else GROUND_CHUNKS_PER_FRAME
        for _ in range(budget):
            if not self.pending_ground_chunks:
                return
            key = self.pending_ground_chunks.popleft()
            self.pending_ground_chunk_set.discard(key)
            if key in self.ground_chunks:
                continue
            node = self.ground_chunk_template.copyTo(self.ground_root)
            node.setPos(key[0] * GROUND_CHUNK, key[1] * GROUND_CHUNK, 0)
            self.ground_chunks[key] = node

    def _open_help(self) -> None:
        if self.modal_mode == "focus_pause":
            return
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
                    "Right hand shows selected box when set is allowed",
                    "Other players can see your held selected box",
                    "Left click: delete target box",
                    "Z: restore the last box you deleted",
                    "Middle click: select target box and orientation",
                    "E: edit target box with box-editor-view",
                    "Alpha 0 in .box: opaque RGB light source",
                    "F2 or Ctrl+S: show save status",
                    "F5: switch first / third person",
                    "C: look at box centroid / origin if empty",
                    "~: server console; Enter/Send sends command",
                    "Console input wraps up to 4096 chars",
                    "Console: help() lists server commands",
                    "Set permission controls place/edit/rotate/restore",
                    "Server permissions can restrict set/fly/break/cmd",
                    "Remote user IDs obey server DISPLAY_USER_ID",
                    "Scroll console logs; X or Esc closes it",
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

    def _apply_permissions(self, value: object) -> None:
        previous_allow_cmd = self.permissions.get("allow_cmd", True)
        permissions = dict(CLIENT_DEFAULT_PERMISSIONS)
        if isinstance(value, dict):
            for key in permissions:
                if key in value:
                    permissions[key] = bool(value[key])
        self.permissions = permissions
        if not self.permissions["allow_fly"] and self.move_mode == "fly":
            self._force_walk_mode("Flight disabled by server")
        if previous_allow_cmd and not self.permissions.get("allow_cmd", True):
            if self.network_client is not None:
                self.network_client.server_logs = []
            self._set_console_permission_denied()
        self._refresh_held_item(force=True)
        self._set_status("Permissions updated")

    def _permission_allowed(self, permission_name: str, action: str) -> bool:
        if self.permissions.get(permission_name, True):
            return True
        self._set_status(f"Not allowed to {action}")
        return False

    def _force_walk_mode(self, status: str | None = None) -> None:
        if self.move_mode != "walk":
            self.move_mode = "walk"
            self.vertical_velocity = 0.0
            self.grounded = self._support_height_below(self.player_pos, STANDING_TOLERANCE) is not None
        self.key_state["up"] = False
        self.key_state["down"] = False
        if status:
            self._set_status(status)

    def _apply_teleport(self, pos: object) -> None:
        if not isinstance(pos, list) or len(pos) != 3:
            return
        try:
            target = Vec3(float(pos[0]), float(pos[1]), float(pos[2]))
        except (TypeError, ValueError):
            return
        self.player_pos = target
        self.player_velocity = Vec3(0, 0, 0)
        self.vertical_velocity = 0.0
        self.grounded = True
        self._clear_movement_keys()
        self.last_udp_player_state = 0.0
        self._send_network_player_state()
        self._update_camera()
        self._set_status("Teleported by server")

    def _open_kicked_modal(self, reason: str) -> None:
        self.kicked_reason = reason
        self._cancel_world_load()
        self._clear_all_modal_panels()
        self._clear_movement_keys()
        self.set_mouse_capture(False)
        self.crosshair.hide()
        if self.network_client is not None:
            self.network_client.close_after_kick()
        self.ui_open = True
        self.modal_mode = "kicked"
        reason_text = reason if reason else "No reason was provided."
        self.kicked_panel = DirectFrame(
            frameColor=(0.05, 0.055, 0.064, 0.98),
            frameSize=(-0.86, 0.86, -0.34, 0.34),
            pos=(0, 0, 0),
        )
        DirectLabel(
            parent=self.kicked_panel,
            text="Removed From Server",
            text_fg=(1, 1, 1, 1),
            text_scale=0.056,
            frameColor=(0, 0, 0, 0),
            pos=(0, 0, 0.19),
        )
        DirectLabel(
            parent=self.kicked_panel,
            text=f"You have been kicked from the server.\nReason: {reason_text}",
            text_fg=(0.94, 0.96, 0.98, 1),
            text_scale=0.034,
            frameColor=(0, 0, 0, 0),
            pos=(0, 0, 0.03),
        )
        DirectButton(
            parent=self.kicked_panel,
            text="OK",
            text_scale=0.036,
            frameSize=(-0.16, 0.16, -0.055, 0.055),
            frameColor=(0.24, 0.29, 0.34, 1),
            text_fg=(1, 1, 1, 1),
            pos=(0, 0, -0.20),
            command=self._quit_after_kick,
        )
        self._set_status("Kicked from server")

    def _open_connect_refused_modal(self, reason: str) -> None:
        reason_text = reason if reason else "Server do not allow connect. (ALLOW_CONNECT = False)"
        self._cancel_world_load()
        self._clear_all_modal_panels()
        self._clear_movement_keys()
        self.set_mouse_capture(False)
        self.crosshair.hide()
        if self.network_client is not None:
            self.network_client.close_after_connect_refused()
        self.ui_open = True
        self.modal_mode = "connect_refused"
        self.connect_refused_panel = DirectFrame(
            frameColor=(0.05, 0.055, 0.064, 0.98),
            frameSize=(-0.86, 0.86, -0.34, 0.34),
            pos=(0, 0, 0),
        )
        DirectLabel(
            parent=self.connect_refused_panel,
            text="Connection Refused",
            text_fg=(1, 1, 1, 1),
            text_scale=0.056,
            frameColor=(0, 0, 0, 0),
            pos=(0, 0, 0.19),
        )
        DirectLabel(
            parent=self.connect_refused_panel,
            text=reason_text,
            text_fg=(0.94, 0.96, 0.98, 1),
            text_scale=0.034,
            frameColor=(0, 0, 0, 0),
            pos=(0, 0, 0.03),
        )
        DirectButton(
            parent=self.connect_refused_panel,
            text="OK",
            text_scale=0.036,
            frameSize=(-0.16, 0.16, -0.055, 0.055),
            frameColor=(0.24, 0.29, 0.34, 1),
            text_fg=(1, 1, 1, 1),
            pos=(0, 0, -0.20),
            command=self._quit_after_kick,
        )
        self._set_status("Connection refused")

    def _open_version_mismatch_modal(self, server_version: str, client_version: str) -> None:
        server_text = server_version.strip() or "unknown"
        client_text = client_version.strip() or "unknown"
        self._cancel_world_load()
        self._clear_all_modal_panels()
        self._clear_movement_keys()
        self.set_mouse_capture(False)
        self.crosshair.hide()
        if self.network_client is not None:
            self.network_client.close_after_version_mismatch()
        self.ui_open = True
        self.modal_mode = "version_mismatch"
        self.version_mismatch_panel = DirectFrame(
            frameColor=(0.05, 0.055, 0.064, 0.98),
            frameSize=(-0.86, 0.86, -0.36, 0.36),
            pos=(0, 0, 0),
        )
        DirectLabel(
            parent=self.version_mismatch_panel,
            text="Version Mismatch",
            text_fg=(1, 1, 1, 1),
            text_scale=0.056,
            frameColor=(0, 0, 0, 0),
            pos=(0, 0, 0.20),
        )
        DirectLabel(
            parent=self.version_mismatch_panel,
            text=f"Version mismatch.\nServer: {server_text}\nClient: {client_text}",
            text_fg=(0.94, 0.96, 0.98, 1),
            text_scale=0.034,
            frameColor=(0, 0, 0, 0),
            pos=(0, 0, 0.02),
        )
        DirectButton(
            parent=self.version_mismatch_panel,
            text="OK",
            text_scale=0.036,
            frameSize=(-0.16, 0.16, -0.055, 0.055),
            frameColor=(0.24, 0.29, 0.34, 1),
            text_fg=(1, 1, 1, 1),
            pos=(0, 0, -0.22),
            command=self._quit_after_kick,
        )
        self._set_status("Version mismatch")

    def _clear_all_modal_panels(self) -> None:
        self.taskMgr.remove("neko-mouse-world-editor-wait")
        self.taskMgr.remove(WORLD_LOAD_TASK_NAME)
        self.taskMgr.remove(STARTUP_MAXIMIZE_TASK_NAME)
        self.taskMgr.remove("neko-mouse-world-console-refocus")
        self._unbind_console_entry_change_events()
        for panel_name in (
            "help_panel",
            "quit_panel",
            "focus_pause_panel",
            "disconnect_panel",
            "console_panel",
            "connect_panel",
            "editor_wait_panel",
            "loading_panel",
            "kicked_panel",
            "connect_refused_panel",
            "version_mismatch_panel",
        ):
            panel = getattr(self, panel_name, None)
            if panel is not None:
                panel.destroy()
                setattr(self, panel_name, None)
        self.disconnect_message = None
        self.console_log_labels = []
        self.console_entry = None
        self.console_send_button = None
        self.console_input_scrollbar = None
        self.console_input_scroll_thumb = None
        self.console_input_scroll_buttons = []
        self.console_entry_event_names = []
        self.console_entry_wrapped_line_count = 1
        self.console_scrollbar = None
        self.console_scroll_thumb = None
        self.connect_host_entry = None
        self.connect_port_entry = None
        self.connect_error_label = None
        self.quit_button_frames = {}
        self.quit_buttons = {}

    def _quit_after_kick(self) -> None:
        raise SystemExit

    def _open_console(self) -> None:
        if self.ui_open or self.modal_mode is not None or self.world_load_job is not None:
            return
        if self.network_client is None:
            self._set_status("Server console is available only in client/server mode")
            return
        self._load_console_logs_from_network()
        self.ui_open = True
        self.modal_mode = "console"
        self._clear_movement_keys()
        self.set_mouse_capture(False)
        self.crosshair.hide()
        if self.console_panel is not None:
            self.console_panel.destroy()
        self.console_panel = DirectFrame(
            frameColor=(0.035, 0.04, 0.048, 0.97),
            frameSize=(-1.10, 1.10, -0.72, 0.72),
            pos=(0, 0, 0),
        )
        DirectLabel(
            parent=self.console_panel,
            text="Server Console",
            text_fg=(1, 1, 1, 1),
            text_scale=0.050,
            frameColor=(0, 0, 0, 0),
            pos=(-1.02, 0, 0.62),
            text_align=0,
        )
        self._make_console_close_button()
        DirectFrame(
            parent=self.console_panel,
            frameColor=(0.015, 0.018, 0.022, 1),
            frameSize=(-1.02, 0.94, -0.49, 0.52),
            pos=(-0.02, 0, 0.04),
        )
        self.console_log_labels = []
        for index in range(CONSOLE_VISIBLE_LINES):
            label = DirectLabel(
                parent=self.console_panel,
                text="",
                text_fg=(0.82, 0.90, 0.86, 1),
                text_scale=0.026,
                text_align=0,
                frameColor=(0, 0, 0, 0),
                pos=(-1.00, 0, 0.50 - index * 0.052),
            )
            self.console_log_labels.append(label)
        self._make_console_scrollbar()
        self.console_entry = DirectEntry(
            parent=self.console_panel,
            initialText="",
            text_scale=0.030,
            frameColor=(0.13, 0.15, 0.18, 1),
            text_fg=(1, 1, 1, 1),
            frameSize=(-0.02, 1.52, -0.052, 0.052),
            pos=(-1.02, 0, CONSOLE_INPUT_CENTER_Z),
            width=CONSOLE_INPUT_WRAP_WIDTH,
            numLines=CONSOLE_INPUT_MIN_LINES,
            focus=1,
            backgroundFocus=1,
            overflow=0,
            command=lambda _text: self._send_console_command(),
        )
        self.console_entry.guiItem.setMaxChars(CONSOLE_COMMAND_MAX_CHARS)
        self.console_entry.onscreenText.textNode.setWordwrap(CONSOLE_INPUT_WRAP_WIDTH)
        self._bind_console_entry_change_events()
        self.console_send_button = DirectButton(
            parent=self.console_panel,
            text="Send",
            text_scale=0.033,
            frameSize=(-0.14, 0.14, -0.052, 0.052),
            frameColor=(0.24, 0.29, 0.34, 1),
            text_fg=(1, 1, 1, 1),
            pos=(0.92, 0, CONSOLE_INPUT_CENTER_Z),
            command=self._send_console_command,
        )
        self._make_console_input_scrollbar()
        self._update_console_entry_layout()
        self._refresh_console_log_view()
        self._refocus_console_entry()
        self._set_status("Server console")

    def _make_console_close_button(self) -> None:
        if self.console_panel is None:
            return
        button = DirectButton(
            parent=self.console_panel,
            text="",
            frameSize=(-0.055, 0.055, -0.055, 0.055),
            frameColor=(0.18, 0.20, 0.23, 1),
            pos=(1.01, 0, 0.62),
            command=self._close_console,
        )
        lines = LineSegs()
        lines.setThickness(2.4)
        lines.setColor(0.94, 0.96, 0.98, 1)
        lines.moveTo(-0.022, 0, -0.022)
        lines.drawTo(0.022, 0, 0.022)
        lines.moveTo(-0.022, 0, 0.022)
        lines.drawTo(0.022, 0, -0.022)
        icon = NodePath(lines.create())
        icon.reparentTo(button)

    def _close_console(self) -> None:
        self.taskMgr.remove("neko-mouse-world-console-refocus")
        self._unbind_console_entry_change_events()
        if self.console_panel is not None:
            self.console_panel.destroy()
        self.console_panel = None
        self.console_log_labels = []
        self.console_entry = None
        self.console_send_button = None
        self.console_input_scrollbar = None
        self.console_input_scroll_thumb = None
        self.console_input_scroll_buttons = []
        self.console_entry_event_names = []
        self.console_entry_wrapped_line_count = 1
        self.console_scrollbar = None
        self.console_scroll_thumb = None
        self.ui_open = False
        self.modal_mode = None
        self._clear_movement_keys()
        self.set_mouse_capture(self._window_has_foreground())
        self._set_status("Ready")

    def _send_console_command(self) -> None:
        if self.modal_mode != "console" or self.console_entry is None:
            return
        command = self.console_entry.get().strip()
        if not command:
            self._refocus_console_entry()
            return
        self.console_entry.enterText("")
        if self.network_client is None or not self.network_client.connected:
            self._append_console_log({"line": f"not connected: {command}", "stream": "stderr"})
            self._refocus_console_entry()
            return
        self.network_client.send_server_command(command)
        self._update_console_entry_layout()
        self._refocus_console_entry()

    def _bind_console_entry_change_events(self) -> None:
        if self.console_entry is None:
            return
        self._unbind_console_entry_change_events()
        self.console_entry_event_names = [
            self.console_entry.guiItem.getTypeEvent(),
            self.console_entry.guiItem.getEraseEvent(),
            self.console_entry.guiItem.getCursormoveEvent(),
            self.console_entry.guiItem.getOverflowEvent(),
        ]
        for event_name in self.console_entry_event_names:
            self.accept(event_name, self._on_console_entry_changed)

    def _on_console_entry_changed(self, *_args) -> None:
        self._update_console_entry_layout()

    def _unbind_console_entry_change_events(self) -> None:
        for event_name in self.console_entry_event_names:
            self.ignore(event_name)
        self.console_entry_event_names = []

    def _update_console_entry_layout(self) -> None:
        if self.modal_mode != "console" or self.console_entry is None:
            return
        text = self.console_entry.get()
        wrapped_lines = self._console_entry_wrapped_lines(text)
        line_count = max(CONSOLE_INPUT_MIN_LINES, len(wrapped_lines))
        visible_lines = max(CONSOLE_INPUT_MIN_LINES, min(CONSOLE_INPUT_MAX_LINES, line_count))
        if self.console_entry["numLines"] != visible_lines:
            self.console_entry["numLines"] = visible_lines
        height = CONSOLE_INPUT_BASE_HEIGHT + (visible_lines - 1) * CONSOLE_INPUT_LINE_STEP
        center_z = CONSOLE_INPUT_BOTTOM + height * 0.5
        self.console_entry["frameSize"] = (-0.02, 1.52, -height * 0.5, height * 0.5)
        self.console_entry.setZ(center_z)
        if self.console_send_button is not None:
            self.console_send_button.setZ(center_z)
        self.console_entry_wrapped_line_count = line_count
        self._update_console_input_scrollbar(line_count, visible_lines)

    def _console_entry_wrapped_lines(self, text: str) -> list[str]:
        if not text:
            return [""]
        lines: list[str] = []
        for raw_line in text.splitlines() or [""]:
            line = raw_line or ""
            while len(line) > CONSOLE_INPUT_WRAP_CHARS:
                lines.append(line[:CONSOLE_INPUT_WRAP_CHARS])
                line = line[CONSOLE_INPUT_WRAP_CHARS:]
            lines.append(line)
        if text.endswith("\n"):
            lines.append("")
        return lines

    def _console_entry_cursor_line(self) -> int:
        if self.console_entry is None:
            return 0
        text = self.console_entry.get()
        cursor = max(0, min(len(text), self.console_entry.getCursorPosition()))
        return max(0, len(self._console_entry_wrapped_lines(text[:cursor])) - 1)

    def _make_console_input_scrollbar(self) -> None:
        if self.console_panel is None:
            return
        self.console_input_scrollbar = DirectFrame(
            parent=self.console_panel,
            frameColor=(0.10, 0.12, 0.15, 1),
            frameSize=(-0.010, 0.010, -0.052, 0.052),
            pos=(CONSOLE_INPUT_SCROLL_X, 0, CONSOLE_INPUT_CENTER_Z),
        )
        self.console_input_scroll_thumb = DirectFrame(
            parent=self.console_input_scrollbar,
            frameColor=(0.48, 0.54, 0.62, 1),
            frameSize=(-0.014, 0.014, -0.020, 0.020),
        )
        self.console_input_scroll_buttons = [
            DirectButton(
                parent=self.console_panel,
                text="^",
                text_scale=0.018,
                frameSize=(-0.018, 0.018, -0.018, 0.018),
                frameColor=(0.18, 0.21, 0.25, 1),
                text_fg=(0.95, 0.97, 1, 1),
                pos=(CONSOLE_INPUT_SCROLL_X, 0, CONSOLE_INPUT_CENTER_Z + 0.034),
                command=self._scroll_console_input_lines,
                extraArgs=[-1],
            ),
            DirectButton(
                parent=self.console_panel,
                text="v",
                text_scale=0.018,
                frameSize=(-0.018, 0.018, -0.018, 0.018),
                frameColor=(0.18, 0.21, 0.25, 1),
                text_fg=(0.95, 0.97, 1, 1),
                pos=(CONSOLE_INPUT_SCROLL_X, 0, CONSOLE_INPUT_CENTER_Z - 0.034),
                command=self._scroll_console_input_lines,
                extraArgs=[1],
            ),
        ]

    def _scroll_console_input_lines(self, delta: int) -> None:
        if self.console_entry is None:
            return
        cursor = self.console_entry.getCursorPosition()
        target = max(0, min(self.console_entry_wrapped_line_count - 1, self._console_entry_cursor_line() + delta))
        self.console_entry.setCursorPosition(self._console_entry_position_for_wrapped_line(target, cursor))
        self._update_console_entry_layout()
        self._refocus_console_entry()

    def _console_entry_position_for_wrapped_line(self, target_line: int, fallback: int) -> int:
        if self.console_entry is None:
            return fallback
        text = self.console_entry.get()
        if target_line <= 0:
            return 0
        current_line = 0
        column = 0
        for index, character in enumerate(text):
            if current_line >= target_line:
                return index
            column += 1
            if character == "\n" or column >= CONSOLE_INPUT_WRAP_CHARS:
                current_line += 1
                column = 0
        return len(text)

    def _update_console_input_scrollbar(self, line_count: int, visible_lines: int) -> None:
        if self.console_input_scrollbar is None or self.console_input_scroll_thumb is None:
            return
        height = CONSOLE_INPUT_BASE_HEIGHT + (visible_lines - 1) * CONSOLE_INPUT_LINE_STEP
        center_z = CONSOLE_INPUT_BOTTOM + height * 0.5
        self.console_input_scrollbar.setZ(center_z)
        self.console_input_scrollbar["frameSize"] = (-0.010, 0.010, -height * 0.5, height * 0.5)
        if self.console_input_scroll_buttons:
            self.console_input_scroll_buttons[0].setZ(center_z + height * 0.5 - 0.018)
            self.console_input_scroll_buttons[1].setZ(center_z - height * 0.5 + 0.018)
        should_show = line_count > visible_lines
        if not should_show:
            self.console_input_scrollbar.hide()
            for button in self.console_input_scroll_buttons:
                button.hide()
            return
        self.console_input_scrollbar.show()
        for button in self.console_input_scroll_buttons:
            button.show()
        visible_fraction = min(1.0, visible_lines / max(1, line_count))
        thumb_height = max(CONSOLE_INPUT_SCROLL_THUMB_MIN_HEIGHT, height * visible_fraction)
        travel = max(0.0, height - thumb_height)
        max_offset = max(1, line_count - visible_lines)
        cursor_line = min(max_offset, self._console_entry_cursor_line())
        offset_fraction = cursor_line / max_offset
        thumb_center = height * 0.5 - thumb_height * 0.5 - travel * offset_fraction
        self.console_input_scroll_thumb["frameSize"] = (-0.014, 0.014, -thumb_height * 0.5, thumb_height * 0.5)
        self.console_input_scroll_thumb.setZ(thumb_center)

    def _append_console_log(self, entry: object) -> None:
        if not self.permissions.get("allow_cmd", True):
            self._set_console_permission_denied()
            return
        normalized = self._normalize_console_log_entry(entry)
        if normalized["line"]:
            self.console_logs.append(normalized)
            self.console_logs = self.console_logs[-CONSOLE_LOG_LIMIT:]
        if self.modal_mode == "console":
            max_offset = max(0, len(self.console_logs) - CONSOLE_VISIBLE_LINES)
            at_bottom = self.console_scroll_offset >= max_offset - 1
            if at_bottom:
                self.console_scroll_offset = max_offset
            self._refresh_console_log_view()

    def _make_console_scrollbar(self) -> None:
        if self.console_panel is None:
            return
        DirectButton(
            parent=self.console_panel,
            text="^",
            text_scale=0.026,
            frameSize=(-0.035, 0.035, -0.030, 0.030),
            frameColor=(0.18, 0.21, 0.25, 1),
            text_fg=(0.95, 0.97, 1, 1),
            pos=(CONSOLE_SCROLL_X, 0, CONSOLE_SCROLL_TRACK_TOP + 0.045),
            command=self._scroll_console_lines,
            extraArgs=[-1],
        )
        DirectButton(
            parent=self.console_panel,
            text="v",
            text_scale=0.026,
            frameSize=(-0.035, 0.035, -0.030, 0.030),
            frameColor=(0.18, 0.21, 0.25, 1),
            text_fg=(0.95, 0.97, 1, 1),
            pos=(CONSOLE_SCROLL_X, 0, CONSOLE_SCROLL_TRACK_BOTTOM - 0.045),
            command=self._scroll_console_lines,
            extraArgs=[1],
        )
        self.console_scrollbar = DirectFrame(
            parent=self.console_panel,
            frameColor=(0.13, 0.15, 0.18, 1),
            frameSize=(-0.018, 0.018, CONSOLE_SCROLL_TRACK_BOTTOM, CONSOLE_SCROLL_TRACK_TOP),
            pos=(CONSOLE_SCROLL_X, 0, 0),
        )
        midpoint = (CONSOLE_SCROLL_TRACK_TOP + CONSOLE_SCROLL_TRACK_BOTTOM) * 0.5
        DirectButton(
            parent=self.console_panel,
            text="",
            frameSize=(-0.018, 0.018, midpoint, CONSOLE_SCROLL_TRACK_TOP),
            frameColor=(0, 0, 0, 0),
            pos=(CONSOLE_SCROLL_X, 0, 0),
            command=self._scroll_console_lines,
            extraArgs=[-CONSOLE_VISIBLE_LINES],
        )
        DirectButton(
            parent=self.console_panel,
            text="",
            frameSize=(-0.018, 0.018, CONSOLE_SCROLL_TRACK_BOTTOM, midpoint),
            frameColor=(0, 0, 0, 0),
            pos=(CONSOLE_SCROLL_X, 0, 0),
            command=self._scroll_console_lines,
            extraArgs=[CONSOLE_VISIBLE_LINES],
        )
        self.console_scroll_thumb = DirectFrame(
            parent=self.console_panel,
            frameColor=(0.42, 0.48, 0.55, 1),
            frameSize=(-0.024, 0.024, -0.04, 0.04),
            pos=(CONSOLE_SCROLL_X, 0, 0),
        )

    def _scroll_console_wheel(self, delta: int) -> None:
        if self.modal_mode == "console":
            self._scroll_console_lines(delta)

    def _scroll_console_lines(self, delta: int) -> None:
        if self.modal_mode != "console":
            return
        max_offset = max(0, len(self.console_logs) - CONSOLE_VISIBLE_LINES)
        self.console_scroll_offset = max(0, min(max_offset, self.console_scroll_offset + delta))
        self._refresh_console_log_view()
        self._refocus_console_entry()

    def _refresh_console_log_view(self) -> None:
        max_offset = max(0, len(self.console_logs) - CONSOLE_VISIBLE_LINES)
        self.console_scroll_offset = max(0, min(self.console_scroll_offset, max_offset))
        self._position_console_scroll_thumb()
        if self.console_log_labels:
            visible = self.console_logs[self.console_scroll_offset : self.console_scroll_offset + CONSOLE_VISIBLE_LINES]
            for index, label in enumerate(self.console_log_labels):
                if index < len(visible):
                    entry = visible[index]
                    label["text"] = entry["line"]
                    label["text_fg"] = (1.0, 0.42, 0.38, 1) if entry["stream"] == "stderr" else (0.82, 0.90, 0.86, 1)
                else:
                    label["text"] = ""

    def _refocus_console_entry(self) -> None:
        if self.modal_mode != "console" or self.console_entry is None:
            return
        try:
            self.console_entry["focus"] = 1
            self.console_entry.focusIn()
        except Exception:
            return
        self.taskMgr.remove("neko-mouse-world-console-refocus")
        self.taskMgr.doMethodLater(0.01, self._refocus_console_entry_task, "neko-mouse-world-console-refocus")

    def _refocus_console_entry_task(self, task):
        if self.modal_mode == "console" and self.console_entry is not None:
            try:
                self.console_entry["focus"] = 1
                self.console_entry.focusIn()
            except Exception:
                pass
        return task.done

    def _normalize_console_log_entry(self, entry: object) -> dict[str, str]:
        if isinstance(entry, dict):
            line = str(entry.get("line", ""))
            stream = "stderr" if str(entry.get("stream", "stdout")) == "stderr" else "stdout"
            return {"line": line, "stream": stream}
        return {"line": str(entry), "stream": "stdout"}

    def _load_console_logs_from_network(self) -> None:
        if self.network_client is None or not self.permissions.get("allow_cmd", True):
            self._set_console_permission_denied(refresh=False)
            return
        self.console_logs = [self._normalize_console_log_entry(entry) for entry in self.network_client.server_logs[-CONSOLE_LOG_LIMIT:]]
        self.console_scroll_offset = max(0, len(self.console_logs) - CONSOLE_VISIBLE_LINES)

    def _set_console_permission_denied(self, message: str = SERVER_LOG_PERMISSION_DENIED_LINE, refresh: bool = True) -> None:
        self.console_logs = [{"line": message or SERVER_LOG_PERMISSION_DENIED_LINE, "stream": "stderr"}]
        self.console_scroll_offset = 0
        if refresh and self.modal_mode == "console":
            self._refresh_console_log_view()

    def _apply_server_log_permission(self, allowed: bool, message: str = SERVER_LOG_PERMISSION_DENIED_LINE) -> None:
        if not allowed:
            self._set_console_permission_denied(message)
        elif self.modal_mode == "console" and self.network_client is not None:
            self.console_logs = [
                self._normalize_console_log_entry(entry)
                for entry in self.network_client.server_logs[-CONSOLE_LOG_LIMIT:]
            ]
            self.console_scroll_offset = max(0, len(self.console_logs) - CONSOLE_VISIBLE_LINES)
            self._refresh_console_log_view()

    def _position_console_scroll_thumb(self) -> None:
        if self.console_scroll_thumb is None:
            return
        total = max(1, len(self.console_logs))
        visible_fraction = min(1.0, CONSOLE_VISIBLE_LINES / total)
        track_height = CONSOLE_SCROLL_TRACK_TOP - CONSOLE_SCROLL_TRACK_BOTTOM
        thumb_height = max(CONSOLE_SCROLL_THUMB_MIN_HEIGHT, track_height * visible_fraction)
        travel = max(0.0, track_height - thumb_height)
        max_offset = max(0, len(self.console_logs) - CONSOLE_VISIBLE_LINES)
        offset_fraction = 0.0 if max_offset == 0 else self.console_scroll_offset / max_offset
        thumb_center = CONSOLE_SCROLL_TRACK_TOP - thumb_height * 0.5 - travel * offset_fraction
        self.console_scroll_thumb["frameSize"] = (-0.024, 0.024, -thumb_height * 0.5, thumb_height * 0.5)
        self.console_scroll_thumb.setZ(thumb_center)

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
        self.network_client = NetworkWorldClient(host, port, desired_user_id=self.default_user_id)
        self.network_spawn_pos = None
        self.network_client_loaded_sent = False
        self.permissions = dict(self.network_client.permissions)
        self.paths = self.network_client.paths
        self.surface_cache = BoxSurfaceCache(self.paths.boxes_dir)
        self.collision_cache = CollisionShapeCache(self.paths.boxes_dir)
        self.world_map = self.network_client.world_map
        self.saved_snapshot = ""
        self.set_mouse_capture(False)
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

    def _open_focus_pause(self) -> None:
        if self.modal_mode in {"editor_wait", "quit", "console", "kicked", "connect_refused", "version_mismatch"}:
            return
        if self.modal_mode == "focus_pause":
            self._position_focus_pause_panel()
            return
        self._clear_movement_keys()
        self.set_mouse_capture(False)
        self.ui_open = True
        self.modal_mode = "focus_pause"
        self.crosshair.hide()
        if self.focus_pause_panel is not None:
            self.focus_pause_panel.destroy()
        self.focus_pause_panel = DirectFrame(
            frameColor=(0.04, 0.05, 0.06, 0.96),
            frameSize=(-0.46, 0.46, -0.22, 0.22),
        )
        self._position_focus_pause_panel()
        DirectLabel(
            parent=self.focus_pause_panel,
            text="Game Paused",
            text_fg=(1, 1, 1, 1),
            text_scale=0.046,
            frameColor=(0, 0, 0, 0),
            pos=(0, 0, 0.095),
        )
        DirectLabel(
            parent=self.focus_pause_panel,
            text="Click Continue to resume.",
            text_fg=(0.86, 0.90, 0.94, 1),
            text_scale=0.027,
            frameColor=(0, 0, 0, 0),
            pos=(0, 0, 0.02),
        )
        DirectButton(
            parent=self.focus_pause_panel,
            text="Continue",
            text_scale=0.033,
            frameSize=(-0.15, 0.15, -0.048, 0.048),
            frameColor=(0.24, 0.29, 0.34, 1),
            text_fg=(1, 1, 1, 1),
            pos=(0, 0, -0.11),
            command=self._resume_from_focus_pause,
        )
        self._set_status("Paused")

    def _position_focus_pause_panel(self) -> None:
        if self.focus_pause_panel is None:
            return
        aspect = max(0.1, float(self.getAspectRatio()) if hasattr(self, "getAspectRatio") else 1.0)
        left = -aspect
        bottom = -1.0
        margin = 0.035
        frame_left, frame_right, frame_bottom, frame_top = -0.46, 0.46, -0.22, 0.22
        width = frame_right - frame_left
        height = frame_top - frame_bottom
        scale = min(1.0, max(0.45, (aspect - margin * 2.0) / width), (1.0 - margin * 2.0) / height)
        self.focus_pause_panel.setScale(scale)
        x = left + margin - frame_left * scale
        z = bottom + margin - frame_bottom * scale
        self.focus_pause_panel.setPos(x, 0, z)

    def _resume_from_focus_pause(self) -> None:
        if self.modal_mode != "focus_pause":
            return
        if not self._window_has_foreground():
            self.set_mouse_capture(False)
            self._set_status("Paused; click the game window first")
            return
        if self.focus_pause_panel is not None:
            self.focus_pause_panel.destroy()
        self.focus_pause_panel = None
        self.ui_open = False
        self.modal_mode = None
        self._clear_movement_keys()
        if self.world_load_job is None:
            self.set_mouse_capture(True)
        self._set_status("Ready")

    def _release_mouse_capture(self) -> None:
        if self.modal_mode in {"kicked", "connect_refused", "version_mismatch"}:
            self._quit_after_kick()
            return
        if self.modal_mode == "focus_pause":
            return
        if self.world_load_job is not None:
            return
        if self.modal_mode == "editor_wait":
            self._focus_editor_if_waiting()
            return
        if self.modal_mode == "quit":
            self._close_quit_confirm()
            return
        if self.modal_mode == "help":
            self._close_help()
            return
        if self.modal_mode == "console":
            self._close_console()
            return
        if self.modal_mode == "connect":
            self._request_quit()
            return
        self._open_quit_confirm()

    def set_mouse_capture(self, captured: bool) -> None:
        if captured and self.world_load_job is not None:
            captured = False
        if captured and not self._window_has_foreground():
            captured = False
        self.mouse_captured = captured
        if captured:
            self.ime_disabled = disable_ime_for_window(self.win) or self.ime_disabled
        props = WindowProperties()
        props.setCursorHidden(captured)
        if hasattr(self.win, "requestProperties"):
            self.win.requestProperties(props)
        if captured:
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
            self._position_focus_pause_panel()
            self.ime_disabled = disable_ime_for_window(self.win) or self.ime_disabled
            if self.world_load_job is not None:
                return
            if self.modal_mode != "quit" and not self._window_has_foreground():
                self._open_focus_pause()

    def _sync_camera_aspect(self) -> None:
        if not self.win or not hasattr(self.win, "getXSize") or not hasattr(self.win, "getYSize"):
            return
        width = max(1, self.win.getXSize())
        height = max(1, self.win.getYSize())
        self.camLens.setAspectRatio(width / height)

    def _check_foreground_pause(self) -> None:
        if self.world_load_job is not None:
            return
        if self.modal_mode in {
            "editor_wait",
            "quit",
            "help",
            "console",
            "connect",
            "kicked",
            "connect_refused",
            "version_mismatch",
        }:
            return
        if not self._window_has_foreground():
            self._open_focus_pause()

    def _window_has_foreground(self) -> bool:
        if self.win is None:
            return True
        props = self.win.getProperties() if hasattr(self.win, "getProperties") else None
        if props is not None and hasattr(props, "hasForeground") and props.hasForeground():
            return bool(props.getForeground())
        if props is not None and hasattr(props, "has_foreground") and props.has_foreground():
            return bool(props.get_foreground())
        return True

    def userExit(self) -> None:
        self._request_quit()

    def _request_quit(self) -> None:
        if self.modal_mode in {"kicked", "connect_refused", "version_mismatch"}:
            self._quit_after_kick()
            return
        if self.modal_mode == "editor_wait":
            self._focus_editor_if_waiting()
            return
        if self.modal_mode == "focus_pause":
            if self.focus_pause_panel is not None:
                self.focus_pause_panel.destroy()
            self.focus_pause_panel = None
            self.ui_open = False
            self.modal_mode = None
        if self.world_load_job is not None:
            self._quit_now()
            return
        if self.modal_mode == "quit":
            return
        if self.modal_mode == "help":
            self._close_help()
        if self.modal_mode == "console":
            self._close_console()
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
        if self.focus_pause_panel is not None:
            self.focus_pause_panel.destroy()
        self.focus_pause_panel = None
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

        self._quit_button("quit", "Quit", (-0.22, 0, -0.17), self._quit_now)
        self._quit_button("cancel", "Cancel", (0.22, 0, -0.17), self._close_quit_confirm)
        self._sync_quit_button_highlight()
        self._set_status("Exit world")

    def _quit_confirm_message(self) -> str:
        if self.network_client is not None:
            return "Do you want to leave the server?"
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
        order = ["quit", "cancel"]
        index = order.index(self.active_quit_choice) if self.active_quit_choice in order else len(order) - 1
        self.active_quit_choice = order[(index + direction) % len(order)]
        self._sync_quit_button_highlight()

    def _sync_quit_button_highlight(self) -> None:
        for choice, frame in self.quit_button_frames.items():
            frame["frameColor"] = (1.0, 0.88, 0.18, 1.0) if choice == self.active_quit_choice else (0.18, 0.21, 0.25, 1)

    def _submit_modal(self) -> None:
        if self.modal_mode in {"kicked", "connect_refused", "version_mismatch"}:
            self._quit_after_kick()
            return
        if self.modal_mode == "focus_pause":
            return
        if self.modal_mode == "help":
            self._close_help()
        elif self.modal_mode == "quit":
            self._activate_quit_choice()
        elif self.modal_mode == "connect":
            self._submit_connect_dialog()
        elif self.modal_mode == "console":
            self._send_console_command()

    def _activate_quit_choice(self) -> None:
        if self.active_quit_choice == "quit":
            self._quit_now()
        else:
            self._close_quit_confirm()

    def _save_and_quit(self) -> None:
        if self.network_client is not None:
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
        self.set_mouse_capture(self.quit_restore_mouse_capture and self._window_has_foreground())
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
            self.world_map.set_box(cell, digest, self.selected_orientation)
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


def _panda_window_hwnd(window) -> int | None:
    if window is None or not hasattr(window, "getWindowHandle"):
        return None
    try:
        handle = window.getWindowHandle()
    except Exception:
        return None
    if handle is None:
        return None
    for method_name in ("getIntHandle", "get_int_handle"):
        method = getattr(handle, method_name, None)
        if method is None:
            continue
        try:
            hwnd = int(method())
        except (TypeError, ValueError):
            continue
        if hwnd > 0:
            return hwnd
    return None


def _maximize_windows_hwnd(hwnd: int) -> bool:
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        get_ancestor = user32.GetAncestor
        get_ancestor.argtypes = (ctypes.c_void_p, ctypes.c_uint)
        get_ancestor.restype = ctypes.c_void_p
        show_window = user32.ShowWindow
        show_window.argtypes = (ctypes.c_void_p, ctypes.c_int)
        show_window.restype = ctypes.c_bool
        is_zoomed = user32.IsZoomed
        is_zoomed.argtypes = (ctypes.c_void_p,)
        is_zoomed.restype = ctypes.c_bool
        root = int(get_ancestor(ctypes.c_void_p(hwnd), 2) or hwnd)
        target = ctypes.c_void_p(root)
        show_window(target, 3)
        return bool(is_zoomed(target))
    except Exception:
        return False


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
