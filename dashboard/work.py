#!/usr/bin/env python3
"""Record what a Claude session is doing about a conversation, for the desk's
"Sessions at work" card.

  python3 work.py set <conversation or thread id> <working|pr-open|blocked|done> \
      [--session "<this session's title>"] [--pr <PR url>] [--note "<a few words>"]
  python3 work.py clear <conversation or thread id>

Writes work.json (the record the desk app reads) and out/work.json (a copy).

First names and products only, from conversations.json. A note is a few words
in your own voice: never an email address or customer text.
"""
import argparse
import datetime as dt
import fcntl
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from config import CONFIG  # noqa: E402

DESK = CONFIG.get("desk", {}).get("desk_name") or "Support Desk"
RECORD, OUT = HERE / "work.json", HERE / "out" / "work.json"
STATES = ("working", "pr-open", "blocked", "done")
KEEP_DONE = dt.timedelta(days=3)


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def conversation(cid):
    """The dashboard row this id names: its own id, or one of its Gmail threads."""
    rows = json.loads((HERE / "conversations.json").read_text())["conversations"]
    row = next((c for c in rows if c["id"] == cid or cid in (c.get("threads") or [])), None)
    if not row:
        raise SystemExit(f"No conversation {cid} in conversations.json")
    return row


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set")
    s.add_argument("id")
    s.add_argument("state", choices=STATES)
    s.add_argument("--session", default="")
    s.add_argument("--pr", default="")
    s.add_argument("--note", default="")
    c = sub.add_parser("clear")
    c.add_argument("id")
    args = ap.parse_args()
    if "@" in args.__dict__.get("note", ""):
        raise SystemExit("A note can't hold an email address")

    row = conversation(args.id)
    with open(HERE / ".work.lock", "w") as lock:  # one writer at a time across sessions
        fcntl.flock(lock, fcntl.LOCK_EX)
        work = json.loads(RECORD.read_text()) if RECORD.exists() else []
        old = next((w for w in work if w["conversation"] == row["id"]), {})
        work = [w for w in work if w["conversation"] != row["id"]]
        if args.cmd == "set":
            work.append({
                "conversation": row["id"], "customer": row["first_name"] or "Unnamed", "product": row["product"],
                "issue": row["issue"], "session": args.session or old.get("session", ""), "state": args.state,
                "pr": args.pr or old.get("pr", ""), "note": args.note[:140], "updated": now(),
            })
        cutoff = dt.datetime.now(dt.timezone.utc) - KEEP_DONE
        work = [w for w in work if w["state"] != "done"
                or dt.datetime.fromisoformat(w["updated"].replace("Z", "+00:00")) > cutoff]
        work.sort(key=lambda w: w["updated"], reverse=True)
        text = json.dumps(work, ensure_ascii=False, indent=1) + "\n"
        assert "@" not in text, "an email address slipped into work.json"
        RECORD.write_text(text)
        OUT.parent.mkdir(exist_ok=True)
        OUT.write_text(text)
    print(f"{args.cmd} {row['first_name'] or 'Unnamed'} ({row['product']}): {getattr(args, 'state', 'cleared')}. "
          f"The {DESK} shows it within a few seconds.")


if __name__ == "__main__":
    main()
