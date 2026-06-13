from __future__ import annotations

from _build_executable import BuildConfig, build_from_cli


CONFIG = BuildConfig(
    target="linux-server",
    platform="linux",
    name="neko-mouse-world-server",
    entry_script="pyinstaller_linux_server_entry.py",
    default_dist_dir="dist/linux-amd64",
    hidden_imports=("box_editor_view.box_file", "box_editor_view.box_hash"),
    copy_metadata=("box-editor-view", "neko-mouse-world"),
)


if __name__ == "__main__":
    raise SystemExit(build_from_cli(CONFIG))
