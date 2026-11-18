#!/usr/bin/env python3
"""Customer notes when a fix reaches a release milestone: worked out here, sent by a
Claude session (notify-runbook.md), and recorded so nobody hears the same news twice.
Each record lists the fixes its note covered, so a newer release that brings a
customer nothing new isn't news.

Milestones, per conversation and per app (and platform) its fix ships on:
  live    customers have the fix. By default that's when its PR merges to main
          (refresh.py); a release check of your own (refresh.py's RELEASE_TRACKS)
          can hold it until a store or a deploy says it's out
  review  your release check says the release with the fix is on its way: it
          reports the fix "unverified", or the fix's release note says "in review"

Email conversations only: a Slack report never gets an email (slack.md).

  python3 notify.py plan                         notes due now, as JSON (nobody holds them)
  python3 notify.py claim <key>... --by "<who>"  take them before sending, so two sessions never both send
  python3 notify.py sent <key>...                record them as sent
  python3 notify.py free <key>...                give a claim back without sending (after a failure)
  python3 notify.py baseline                     record everything due now as already told (first run only)
  python3 notify.py log                          what was sent, newest first

Reads out/conversations.json and out/prs.json (refresh.py writes both). Writes notified.json.
"""
import argparse
import datetime as dt
import fcntl
import json
import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent
LEDGER = HERE / "notified.json"
CLAIM_FOR = dt.timedelta(hours=2)  # a claim older than this was abandoned
IN_REVIEW = re.compile(r"in review|waiting for review|ready for review", re.I)  # in a waiting fix's release_note


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def load(name, default):
    try:
        return json.loads((HERE / name).read_text())
    except (OSError, ValueError):
        return default


def milestone(p):
    """('live' | 'review' | None) for one fix on one app or platform."""
    if p.get("pr_state") != "merged":
        return None
    if p.get("release_state") == "live":
        return "live"
    if p.get("release_state") == "unverified":  # sent out, but the release check can't confirm customers have it
        return "review"
    if p.get("release_state") == "waiting" and IN_REVIEW.search(p.get("release_note") or ""):
        return "review"
    return None


def told(ledger):
    """The fixes each (conversation, app, milestone) has heard about, from the sent notes that list them. A newer
    release keys the same fixes afresh (Example App iOS 1.0.26, then 1.0.27), so the key alone can't tell."""
    out = {}
    for k, v in ledger.items():
        if v["state"] == "sent":
            cid, app, _, m = k.split("|")
            out.setdefault((cid, app, m), set()).update(v.get("prs", []))
    return out


def due(ledger):
    """Notes due now, one entry per conversation, each listing its milestone items and only the fixes in them
    that are news to the customer."""
    convs = {c["id"]: c for c in load("out/conversations.json", [])}
    raw = {c["id"]: c for c in load("conversations.json", {}).get("conversations", [])}
    items = {}
    for p in load("out/prs.json", []):
        m = milestone(p)
        c = convs.get(p["conversation"])
        if not m or not c:
            continue
        if c.get("source") == "slack" or raw.get(c["id"], {}).get("source") == "slack":  # no email (slack.md)
            continue
        key = f"{c['id']}|{p['app']}|{p['release']}|{m}"
        entry = items.setdefault(key, {"key": key, "milestone": m, "app": p["app"], "release": p["release"],
                                       "note": p.get("release_note", ""), "prs": []})
        entry["prs"].append({"pr": p["pr"], "title": p["title"]})
    # Live beats review: once an app is live, its review note is moot.
    live_apps = {(k.split("|")[0], v["app"]) for k, v in items.items() if v["milestone"] == "live"}
    heard = told(ledger)
    t = now()
    out = {}
    for key, it in items.items():
        cid = key.split("|")[0]
        if it["milestone"] == "review" and (cid, it["app"]) in live_apps:
            continue
        rec = ledger.get(key)
        if rec and (rec["state"] == "sent" or t - dt.datetime.fromisoformat(rec["at"].replace("Z", "+00:00")) < CLAIM_FOR):
            continue
        known = heard.get((cid, it["app"], "live"), set())
        if it["milestone"] == "review":  # "it's live" already covers "it's on its way"
            known = known | heard.get((cid, it["app"], "review"), set())
        it["prs"] = [x for x in it["prs"] if x["pr"] not in known]
        if not it["prs"]:  # a newer release of fixes they've already heard about
            continue
        c = convs[cid]
        conv = out.setdefault(cid, {"conversation": cid, "customer": c["customer"], "product": c["product"],
                                    "issue": c["issue"], "status": c["status"], "owed": c["owed"],
                                    "threads": raw.get(cid, {}).get("threads", [cid]), "items": []})
        conv["items"].append(it)
    return list(out.values())


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan")
    sub.add_parser("baseline")
    sub.add_parser("log")
    c = sub.add_parser("claim")
    c.add_argument("keys", nargs="+")
    c.add_argument("--by", required=True)
    for name in ("sent", "free"):
        sub.add_parser(name).add_argument("keys", nargs="+")
    args = ap.parse_args()

    with open(HERE / ".notify.lock", "w") as lock:  # one writer at a time across sessions
        fcntl.flock(lock, fcntl.LOCK_EX)
        ledger = load("notified.json", {})
        stamp = now().isoformat().replace("+00:00", "Z")
        if args.cmd == "plan":
            print(json.dumps(due(ledger), ensure_ascii=False, indent=1))
            return
        if args.cmd == "log":
            for k, v in sorted(ledger.items(), key=lambda kv: kv[1]["at"], reverse=True):
                print(v["at"], v["state"], v.get("by", ""), k)
            return
        # Each note records the fixes it covered, so the next release of the same fixes isn't news (told()).
        covers = lambda: {it["key"]: [x["pr"] for x in it["prs"]] for conv in due(ledger) for it in conv["items"]}  # noqa: E731
        if args.cmd == "baseline":
            open_items = covers()
            for k, prs in open_items.items():
                ledger[k] = {"state": "sent", "by": "baseline (already told before notes were automatic)", "at": stamp,
                             "prs": prs}
            print(f"baselined {len(open_items)}")
        elif args.cmd == "claim":
            open_items = covers()
            taken = [k for k in args.keys if k in open_items]
            for k in taken:
                ledger[k] = {"state": "claimed", "by": args.by, "at": stamp, "prs": open_items[k]}
            print(json.dumps({"claimed": taken, "skipped": [k for k in args.keys if k not in taken]}))
        elif args.cmd == "sent":
            open_items = covers()
            for k in args.keys:
                prev = ledger.get(k, {})
                ledger[k] = {"state": "sent", "by": prev.get("by", ""), "at": stamp,
                             "prs": prev.get("prs") or open_items.get(k, [])}
            print(f"recorded {len(args.keys)} as sent")
        elif args.cmd == "free":
            for k in args.keys:
                if ledger.get(k, {}).get("state") == "claimed":
                    del ledger[k]
            print(f"freed {len(args.keys)}")
        LEDGER.write_text(json.dumps(ledger, ensure_ascii=False, indent=1) + "\n")


if __name__ == "__main__":
    main()
