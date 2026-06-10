# neko-mouse-world

Voxel-based cubic world editor built from reusable `.box` files created with
`box-editor-view`.

## Run

Use the project virtual environment:

```powershell
venv\Scripts\python.exe -m neko_mouse_world.server path\to\world-folder --host 127.0.0.1 --port 5678
venv\Scripts\python.exe -m neko_mouse_world.client --host 127.0.0.1 --port 5678
```

If the client is started without `--host` and `--port`, it opens a modal
connection dialog first. Fill in the server host and TCP port, then press OK.

The world path is a folder. When `info.world` or `boxes/` is missing, the editor
creates them automatically. If `info.world` exists but is malformed, startup
prints `FormatError` and exits.

For single-player convenience, run the server with a main local client:

```powershell
venv\Scripts\python.exe -m neko_mouse_world.server path\to\world-folder --with-client
```

In `--with-client` mode the server runs in the background, launches one client
connected to itself, and shuts down when that main client exits.

The server accepts `--udp-host` and `--udp-port`. The default UDP port is `0`,
which means the OS chooses a free port. The selected UDP endpoint is negotiated
over TCP. World/map changes always use TCP. Player positions first try UDP; the
client sends 5 probe packets and UDP is used only when at least 3 probes succeed.
If the test fails, player positions automatically fall back to TCP.

## World Format

`info.world` is a SQLite database containing the world grid. Each occupied world
cell stores the content hash of a `.box` file plus an `orientation` value from
`0..23`. The hash identifies the reusable shape; the orientation only rotates
that instance. The corresponding file is loaded from:

```text
boxes/<hash>.box
```

The hash is the same stable digest printed by:

```powershell
venv\Scripts\python.exe -m box_editor_view --hash some.box
```

If `info.world` references a missing `.box` file, that world cell is removed on
load and the repaired world file is saved.

## Controls

- Mouse look after the mouse is captured.
- `WASD`: move.
- `F`: switch walk and fly modes.
- Walk mode `Space`: jump 1.1 world units.
- Fly mode `Space` / `Shift`: move up / down.
- Right click: place the selected `.box`.
- Left click: delete the targeted world box.
- Middle click: select the targeted world box type.
- `E`: edit the targeted world box in `box-editor-view`.
- Numpad `4` / `6`: rotate the targeted box around the player's view-up axis.
- Numpad `8` / `2`: rotate the targeted box around the player's view direction axis.
- `F2` or `Ctrl+S`: save.
- `F5`: switch first-person / third-person view.
- `C`: look at the world-box centroid, or the origin when the world is empty.
- `H`: show help.
- `Esc`: release the mouse and show exit choices.

Placing, deleting, selecting, editing, and hover highlighting only work within
10 world units of the player.

World boxes collide using the convex hull of their `.box` voxel vertices, not a
full cube. In walk mode the player can step up onto obstacles up to 0.5 world
units high and follows convex slope surfaces.

The default placed object is the gray `N=0` single-cube `.box`.
