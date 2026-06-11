from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import sqlite3
import time
from typing import Mapping

from .orientation import IDENTITY_ORIENTATION, validate_orientation as _validate_orientation


Cell = tuple[int, int, int]
PlayerPosition = tuple[float, float, float]

WORLD_SCHEMA_VERSION = 3
WORLD_FILE_NAME = "info.world"
BOXES_DIR_NAME = "boxes"


class WorldFormatError(ValueError):
    """Raised when an info.world SQLite database is not valid."""


@dataclass
class WorldMap:
    boxes: dict[Cell, str] = field(default_factory=dict)
    orientations: dict[Cell, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized: dict[Cell, str] = {}
        normalized_orientations: dict[Cell, int] = {}
        for cell, digest in self.boxes.items():
            normalized_cell = validate_cell(cell)
            normalized[normalized_cell] = validate_digest(digest)
            normalized_orientations[normalized_cell] = validate_world_orientation(
                self.orientations.get(cell, IDENTITY_ORIENTATION)
            )
        self.boxes = normalized
        self.orientations = normalized_orientations

    def set_box(self, cell: Cell, digest: str, orientation: int | None = None) -> None:
        normalized_cell = validate_cell(cell)
        self.boxes[normalized_cell] = validate_digest(digest)
        if orientation is None:
            self.orientations.setdefault(normalized_cell, IDENTITY_ORIENTATION)
        else:
            self.orientations[normalized_cell] = validate_world_orientation(orientation)

    def remove_box(self, cell: Cell) -> bool:
        normalized_cell = validate_cell(cell)
        self.orientations.pop(normalized_cell, None)
        return self.boxes.pop(normalized_cell, None) is not None

    def get_box(self, cell: Cell) -> str | None:
        return self.boxes.get(validate_cell(cell))

    def get_orientation(self, cell: Cell) -> int:
        return self.orientations.get(validate_cell(cell), IDENTITY_ORIENTATION)

    def set_orientation(self, cell: Cell, orientation: int) -> None:
        normalized_cell = validate_cell(cell)
        if normalized_cell not in self.boxes:
            raise WorldFormatError(f"cannot set orientation for empty cell {normalized_cell}")
        self.orientations[normalized_cell] = validate_world_orientation(orientation)


@dataclass(frozen=True)
class WorldPaths:
    root: Path
    info_file: Path
    boxes_dir: Path


@dataclass(frozen=True)
class LoadedWorld:
    paths: WorldPaths
    world_map: WorldMap
    player_positions: dict[str, PlayerPosition] = field(default_factory=dict)
    removed_missing_refs: int = 0


def validate_cell(cell: Cell) -> Cell:
    if len(cell) != 3:
        raise WorldFormatError("cell coordinates must have three values")
    try:
        return tuple(int(part) for part in cell)  # type: ignore[return-value]
    except (TypeError, ValueError) as exc:
        raise WorldFormatError("cell coordinates must be integers") from exc


def validate_digest(digest: object) -> str:
    text = str(digest)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise WorldFormatError(f"invalid box hash {text!r}")
    return text


def validate_world_orientation(value: object) -> int:
    try:
        return _validate_orientation(value)
    except ValueError as exc:
        raise WorldFormatError("orientation must be an integer from 0 to 23") from exc


def world_paths(root: str | Path) -> WorldPaths:
    root_path = Path(root).resolve()
    return WorldPaths(
        root=root_path,
        info_file=root_path / WORLD_FILE_NAME,
        boxes_dir=root_path / BOXES_DIR_NAME,
    )


def load_or_create_world(root: str | Path) -> LoadedWorld:
    paths = world_paths(root)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.boxes_dir.mkdir(parents=True, exist_ok=True)

    if not paths.info_file.exists():
        world_map = WorldMap()
        save_world(paths.info_file, world_map)
        return LoadedWorld(paths=paths, world_map=world_map)

    world_map, player_positions = load_world_data(paths.info_file)
    removed = remove_missing_box_references(world_map, paths.boxes_dir)
    if removed:
        save_world(paths.info_file, world_map, player_positions)
    return LoadedWorld(paths=paths, world_map=world_map, player_positions=player_positions, removed_missing_refs=removed)


def remove_missing_box_references(world_map: WorldMap, boxes_dir: Path) -> int:
    missing = [
        cell
        for cell, digest in world_map.boxes.items()
        if not box_path_for_hash(boxes_dir, digest).is_file()
    ]
    for cell in missing:
        world_map.remove_box(cell)
    return len(missing)


def box_path_for_hash(boxes_dir: Path, digest: str) -> Path:
    return boxes_dir / f"{validate_digest(digest)}.box"


def load_world(path: str | Path) -> WorldMap:
    world_map, _player_positions = load_world_data(path)
    return world_map


def load_world_data(path: str | Path) -> tuple[WorldMap, dict[str, PlayerPosition]]:
    file_path = Path(path)
    try:
        connection = sqlite3.connect(file_path)
        try:
            connection.row_factory = sqlite3.Row
            return _load_world_from_connection(connection, file_path)
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        raise WorldFormatError(f"{file_path} is not a valid SQLite info.world file") from exc


def save_world(
    path: str | Path,
    world_map: WorldMap,
    player_positions: Mapping[str, PlayerPosition] | None = None,
) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    if player_positions is None:
        player_positions = _load_existing_player_positions(file_path)
    temp_path = file_path.with_name(f"{file_path.name}.tmp")
    if temp_path.exists():
        temp_path.unlink()

    try:
        connection = sqlite3.connect(temp_path)
        try:
            _configure_connection(connection)
            _create_schema(connection)
            _write_world_map(connection, world_map, player_positions)
            connection.commit()
        finally:
            connection.close()
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise
    _replace_file(temp_path, file_path)


def _load_world_from_connection(connection: sqlite3.Connection, path: Path) -> tuple[WorldMap, dict[str, PlayerPosition]]:
    tables = _table_names(connection)
    _require_tables(tables, {"metadata"}, path)
    schema_version = _read_schema_version(connection, path)
    if schema_version != WORLD_SCHEMA_VERSION:
        raise WorldFormatError(
            f"{path} uses info.world schema version {schema_version}; expected {WORLD_SCHEMA_VERSION}"
        )
    _require_tables(tables, {"boxes"}, path)

    boxes: dict[Cell, str] = {}
    orientations: dict[Cell, int] = {}
    _require_columns(connection, "boxes", {"x", "y", "z", "hash", "orientation"}, path)
    rows = connection.execute("SELECT x, y, z, hash, orientation FROM boxes ORDER BY x, y, z").fetchall()
    for row in rows:
        cell = validate_cell((row["x"], row["y"], row["z"]))
        boxes[cell] = validate_digest(row["hash"])
        orientations[cell] = validate_world_orientation(row["orientation"])
    player_positions = _load_player_positions(connection, tables, path)
    return WorldMap(boxes=boxes, orientations=orientations), player_positions


def _load_player_positions(
    connection: sqlite3.Connection,
    tables: set[str],
    path: Path,
) -> dict[str, PlayerPosition]:
    _require_tables(tables, {"player_positions"}, path)
    _require_columns(connection, "player_positions", {"user_id", "x", "y", "z"}, path)
    rows = connection.execute("SELECT user_id, x, y, z FROM player_positions ORDER BY user_id").fetchall()
    positions: dict[str, PlayerPosition] = {}
    for row in rows:
        user_id = validate_position_user_id(row["user_id"])
        positions[user_id] = validate_player_position((row["x"], row["y"], row["z"]))
    return positions


def _configure_connection(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        ) WITHOUT ROWID;

        CREATE TABLE boxes (
            x INTEGER NOT NULL,
            y INTEGER NOT NULL,
            z INTEGER NOT NULL,
            hash TEXT NOT NULL,
            orientation INTEGER NOT NULL DEFAULT 0 CHECK (orientation BETWEEN 0 AND 23),
            PRIMARY KEY (x, y, z)
        ) WITHOUT ROWID;

        CREATE TABLE player_positions (
            user_id TEXT PRIMARY KEY,
            x REAL NOT NULL,
            y REAL NOT NULL,
            z REAL NOT NULL
        ) WITHOUT ROWID;
        """
    )


def _write_world_map(
    connection: sqlite3.Connection,
    world_map: WorldMap,
    player_positions: Mapping[str, PlayerPosition],
) -> None:
    normalized = WorldMap(boxes=world_map.boxes, orientations=world_map.orientations)
    normalized_player_positions = {
        validate_position_user_id(user_id): validate_player_position(position)
        for user_id, position in player_positions.items()
    }
    connection.executemany(
        "INSERT INTO metadata (key, value) VALUES (?, ?)",
        (("schema_version", str(WORLD_SCHEMA_VERSION)),),
    )
    connection.executemany(
        "INSERT INTO boxes (x, y, z, hash, orientation) VALUES (?, ?, ?, ?, ?)",
        (
            (cell[0], cell[1], cell[2], digest, normalized.get_orientation(cell))
            for cell, digest in sorted(normalized.boxes.items())
        ),
    )
    connection.executemany(
        "INSERT INTO player_positions (user_id, x, y, z) VALUES (?, ?, ?, ?)",
        (
            (user_id, position[0], position[1], position[2])
            for user_id, position in sorted(normalized_player_positions.items())
        ),
    )


def _load_existing_player_positions(path: Path) -> dict[str, PlayerPosition]:
    if not path.exists():
        return {}
    try:
        _world_map, player_positions = load_world_data(path)
        return player_positions
    except WorldFormatError:
        return {}


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _require_columns(connection: sqlite3.Connection, table: str, required: set[str], path: Path) -> None:
    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    missing = required - columns
    if missing:
        raise WorldFormatError(f"{path} table {table} is missing columns: {', '.join(sorted(missing))}")


def _require_tables(tables: set[str], required: set[str], path: Path) -> None:
    missing = required - tables
    if missing:
        raise WorldFormatError(f"{path} is missing SQLite info.world tables: {', '.join(sorted(missing))}")


def _read_schema_version(connection: sqlite3.Connection, path: Path) -> int:
    version = _read_metadata(connection, "schema_version", path)
    try:
        return int(version)
    except ValueError as exc:
        raise WorldFormatError(f"{path} has invalid info.world schema version {version}") from exc


def _read_metadata(connection: sqlite3.Connection, key: str, path: Path) -> str:
    row = connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    if row is None:
        raise WorldFormatError(f"{path} is missing metadata key {key}")
    return str(row[0])


def validate_position_user_id(value: object) -> str:
    text = str(value).strip()
    if not text:
        raise WorldFormatError("player position user_id cannot be empty")
    return text


def validate_player_position(position: object) -> PlayerPosition:
    if not isinstance(position, (list, tuple)) or len(position) != 3:
        raise WorldFormatError("player position must have three numeric values")
    try:
        values = (float(position[0]), float(position[1]), float(position[2]))
    except (TypeError, ValueError) as exc:
        raise WorldFormatError("player position must have numeric values") from exc
    if not all(math.isfinite(value) for value in values):
        raise WorldFormatError("player position values must be finite")
    return values


def _replace_file(source: Path, target: Path) -> None:
    last_error: PermissionError | None = None
    for _ in range(10):
        try:
            os.replace(source, target)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.05)
    if last_error is not None:
        raise last_error
