# Client Developer API

The client exposes a small Python API for automation scripts. The API is
available on `NekoMouseWorldApp`, and `neko_mouse_world.client.create_client_app`
creates a ready-to-run network client.

```python
from neko_mouse_world.client import create_client_app

app = create_client_app("127.0.0.1", 5678, user_id="bot")

def drive(task):
    print(app.get_player_position())
    print(app.get_box_hash_at(0, 0, 0))
    app.capture_screenshot()
    app.set_automation_movement(forward=1.0)
    return task.cont

app.taskMgr.add(drive, "drive-bot")
app.run()
```

## World Queries

- `get_player_position()` returns `(x, y, z)` for the local player's feet.
- `get_player_coordinates()` is an alias for `get_player_position()`.
- `get_player_pose()` returns a dictionary with position, velocity, heading,
  pitch, movement mode, and grounded state.
- `get_box_hash_at(x, y, z)` returns the `.box` hash at a world-grid cell, or
  `None` if the cell is empty.
- `get_box_hash_at((x, y, z))` is also accepted.
- `get_box_orientation_at(x, y, z)` returns the orientation `0..23`, or `None`
  if the cell is empty.
- `get_box_at(x, y, z)` returns `(hash, orientation)`, or `None` if empty.

## Screenshots

- Press `F12` in-game to save a screenshot under the world's `screenshots/`
  directory.
- `capture_screenshot(path=None)` captures the current client window and
  returns the written `Path`, or `None` if saving failed.
- When `path` is omitted, the client writes a timestamped PNG under
  `screenshots/` in the opened world folder.
- When `path` has no suffix, `.png` is added.
- `take_screenshot(path=None)` and `save_screenshot(path=None)` are aliases for
  `capture_screenshot(path=None)`.

## Movement Automation

- `set_human_input_enabled(False)` disables direct keyboard/mouse gameplay
  controls while keeping automation APIs active. Use it for visible agent-owned
  clients.
- `get_human_input_enabled()` returns the current direct-input state.
- `set_automation_movement(forward=0, right=0, vertical=0)` sets scripted
  movement axes. Each value is clamped to `-1..1`.
- `stop_automation_movement()` clears scripted movement without touching the
  user's keyboard state.
- `automation_jump()` requests one walk-mode jump on the next update.
- `set_automation_look(heading=None, pitch=None)` sets camera angles in degrees.
- `turn_automation_look(heading_delta=0, pitch_delta=0)` turns relative to the
  current camera angles.

Keyboard and mouse controls keep working by default. Scripted movement is added
to normal keyboard movement and then clamped, so automation does not replace the
existing control path unless direct human input is explicitly disabled. Movement
automation obeys the same client state as player input: it does not move the
player while loading screens or modal UI block controls.
