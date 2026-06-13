from __future__ import annotations

import sys


def _dispatch_module(argv: list[str]) -> int | None:
    if len(argv) < 2 or argv[0] != "-m":
        return None

    module = argv[1]
    module_args = argv[2:]
    if module in {"neko_mouse_world", "neko_mouse_world.client"}:
        from neko_mouse_world.client import main

        return main(module_args)
    if module == "neko_mouse_world.server":
        from neko_mouse_world.server import main

        return main(module_args)
    if module == "box_editor_view":
        from box_editor_view.__main__ import main

        return main(module_args)
    return None


def main() -> int:
    argv = sys.argv[1:]
    module_result = _dispatch_module(argv)
    if module_result is not None:
        return module_result

    if argv and argv[0] == "server":
        from neko_mouse_world.server import main as server_main

        return server_main(argv[1:])
    if argv and argv[0] == "client":
        from neko_mouse_world.client import main as client_main

        return client_main(argv[1:])
    if argv and argv[0] == "box-editor-view":
        from box_editor_view.__main__ import main as box_editor_main

        return box_editor_main(argv[1:])

    from neko_mouse_world.client import main as client_main

    return client_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
