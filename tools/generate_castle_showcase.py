from __future__ import annotations

import argparse
import math
from pathlib import Path
import random
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from box_editor_view.box_file import BoxMap, RGBA, save_box
from box_editor_view.box_hash import box_hash

from neko_mouse_world.box_assets import ensure_default_box
from neko_mouse_world.world_file import Cell, WorldMap, save_world, world_paths


def rgba(red: int, green: int, blue: int, alpha: int = 255) -> RGBA:
    return (red / 255.0, green / 255.0, blue / 255.0, alpha / 255.0)


STONE = (rgba(134, 137, 137), rgba(157, 159, 157), rgba(107, 111, 112), rgba(183, 181, 171))
STONE_DARK = (rgba(86, 91, 96), rgba(104, 110, 112), rgba(122, 124, 120))
STONE_WARM = (rgba(168, 163, 150), rgba(190, 184, 166), rgba(132, 128, 121))
ROOF = (rgba(122, 32, 30), rgba(158, 46, 38), rgba(188, 76, 50), rgba(98, 25, 33))
PATH = (rgba(156, 146, 126), rgba(124, 116, 102), rgba(184, 170, 140), rgba(104, 99, 91))
WATER = (rgba(49, 131, 181, 165), rgba(60, 168, 205, 150), rgba(30, 88, 150, 175))
GRASS = (rgba(68, 128, 67), rgba(82, 151, 76), rgba(49, 103, 58), rgba(109, 162, 79))
SAND = (rgba(172, 151, 104), rgba(201, 183, 131), rgba(138, 119, 88))
WOOD = (rgba(102, 67, 39), rgba(130, 85, 48), rgba(76, 49, 32), rgba(158, 107, 61))
LEAVES = (rgba(40, 103, 63), rgba(59, 138, 73), rgba(33, 82, 55), rgba(89, 162, 83))
WINDOW = rgba(22, 37, 56)
WINDOW_GLOW = rgba(241, 191, 95)
GOLD = rgba(217, 174, 72)


def full_textured_cube(n: int, palette: tuple[RGBA, ...], seed: int, edge: RGBA | None = None) -> BoxMap:
    size = 2**n
    boxes: dict[Cell, RGBA] = {}
    for x in range(size):
        for y in range(size):
            for z in range(size):
                color = palette[(x * 11 + y * 7 + z * 5 + seed) % len(palette)]
                if edge is not None and (x in {0, size - 1} or y in {0, size - 1} or z in {0, size - 1}):
                    if (x + 2 * y + 3 * z + seed) % 7 == 0:
                        color = edge
                boxes[(x, y, z)] = color
    return BoxMap(n=n, boxes=boxes)


def window_stone_block(seed: int) -> BoxMap:
    box = full_textured_cube(3, STONE, seed, edge=STONE_DARK[0])
    size = box.size
    for x in range(2, 6):
        for z in range(2, 6):
            for y in (0, size - 1):
                box.boxes[(x, y, z)] = WINDOW_GLOW if z == 2 and x in {2, 5} else WINDOW
    for y in range(2, 6):
        for z in range(2, 6):
            for x in (0, size - 1):
                box.boxes[(x, y, z)] = WINDOW_GLOW if z == 2 and y in {2, 5} else WINDOW
    return box


def low_layer(n: int, palette: tuple[RGBA, ...], layers: int, seed: int) -> BoxMap:
    size = 2**n
    boxes: dict[Cell, RGBA] = {}
    for x in range(size):
        for y in range(size):
            for z in range(layers):
                boxes[(x, y, z)] = palette[(x * 5 + y * 3 + z + seed) % len(palette)]
    return BoxMap(n=n, boxes=boxes)


def water_box() -> BoxMap:
    size = 8
    boxes: dict[Cell, RGBA] = {}
    for x in range(size):
        for y in range(size):
            boxes[(x, y, 0)] = WATER[(x + 2 * y) % len(WATER)]
    return BoxMap(n=3, boxes=boxes)


def leaf_blob(seed: int) -> BoxMap:
    rng = random.Random(seed)
    size = 8
    center = (3.5, 3.5, 3.4)
    boxes: dict[Cell, RGBA] = {}
    for x in range(size):
        for y in range(size):
            for z in range(size):
                dx = (x - center[0]) / 3.8
                dy = (y - center[1]) / 3.8
                dz = (z - center[2]) / 3.4
                if dx * dx + dy * dy + dz * dz <= 1.0 + rng.uniform(-0.12, 0.10):
                    boxes[(x, y, z)] = LEAVES[(x + y * 2 + z * 3 + seed) % len(LEAVES)]
    return BoxMap(n=3, boxes=boxes)


def trunk_block(seed: int) -> BoxMap:
    size = 8
    boxes: dict[Cell, RGBA] = {}
    for x in range(size):
        for y in range(size):
            for z in range(size):
                stripe = 1 if (x in {1, 6} or y in {1, 6}) and z % 3 != 0 else 0
                boxes[(x, y, z)] = WOOD[(x + y + stripe + seed) % len(WOOD)]
    return BoxMap(n=3, boxes=boxes)


def store_asset(boxes_dir: Path, box_map: BoxMap) -> str:
    digest = box_hash(box_map)
    target = boxes_dir / f"{digest}.box"
    if not target.exists():
        save_box(target, box_map)
    return digest


def build_showcase(output: Path) -> None:
    paths = world_paths(output)
    paths.root.mkdir(parents=True, exist_ok=True)
    if paths.boxes_dir.exists():
        for box_file in paths.boxes_dir.glob("*.box"):
            box_file.unlink()
    paths.boxes_dir.mkdir(parents=True, exist_ok=True)
    ensure_default_box(paths.boxes_dir)

    assets = {
        "stone_a": store_asset(paths.boxes_dir, full_textured_cube(3, STONE, 1, edge=STONE_DARK[0])),
        "stone_b": store_asset(paths.boxes_dir, full_textured_cube(3, STONE_WARM, 2, edge=STONE_DARK[1])),
        "stone_c": store_asset(paths.boxes_dir, full_textured_cube(3, STONE_DARK, 3, edge=STONE_WARM[0])),
        "window": store_asset(paths.boxes_dir, window_stone_block(4)),
        "roof": store_asset(paths.boxes_dir, full_textured_cube(3, ROOF, 5, edge=ROOF[3])),
        "gold": store_asset(paths.boxes_dir, full_textured_cube(2, (GOLD,), 6)),
        "path": store_asset(paths.boxes_dir, low_layer(3, PATH, 2, 7)),
        "water": store_asset(paths.boxes_dir, water_box()),
        "bank": store_asset(paths.boxes_dir, low_layer(3, SAND + GRASS, 2, 8)),
        "wood": store_asset(paths.boxes_dir, low_layer(3, WOOD, 3, 9)),
        "trunk": store_asset(paths.boxes_dir, trunk_block(10)),
        "leaves": store_asset(paths.boxes_dir, leaf_blob(11)),
    }

    world = WorldMap()

    def stone_for(cell: Cell) -> str:
        x, y, z = cell
        if z <= 1 or (x * 13 + y * 17 + z * 19) % 11 == 0:
            return assets["stone_c"]
        if (x * 3 - y * 5 + z * 7) % 7 == 0:
            return assets["stone_b"]
        return assets["stone_a"]

    def put(cell: Cell, digest: str, overwrite: bool = True) -> None:
        if overwrite or cell not in world.boxes:
            world.set_box(cell, digest)

    def put_stone(cell: Cell, window: bool = False) -> None:
        put(cell, assets["window"] if window else stone_for(cell))

    def add_stream() -> set[tuple[int, int]]:
        water_cells: set[tuple[int, int]] = set()
        bridge = {(x, y) for x in range(-3, 4) for y in range(-20, -6)}
        for x in range(-34, 35):
            center_y = -14 + round(2.4 * math.sin((x + 4) / 5.0))
            for dy in (-1, 0, 1):
                pos = (x, center_y + dy)
                if pos not in bridge:
                    put((pos[0], pos[1], 0), assets["water"], overwrite=False)
                water_cells.add(pos)
            for dy in (-3, -2, 2, 3):
                pos = (x, center_y + dy)
                if pos not in water_cells and pos not in bridge:
                    put((pos[0], pos[1], 0), assets["bank"], overwrite=False)
        for x in range(-3, 4):
            for y in range(-20, -6):
                put((x, y, 0), assets["wood"])
        return water_cells

    def add_path() -> None:
        for y in range(-27, 8):
            width = 2 if y < -7 else 1
            for x in range(-width, width + 1):
                put((x, y, 0), assets["path"], overwrite=False)
        for x in range(-5, 6):
            for y in range(0, 8):
                if abs(x) <= 1 or y in {0, 7}:
                    put((x, y, 0), assets["path"], overwrite=False)

    def add_outer_wall() -> None:
        x0, x1, y0, y1 = -14, 14, -6, 18
        for z in range(4):
            for x in range(x0, x1 + 1):
                for y in (y0, y1):
                    if y == y0 and -2 <= x <= 2 and z <= 2:
                        continue
                    put_stone((x, y, z))
            for y in range(y0 + 1, y1):
                for x in (x0, x1):
                    put_stone((x, y, z))
        for x in range(x0, x1 + 1):
            if x % 2 == 0:
                put_stone((x, y0, 4))
                put_stone((x, y1, 4))
        for y in range(y0 + 1, y1):
            if y % 2 == 0:
                put_stone((x0, y, 4))
                put_stone((x1, y, 4))
        for x in range(-2, 3):
            put_stone((x, y0, 3))

    def add_tower(cx: int, cy: int, height: int, radius: int = 2) -> None:
        for z in range(height):
            for x in range(cx - radius, cx + radius + 1):
                for y in range(cy - radius, cy + radius + 1):
                    edge = abs(x - cx) == radius or abs(y - cy) == radius
                    if edge:
                        window = z in {3, 5} and ((x == cx and abs(y - cy) == radius) or (y == cy and abs(x - cx) == radius))
                        put_stone((x, y, z), window=window)
        for x in range(cx - radius, cx + radius + 1):
            for y in range(cy - radius, cy + radius + 1):
                if (abs(x - cx) == radius or abs(y - cy) == radius) and (x + y) % 2 == 0:
                    put_stone((x, y, height))
        for level in range(3):
            span = radius + 1 - level
            z = height + 1 + level
            for x in range(cx - span, cx + span + 1):
                for y in range(cy - span, cy + span + 1):
                    if max(abs(x - cx), abs(y - cy)) <= span:
                        put((x, y, z), assets["roof"])
        put((cx, cy, height + 4), assets["gold"])

    def add_gatehouse() -> None:
        for cx in (-5, 5):
            add_tower(cx, -6, 6, radius=1)
        for x in range(-4, 5):
            for z in range(3, 6):
                if not (-2 <= x <= 2 and z <= 4):
                    put_stone((x, -7, z))
        for x in range(-5, 6):
            if x % 2 == 0:
                put_stone((x, -7, 6))

    def add_keep() -> None:
        x0, x1, y0, y1, height = -6, 6, 4, 14, 8
        for z in range(height):
            for x in range(x0, x1 + 1):
                for y in (y0, y1):
                    if y == y0 and -1 <= x <= 1 and z <= 2:
                        continue
                    window = z in {3, 5} and x in {-4, 0, 4}
                    put_stone((x, y, z), window=window)
            for y in range(y0 + 1, y1):
                for x in (x0, x1):
                    window = z in {3, 5} and y in {7, 11}
                    put_stone((x, y, z), window=window)
        for level, z in enumerate(range(height, height + 4)):
            x_span = 7 - level
            y_span = 6 - level
            for x in range(-x_span, x_span + 1):
                for y in range(9 - y_span, 9 + y_span + 1):
                    put((x, y, z), assets["roof"])
        for z in range(12, 15):
            for x in range(-1, 2):
                for y in range(8, 11):
                    if abs(x) == 1 or y in {8, 10}:
                        put_stone((x, y, z), window=(z == 13))
        put((0, 9, 15), assets["gold"])

    def add_tree(x: int, y: int, height: int = 3) -> None:
        for z in range(height):
            put((x, y, z), assets["trunk"], overwrite=False)
        leaf_positions = []
        for dx in range(-2, 3):
            for dy in range(-2, 3):
                for dz in range(-1, 3):
                    if dx * dx + dy * dy + dz * dz <= 6 and not (abs(dx) == 2 and abs(dy) == 2):
                        leaf_positions.append((x + dx, y + dy, height + dz))
        for cell in leaf_positions:
            put(cell, assets["leaves"], overwrite=False)

    add_stream()
    add_path()
    add_outer_wall()
    for center in [(-14, -6), (14, -6), (-14, 18), (14, 18)]:
        add_tower(center[0], center[1], 7)
    add_gatehouse()
    add_keep()

    for tree in [(-25, -20), (-20, -8), (-24, 9), (22, -18), (25, -4), (21, 12), (-19, 24), (19, 25)]:
        add_tree(*tree)
    for x, y in [(-9, -22), (8, -23), (-17, -13), (17, -12), (-10, 22), (10, 22)]:
        put((x, y, 0), assets["bank"], overwrite=False)

    save_world(paths.info_file, world)
    print(f"Wrote {paths.root}")
    print(f"World boxes: {len(world.boxes)}")
    print(f"Reusable .box assets: {len(list(paths.boxes_dir.glob('*.box')))}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the castle showcase world.")
    parser.add_argument(
        "output",
        nargs="?",
        default=str(ROOT / "examples" / "castle_showcase"),
        help="world output folder",
    )
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if output.exists() and not output.is_dir():
        raise SystemExit(f"{output} is not a directory")
    build_showcase(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
