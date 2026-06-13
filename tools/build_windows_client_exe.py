from __future__ import annotations

from _build_executable import BuildConfig, build_from_cli


CONFIG = BuildConfig(
    target="windows-client",
    platform="windows",
    name="neko-mouse-world-client",
    entry_script="pyinstaller_client_entry.py",
    default_dist_dir="dist/windows-amd64",
    windowed_by_default=True,
    collect_all=("panda3d",),
    collect_submodules=("direct", "box_editor_view", "neko_mouse_world"),
    copy_metadata=("panda3d", "box-editor-view", "neko-mouse-world"),
)


if __name__ == "__main__":
    raise SystemExit(build_from_cli(CONFIG, allow_console_override=True))
