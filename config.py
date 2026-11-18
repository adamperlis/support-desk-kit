"""The desk's settings: your config.json, or config.example.json until you've made one.

Everything that's about your own setup lives in config.json (gitignored): your name, your support mailbox,
the Composio account that reads it, your Slack channel, your GitHub user and your products. The code and
the runbooks read it from here, so nothing about you has to be written into them.

    from config import CONFIG, code_dir, products
"""
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent


def _load():
    mine = ROOT / "config.json"
    path = mine if mine.exists() else ROOT / "config.example.json"
    if path != mine:
        print("support desk: no config.json yet, using config.example.json (copy it and fill it in)", file=sys.stderr)
    return json.loads(path.read_text())


CONFIG = _load()


def code_dir():
    """The folder your product repos are cloned under."""
    return pathlib.Path(os.path.expanduser(CONFIG["desk"]["code_dir"]))


def products():
    """{product name: {"repo": GitHub repo name, "folder": folder under code_dir()}} for the products the
    fix sessions can work on."""
    return CONFIG.get("products", {})


def owner():
    return CONFIG["desk"].get("owner_name") or "the owner"


if __name__ == "__main__":  # `python3 config.py` prints what the desk will use, to check your file
    print(json.dumps(CONFIG, indent=2))
