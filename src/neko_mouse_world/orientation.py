from __future__ import annotations

from dataclasses import dataclass
from itertools import permutations, product


Vector = tuple[int, int, int]
Point = tuple[float, float, float]

IDENTITY_ORIENTATION = 0


@dataclass(frozen=True)
class Orientation:
    x_axis: Vector
    y_axis: Vector
    z_axis: Vector


def _cross(a: Vector, b: Vector) -> Vector:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _dot(a: Vector, b: Vector) -> int:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _build_orientations() -> tuple[Orientation, ...]:
    orientations = [Orientation((1, 0, 0), (0, 1, 0), (0, 0, 1))]
    for permutation in permutations((0, 1, 2)):
        for signs in product((-1, 1), repeat=3):
            axes: list[Vector] = []
            for index, sign in zip(permutation, signs, strict=True):
                axis = [0, 0, 0]
                axis[index] = sign
                axes.append((axis[0], axis[1], axis[2]))
            candidate = Orientation(axes[0], axes[1], axes[2])
            if _cross(candidate.x_axis, candidate.y_axis) != candidate.z_axis:
                continue
            if candidate not in orientations:
                orientations.append(candidate)
    return tuple(orientations)


ORIENTATIONS = _build_orientations()
ORIENTATION_INDEX = {orientation: index for index, orientation in enumerate(ORIENTATIONS)}


def validate_orientation(value: object) -> int:
    try:
        orientation = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("orientation must be an integer from 0 to 23") from exc
    if orientation < 0 or orientation >= len(ORIENTATIONS):
        raise ValueError("orientation must be an integer from 0 to 23")
    return orientation


def rotate_point(point: Point, orientation_id: int) -> Point:
    orientation = ORIENTATIONS[validate_orientation(orientation_id)]
    centered = (point[0] - 0.5, point[1] - 0.5, point[2] - 0.5)
    rotated = _apply_axes(centered, orientation)
    return (rotated[0] + 0.5, rotated[1] + 0.5, rotated[2] + 0.5)


def rotate_normal(normal: Vector, orientation_id: int) -> Vector:
    orientation = ORIENTATIONS[validate_orientation(orientation_id)]
    return _apply_axes(normal, orientation)  # type: ignore[return-value]


def rotate_outer_face(normal: Vector | None, orientation_id: int) -> Vector | None:
    if normal is None:
        return None
    return rotate_normal(normal, orientation_id)


def turn_orientation(orientation_id: int, command: str) -> int:
    orientation = ORIENTATIONS[validate_orientation(orientation_id)]
    if command == "left":
        return turn_orientation_around_axis(orientation_id, (0, 0, 1), 1)
    if command == "right":
        return turn_orientation_around_axis(orientation_id, (0, 0, 1), -1)
    if command == "up":
        return _compose_local_rotation(orientation, _rotation_x_quarter(1))
    if command == "down":
        return _compose_local_rotation(orientation, _rotation_x_quarter(-1))
    raise ValueError(f"unknown orientation command {command!r}")


def turn_orientation_around_axis(orientation_id: int, axis: Vector, turns: int) -> int:
    orientation = ORIENTATIONS[validate_orientation(orientation_id)]
    return _compose_world_rotation(_rotation_axis_quarter(axis, turns), orientation)


def nearest_axis(vector: tuple[float, float, float]) -> Vector:
    components = (float(vector[0]), float(vector[1]), float(vector[2]))
    axis_index = max(range(3), key=lambda index: abs(components[index]))
    sign = 1 if components[axis_index] >= 0.0 else -1
    axis = [0, 0, 0]
    axis[axis_index] = sign
    return (axis[0], axis[1], axis[2])


def _apply_axes(point: tuple[float, float, float], orientation: Orientation) -> tuple[float, float, float]:
    return (
        point[0] * orientation.x_axis[0] + point[1] * orientation.y_axis[0] + point[2] * orientation.z_axis[0],
        point[0] * orientation.x_axis[1] + point[1] * orientation.y_axis[1] + point[2] * orientation.z_axis[1],
        point[0] * orientation.x_axis[2] + point[1] * orientation.y_axis[2] + point[2] * orientation.z_axis[2],
    )


def _compose_world_rotation(rotation: Orientation, orientation: Orientation) -> int:
    composed = Orientation(
        _apply_int_axes(orientation.x_axis, rotation),
        _apply_int_axes(orientation.y_axis, rotation),
        _apply_int_axes(orientation.z_axis, rotation),
    )
    return ORIENTATION_INDEX[composed]


def _compose_local_rotation(orientation: Orientation, local_rotation: Orientation) -> int:
    composed = Orientation(
        _local_axis_to_world(local_rotation.x_axis, orientation),
        _local_axis_to_world(local_rotation.y_axis, orientation),
        _local_axis_to_world(local_rotation.z_axis, orientation),
    )
    return ORIENTATION_INDEX[composed]


def _local_axis_to_world(axis: Vector, orientation: Orientation) -> Vector:
    return (
        axis[0] * orientation.x_axis[0] + axis[1] * orientation.y_axis[0] + axis[2] * orientation.z_axis[0],
        axis[0] * orientation.x_axis[1] + axis[1] * orientation.y_axis[1] + axis[2] * orientation.z_axis[1],
        axis[0] * orientation.x_axis[2] + axis[1] * orientation.y_axis[2] + axis[2] * orientation.z_axis[2],
    )


def _apply_int_axes(axis: Vector, orientation: Orientation) -> Vector:
    result = _apply_axes(axis, orientation)
    return (round(result[0]), round(result[1]), round(result[2]))


def _rotation_z_quarter(turns: int) -> Orientation:
    turns %= 4
    if turns == 0:
        return ORIENTATIONS[IDENTITY_ORIENTATION]
    if turns == 1:
        return Orientation((0, 1, 0), (-1, 0, 0), (0, 0, 1))
    if turns == 2:
        return Orientation((-1, 0, 0), (0, -1, 0), (0, 0, 1))
    return Orientation((0, -1, 0), (1, 0, 0), (0, 0, 1))


def _rotation_x_quarter(turns: int) -> Orientation:
    turns %= 4
    if turns == 0:
        return ORIENTATIONS[IDENTITY_ORIENTATION]
    if turns == 1:
        return Orientation((1, 0, 0), (0, 0, 1), (0, -1, 0))
    if turns == 2:
        return Orientation((1, 0, 0), (0, -1, 0), (0, 0, -1))
    return Orientation((1, 0, 0), (0, 0, -1), (0, 1, 0))


def _rotation_axis_quarter(axis: Vector, turns: int) -> Orientation:
    if axis == (1, 0, 0):
        return _rotation_x_quarter(turns)
    if axis == (-1, 0, 0):
        return _rotation_x_quarter(-turns)
    if axis == (0, 0, 1):
        return _rotation_z_quarter(turns)
    if axis == (0, 0, -1):
        return _rotation_z_quarter(-turns)
    if axis == (0, 1, 0):
        return _rotation_y_quarter(turns)
    if axis == (0, -1, 0):
        return _rotation_y_quarter(-turns)
    raise ValueError(f"axis must be a unit cardinal vector, got {axis!r}")


def _rotation_y_quarter(turns: int) -> Orientation:
    turns %= 4
    if turns == 0:
        return ORIENTATIONS[IDENTITY_ORIENTATION]
    if turns == 1:
        return Orientation((0, 0, -1), (0, 1, 0), (1, 0, 0))
    if turns == 2:
        return Orientation((-1, 0, 0), (0, 1, 0), (0, 0, -1))
    return Orientation((0, 0, 1), (0, 1, 0), (-1, 0, 0))
