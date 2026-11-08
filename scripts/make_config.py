#!/usr/bin/env python3
"""Builds config.json from config.template.json by substituting ${VAR_NAME}
placeholders with environment variables, so secrets never have to be typed
into a chat, a command-line arg, or committed to the repo.

Edit config.template.json (tracked in git) to add real services/notifiers.
Any field that should be a secret gets a ${SOME_ENV_VAR} placeholder instead
of a literal value. Then, in your own terminal:

    export MINUTEPING_WIFI_SSID="your-wifi-name"
    export MINUTEPING_WIFI_PASSWORD="your-wifi-password"
    export MINUTEPING_GATEWAY_IP="192.168.1.1"
    python3 scripts/make_config.py

This writes config.json (gitignored) in the project root. Values are never
printed.
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLACEHOLDER = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def substitute(node, missing):
    if isinstance(node, dict):
        return {key: substitute(value, missing) for key, value in node.items()}
    if isinstance(node, list):
        return [substitute(value, missing) for value in node]
    if isinstance(node, str):
        match = PLACEHOLDER.match(node)
        if match:
            var_name = match.group(1)
            if var_name not in os.environ:
                missing.append(var_name)
                return node
            return os.environ[var_name]
    return node


def main():
    template_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "config.template.json")
    out_path = os.path.join(ROOT, "config.json")

    with open(template_path) as f:
        template = json.load(f)

    missing = []
    config = substitute(template, missing)

    if missing:
        print("Missing environment variables for: {}".format(", ".join(sorted(set(missing)))),
              file=sys.stderr)
        sys.exit(1)

    with open(out_path, "w") as f:
        json.dump(config, f, indent=2)

    print("Wrote {} ({} bytes).".format(out_path, os.path.getsize(out_path)))


if __name__ == "__main__":
    main()
