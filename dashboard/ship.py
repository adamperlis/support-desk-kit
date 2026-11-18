#!/usr/bin/env python3
"""Coordinate shipping per repo, so fixes for the same product sent to ship on the
same day go out in one release instead of one each.

One release per repo per day. The first ship session of the day owns it and cuts
the release; later ones join it and only merge. The repos are your products' repos
in config.json. For a product where a merge to main is the release, there's nothing
to cut: sessions only merge, and the record just shows what went out today.

  python3 ship.py join <repo> <pr> --session "<this session's title>"
      prints OWNER, JOINED or NEXT (see ship-runbook.md)
  python3 ship.py merged <repo> <pr>
  python3 ship.py drop <repo> <pr>
  python3 ship.py set <repo> <collecting|building|submitted|live> [--version V] [--note TEXT]
  python3 ship.py status [<repo>]

A release whose owner hasn't updated it for STALE hours is taken over by the next
session that joins, so a stopped session can't hold up a release. "submitted" is for
a release waiting on someone else's review, such as an app store's.
"""
import argparse
import datetime as dt
import fcntl
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from config import products  # noqa: E402

RECORD = HERE / "ship.json"  # {repo: today's release}; the desk's Releases view reads it
REPOS = tuple(sorted({p["repo"] for p in products().values() if p.get("repo")})) or None  # None: any repo
STATES = ("collecting", "building", "submitted", "live")
STALE = dt.timedelta(hours=3)


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t):
    return t.isoformat().replace("+00:00", "Z")


def parse(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def describe(repo, t):
    prs = ", ".join(f"#{p['n']}{'' if p['merged'] else ' (not merged)'}" for p in t["prs"]) or "none yet"
    ver = f" {t['version']}" if t.get("version") else ""
    return (f"{repo} release{ver}: {t['state']}, owner \"{t['owner']}\", PRs {prs}"
            + (f", next release: {', '.join('#' + str(n) for n in t['next'])}" if t.get("next") else "")
            + f", updated {t['updated']}" + (f". {t['note']}" if t.get("note") else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    j = sub.add_parser("join")
    j.add_argument("repo", choices=REPOS)
    j.add_argument("pr", type=int)
    j.add_argument("--session", required=True)
    for name in ("merged", "drop"):
        x = sub.add_parser(name)
        x.add_argument("repo", choices=REPOS)
        x.add_argument("pr", type=int)
    s = sub.add_parser("set")
    s.add_argument("repo", choices=REPOS)
    s.add_argument("state", choices=STATES)
    s.add_argument("--version", default="")
    s.add_argument("--note", default="")
    st = sub.add_parser("status")
    st.add_argument("repo", nargs="?", choices=REPOS)
    args = ap.parse_args()

    with open(HERE / ".ship.lock", "w") as lock:  # one writer at a time across sessions
        fcntl.flock(lock, fcntl.LOCK_EX)
        releases = json.loads(RECORD.read_text()) if RECORD.exists() else {}
        t = releases.get(getattr(args, "repo", None) or "")
        today = dt.date.today().isoformat()
        out = ""

        if args.cmd == "status":
            out = "\n".join(describe(r, x) for r, x in releases.items() if not args.repo or r == args.repo) or "No releases recorded."
        elif args.cmd == "join":
            entry = {"n": args.pr, "merged": False, "session": args.session}
            active = t and t["day"] == today and t["state"] in ("collecting", "building")
            stale = active and now() - parse(t["updated"]) > STALE
            if active and t["state"] == "collecting" and not stale:
                if all(p["n"] != args.pr for p in t["prs"]):
                    t["prs"].append(entry)
                role = f"JOINED: merge #{args.pr} and stop; it ships in the release \"{t['owner']}\" is preparing."
            elif active and t["state"] == "building" and not stale:
                t.setdefault("next", [])
                if args.pr not in t["next"]:
                    t["next"].append(args.pr)
                role = (f"NEXT: today's {args.repo} release is already building. Merge #{args.pr} and stop; "
                        "it ships in the following release.")
            else:
                carried = [n for n in (t or {}).get("next", []) if n != args.pr]
                t = releases[args.repo] = {"day": today, "owner": args.session, "state": "collecting", "version": "",
                                         "prs": [entry] + [{"n": n, "merged": True, "session": ""} for n in carried],
                                         "next": [], "note": "", "updated": iso(now())}
                role = ("OWNER (took over a stalled release): " if stale else "OWNER: ") + \
                       f"you cut today's {args.repo} release. Follow the OWNER steps in ship-runbook.md."
            t["updated"] = iso(now())
            out = role + "\n" + describe(args.repo, t)
        elif not t:
            raise SystemExit(f"No {args.repo} release recorded: run join first")
        elif args.cmd in ("merged", "drop"):
            if args.cmd == "drop":
                t["prs"] = [p for p in t["prs"] if p["n"] != args.pr]
                t["next"] = [n for n in t.get("next", []) if n != args.pr]
            for p in t["prs"]:
                if p["n"] == args.pr:
                    p["merged"] = True
            t["updated"] = iso(now())
            out = describe(args.repo, t)
        else:  # set, by the owner
            t["state"] = args.state
            t["version"] = args.version or t.get("version", "")
            t["note"] = args.note[:200] or t.get("note", "")
            t["updated"] = iso(now())
            out = describe(args.repo, t)

        RECORD.write_text(json.dumps(releases, ensure_ascii=False, indent=1) + "\n")
    print(out)


if __name__ == "__main__":
    main()
