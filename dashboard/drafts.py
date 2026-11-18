#!/usr/bin/env python3
"""Reply drafts waiting for you in the Support Desk. A drafting session saves one here
and stops. You read it in the desk, edit it if you want, and press Send, which
starts a session that sends exactly the approved text (send-runbook.md).

  python3 drafts.py add --conversation <id> --thread <thread id> --subject "<subject>" --body-file <file> [--why "<one line>"]
                        [--lang <language, e.g. Indonesian> --english-file <file>]   for a reply in another language
  python3 drafts.py show <draft id>          the draft as JSON (the approved text once approved)
  python3 drafts.py sent <draft id>          after sending
  python3 drafts.py failed <draft id> --why "<reason>"
  python3 drafts.py list                     drafts waiting for you

The desk approves and discards drafts itself. Customer addresses are never stored
here: the sending session takes the address from the Gmail thread.

A reply in the customer's own language carries `lang` and `body_en`, an English version
for you. The desk shows it on an English tab, and turns your English edits back into the
customer's language before you send. `body` is always the text that goes out.
"""
import argparse
import datetime as dt
import fcntl
import json
import pathlib
import secrets
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from config import CONFIG  # noqa: E402

STORE = HERE / "drafts.json"
DESK = CONFIG.get("desk", {}).get("desk_name") or "Support Desk"


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def conversation(cid):
    rows = json.loads((HERE / "conversations.json").read_text())["conversations"]
    return next((c for c in rows if c["id"] == cid or cid in (c.get("threads") or [])), None)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("--conversation", required=True)
    a.add_argument("--thread", required=True)
    a.add_argument("--subject", required=True)
    a.add_argument("--body-file", required=True)
    a.add_argument("--why", default="")
    a.add_argument("--lang", default="", help="the reply's language when it isn't English")
    a.add_argument("--english-file", help="an English version of the body, for the desk's owner to read")
    for name in ("show", "sent"):
        sub.add_parser(name).add_argument("id")
    f = sub.add_parser("failed")
    f.add_argument("id")
    f.add_argument("--why", required=True)
    sub.add_parser("list")
    args = ap.parse_args()

    with open(HERE / ".drafts.lock", "w") as lock:  # the desk and sessions write this file
        fcntl.flock(lock, fcntl.LOCK_EX)
        drafts = json.loads(STORE.read_text()) if STORE.exists() else []
        if args.cmd == "list":
            print(json.dumps([d for d in drafts if d["status"] == "pending"], ensure_ascii=False, indent=1))
            return
        if args.cmd == "add":
            c = conversation(args.conversation)
            if not c:
                raise SystemExit(f"No conversation {args.conversation}")
            body = pathlib.Path(args.body_file).read_text().strip()
            if "@" in body.split("\n")[0] or len(body) > 4000:
                raise SystemExit("A draft body is the email text only: no address line, under 4000 characters")
            # A newer draft for the same conversation replaces a pending one.
            for d in drafts:
                if d["conversation"] == c["id"] and d["status"] == "pending":
                    d["status"] = "replaced"
            d = {"id": secrets.token_hex(4), "conversation": c["id"], "customer": c["first_name"] or "Unnamed",
                 "product": c["product"], "thread": args.thread, "subject": args.subject, "body": body,
                 "why": args.why[:300], "status": "pending", "created": now(), "updated": now()}
            if args.lang and args.lang.strip().lower() != "english":
                if not args.english_file:
                    raise SystemExit("A reply in another language needs --english-file, the English version for the desk")
                d.update(lang=args.lang.strip()[:40], body_en=pathlib.Path(args.english_file).read_text().strip()[:4000])
            elif args.lang:
                d["lang"] = "English"
            drafts.append(d)
            out = f"saved draft {d['id']} for {d['customer']}: it waits in the {DESK}'s For you list"
        else:
            d = next((x for x in drafts if x["id"] == args.id), None)
            if not d:
                raise SystemExit(f"No draft {args.id}")
            if args.cmd == "show":
                print(json.dumps(d, ensure_ascii=False, indent=1))
                return
            d["status"] = "sent" if args.cmd == "sent" else "failed"
            d["updated"] = now()
            if args.cmd == "failed":
                d["error"] = args.why[:300]
            out = f"draft {d['id']}: {d['status']}"
        STORE.write_text(json.dumps(drafts[-300:], ensure_ascii=False, indent=1) + "\n")
    print(out)


if __name__ == "__main__":
    main()
