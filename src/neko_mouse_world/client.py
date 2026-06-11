from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile

from .app import NekoMouseWorldApp
from .client_net import NetworkWorldClient
from .net_protocol import is_valid_user_id
from .server import DEFAULT_HOST, DEFAULT_PORT
from .world_file import LoadedWorld, WorldMap, world_paths


def _user_id_argument(value: str) -> str:
    if value and not is_valid_user_id(value):
        raise argparse.ArgumentTypeError("--user-id may contain only letters, digits, underscores, and hyphens")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Neko Mouse World multiplayer client")
    parser.add_argument("--host", default=None, help="server TCP host")
    parser.add_argument("--port", default=None, type=int, help="server TCP port")
    parser.add_argument(
        "--user-id",
        default="",
        type=_user_id_argument,
        help="requested user ID; letters, digits, underscores, and hyphens only",
    )
    return parser


def create_client_app(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, user_id: str = "") -> NekoMouseWorldApp:
    """Create a networked client app for developer automation scripts.

    The caller owns the returned app and should call app.run(). When finished,
    close app.network_client if it is still present.
    """
    desired_user_id = _user_id_argument(user_id)
    network_client = NetworkWorldClient(host, int(port), desired_user_id=desired_user_id)
    loaded_world = LoadedWorld(paths=network_client.paths, world_map=network_client.world_map)
    return NekoMouseWorldApp(loaded_world, network_client=network_client)


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(raw_args)
    prompt_for_connection = args.host is None and args.port is None

    app: NekoMouseWorldApp | None = None
    network_client: NetworkWorldClient | None = None
    try:
        if prompt_for_connection:
            cache_root = Path(tempfile.gettempdir()) / "neko_mouse_world_client_pending"
            paths = world_paths(cache_root)
            paths.root.mkdir(parents=True, exist_ok=True)
            paths.boxes_dir.mkdir(parents=True, exist_ok=True)
            loaded_world = LoadedWorld(paths=paths, world_map=WorldMap())
            app = NekoMouseWorldApp(
                loaded_world,
                show_connect_dialog=True,
                default_connect_host=DEFAULT_HOST,
                default_connect_port=DEFAULT_PORT,
                default_user_id=args.user_id,
            )
        else:
            host = args.host or DEFAULT_HOST
            port = args.port if args.port is not None else DEFAULT_PORT
            network_client = NetworkWorldClient(host, port, desired_user_id=args.user_id)
            loaded_world = LoadedWorld(paths=network_client.paths, world_map=network_client.world_map)
            app = NekoMouseWorldApp(loaded_world, network_client=network_client)
        app.run()
    finally:
        if app and app.network_client is not None:
            app.network_client.close()
        elif network_client is not None:
            network_client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
