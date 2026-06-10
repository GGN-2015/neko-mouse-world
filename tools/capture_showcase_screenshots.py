from __future__ import annotations

import argparse
from pathlib import Path
import sys

from panda3d.core import Filename, Point3, Vec3, loadPrcFileData


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

loadPrcFileData(
    "",
    "\n".join(
        [
            "window-type offscreen",
            "win-size 1280 720",
            "fullscreen false",
            "audio-library-name null",
            "show-frame-rate-meter false",
        ]
    ),
)

from neko_mouse_world.app import NekoMouseWorldApp
from neko_mouse_world.world_file import LoadedWorld, load_or_create_world


Shot = tuple[str, tuple[float, float, float], tuple[float, float, float], str]


SHOTS: tuple[Shot, ...] = (
    (
        "castle_gate.png",
        (0.5, -30.0, 5.4),
        (0.0, -5.5, 3.3),
        "Approaching the castle gate across the stream",
    ),
    (
        "castle_courtyard.png",
        (-12.0, -4.0, 6.2),
        (0.0, 9.5, 5.8),
        "Courtyard view toward the keep",
    ),
    (
        "castle_overlook.png",
        (22.0, -28.0, 18.0),
        (0.0, 7.0, 4.5),
        "Aerial view of the castle, stream, bridge, and trees",
    ),
)


def capture(world_dir: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    loaded = load_or_create_world(world_dir)
    app = NekoMouseWorldApp(LoadedWorld(paths=loaded.paths, world_map=loaded.world_map))
    app.set_mouse_capture(False)
    if hasattr(app, "setFrameRateMeter"):
        app.setFrameRateMeter(False)
    app.status.hide()
    app.detail.hide()
    app.help_hint.hide()
    app.crosshair.hide()
    app.hover_outline.hide()

    try:
        for name, camera_pos, target, description in SHOTS:
            app.player_pos = Vec3(camera_pos[0], camera_pos[1], max(0.0, camera_pos[2] - 1.7))
            app._update_ground(force=True)
            app.camera.setPos(Point3(*camera_pos))
            app.camera.lookAt(Point3(*target))
            for _ in range(6):
                app.graphicsEngine.renderFrame()
            destination = output_dir / name
            saved = app.win.saveScreenshot(Filename.fromOsSpecific(str(destination)))
            if not saved:
                raise SystemExit(f"Failed to save screenshot {destination}")
            print(f"Wrote {destination}")
    finally:
        app.destroy()


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture README screenshots for the castle showcase world.")
    parser.add_argument(
        "--world",
        default=str(ROOT / "examples" / "castle_showcase"),
        help="world folder to render",
    )
    parser.add_argument(
        "--output",
        default=str(ROOT / "docs" / "images"),
        help="directory for screenshot PNG files",
    )
    args = parser.parse_args()
    capture(Path(args.world).resolve(), Path(args.output).resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
