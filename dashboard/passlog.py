#!/usr/bin/env python3
"""The passes' log, for the desk's Autopilot view.

  python3 passlog.py add [--acked "Ana, Ben"] [--notes "..."] [--closed "..."] [--needs-owner "..."] --summary "..."
  python3 passlog.py show [N]

First names only. --needs-owner lists the conversations only you can answer. Keeps the
newest 200 passes in passlog.json.
"""
import argparse
import datetime as dt
import fcntl
import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
LOG = HERE / "passlog.json"


def names(s):
    return [n.strip() for n in (s or "").split(",") if n.strip()]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    for flag in ("acked", "notes", "closed", "needs-owner"):
        a.add_argument(f"--{flag}", default="")
    a.add_argument("--summary", required=True)
    s = sub.add_parser("show")
    s.add_argument("n", nargs="?", type=int, default=5)
    args = ap.parse_args()

    with open(HERE / ".passlog.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        log = json.loads(LOG.read_text()) if LOG.exists() else []
        if args.cmd == "show":
            print(json.dumps(log[-args.n:][::-1], ensure_ascii=False, indent=1))
            return
        entry = {"at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                 "acked": names(args.acked), "notes": names(args.notes), "closed": names(args.closed),
                 "needs_owner": names(args.needs_owner), "summary": args.summary[:300]}
        text = json.dumps((log + [entry])[-200:], ensure_ascii=False, indent=1) + "\n"
        assert "@" not in text, "an email address slipped into the pass log"
        LOG.write_text(text)
        print("logged")


if __name__ == "__main__":
    main()
