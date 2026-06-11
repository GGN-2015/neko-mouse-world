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
full `/v1/chat/completions` URL.

After the client window opens, type instructions into the same terminal. The
agent joins the server as `ai-agent` by default, disables direct player
keyboard/mouse controls inside that client, and uses automation tools to move,
look, create `.box` assets, place/delete boxes, fill cuboid regions, and capture
screenshots. New terminal instructions interrupt the current movement policy;
the agent stops current automation movement before planning from the new
instruction.

Useful options:

```powershell
venv\Scripts\python.exe tools\ai_agent.py --host 127.0.0.1 --port 5678 --model gpt-4.1-mini --user-id builder-bot
```

- `--model` selects the OpenAI-compatible chat model.
- `--user-id` selects the requested multiplayer user ID.
- `--max-tool-steps` limits one instruction's tool-call rounds.
- `--max-fill-volume` caps bulk region edits.
- Type `exit` or `quit` to stop the agent.
