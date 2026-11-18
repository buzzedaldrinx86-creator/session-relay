#!/usr/bin/env python3
"""session-relay hook entry: `relay_hook.py post-tool` (PostToolUse) or `relay_hook.py stop` (Stop).
Advisory only: always exits 0 with no output on error; errors go to <data>/errors.log."""
import os, sys, time, traceback

HERE = os.path.dirname(os.path.abspath(__file__))


def _log(where):
    try:
        root = (os.environ.get("RELAY_ROOT") or os.environ.get("CLAUDE_PLUGIN_DATA")
                or os.path.join(os.path.expanduser("~"), ".session-relay"))
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, "errors.log"), "a") as f:
            f.write("%s %s\n%s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), where, traceback.format_exc()))
    except Exception:
        pass


def _main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        sys.path.insert(0, os.environ.get("RELAY_CODE") or HERE)
        import relay
    except Exception:
        _log("import relay")
        return 0
    return relay.run_hook(mode)


if __name__ == "__main__":
    try:
        _main()
    except Exception:
        _log("wrapper")
    sys.exit(0)
