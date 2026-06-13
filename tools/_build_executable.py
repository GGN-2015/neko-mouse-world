from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = Path(__file__).resolve().parent
AMD64_MACHINES = {"amd64", "x86_64", "x64"}


@dataclass(frozen=True)
class BuildConfig:
    target: str
    platform: str
    name: str
    entry_script: str
    default_dist_dir: str
    windowed_by_default: bool = False
    collect_all: tuple[str, ...] = ()
    collect_submodules: tuple[str, ...] = ()
    hidden_imports: tuple[str, ...] = ()
    copy_metadata: tuple[str, ...] = ()
    add_pyproject: bool = True


def build_from_cli(
    config: BuildConfig,
    argv: Sequence[str] | None = None,
    *,
    allow_console_override: bool = False,
) -> int:
    parser = argparse.ArgumentParser(description=f"Build {config.target} as a single-file executable")
    parser.add_argument("--python", default="", help="Python executable to build with")
    parser.add_argument("--dist-dir", default="", help="output directory")
    parser.add_argument("--skip-install", action="store_true", help="skip pip/PyInstaller/project installation")
    parser.add_argument("--clean", action="store_true", help="remove this target's PyInstaller work folder before build")
    if allow_console_override:
        parser.add_argument("--console", action="store_true", help="build a console executable")
    args = parser.parse_args(argv)

    try:
        build_executable(
            config,
            python=args.python,
            dist_dir=args.dist_dir,
            skip_install=args.skip_install,
            clean=args.clean,
            force_console=bool(getattr(args, "console", False)),
        )
    except RuntimeError as exc:
        parser.exit(1, f"error: {exc}\n")
    return 0


def build_executable(
    config: BuildConfig,
    *,
    python: str = "",
    dist_dir: str = "",
    skip_install: bool = False,
    clean: bool = False,
    force_console: bool = False,
) -> Path:
    python_exe = resolve_python(config.platform, python)
    python_info = inspect_python(python_exe)
    assert_build_python(python_info, config.platform)

    if not skip_install:
        install_build_dependencies(python_exe)

    output_dir = Path(dist_dir).resolve() if dist_dir else (REPO_ROOT / config.default_dist_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    build_root = REPO_ROOT / "build" / "pyinstaller"
    work_dir = build_root / config.target
    spec_dir = build_root / "spec"
    build_root.mkdir(parents=True, exist_ok=True)
    spec_dir.mkdir(parents=True, exist_ok=True)

    if clean:
        remove_inside(work_dir, build_root)

    extension = ".exe" if config.platform == "windows" else ""
    output_file = output_dir / f"{config.name}{extension}"
    output_one_dir = output_dir / config.name
    remove_inside(output_file, output_dir)
    remove_inside(output_one_dir, output_dir)

    pyinstaller_args = pyinstaller_command(config, python_info, output_dir, work_dir, spec_dir)
    if config.windowed_by_default and not force_console:
        pyinstaller_args.append("--windowed")
    else:
        pyinstaller_args.append("--console")
    pyinstaller_args.append(str((TOOLS_DIR / config.entry_script).resolve()))

    print(f"Building {config.target}...")
    run_checked([str(python_exe), *pyinstaller_args])

    if not output_file.is_file():
        raise RuntimeError(f"PyInstaller did not create expected output: {output_file}")
    if output_one_dir.exists():
        raise RuntimeError(f"Unexpected onedir output exists; expected a single file only: {output_one_dir}")

    if config.platform == "windows":
        assert_windows_amd64_pe(output_file)
    elif config.platform == "linux":
        assert_linux_amd64_elf(output_file)
    else:
        raise RuntimeError(f"Unsupported target platform: {config.platform}")

    size_mb = output_file.stat().st_size / (1024 * 1024)
    print(f"Built single-file {config.platform} amd64 executable: {output_file} ({size_mb:.2f} MB)")
    return output_file


def resolve_python(target_platform: str, explicit_python: str) -> Path:
    if explicit_python:
        command = shutil.which(explicit_python)
        if command:
            return Path(command).resolve()
        explicit_path = Path(explicit_python)
        if explicit_path.exists():
            return explicit_path.resolve()
        raise RuntimeError(f"Python executable not found: {explicit_python}")

    venv_python = REPO_ROOT / "venv" / ("Scripts/python.exe" if target_platform == "windows" else "bin/python")
    if venv_python.exists():
        return venv_python.resolve()

    current_python = Path(sys.executable)
    if current_python.exists():
        return current_python.resolve()

    alternate_venv_python = REPO_ROOT / "venv" / ("bin/python" if target_platform == "windows" else "Scripts/python.exe")
    if alternate_venv_python.exists():
        return alternate_venv_python.resolve()

    for name in ("python3", "python"):
        command = shutil.which(name)
        if command:
            return Path(command).resolve()

    raise RuntimeError("No Python executable found. Create venv or pass --python PATH.")


def inspect_python(python_exe: Path) -> dict[str, object]:
    code = r"""
import json
import platform
import struct
import sys

print(json.dumps({
    "platform": sys.platform,
    "machine": platform.machine(),
    "bits": struct.calcsize("P") * 8,
    "executable": sys.executable,
}))
"""
    completed = subprocess.run(
        [str(python_exe), "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Failed to inspect Python executable: {python_exe}\n{completed.stderr}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Python inspection returned invalid JSON: {completed.stdout!r}") from exc


def assert_build_python(info: dict[str, object], target_platform: str) -> None:
    sys_platform = str(info.get("platform", ""))
    bits = int(info.get("bits", 0))
    machine = str(info.get("machine", "")).lower()
    executable = str(info.get("executable", ""))

    if target_platform == "windows":
        if sys_platform != "win32":
            raise RuntimeError(f"Windows builds must run on Windows. Current Python platform: {sys_platform}")
    elif target_platform == "linux":
        if not sys_platform.startswith("linux"):
            raise RuntimeError(f"Linux builds must run on Linux. Current Python platform: {sys_platform}")
    else:
        raise RuntimeError(f"Unsupported target platform: {target_platform}")

    if bits != 64:
        raise RuntimeError(f"The build Python must be 64-bit. Current Python bits: {bits}")
    if machine not in AMD64_MACHINES:
        raise RuntimeError(f"The build Python must target amd64/x86_64. Current machine: {machine}")

    print(f"Using {target_platform} amd64 Python: {executable}")


def install_build_dependencies(python_exe: Path) -> None:
    print("Installing build dependencies into the selected Python environment...")
    run_checked([str(python_exe), "-m", "pip", "install", "--upgrade", "pip"])
    run_checked([str(python_exe), "-m", "pip", "install", "--upgrade", "pyinstaller>=6.11"])
    run_checked([str(python_exe), "-m", "pip", "install", "--editable", str(REPO_ROOT)])


def pyinstaller_command(
    config: BuildConfig,
    python_info: dict[str, object],
    output_dir: Path,
    work_dir: Path,
    spec_dir: Path,
) -> list[str]:
    add_data_separator = ";" if config.platform == "windows" else ":"
    args = [
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name",
        config.name,
        "--distpath",
        str(output_dir),
        "--workpath",
        str(work_dir),
        "--specpath",
        str(spec_dir),
        "--paths",
        str(REPO_ROOT / "src"),
    ]

    if config.add_pyproject:
        args.extend(["--add-data", f"{REPO_ROOT / 'pyproject.toml'}{add_data_separator}."])
    for package in config.collect_all:
        args.extend(["--collect-all", package])
    for package in config.collect_submodules:
        args.extend(["--collect-submodules", package])
    for module in config.hidden_imports:
        args.extend(["--hidden-import", module])
    for distribution in config.copy_metadata:
        if python_has_distribution(Path(str(python_info["executable"])), distribution):
            args.extend(["--copy-metadata", distribution])
        else:
            print(f"Warning: skipping missing Python distribution metadata: {distribution}", file=sys.stderr)
    return args


def python_has_distribution(python_exe: Path, distribution: str) -> bool:
    code = f"import importlib.metadata as m; m.version({distribution!r})"
    completed = subprocess.run([str(python_exe), "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return completed.returncode == 0


def run_checked(command: Sequence[str]) -> None:
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {completed.returncode}: {format_command(command)}")


def format_command(command: Sequence[str]) -> str:
    return " ".join(str(part) for part in command)


def remove_inside(path: Path, allowed_root: Path) -> None:
    if not path.exists():
        return
    resolved_path = path.resolve()
    resolved_root = allowed_root.resolve()
    common_path = os.path.commonpath([os.path.normcase(str(resolved_path)), os.path.normcase(str(resolved_root))])
    if common_path != os.path.normcase(str(resolved_root)):
        raise RuntimeError(f"Refusing to remove path outside allowed root. Path: {resolved_path} Root: {resolved_root}")
    if resolved_path.is_dir():
        shutil.rmtree(resolved_path)
    else:
        resolved_path.unlink()


def assert_windows_amd64_pe(path: Path) -> None:
    with path.open("rb") as exe:
        if exe.read(2) != b"MZ":
            raise RuntimeError(f"Output is not a Windows PE executable: {path}")
        exe.seek(0x3C)
        pe_offset = int.from_bytes(exe.read(4), "little")
        exe.seek(pe_offset)
        if exe.read(4) != b"PE\0\0":
            raise RuntimeError(f"Output has an invalid PE signature: {path}")
        machine = int.from_bytes(exe.read(2), "little")
    if machine != 0x8664:
        raise RuntimeError(f"Output exe is not amd64. PE machine: 0x{machine:04X}")


def assert_linux_amd64_elf(path: Path) -> None:
    with path.open("rb") as exe:
        header = exe.read(20)
    if len(header) < 20 or header[:4] != b"\x7fELF":
        raise RuntimeError(f"Output is not a Linux ELF executable: {path}")
    if header[4] != 2:
        raise RuntimeError(f"Output ELF is not 64-bit: {path}")
    if header[5] != 1:
        raise RuntimeError(f"Output ELF is not little-endian: {path}")
    machine = int.from_bytes(header[18:20], "little")
    if machine != 0x3E:
        raise RuntimeError(f"Output ELF is not amd64/x86_64. ELF machine: 0x{machine:04X}")

    mode = path.stat().st_mode
    if not mode & stat.S_IXUSR:
        path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
