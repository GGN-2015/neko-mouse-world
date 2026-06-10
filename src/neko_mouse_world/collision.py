from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Iterable

from box_editor_view.box_file import BoxMap, load_box

from .orientation import IDENTITY_ORIENTATION, rotate_point
from .world_file import box_path_for_hash


Point3 = tuple[float, float, float]
Point2 = tuple[float, float]
Plane = tuple[float, float, float, float]

EPSILON = 1e-7


@dataclass(frozen=True)
class _Face:
    vertices: tuple[int, int, int]
    plane: Plane

    def distance(self, point: Point3) -> float:
        nx, ny, nz, d = self.plane
        return nx * point[0] + ny * point[1] + nz * point[2] + d

    def edges(self) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]]:
        a, b, c = self.vertices
        return (a, b), (b, c), (c, a)


@dataclass(frozen=True)
class HeightRange:
    minimum: float
    top: float


@dataclass(frozen=True)
class ConvexCollisionShape:
    projection: tuple[Point2, ...]
    upper_planes: tuple[Plane, ...]
    lower_planes: tuple[Plane, ...]
    minimum_z: float
    maximum_z: float

    @property
    def empty(self) -> bool:
        return not self.projection

    def top_height_at(self, x: float, y: float) -> float | None:
        if self.empty or not _point_in_convex_polygon((x, y), self.projection):
            return None
        if not self.upper_planes:
            return self.maximum_z
        top = math.inf
        for nx, ny, nz, d in self.upper_planes:
            if nz <= EPSILON:
                continue
            top = min(top, -(nx * x + ny * y + d) / nz)
        if math.isinf(top):
            return self.maximum_z
        return max(self.minimum_z, min(self.maximum_z, top))

    def bottom_height_at(self, x: float, y: float) -> float | None:
        if self.empty or not _point_in_convex_polygon((x, y), self.projection):
            return None
        if not self.lower_planes:
            return self.minimum_z
        bottom = -math.inf
        for nx, ny, nz, d in self.lower_planes:
            if nz >= -EPSILON:
                continue
            bottom = max(bottom, -(nx * x + ny * y + d) / nz)
        if math.isinf(bottom):
            return self.minimum_z
        return max(self.minimum_z, min(self.maximum_z, bottom))

    def height_range_for_aabb(self, min_x: float, max_x: float, min_y: float, max_y: float) -> HeightRange | None:
        if self.empty or not _rect_intersects_polygon(min_x, max_x, min_y, max_y, self.projection):
            return None

        samples: set[Point2] = set()
        rect = ((min_x, min_y), (max_x, min_y), (max_x, max_y), (min_x, max_y))
        center = ((min_x + max_x) * 0.5, (min_y + max_y) * 0.5)
        for point in (*rect, center):
            if _point_in_convex_polygon(point, self.projection):
                samples.add(point)
        for point in self.projection:
            if min_x - EPSILON <= point[0] <= max_x + EPSILON and min_y - EPSILON <= point[1] <= max_y + EPSILON:
                samples.add(point)
        for point in _rect_polygon_intersections(rect, self.projection):
            samples.add(point)

        if not samples:
            samples.add(center)

        tops = [height for x, y in samples if (height := self.top_height_at(x, y)) is not None]
        bottoms = [height for x, y in samples if (height := self.bottom_height_at(x, y)) is not None]
        if not tops or not bottoms:
            return None
        return HeightRange(min(bottoms), max(tops))


class CollisionShapeCache:
    def __init__(self, boxes_dir: Path) -> None:
        self.boxes_dir = boxes_dir
        self._shapes: dict[tuple[str, int], ConvexCollisionShape] = {}

    def get(self, digest: str, orientation: int = IDENTITY_ORIENTATION) -> ConvexCollisionShape:
        key = (digest, orientation)
        shape = self._shapes.get(key)
        if shape is not None:
            return shape
        shape = build_collision_shape(load_box(box_path_for_hash(self.boxes_dir, digest)), orientation)
        self._shapes[key] = shape
        return shape

    def invalidate(self, digest: str | None = None) -> None:
        if digest is None:
            self._shapes.clear()
            return
        for key in [key for key in self._shapes if key[0] == digest]:
            self._shapes.pop(key, None)


def build_collision_shape(box_map: BoxMap, orientation: int = IDENTITY_ORIENTATION) -> ConvexCollisionShape:
    points = _box_vertices(box_map, orientation)
    if not points:
        return ConvexCollisionShape((), (), (), 0.0, 0.0)
    projection = tuple(_convex_hull_2d((point[0], point[1]) for point in points))
    minimum_z = min(point[2] for point in points)
    maximum_z = max(point[2] for point in points)
    faces = _quickhull(points)
    upper_planes = tuple(face.plane for face in faces if face.plane[2] > EPSILON)
    lower_planes = tuple(face.plane for face in faces if face.plane[2] < -EPSILON)
    if not upper_planes and maximum_z > minimum_z:
        upper_planes = ((0.0, 0.0, 1.0, -maximum_z),)
    if not lower_planes and maximum_z > minimum_z:
        lower_planes = ((0.0, 0.0, -1.0, minimum_z),)
    return ConvexCollisionShape(projection, upper_planes, lower_planes, minimum_z, maximum_z)


def _box_vertices(box_map: BoxMap, orientation: int) -> tuple[Point3, ...]:
    if not box_map.boxes:
        return ()

    scale = 1.0 / float(box_map.size)
    vertices: set[Point3] = set()
    for x, y, z in box_map.boxes:
        for dx in (0, 1):
            for dy in (0, 1):
                for dz in (0, 1):
                    point = ((x + dx) * scale, (y + dy) * scale, (z + dz) * scale)
                    vertices.add(rotate_point(point, orientation))
    return tuple(sorted(vertices))


def _quickhull(points: tuple[Point3, ...]) -> tuple[_Face, ...]:
    if len(points) < 4:
        return ()

    tetra = _initial_tetrahedron(points)
    if tetra is None:
        return ()

    interior = _centroid(points[index] for index in tetra)
    faces = [
        face
        for face in (
            _make_face(tetra[0], tetra[1], tetra[2], points, interior),
            _make_face(tetra[0], tetra[3], tetra[1], points, interior),
            _make_face(tetra[0], tetra[2], tetra[3], points, interior),
            _make_face(tetra[1], tetra[3], tetra[2], points, interior),
        )
        if face is not None
    ]

    tetra_set = set(tetra)
    for index, point in enumerate(points):
        if index in tetra_set:
            continue
        visible = [face for face in faces if face.distance(point) > EPSILON]
        if not visible:
            continue

        visible_ids = {id(face) for face in visible}
        horizon: dict[tuple[int, int], tuple[int, int] | None] = {}
        for face in visible:
            for edge in face.edges():
                key = (min(edge), max(edge))
                horizon[key] = None if key in horizon else edge

        faces = [face for face in faces if id(face) not in visible_ids]
        for edge in horizon.values():
            if edge is None:
                continue
            new_face = _make_face(edge[0], edge[1], index, points, interior)
            if new_face is not None:
                faces.append(new_face)
    return tuple(faces)


def _initial_tetrahedron(points: tuple[Point3, ...]) -> tuple[int, int, int, int] | None:
    first = min(range(len(points)), key=lambda index: (points[index][0], points[index][1], points[index][2]))
    second = max(range(len(points)), key=lambda index: _distance_squared(points[first], points[index]))
    if _distance_squared(points[first], points[second]) <= EPSILON:
        return None

    line = _sub(points[second], points[first])
    third = max(
        range(len(points)),
        key=lambda index: _length_squared(_cross(line, _sub(points[index], points[first]))),
    )
    if _length_squared(_cross(line, _sub(points[third], points[first]))) <= EPSILON:
        return None

    normal = _cross(_sub(points[second], points[first]), _sub(points[third], points[first]))
    fourth = max(
        range(len(points)),
        key=lambda index: abs(_dot(normal, _sub(points[index], points[first]))),
    )
    if abs(_dot(normal, _sub(points[fourth], points[first]))) <= EPSILON:
        return None
    return first, second, third, fourth


def _make_face(a: int, b: int, c: int, points: tuple[Point3, ...], interior: Point3) -> _Face | None:
    pa, pb, pc = points[a], points[b], points[c]
    normal = _cross(_sub(pb, pa), _sub(pc, pa))
    length = math.sqrt(_length_squared(normal))
    if length <= EPSILON:
        return None
    normal = (normal[0] / length, normal[1] / length, normal[2] / length)
    d = -_dot(normal, pa)
    if _dot(normal, interior) + d > 0.0:
        b, c = c, b
        normal = (-normal[0], -normal[1], -normal[2])
        d = -d
    return _Face((a, b, c), (normal[0], normal[1], normal[2], d))


def _convex_hull_2d(points: Iterable[Point2]) -> list[Point2]:
    unique = sorted(set(points))
    if len(unique) <= 1:
        return unique

    def cross(origin: Point2, a: Point2, b: Point2) -> float:
        return (a[0] - origin[0]) * (b[1] - origin[1]) - (a[1] - origin[1]) * (b[0] - origin[0])

    lower: list[Point2] = []
    for point in unique:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= EPSILON:
            lower.pop()
        lower.append(point)

    upper: list[Point2] = []
    for point in reversed(unique):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= EPSILON:
            upper.pop()
        upper.append(point)

    return lower[:-1] + upper[:-1]


def _point_in_convex_polygon(point: Point2, polygon: tuple[Point2, ...]) -> bool:
    if len(polygon) < 3:
        return False
    for index, start in enumerate(polygon):
        end = polygon[(index + 1) % len(polygon)]
        if (end[0] - start[0]) * (point[1] - start[1]) - (end[1] - start[1]) * (point[0] - start[0]) < -EPSILON:
            return False
    return True


def _rect_intersects_polygon(min_x: float, max_x: float, min_y: float, max_y: float, polygon: tuple[Point2, ...]) -> bool:
    rect = ((min_x, min_y), (max_x, min_y), (max_x, max_y), (min_x, max_y))
    if any(_point_in_convex_polygon(point, polygon) for point in rect):
        return True
    if any(min_x - EPSILON <= x <= max_x + EPSILON and min_y - EPSILON <= y <= max_y + EPSILON for x, y in polygon):
        return True
    return any(True for _ in _rect_polygon_intersections(rect, polygon))


def _rect_polygon_intersections(rect: tuple[Point2, Point2, Point2, Point2], polygon: tuple[Point2, ...]) -> list[Point2]:
    intersections: list[Point2] = []
    rect_edges = tuple((rect[index], rect[(index + 1) % len(rect)]) for index in range(len(rect)))
    polygon_edges = tuple((polygon[index], polygon[(index + 1) % len(polygon)]) for index in range(len(polygon)))
    for edge_a in rect_edges:
        for edge_b in polygon_edges:
            intersection = _segment_intersection(edge_a[0], edge_a[1], edge_b[0], edge_b[1])
            if intersection is not None:
                intersections.append(intersection)
    return intersections


def _segment_intersection(a: Point2, b: Point2, c: Point2, d: Point2) -> Point2 | None:
    r = (b[0] - a[0], b[1] - a[1])
    s = (d[0] - c[0], d[1] - c[1])
    denominator = r[0] * s[1] - r[1] * s[0]
    if abs(denominator) <= EPSILON:
        return None
    u = ((c[0] - a[0]) * r[1] - (c[1] - a[1]) * r[0]) / denominator
    t = ((c[0] - a[0]) * s[1] - (c[1] - a[1]) * s[0]) / denominator
    if -EPSILON <= t <= 1.0 + EPSILON and -EPSILON <= u <= 1.0 + EPSILON:
        return (a[0] + t * r[0], a[1] + t * r[1])
    return None


def _centroid(points: Iterable[Point3]) -> Point3:
    total = (0.0, 0.0, 0.0)
    count = 0
    for point in points:
        total = (total[0] + point[0], total[1] + point[1], total[2] + point[2])
        count += 1
    return (total[0] / count, total[1] / count, total[2] / count)


def _sub(a: Point3, b: Point3) -> Point3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a: Point3, b: Point3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Point3, b: Point3) -> Point3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _length_squared(point: Point3) -> float:
    return _dot(point, point)


def _distance_squared(a: Point3, b: Point3) -> float:
    return _length_squared(_sub(a, b))
