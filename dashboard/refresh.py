#!/usr/bin/env python3
"""Build the data files behind the Support Desk.

Input:  conversations.json, the conversations the support passes curate from your inbox (and Slack, if it's on).
Output: out/conversations.json, out/prs.json, out/releases.json, out/unreleased.json, out/meta.json

For every fix linked to a conversation (a "repo#N" key whose repo is one of your products in config.json) it
asks GitHub, through gh, for the PR's state:

  open    release_state "open"    the fix is in review
  merged  release_state "live"    by default a merge to main is the release
  closed  release_state "closed"  closed without merging

A key whose repo isn't in config.json's products is listed with its conversation, with no state guessed for it.

If a product ships some other way (an app store, a versioned release, a deploy you run by hand), plug your own
check in at RELEASE_TRACKS below. Merged fixes then wait until your check says customers have them.

Read-only. It never merges, releases, sends mail or changes a label. It reads no secrets and writes none.

  python3 refresh.py            # PR states from GitHub (nothing goes online when no fix is linked)
  python3 refresh.py --stores   # also run the slower checks you added to RELEASE_TRACKS (the desk passes this)
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from config import CONFIG, products  # noqa: E402

OUT = HERE / "out"
OWNER = CONFIG.get("github", {}).get("owner", "")         # the GitHub user or org that owns your product repos
GMAIL_AUTHUSER = CONFIG.get("support", {}).get("gmail_authuser", "")  # the Google account the mailbox lands in
SLACK_WORKSPACE = CONFIG.get("slack", {}).get("workspace", "")        # e.g. your-workspace.slack.com
PR_FIELDS = "number,title,state,mergedAt,url"
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sh(cmd, cwd=None, timeout=300):
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError(f"{cmd[0]} isn't installed or isn't on PATH")
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])} failed: {EMAIL.sub('[email]', r.stderr.strip())[:300]}")
    return r.stdout


def gh_api(path, jq=None):
    out = sh(["gh", "api", path] + (["--jq", jq] if jq else []))
    return out if jq else json.loads(out)


def pr_view(repo, n):
    """gh's JSON for one PR: number, title, state (OPEN, MERGED or CLOSED), mergedAt and url."""
    return json.loads(sh(["gh", "pr", "view", str(n), "--repo", f"{OWNER}/{repo}", "--json", PR_FIELDS]))


# ---------- tracks: where each product ships ----------

def track(product, repo, **fields):
    """One release track, with the desk's defaults filled in: a merge to main is the release. Pass whatever your
    track does differently (see RELEASE_TRACKS for each field)."""
    t = {
        "track": repo, "app": product, "platform": products().get(product, {}).get("platform", ""), "repo": repo,
        "live_version": "main", "live_state": "live", "live_source": "GitHub: a merge to main is the release",
        "live_since": "", "live_note": "", "next_version": "", "next_stage": "Nothing to wait for: the next merge ships",
        "later": "",
        "place": lambda pr: ("live", "", "Merged to main"),
        "unreleased": [],
    }
    t.update(fields)
    return t


def main_branch_tracks(product, repo, linked, deep):
    """The default for every product: a fix is live the moment its PR merges. Makes no network calls."""
    return [track(product, repo)]


def github_release_tracks(product, repo, linked, deep):
    """EXAMPLE, off until you register it in RELEASE_TRACKS: a product that ships when you publish a GitHub
    release. A merged PR is live once the newest published release contains it; anything merged into the
    default branch after that release's tag waits for the next one. Read-only gh calls."""
    full = f"{OWNER}/{repo}"
    try:
        rel = json.loads(sh(["gh", "release", "view", "--repo", full, "--json", "tagName,publishedAt"]))
    except RuntimeError:  # no published release yet
        return [track(product, repo, live_version="none yet", live_state="unverified",
                      live_source="GitHub releases", live_note="No release published yet",
                      next_version="first release", next_stage="Merged fixes ship in the first release",
                      place=lambda pr: ("waiting", "next release", "Merged; no release published yet"))]
    tag = rel["tagName"]
    branch = gh_api(f"repos/{full}", jq=".default_branch").strip()
    ahead, page = set(), 1  # PRs merged into the default branch after the tag
    while True:
        firsts = gh_api(f"repos/{full}/compare/{urllib.parse.quote(tag)}...{branch}?per_page=100&page={page}",
                        jq='.commits[].commit.message | split("\\n")[0]').splitlines()
        for line in firsts:
            m = re.match(r"Merge pull request #(\d+) ", line) or re.search(r"\(#(\d+)\)$", line)
            if m:
                ahead.add(int(m.group(1)))
        if len(firsts) < 100:
            break
        page += 1
    merged = json.loads(sh(["gh", "pr", "list", "--repo", full, "--state", "merged", "--limit", "100",
                            "--json", "number,title,mergedAt,url,headRefName"]))
    waiting = [{"pr": f"{repo}#{p['number']}", "title": p["title"], "merged_at": (p.get("mergedAt") or "")[:10],
                "url": p["url"], "release": "next release", "release_note": f"Merged after {tag}",
                "support": p["headRefName"].startswith("support/")}
               for p in merged if p["number"] in ahead]
    return [track(product, repo, live_version=tag, live_source="GitHub releases (newest published)",
                  live_since=(rel.get("publishedAt") or "")[:10],
                  next_version="next release" if waiting else "",
                  next_stage=f"{len(waiting)} merged since {tag}" if waiting else "Nothing merged since",
                  place=lambda pr: (("waiting", "next release", f"Merged after {tag}; not released yet")
                                    if pr["number"] in ahead else ("live", tag, f"In {tag}")),
                  unreleased=waiting)]


# ---------- extension point: your own release checks ----------
#
# Out of the box, every product ships from main: a merged fix is live. If yours ships some other way, write a
# track function and register it here under the product's repo name from config.json:
#
#   RELEASE_TRACKS = {"example-app": github_release_tracks}   # the example above: live once in a GitHub release
#
# A track function runs once per refresh as  fn(product, repo, linked, deep):
#   product  the product's name in config.json, e.g. "Example App"
#   repo     its GitHub repo name, e.g. "example-app" (the owner is OWNER, from config.json's github.owner)
#   linked   {PR number: gh's JSON (number, title, state, mergedAt, url)} for this repo's fixes linked to
#            conversations, so you can batch your lookups
#   deep     True when refresh.py runs with --stores: the time for slow or signed-in checks (fastlane, a store
#            API, your deploy tool). Read-only, and read any credentials from their own tools, never from here.
# and returns a list of tracks, one per place the product ships (just one, or "iOS" and "Android"). Build each
# with track(product, repo, ...) and override what differs:
#   track         a short id, unique across products, e.g. "example-app-ios"
#   platform      shown beside the product's name, e.g. "iOS". With more than one track, each fix gets a row per
#                 track, labeled "Example App iOS", "Example App Android"
#   live_version  what customers have today, e.g. "1.4.2"; live_state "live", or "unverified" when you can't
#                 confirm it. live_source, live_since (YYYY-MM-DD) and live_note say where that came from
#   next_version, next_stage, later   what ships next and where it stands, e.g. "1.4.3", "In App Store review"
#   place(pr)     for a merged PR (gh's JSON): (release_state, release, release_note). release_state is "live"
#                 (customers have it), "waiting" (merged, not out yet) or "unverified" (sent, can't confirm it).
#                 release names the release, e.g. "1.4.3". notify.py plans an "it's live" note for "live", and an
#                 "it's on its way" note for "unverified" or a note that says "in review". A "waiting" fix whose
#                 release is "next release" puts a release card in the desk's For you list
#   unreleased    merged PRs not out yet, for the Releases view: dicts with pr ("example-app#12"), title,
#                 merged_at (YYYY-MM-DD), url, release, release_note and, optionally, support (True if it's a fix)
RELEASE_TRACKS = {}


def tracks_for(product, repo, linked, deep):
    fn = RELEASE_TRACKS.get(repo, main_branch_tracks)
    try:
        return fn(product, repo, linked, deep)
    except Exception as e:  # keep the last good out/ files rather than write a guess
        raise SystemExit(f"refresh: the release check for {product} ({fn.__name__}) failed: {e}")


def app_label(t, tracks):
    """The product's name, plus the platform when it ships on more than one track."""
    return f"{t['app']} {t['platform']}".strip() if len(tracks) > 1 else t["app"]


# ---------- links ----------

def gmail_url(thread):
    who = urllib.parse.quote(GMAIL_AUTHUSER, safe="")
    return f"https://mail.google.com/mail/u/?authuser={who}#all/{thread}"


def slack_url(c):
    """A Slack conversation's first message (see slack.md). Slack rows have no Gmail threads."""
    s = c.get("slack") or {}
    ts = (s.get("ts") or [""])[0]
    if not (ts and s.get("channel") and SLACK_WORKSPACE):
        return ""
    return f"https://{SLACK_WORKSPACE}/archives/{s['channel']}/p{ts.replace('.', '')}"


def split_key(ref):
    """("repo#12", "ios", "repo", 12) from "repo#12:ios". The part after ":" limits a fix to the tracks whose
    id ends with it; the number is None when the key isn't repo#number."""
    key, _, only = ref.partition(":")
    name, _, n = key.partition("#")
    return key, only, name, int(n) if n.isdigit() else None


def write(name, rows):
    tmp = OUT / f".{name}.json.tmp"
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n")
    os.replace(tmp, OUT / f"{name}.json")  # the desk polls these, so it never sees half a file
    print(f"out/{name}.json: {len(rows)} rows")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stores", action="store_true", help="also run the slower checks you added to RELEASE_TRACKS")
    args = ap.parse_args()
    src = json.loads((HERE / "conversations.json").read_text())
    convs = src.get("conversations", [])
    stamp = now()

    catalog = {name: p for name, p in products().items() if p.get("repo")}
    repo_product = {}
    for name, p in catalog.items():
        repo_product.setdefault(p["repo"], name)

    # Each linked fix is read once, however many conversations link it.
    wanted = sorted({(name, n) for c in convs for ref in c.get("prs") or []
                     for _, _, name, n in [split_key(ref)] if name in repo_product and n})
    if wanted and not OWNER:
        raise SystemExit("refresh: set github.owner in config.json")
    with ThreadPoolExecutor(8) as ex:
        fetched = dict(zip(wanted, ex.map(lambda k: pr_view(*k), wanted)))
    linked = {}
    for (name, n), p in fetched.items():
        linked.setdefault(name, {})[n] = p

    tracks = {name: tracks_for(name, p["repo"], linked.get(p["repo"], {}), args.stores) for name, p in catalog.items()}

    pr_rows, conv_rows = [], []
    for c in convs:
        cust = c.get("first_name") or "Unnamed"
        states, waits, keys = [], [], []
        for ref in c.get("prs") or []:
            key, only, name, n = split_key(ref)
            keys.append(key)
            if name not in repo_product or not n:  # a repo the desk doesn't know: listed, no state guessed
                continue
            p = fetched[(name, n)]
            product = c.get("product") if catalog.get(c.get("product"), {}).get("repo") == name else repo_product[name]
            mine = tracks[product]
            for t in [t for t in mine if not only or t["track"].endswith(only)]:
                if p["state"] == "MERGED":
                    state, release, note = t["place"](p)
                else:
                    state, release, note = ("open" if p["state"] == "OPEN" else "closed"), "", "Not merged"
                pr_rows.append({
                    "key": f"{key} {t['platform']}".strip() if len(mine) > 1 else key,
                    "conversation": c["id"], "customer": cust, "pr": key,
                    "title": p["title"], "pr_state": p["state"].lower(), "merged_at": (p.get("mergedAt") or "")[:10],
                    "url": p["url"], "app": app_label(t, mine),
                    "release_state": state, "release": release, "release_note": note,
                })
                states.append(state)
                if state == "waiting":
                    waits.append(f"{app_label(t, mine)} {release}".strip())
                elif state == "open":
                    waits.append(f"{key} in review")
        if not states:
            rstate, rsum = "none", "Linked to a repo the desk doesn't track" if keys else "No fix linked"
        elif "waiting" in states:
            rstate, rsum = "waiting", "Waiting on " + " · ".join(dict.fromkeys(w for w in waits if " in review" not in w))
        elif "open" in states:
            rstate, rsum = "open", "Fix not merged: " + ", ".join(dict.fromkeys(w.split(" ")[0] for w in waits))
        elif "unverified" in states:
            rstate, rsum = "unverified", "Shipped; can't confirm it's live yet"
        elif "live" in states:  # a closed first try doesn't count against a fix that went live
            live_in = sorted({(r["app"] + " " + r["release"]).strip() for r in pr_rows
                              if r["conversation"] == c["id"] and r["release_state"] == "live"})
            rstate, rsum = "live", "Live in " + ", ".join(live_in)
        else:
            rstate, rsum = "closed", "Fix PR closed without merging"
        threads = c.get("threads") or []
        conv_rows.append({
            "id": c["id"], "customer": cust, "product": c.get("product", ""), "issue": c.get("issue", ""),
            "status": c.get("status", ""), "labels": " · ".join(c.get("labels") or []),
            "last_activity": c.get("last_activity", ""), "last_from": c.get("last_from", ""), "owed": c.get("owed", ""),
            "prs": ", ".join(keys), "release_state": rstate, "release_summary": rsum, "threads": len(threads),
            "needs_fix": bool(c.get("needs_fix")),
            "source": c.get("source", "gmail"),
            "gmail": gmail_url(threads[0]) if threads and c.get("source", "gmail") != "slack" else "",
            "slack": slack_url(c) if c.get("source") == "slack" else "",
        })

    release_rows, unreleased_rows = [], []
    linked_keys = {split_key(ref)[0] for c in convs for ref in c.get("prs") or []}
    for product, mine in tracks.items():
        for t in mine:
            for u in t["unreleased"]:
                unreleased_rows.append({
                    "key": f"{t['track']} {u['pr']}", "app": app_label(t, mine), "pr": u["pr"], "title": u.get("title", ""),
                    "merged_at": u.get("merged_at", ""), "url": u.get("url", ""),
                    "support": "support" if u.get("support") or u["pr"] in linked_keys else "",
                    "release": u.get("release", ""), "release_note": u.get("release_note", ""),
                })
            release_rows.append({
                "track": t["track"], "app": t["app"], "platform": t["platform"], "repo": t["repo"],
                "live_version": t["live_version"], "live_state": t["live_state"], "live_source": t["live_source"],
                "live_since": t["live_since"], "live_note": t["live_note"],
                "next_version": t["next_version"], "next_stage": t["next_stage"], "later": t["later"],
                "unreleased": len(t["unreleased"]), "checked_at": stamp,
            })

    meta_rows = [{"gmail_read_at": src.get("read_at", ""), "github_checked_at": stamp,
                  "store_lanes": "yes" if args.stores else "no"}]
    blob = json.dumps([conv_rows, pr_rows, release_rows, unreleased_rows, meta_rows], ensure_ascii=False)
    assert not EMAIL.search(blob), "an email address slipped into the output"
    OUT.mkdir(exist_ok=True)
    for name, rows in [("conversations", conv_rows), ("prs", pr_rows), ("releases", release_rows),
                       ("unreleased", unreleased_rows), ("meta", meta_rows)]:
        write(name, rows)


if __name__ == "__main__":
    main()
