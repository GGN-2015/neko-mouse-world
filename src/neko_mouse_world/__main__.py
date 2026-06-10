from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "server":
        from .server import main as server_main

        return server_main(args[1:])
    if args and args[0] == "client":
        from .client import main as client_main

        return client_main(args[1:])

    from .client import main as client_main

    return client_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
