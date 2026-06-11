# AI Agent Tool

`tools/ai_agent.py` is a repository tool, not a packaged Python entry point.
The package build only discovers modules under `src/`, so this script stays out
of the `neko-mouse-world` Python package.

Start a server first, then run the agent from the project checkout:

```powershell
venv\Scripts\python.exe tools\ai_agent.py --host 127.0.0.1 --port 5678
```

The agent prompts on STDIN for an OpenAI-compatible endpoint and then for an SK
key using hidden input. The endpoint can be either a base URL, a `/v1` URL, or a
full `/v1/responses` URL. If an old `/v1/chat/completions` URL is entered, the
tool rewrites it to `/v1/responses`; requests are still sent through the
Responses API.

After the client window opens, type instructions into the same terminal. The
agent joins the server as `ai-agent` by default and disables direct player
keyboard/mouse controls inside that client. It talks back in the terminal while
it works, uses Responses function tools to move, look, create `.box` assets,
place/delete boxes, fill cuboid regions, and captures screenshots as visual
feedback after tool calls. New terminal instructions interrupt the current
movement policy; the agent stops current automation movement before planning
from the new instruction.

World-editing tools wait for queued network sends before returning tool results
to the model, so the agent is less likely to plan from a local-only optimistic
view while other clients are still catching up.
Bulk region fills upload the selected `.box` asset once, then send cell edits
without repeating the asset payload for every block.

Useful options:

```powershell
venv\Scripts\python.exe tools\ai_agent.py --host 127.0.0.1 --port 5678 --model gpt-4.1-mini --user-id builder-bot
```

- `--model` selects the OpenAI-compatible Responses model.
- `--user-id` selects the requested multiplayer user ID.
- `--request-timeout` controls how long one model request may wait; default is
  60 seconds.
- `--request-retries` retries transient Responses request failures; default is
  2 retries.
- `--max-tool-steps` limits one instruction's tool-call rounds.
- `--max-fill-volume` caps bulk region edits.
- `--screenshot-detail` controls the image detail sent to the model.
- Type `exit` or `quit` to stop the agent.
