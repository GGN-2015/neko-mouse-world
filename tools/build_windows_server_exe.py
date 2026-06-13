from __future__ import annotations

from _build_executable import BuildConfig, build_from_cli


CONFIG = BuildConfig(
    target="windows-server",
    platform="windows",
    name="neko-mouse-world-server",
    entry_script="pyinstaller_server_entry.py",
    default_dist_dir="dist/windows-amd64",
    collect_all=("panda3d",),
    collect_submodules=("direct", "box_editor_view", "neko_mouse_world"),
    copy_metadata=("panda3d", "box-editor-view", "neko-mouse-world"),
)


if __name__ == "__main__":
    raise SystemExit(build_from_cli(CONFIG))
