# Server Command Console

The in-game server command console is opened with `~` in client/server mode when
no other modal window is open.

Use the bottom input field to type a command. Press `Enter` or click `Send` to
send it to the server. The top-right `X` button or `Esc` closes the console and
returns to the game.

The command input accepts up to 4096 characters. It grows from one to three
wrapped lines as you type, returns to its original height when the text becomes
short again, and shows a small scrollbar when the content is longer than the
visible input area.

The console displays the latest 300 server log lines. Server stdout is shown in
the normal log color, and server stderr is shown in red. Use the scrollbar on
the right side to inspect older lines.

Server logs are visible only when your current `allow_cmd` permission is true.
Without that permission, the console displays only this red line:

```text
server log: permission denied, use pin('PIN-CONTENT') to retrive admin access.
```

If your command permission is revoked while the console is open, the client
immediately clears the old log view and replaces it with that line. A top-level
`pin("secret")` command can still be sent without command permission. If the
server was started without `--pin`, it generated a random nine-digit PIN and
printed `Random Pin: <pin>` to the server process stdout only; that line is not
visible in this in-game log view.

## Commands

Commands are entered as restricted Python expressions. The server exposes only
the documented command functions, but normal list and dict literals, indexing,
boolean expressions, and int/float arithmetic are supported. Attribute access,
imports, comprehensions, lambdas, and undocumented function calls are rejected.
When a command returns a value other than `None`, the server writes `repr(value)`
to stdout, so returned strings appear with quotes.

Examples:

```python
1 + 2 * 3
{"target": "1", "pos": [10 / 2, 3 + 4]}
tp("1", 8 + 2, 4.5)
ignore(setenv("mode", {"fly": true}), getenv("mode"))
```

- `help()`: write a summary of available commands to the server log.
- `ls()`: return the list of online user IDs as strings.
- `seepri()`: write one permission row for every online user to stdout:
  `<user_id>: set: <can_set>, break: <can_break>, fly: <can_fly>, cmd: <can_cmd>`.
- `stop()`: kick every connected user with reason
  `server stopped by command (triggered by <user_id>)`, stop accepting new
  logins, save the world, remove unused `.box` files, and gracefully stop the
  server.
- `kick(user_id, reason="")`: kick the online player named by `user_id`.
  The kicked client shows a blocking message and exits after the user clicks
  `OK`.
- `setenv(name, value)`: set a non-empty server environment variable name to
  `str(value)`.
- `getenv(name)`: return the server environment variable value, or `None`.
- `tp(user_id, x, y)`: teleport the player to `x, y` and place them on the
  highest occupied world-grid cell in that column, or on ground `z=0` if the
  column is empty.
- `tp(user_id, x, y, z)`: teleport the player to the exact `x, y, z` position.
- `allow_set(user_id, true/false)`: allow or deny placing boxes, rotating
  existing boxes, restoring deleted boxes, and editing boxes with `E`.
- `allow_fly(user_id, true/false)`: allow or deny switching to fly mode. When
  disabled, the target client immediately returns to walk mode.
- `allow_break(user_id, true/false)`: allow or deny breaking boxes.
- `allow_cmd(user_id, true/false)`: allow or deny using server commands.
- `pin(secret)`: if the value matches the server PIN, enable commands for your
  own player. The PIN is either the explicit `--pin secret` value or, when
  `--pin` is omitted, the random nine-digit decimal value printed as
  `Random Pin: <pin>` to the server process stdout only. A top-level `pin(...)`
  call is the only command accepted when your current `allow_cmd` permission is
  false. Running `pin(...)` while already allowed is harmless; a wrong secret
  does not remove existing command permission. The submitted secret is redacted
  from server stdout, stderr, and the in-game log.
- `ignore(*args)`: evaluate any number of arguments and always return `None`.
  This is useful when you want side effects from nested commands without logging
  a returned value.

## Default Permission Environment

New clients start with permissions from these server environment variables:

| Variable | Permission | Default |
| --- | --- | --- |
| `DEFAULT_SET` | placing, rotating/restoring boxes, and editing with `E` | `true` |
| `DEFAULT_FLY` | switching to fly mode | `true` |
| `DEFAULT_BREAK` | breaking boxes | `true` |
| `DEFAULT_CMD` | using server commands | `true` |

Each variable accepts `true`/`false`, `1`/`0`, `yes`/`no`, or `on`/`off`.
The values are read only when a client joins, so changing them affects future
clients but not players who are already online.

Initial values can also be supplied when starting the server with repeated
`--setenv NAME=value` options. For example:

```powershell
venv\Scripts\python.exe -m neko_mouse_world.server path\to\world-folder --setenv DEFAULT_FLY=false --setenv DEFAULT_SET=false --setenv DEFAULT_BREAK=false --setenv DEFAULT_CMD=false
```

## Client Display Environment

`DISPLAY_USER_ID` controls whether clients draw online user IDs above other
players' heads in both first-person and third-person view. It defaults to
`true` and accepts the same boolean values as the permission variables. Unlike
the default permission variables, changing it with
`setenv("DISPLAY_USER_ID", false)` or `setenv("DISPLAY_USER_ID", true)`
broadcasts the new setting to every connected client over TCP.

`ALLOW_CONNECT` controls new client handshakes. It defaults to `true`. When it
is `false`, new clients receive `Server do not allow connect. (ALLOW_CONNECT =
False)`, show a one-button modal, stop retrying, and exit after the user clicks
OK or closes the window. Existing connected clients are not kicked by changing
this value.

## User IDs

Clients may start with `--user-id NAME` to request a human-readable online user
ID. During the TCP handshake, the client sends the requested value and the
server replies with the actual value in the welcome message. If the requested ID
is already online, the server appends `_1`, `_2`, and so on. Requested IDs may
contain only ASCII letters, digits, underscores, and hyphens. If no ID is
requested, the server assigns the first unused positive integer string. The
`--with-client` main local client requests `root` by default.

Server commands such as `kick`, `tp`, `allow_set`, `allow_fly`, `allow_break`,
and `allow_cmd` use these string user IDs. Numeric internal player IDs are still
accepted as a compatibility fallback.

Every received command is also recorded in the server log as:

```text
<user id>: <command text>
```
