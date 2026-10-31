"""Run the API from the command line.

`python -m app.api` serves the app. With `--replay` it serves without keys, so a new
question cannot cost anything. `python -m app.api warm` runs every suggested question
once with keys, so the answers are saved for later. It prints one line for each
question and the total cost, and it stops at a spending cap.
"""

import argparse
import sys

import uvicorn

from app.api import suggestions
from app.api.app import create_app
from app.config import Settings

HOST = "127.0.0.1"
PORT = 8000


def _line(result: suggestions.Warmed) -> str:
    return (
        f"{result.status:13} ${result.usd:.4f}  {result.paid_calls} paid, "
        f"{result.cache_hits} cached  {result.question}"
    )


def _warm() -> int:
    try:
        results = suggestions.warm()
    except suggestions.NoKeys as error:
        print(error, file=sys.stderr)
        return 1
    except suggestions.WarmStopped as error:
        print("\n".join(_line(r) for r in error.done))
        print(error, file=sys.stderr)
        return 1
    print("\n".join(_line(r) for r in results))
    paid = sum(r.paid_calls for r in results)
    print(f"total: ${sum(r.usd for r in results):.4f}, {paid} paid calls")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.api")
    parser.add_argument(
        "--replay",
        action="store_true",
        help="serve on keyless settings, whatever the environment and .env hold",
    )
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("warm", help="run every suggested question once, with keys")
    args = parser.parse_args(argv)
    if args.command == "warm":
        return _warm()
    app = create_app(Settings()) if args.replay else create_app()
    uvicorn.run(app, host=HOST, port=PORT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
