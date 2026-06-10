from __future__ import annotations

from pathlib import Path
import shutil

from box_editor_view.box_file import BoxMap, DEFAULT_COLOR, save_box
from box_editor_view.box_hash import box_hash, hash_box_file

from .world_file import box_path_for_hash


def default_box_map() -> BoxMap:
    return BoxMap(n=0, boxes={(0, 0, 0): DEFAULT_COLOR})


def ensure_default_box(boxes_dir: Path) -> str:
    return store_box_map(boxes_dir, default_box_map())


def store_box_map(boxes_dir: Path, box_map: BoxMap) -> str:
    boxes_dir.mkdir(parents=True, exist_ok=True)
    digest = box_hash(box_map)
    target = box_path_for_hash(boxes_dir, digest)
    if not target.exists():
        save_box(target, box_map)
    return digest


def store_box_file_by_hash(boxes_dir: Path, source: Path) -> str:
    boxes_dir.mkdir(parents=True, exist_ok=True)
    digest = hash_box_file(source)
    target = box_path_for_hash(boxes_dir, digest)
    if source.resolve() != target.resolve():
        shutil.copyfile(source, target)
    return digest


def copy_box_for_editing(boxes_dir: Path, digest: str, destination: Path) -> None:
    source = box_path_for_hash(boxes_dir, digest)
    shutil.copyfile(source, destination)
