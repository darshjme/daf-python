"""One JSON request per invocation; stdout is reserved for one JSON result."""

import argparse
import asyncio
import json
import sys

from .protocol import MAX_REQUEST_BYTES, capabilities
from .runner import run


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON field: {key}")
        value[key] = item
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("capabilities", "run"))
    parser.add_argument("request", nargs="?", default="-", help="JSON file or - for stdin")
    args = parser.parse_args()
    if args.command == "capabilities":
        print(json.dumps(capabilities()))
        return 0
    try:
        if args.request == "-":
            raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        else:
            with open(args.request, "rb") as stream:
                raw = stream.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("request exceeds 1 MiB")
        value = json.loads(raw, object_pairs_hook=unique_object)
        result = asyncio.run(run(value))
        print(json.dumps(result, ensure_ascii=True))
        return 0 if result["status"] == "succeeded" else 1
    except (OSError, ValueError, RecursionError) as error:
        print(f"Invalid adapter request: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
