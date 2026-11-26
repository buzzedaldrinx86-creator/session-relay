#!/usr/bin/env python3
"""session-relay hook entry: `relay_hook.py post-tool` (PostToolUse) or `relay_hook.py stop` (Stop).
Advisory only: always exits 0 with no output on error; errors go to <data>/errors.log."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from relay_storage import data_root, log_error


def _main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if os.environ.get("RELAY_CODE"):
            sys.path.insert(0, os.environ["RELAY_CODE"])
        import relay
    except Exception:
        log_error(data_root(), "import relay")
        return 0
    return relay.run_hook(mode)


if __name__ == "__main__":
    try:
        _main()
    except Exception:
        log_error(data_root(), "wrapper")
    sys.exit(0)
