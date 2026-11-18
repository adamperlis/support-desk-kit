#!/usr/bin/env python3
"""Tickets for the desk's owner: things across your products that only you can do
(a merge, a decision, a sign-in, a key, a store submission). They sit beside the customer
conversations, never in them. The desk's autopilot reads dashboard/, not this file, so a
ticket never sends an email, starts a fix or closes a conversation.

  python3 desk/tickets.py add --product "Example App" --title "Merge example-app#436" \
      --needed "Review and merge it once #418 is in." [--context "<one or two sentences>"] \
      [--link https://github.com/...|claude://...|/abs/path] [--priority now|soon|later] [--key example-app#436] [--source "<who added it>"]
  python3 desk/tickets.py list [--all]       open tickets (or every ticket) as JSON
  python3 desk/tickets.py show <ticket id or key>   one ticket as JSON
  python3 desk/tickets.py done <ticket id or key> [--note "<how it was done>"]
  python3 desk/tickets.py reopen <ticket id or key>
  python3 desk/tickets.py check-prs          close tickets whose GitHub PR was merged or closed

Add a ticket only when the owner asks for one. Mark it done as soon as its work is complete,
by whoever finished it: `done example-app#125`. A ticket whose key is the pull request it
links (key example-app#125, link .../example-app/pull/125) also closes by itself once that PR is merged
or closed; the desk checks every 15 minutes. A ticket that links a PR only as background
(another key) stays open until someone marks it done.

`--key` makes an add idempotent: adding a ticket whose key matches one that's still open
updates that ticket instead of adding a second. Use the PR ("repo#123") or a short slug.

Never put a secret, a token, an email address or a customer's surname in a ticket: name a
secret by where it lives ("EXAMPLE_API_KEY in your host's settings"), and people by first name.
"""
import argparse
import datetime as dt
import fcntl
import json
import pathlib
import re
import secrets
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
STORE = HERE / "tickets.json"
LOCK = HERE / ".tickets.lock"
PRIORITIES = ("now", "soon", "later")
ID = re.compile(r"t-[0-9a-f]{8}")
LIMITS = {"product": 40, "title": 160, "needed": 400, "context": 800, "link": 500, "key": 120, "source": 120}


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load():
    try:
        rows = json.loads(STORE.read_text())
        return rows if isinstance(rows, list) else []
    except (OSError, ValueError):
        return []


def save(rows):
    tmp = STORE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n")
    tmp.replace(STORE)  # whole or not at all: the desk reads it every few seconds


def clean(field, value):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[: LIMITS[field]]


def add(product, title, needed, context="", link="", priority="soon", key="", source=""):
    """Add a ticket, or update the open one with the same key. Returns (ticket, created)."""
    fields = {"product": clean("product", product), "title": clean("title", title), "needed": clean("needed", needed),
              "context": clean("context", context), "link": clean("link", link), "key": clean("key", key),
              "source": clean("source", source)}
    if not (fields["product"] and fields["title"] and fields["needed"]):
        raise ValueError("A ticket needs a product, a title and what's needed from you.")
    if fields["link"] and not fields["link"].startswith(("https://", "claude://", "/")):
        raise ValueError("A link is an https:// address, a claude:// session link or an absolute file path.")
    if priority not in PRIORITIES:
        raise ValueError(f"Priority is one of {', '.join(PRIORITIES)}.")
    with open(LOCK, "w") as lock:  # the desk and sessions both write this file
        fcntl.flock(lock, fcntl.LOCK_EX)
        rows = load()
        same = fields["key"] and next((t for t in rows if t.get("key") == fields["key"] and t["status"] == "open"), None)
        if same:
            same.update({k: v for k, v in fields.items() if v}, priority=priority, updated=now())
            save(rows)
            return same, False
        ticket = {"id": f"t-{secrets.token_hex(4)}", **fields, "priority": priority, "status": "open",
                  "created": now(), "updated": now(), "done_at": None}
        rows.append(ticket)
        save(rows)
        return ticket, True


def find(tid):
    """A ticket by its id, or by its key (the open one with that key, else the latest)."""
    tid, rows = str(tid).strip(), load()
    if ID.fullmatch(tid):
        return next((x for x in rows if x["id"] == tid), None)
    same = [x for x in rows if tid and x.get("key") == tid]
    return next((x for x in reversed(same) if x["status"] == "open"), same[-1] if same else None)


def set_status(tid, status, note=""):
    """Mark a ticket done or open again, by its id or (for a session) its key. Returns the ticket, or
    None if there's no such ticket. By key it finds the open one to mark done, or the latest done one to reopen."""
    tid = str(tid).strip()
    if not tid or status not in ("open", "done"):
        return None
    with open(LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rows = load()
        if ID.fullmatch(tid):
            t = next((x for x in rows if x["id"] == tid), None)
        else:
            want = "open" if status == "done" else "done"
            t = next((x for x in reversed(rows) if x.get("key") == tid and x["status"] == want), None)
        if not t:
            return None
        t.update(status=status, done_at=now() if status == "done" else None, updated=now(),
                 done_note=clean("context", note) if status == "done" else "")
        save(rows)
        return t


PR_LINK = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)/?")


def gh_cli():
    """gh on PATH, or Homebrew's (a login item's PATH can be short)."""
    return shutil.which("gh") or next((p for p in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh") if pathlib.Path(p).exists()), None)


def check_prs():
    """Close every open ticket that is about a pull request (its key is "repo#123" and its link is that PR) once
    the PR has been merged or closed. Returns the closed tickets. Read-only on GitHub; a PR it can't read is left alone."""
    gh = gh_cli()
    if not gh:
        return []
    closed = []
    for t in [x for x in load() if x["status"] == "open"]:
        m = PR_LINK.fullmatch(t.get("link") or "")
        if not m:
            continue
        owner, repo, number = m.groups()
        if t.get("key") != f"{repo}#{number}":
            continue  # the PR is background to this ticket, not its subject
        try:
            r = subprocess.run([gh, "pr", "view", number, "--repo", f"{owner}/{repo}", "--json", "state", "--jq", ".state"],
                               capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        state = r.stdout.strip()
        if r.returncode == 0 and state in ("MERGED", "CLOSED"):
            done = set_status(t["id"], "done", f"{repo}#{number} was {state.lower()} on GitHub.")
            if done:
                closed.append(done)
    return closed


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("--product", required=True)
    a.add_argument("--title", required=True)
    a.add_argument("--needed", required=True, help="what only you can do, in one sentence")
    a.add_argument("--context", default="")
    a.add_argument("--link", default="")
    a.add_argument("--priority", default="soon", choices=PRIORITIES)
    a.add_argument("--key", default="", help="makes the add idempotent, e.g. example-app#436")
    a.add_argument("--source", default="", help="who added it, e.g. the session's title")
    ls = sub.add_parser("list")
    ls.add_argument("--all", action="store_true")
    sub.add_parser("show").add_argument("id", help="the ticket's id, or its key")
    d = sub.add_parser("done")
    d.add_argument("id", help="the ticket's id, or its key (e.g. example-app#125)")
    d.add_argument("--note", default="", help="how it was done, in one line")
    sub.add_parser("reopen").add_argument("id", help="the ticket's id, or its key")
    sub.add_parser("check-prs")
    args = ap.parse_args()

    if args.cmd == "add":
        try:
            t, created = add(args.product, args.title, args.needed, args.context, args.link, args.priority, args.key, args.source)
        except ValueError as e:
            sys.exit(str(e))
        print(f"{'Added' if created else 'Updated'} {t['id']}: {t['title']}")
    elif args.cmd == "list":
        print(json.dumps([t for t in load() if args.all or t["status"] == "open"], ensure_ascii=False, indent=1))
    elif args.cmd == "show":
        t = find(args.id)
        if not t:
            sys.exit(f"No ticket {args.id}.")
        print(json.dumps(t, ensure_ascii=False, indent=1))
    elif args.cmd == "check-prs":
        for t in check_prs():
            print(f"Done: {t['title']} ({t['done_note']})")
    else:
        t = set_status(args.id, "done" if args.cmd == "done" else "open", getattr(args, "note", ""))
        if not t:
            sys.exit(f"No ticket {args.id}.")
        print(f"{'Done' if args.cmd == 'done' else 'Reopened'}: {t['title']}")


if __name__ == "__main__":
    main()
