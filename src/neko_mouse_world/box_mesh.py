from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from panda3d.core import (
    Geom,
    GeomNode,
    GeomTriangles,
    GeomVertexData,
    GeomVertexFormat,
    GeomVertexWriter,
    NodePath,
    TransparencyAttrib,
)

from box_editor_view.box_file import BoxMap, RGBA, load_box
from box_editor_view.geometry import FaceNormal
from box_editor_view.voxel_mesh import visible_faces_for_cell

from .orientation import IDENTITY_ORIENTATION, rotate_normal, rotate_outer_face, rotate_point
from .world_file import Cell, box_path_for_hash


WORLD_CHUNK_SIZE = 16
FACE_NORMALS: tuple[FaceNormal, ...] = (
    (1, 0, 0),
    (-1, 0, 0),
    (0, 1, 0),
    (0, -1, 0),
    (0, 0, 1),
    (0, 0, -1),
)
OPAQUE_OCCUPANCY_THRESHOLD = 0.999

ChunkKey = tuple[int, int, int]
Quad = tuple[FaceNormal, tuple[tuple[float, float, float], ...], RGBA, FaceNormal | None]


@dataclass(frozen=True)
class BoxSurface:
    opaque_quads: tuple[Quad, ...]
    transparent_quads: tuple[Quad, ...]
    opaque_boundary_faces: frozenset[FaceNormal]
    blocks: int


@dataclass(frozen=True)
class WorldChunkMesh:
    opaque: NodePath | None
    transparent: NodePath | None
    cells: int
    quads: int


class BoxSurfaceCache:
    def __init__(self, boxes_dir: Path) -> None:
        self.boxes_dir = boxes_dir
        self._surfaces: dict[str, BoxSurface] = {}

    def get(self, digest: str) -> BoxSurface:
        surface = self._surfaces.get(digest)
        if surface is not None:
            return surface
        path = box_path_for_hash(self.boxes_dir, digest)
        surface = build_box_surface_from_file(path)
        self._surfaces[digest] = surface
        return surface

    def invalidate(self, digest: str | None = None) -> None:
        if digest is None:
            self._surfaces.clear()
        else:
            self._surfaces.pop(digest, None)


class _MeshBuilder:
    def __init__(self, name: str) -> None:
        self.vertex_data = GeomVertexData(name, GeomVertexFormat.getV3n3c4(), Geom.UHStatic)
        self.vertex_writer = GeomVertexWriter(self.vertex_data, "vertex")
        self.normal_writer = GeomVertexWriter(self.vertex_data, "normal")
        self.color_writer = GeomVertexWriter(self.vertex_data, "color")
        self.triangles = GeomTriangles(Geom.UHStatic)
        self.vertex_index = 0
        self.quads = 0

    def add_quad(
        self,
        vertices: tuple[tuple[float, float, float], ...],
        normal: FaceNormal,
        color: RGBA,
        offset: Cell = (0, 0, 0),
    ) -> None:
        ox, oy, oz = offset
        for vertex in vertices:
            self.vertex_writer.addData3f(vertex[0] + ox, vertex[1] + oy, vertex[2] + oz)
            self.normal_writer.addData3f(*normal)
            self.color_writer.addData4f(*color)
        self.triangles.addVertices(self.vertex_index, self.vertex_index + 1, self.vertex_index + 2)
        self.triangles.addVertices(self.vertex_index, self.vertex_index + 2, self.vertex_index + 3)
        self.vertex_index += 4
        self.quads += 1

    def to_node(self) -> NodePath | None:
        if self.vertex_index == 0:
            return None
        geom = Geom(self.vertex_data)
        geom.addPrimitive(self.triangles)
        node = GeomNode(self.vertex_data.getName())
        node.addGeom(geom)
        return NodePath(node)


def chunk_key_for_cell(cell: Cell) -> ChunkKey:
    return (
        cell[0] // WORLD_CHUNK_SIZE,
        cell[1] // WORLD_CHUNK_SIZE,
        cell[2] // WORLD_CHUNK_SIZE,
    )


def neighbor_cells(cell: Cell) -> tuple[Cell, ...]:
    return tuple((cell[0] + normal[0], cell[1] + normal[1], cell[2] + normal[2]) for normal in FACE_NORMALS)


def chunk_keys_for_cell_and_neighbors(cell: Cell) -> set[ChunkKey]:
    return {chunk_key_for_cell(candidate) for candidate in (cell, *neighbor_cells(cell))}


def build_box_surface_from_file(path: Path) -> BoxSurface:
    return build_box_surface(load_box(path))


def build_box_surface(box_map: BoxMap) -> BoxSurface:
    size = float(box_map.size)
    unit = 1.0 / size
    groups: dict[tuple[bool, FaceNormal, int, RGBA], set[tuple[int, int]]] = {}
    for cell, color in box_map.boxes.items():
        visible = visible_faces_for_cell(cell, color, box_map.boxes)
        for normal in visible:
            plane, u, v = _face_plane_uv(cell, normal)
            transparent = color[3] < 1.0
            groups.setdefault((transparent, normal, plane, color), set()).add((u, v))

    opaque_quads: list[Quad] = []
    transparent_quads: list[Quad] = []
    for (transparent, normal, plane, color), positions in groups.items():
        target = transparent_quads if transparent else opaque_quads
        for u0, v0, u1, v1 in _greedy_rectangles(positions):
            target.append(
                (
                    normal,
                    _scaled_quad_vertices(normal, plane, u0, v0, u1, v1, unit),
                    color,
                    _outer_face_for_plane(normal, plane, box_map.size),
                )
            )

    return BoxSurface(
        opaque_quads=tuple(opaque_quads),
        transparent_quads=tuple(transparent_quads),
        opaque_boundary_faces=_opaque_full_boundary_faces(box_map),
        blocks=len(box_map.boxes),
    )


def build_world_chunk_mesh(
    world_boxes: Mapping[Cell, str],
    world_orientations: Mapping[Cell, int],
    surface_cache: BoxSurfaceCache,
    chunk_key: ChunkKey,
    chunk_cells: Iterable[Cell] | None = None,
) -> WorldChunkMesh:
    cells = chunk_cells if chunk_cells is not None else world_boxes.keys()
    chunk_entries = [
        (cell, digest)
        for cell in cells
        if (digest := world_boxes.get(cell)) is not None
        if chunk_key_for_cell(cell) == chunk_key
    ]
    if not chunk_entries:
        return WorldChunkMesh(None, None, cells=0, quads=0)

    opaque = _MeshBuilder(f"world-chunk-{chunk_key[0]}-{chunk_key[1]}-{chunk_key[2]}-opaque")
    transparent = _MeshBuilder(f"world-chunk-{chunk_key[0]}-{chunk_key[1]}-{chunk_key[2]}-transparent")

    for cell, digest in chunk_entries:
        surface = surface_cache.get(digest)
        orientation = world_orientations.get(cell, IDENTITY_ORIENTATION)
        hidden_faces = _hidden_world_faces(cell, world_boxes, world_orientations, surface_cache)
        for normal, vertices, color, outer_face in surface.opaque_quads:
            rotated_outer_face = rotate_outer_face(outer_face, orientation)
            if rotated_outer_face not in hidden_faces:
                opaque.add_quad(_rotate_vertices(vertices, orientation), rotate_normal(normal, orientation), color, offset=cell)
        for normal, vertices, color, outer_face in surface.transparent_quads:
            rotated_outer_face = rotate_outer_face(outer_face, orientation)
            if rotated_outer_face not in hidden_faces:
                transparent.add_quad(
                    _rotate_vertices(vertices, orientation),
                    rotate_normal(normal, orientation),
                    color,
                    offset=cell,
                )

    opaque_node = opaque.to_node()
    transparent_node = transparent.to_node()
    if transparent_node:
        transparent_node.setTransparency(TransparencyAttrib.MAlpha)
        transparent_node.setBin("transparent", 0)
        transparent_node.setDepthWrite(False)
    return WorldChunkMesh(
        opaque=opaque_node,
        transparent=transparent_node,
        cells=len(chunk_entries),
        quads=opaque.quads + transparent.quads,
    )


def _hidden_world_faces(
    cell: Cell,
    world_boxes: Mapping[Cell, str],
    world_orientations: Mapping[Cell, int],
    surface_cache: BoxSurfaceCache,
) -> set[FaceNormal]:
    hidden: set[FaceNormal] = set()
    current_digest = world_boxes[cell]
    current_surface = surface_cache.get(current_digest)
    current_orientation = world_orientations.get(cell, IDENTITY_ORIENTATION)
    for normal in FACE_NORMALS:
        neighbor = (cell[0] + normal[0], cell[1] + normal[1], cell[2] + normal[2])
        neighbor_digest = world_boxes.get(neighbor)
        if neighbor_digest is None:
            continue
        opposite = (-normal[0], -normal[1], -normal[2])
        neighbor_surface = surface_cache.get(neighbor_digest)
        neighbor_orientation = world_orientations.get(neighbor, IDENTITY_ORIENTATION)
        current_faces = {
            rotate_normal(face, current_orientation)
            for face in current_surface.opaque_boundary_faces
        }
        neighbor_faces = {
            rotate_normal(face, neighbor_orientation)
            for face in neighbor_surface.opaque_boundary_faces
        }
        if normal in current_faces and opposite in neighbor_faces:
            hidden.add(normal)
    return hidden


def _rotate_vertices(vertices: tuple[tuple[float, float, float], ...], orientation: int) -> tuple[tuple[float, float, float], ...]:
    if orientation == IDENTITY_ORIENTATION:
        return vertices
    return tuple(rotate_point(vertex, orientation) for vertex in vertices)


def _opaque_full_boundary_faces(box_map: BoxMap) -> frozenset[FaceNormal]:
    opaque_cells = {cell for cell, color in box_map.boxes.items() if color[3] >= OPAQUE_OCCUPANCY_THRESHOLD}
    size = box_map.size
    faces: set[FaceNormal] = set()

    if _full_plane(opaque_cells, ((size - 1, y, z) for y in range(size) for z in range(size))):
        faces.add((1, 0, 0))
    if _full_plane(opaque_cells, ((0, y, z) for y in range(size) for z in range(size))):
        faces.add((-1, 0, 0))
    if _full_plane(opaque_cells, ((x, size - 1, z) for x in range(size) for z in range(size))):
        faces.add((0, 1, 0))
    if _full_plane(opaque_cells, ((x, 0, z) for x in range(size) for z in range(size))):
        faces.add((0, -1, 0))
    if _full_plane(opaque_cells, ((x, y, size - 1) for x in range(size) for y in range(size))):
        faces.add((0, 0, 1))
    if _full_plane(opaque_cells, ((x, y, 0) for x in range(size) for y in range(size))):
        faces.add((0, 0, -1))
    return frozenset(faces)


def _full_plane(opaque_cells: set[Cell], cells: Iterable[Cell]) -> bool:
    return all(cell in opaque_cells for cell in cells)


def _outer_face_for_plane(normal: FaceNormal, plane: int, size: int) -> FaceNormal | None:
    if normal in ((1, 0, 0), (0, 1, 0), (0, 0, 1)):
        return normal if plane == size else None
    return normal if plane == 0 else None


def _face_plane_uv(cell: Cell, normal: FaceNormal) -> tuple[int, int, int]:
    x, y, z = cell
    if normal == (1, 0, 0):
        return x + 1, y, z
    if normal == (-1, 0, 0):
        return x, y, z
    if normal == (0, 1, 0):
        return y + 1, x, z
    if normal == (0, -1, 0):
        return y, x, z
    if normal == (0, 0, 1):
        return z + 1, x, y
    return z, x, y


def _greedy_rectangles(positions: set[tuple[int, int]]) -> list[tuple[int, int, int, int]]:
    remaining = set(positions)
    rectangles: list[tuple[int, int, int, int]] = []
    while remaining:
        u0, v0 = min(remaining, key=lambda item: (item[1], item[0]))
        width = 1
        while (u0 + width, v0) in remaining:
            width += 1

        height = 1
        while all((u, v0 + height) in remaining for u in range(u0, u0 + width)):
            height += 1

        for u in range(u0, u0 + width):
            for v in range(v0, v0 + height):
                remaining.remove((u, v))
        rectangles.append((u0, v0, u0 + width, v0 + height))
    return rectangles


def _scaled_quad_vertices(
    normal: FaceNormal,
    plane: int,
    u0: int,
    v0: int,
    u1: int,
    v1: int,
    scale: float,
) -> tuple[tuple[float, float, float], ...]:
    p = float(plane) * scale
    a = float(u0) * scale
    b = float(v0) * scale
    c = float(u1) * scale
    d = float(v1) * scale

    if normal == (1, 0, 0):
        return ((p, a, b), (p, c, b), (p, c, d), (p, a, d))
    if normal == (-1, 0, 0):
        return ((p, a, b), (p, a, d), (p, c, d), (p, c, b))
    if normal == (0, 1, 0):
        return ((a, p, b), (a, p, d), (c, p, d), (c, p, b))
    if normal == (0, -1, 0):
        return ((a, p, b), (c, p, b), (c, p, d), (a, p, d))
    if normal == (0, 0, 1):
        return ((a, b, p), (c, b, p), (c, d, p), (a, d, p))
    return ((a, b, p), (a, d, p), (c, d, p), (c, b, p))
