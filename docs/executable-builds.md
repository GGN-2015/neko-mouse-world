# Single-File Executable Builds

This project ships Python build scripts for PyInstaller-based single-file
executables.

Supported build targets:

- Windows amd64 server
- Windows amd64 client
- Linux amd64 server

PyInstaller builds for the current operating system. Build Windows executables
on Windows, and build the Linux server executable on Linux amd64. Do not expect a
Windows host to cross-compile a Linux executable.

## Requirements

- Use a 64-bit amd64/x86_64 Python environment.
- Run each build script from the repository root.
- The default Python is `venv\Scripts\python.exe` on Windows or
  `venv/bin/python` on Linux when it exists. Otherwise, the scripts use the
  Python currently running the build script, then try `python3` and `python`
  from `PATH`.
- Network access is needed the first time unless PyInstaller and project
  dependencies are already installed.

Each script verifies the selected Python platform and architecture before
building. It also verifies the output file format:

- Windows outputs must be amd64 PE files.
- Linux outputs must be 64-bit x86_64 ELF files.

## Windows Builds

Build the server:

```powershell
python tools\build_windows_server_exe.py
```

Build the client:

```powershell
python tools\build_windows_client_exe.py
```

Default outputs:

```text
dist\windows-amd64\neko-mouse-world-server.exe
dist\windows-amd64\neko-mouse-world-client.exe
```

The client build is windowed by default. Add `--console` when debugging startup
errors:

```powershell
python tools\build_windows_client_exe.py --console
```

## Linux Server Build

Run this on Linux amd64/x86_64:

```bash
python3 tools/build_linux_server_exe.py
```

Default output:

```text
dist/linux-amd64/neko-mouse-world-server
```

The Linux target is server-only. It does not build a Linux graphical client.

For broad Linux compatibility, build on the oldest glibc-based distribution you
intend to support, or build inside a compatible release container. A PyInstaller
binary built on a newer Linux distribution may not run on older distributions
with older system libraries.

## Common Options

All build scripts support:

```text
--python PATH_OR_COMMAND
```

Use a specific 64-bit Python.

```text
--dist-dir PATH
```

Write the built executable to a different folder.

```text
--skip-install
```

Skip installing/upgrading `pip`, `pyinstaller`, and the editable project. Use
this when the environment is already prepared.

```text
--clean
```

Remove the matching PyInstaller work folder before building.

## Examples

Build both Windows executables into the default output folder:

```powershell
python tools\build_windows_server_exe.py
python tools\build_windows_client_exe.py
```

Build both Windows executables with an explicit Python:

```powershell
python tools\build_windows_server_exe.py --python C:\Python313\python.exe
python tools\build_windows_client_exe.py --python C:\Python313\python.exe
```

Build Windows executables into a release folder:

```powershell
python tools\build_windows_server_exe.py --dist-dir .\release\windows-amd64
python tools\build_windows_client_exe.py --dist-dir .\release\windows-amd64
```

Build a Linux server into a release folder:

```bash
python3 tools/build_linux_server_exe.py --dist-dir ./release/linux-amd64
```

Rebuild without reinstalling dependencies:

```powershell
python tools\build_windows_server_exe.py --skip-install --clean
python tools\build_windows_client_exe.py --skip-install --clean
```

```bash
python3 tools/build_linux_server_exe.py --skip-install --clean
```

## Running The Outputs

Run the Windows server:

```powershell
.\dist\windows-amd64\neko-mouse-world-server.exe path\to\world-folder --host 127.0.0.1 --port 5678
```

Run the Windows client:

```powershell
.\dist\windows-amd64\neko-mouse-world-client.exe --host 127.0.0.1 --port 5678 --user-id neko
```

Run the Linux server:

```bash
./dist/linux-amd64/neko-mouse-world-server path/to/world-folder --host 127.0.0.1 --port 5678
```

For Windows single-player mode, keep both Windows exe files in the same folder:

```powershell
.\dist\windows-amd64\neko-mouse-world-server.exe path\to\world-folder --with-client
```

When frozen, `--with-client` looks for
`neko-mouse-world-client.exe` next to `neko-mouse-world-server.exe`. If that file
is present, the server launches it directly.

## Notes

PyInstaller one-file executables extract bundled files to a temporary directory
at startup. This is normal for single-file Python executables.

The Windows client includes Panda3D and bundled native libraries, but it still
needs a Windows machine with a usable graphics driver. Server-only mode does not
need a graphics window.

Some antivirus tools may scan or quarantine newly built PyInstaller one-file
executables. Code signing or adding the release folder to trusted paths may be
needed for distribution outside local development.
