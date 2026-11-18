#!/usr/bin/env python3
"""Support Desk: a local app over the support dashboard's data, with buttons that act.

  python3 desk/server.py            # http://127.0.0.1:<desk.port>, open it in the Claude app's Browser pane

Everything about your own setup (your name, the port, your products and repos, the mailbox's Composio
account, the Slack channel, the autopilot's hours) comes from config.json at the kit's root; see config.py.

It reads dashboard/out/*.json, dashboard/work.json and dashboard/ship.json straight
from disk, so it is always as current as the last refresh and shows each session's
status within seconds.

Actions start a Claude Code session in the background on this Mac (`claude --bg`, auto
permission mode) that does the work right away, following the matching runbook in
dashboard/. The desk tracks each one (`claude agents`) and shows its latest step from its
transcript. Open hands a session to the Claude app (`claude --desktop --resume`), where you
answer it; one that's still working pauses on the way and carries on when told to. "Recheck releases" runs
refresh.py --stores here (GitHub and stores only; Gmail needs a Claude session).

Opening a conversation reads its emails (read_messages: a one-shot Claude call runs
thread_fetch.py in the Composio workbench) and keeps them, addresses blanked, in
~/Library/Caches/support-desk/messages on this Mac only (SUPPORT_DESK_CACHE_DIR or
SUPPORT_DESK_DATA_DIR moves it; see desk_auth.py).

It listens on 127.0.0.1 and accepts local addresses plus an explicitly configured public
HTTPS origin. One owner password protects the desk and its APIs. Every action also needs
the token printed into the page, and prompts are built here from validated ids.
"""
import base64
import fcntl
import glob
import hashlib
import hmac
import html as html_lib
import os
import shutil
import http.server
import json
import mmap
import pathlib
import re
import zlib
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
from http.cookies import SimpleCookie, CookieError

from tickets import check_prs as check_ticket_prs, find as find_ticket, gh_cli, set_status as set_ticket_status
from desk_auth import AuthError, PasswordAuth, auth_path, cache_dir, data_dir

HERE = pathlib.Path(__file__).resolve().parent
KIT = HERE.parent
sys.path.insert(0, str(KIT))
from config import CONFIG, code_dir, owner, products  # noqa: E402 (the kit's root has to be on the path first)

DASH = KIT / "dashboard"
STATIC = HERE / "static"
CODE = code_dir()
DESK = CONFIG.get("desk", {})
PORT = int(os.environ.get("SUPPORT_DESK_PORT") or DESK.get("port") or 4150)  # the variable is for a second, test desk
DESK_NAME = DESK.get("desk_name") or "Support Desk"
USE_CASE = DESK.get("use_case") or "support"
OWNER = owner()  # the first name replies are written as
# A draft still holding an "[Owner: …]" gap (the runbooks' marker; "[<your name>: …]" counts too) has a part only
# the owner can fill in, so it can't be sent yet.
GAPS = ("[Owner:", f"[{OWNER}:")
SUPPORT = CONFIG.get("support", {})
SLACK = CONFIG.get("slack", {})
AUTH = PasswordAuth(auth_path())
PUBLIC_ORIGIN = (os.environ.get('SUPPORT_DESK_PUBLIC_ORIGIN') or DESK.get('public_origin') or '').rstrip('/')
if PUBLIC_ORIGIN:
    public = urllib.parse.urlsplit(PUBLIC_ORIGIN)
    if (public.scheme != 'https' or not public.hostname or public.username or public.password
            or public.path or public.query or public.fragment or public.port not in (None, 443)):
        raise SystemExit('desk.public_origin (or SUPPORT_DESK_PUBLIC_ORIGIN) must be an HTTPS origin, '
                         'for example https://support.example.com')
    PUBLIC_ORIGIN = 'https://' + public.netloc.lower()


def desk_token():
    """The key every action from the page must carry. It's kept in a private file so it survives
    restarts: a page loaded before one would otherwise have its buttons refused."""
    f = data_dir() / "token"
    try:
        if len(f.read_text().strip()) >= 32:
            return f.read_text().strip()
    except OSError:
        pass
    f.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write(token + "\n")
    return token


TOKEN = desk_token()
# config.json's products: {product name: {"repo": its GitHub repo, "folder": where it's cloned under CODE}}.
# A fix or build for a product's conversation starts in its folder; a PR key ("repo#12") names the repo.
REPO_FOLDER = {name: p["folder"] for name, p in products().items() if p.get("folder")}
REPO_DIR = {p["repo"]: p["folder"] for p in products().values() if p.get("repo") and p.get("folder")}
REPOS = set(REPO_DIR)
GITHUB_OWNER = (CONFIG.get("github") or {}).get("owner") or ""
FILES = {
    "conversations": DASH / "out" / "conversations.json", "prs": DASH / "out" / "prs.json",
    "releases": DASH / "out" / "releases.json", "unreleased": DASH / "out" / "unreleased.json",
    "meta": DASH / "out" / "meta.json", "work": DASH / "work.json", "ship": DASH / "ship.json",
    "drafts": DASH / "drafts.json",
    # The owner's own tickets across their products (tickets.py). The autopilot reads dashboard/, never this.
    "tickets": HERE / "tickets.json",
}
RUNBOOK = {name: str(DASH / f"{name}-runbook.md")
           for name in ("fix", "close", "ship", "reply", "pass", "send", "digest", "tripwire")}
TICKET_RUNBOOK = HERE / "ticket-runbook.md"  # tickets live apart from dashboard/, and so does their runbook
# Where a ticket's session starts: only folders you already work in, so the Claude app files the session
# under one of your projects instead of adding a folder. A repo the key or link names maps to its project folder
# (the first part of its product's folder: a repo cloned at CODE/acme/app works from CODE/acme), else the
# ticket's product's folder (config.json's ticket_products), else CODE itself. This kit is a project too.
TICKET_REPO_DIR = {repo: pathlib.PurePosixPath(folder).parts[0] for repo, folder in REPO_DIR.items()}
TICKET_REPO_DIR[KIT.name] = str(KIT)
TICKET_PRODUCT_DIR = dict(CONFIG.get("ticket_products") or {})
# At most TICKET_CAP ticket sessions work at once (35 at a time pushed a 10-core Mac's load past 70).
# A Work on it press beyond that waits its turn in TICKET_QUEUE, and watch_ticket_queue starts it once a session
# finishes or stops to ask you something. {"waiting": [{"id", "at"}], "failed": {ticket id: why it didn't start}}
TICKET_CAP = int(DESK.get("ticket_cap") or 4)
TICKET_QUEUE = HERE / "ticket-queue.json"
_queue_lock = threading.Lock()
CLOSED = HERE / "closed.json"  # {id: {"at", "reason"}}: closed here, until the data catches up
AUTOPILOT = HERE / "autopilot.json"  # {"on": bool, ...}: the pause switch and the loop's last runs
REFRESH_TASK = DASH / "refresh-runbook.md"  # the full refresh, step by step

refresh = {"running": False, "started": None, "finished": None, "ok": None, "tail": ""}


def load(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def closed_here(rows):
    """Show conversations closed from the desk as closed straight away. The close session labels Gmail
    and updates the data in the background; an entry goes once the data shows it, or after two days."""
    marks = load(CLOSED, {})
    if not marks:
        return
    by_id = {c["id"]: c for c in rows}
    keep = {}
    for cid, m in marks.items():
        c = by_id.get(cid)
        if not c or c.get("status") == "resolved" or time.time() - m.get("t", 0) > 2 * 86400:
            continue
        c.update(status="resolved", owed="", closing=True)
        keep[cid] = m
    if keep != marks:
        CLOSED.write_text(json.dumps(keep, indent=1) + "\n")


# The few settings the page shows or words itself by. Nothing here names an account.
PAGE_CONFIG = {
    "desk_name": DESK_NAME, "use_case": USE_CASE, "owner": OWNER,  # owner: the name a draft's gap may carry
    "slack": {"enabled": bool(SLACK.get("enabled")), "channel_name": SLACK.get("channel_name") or ""},
    "products": list(REPO_FOLDER),  # the products a fix or build session can work on
}


def state():
    data = {name: load(path, {} if name == "ship" else []) for name, path in FILES.items()}
    data["desk"] = PAGE_CONFIG
    closed_here(data["conversations"])
    stamp = "|".join(f"{p.stat().st_mtime_ns}" if p.exists() else "-" for p in FILES.values())
    data["refresh"] = refresh
    data["desk_sessions"] = desk_sessions()
    data["ticket_queue"] = queue_view()
    data["made_by"] = made_by()
    data["cli"] = {"found": bool(CLAUDE), "signed_in": signed_in(cached=True)}
    data["autopilot"] = autopilot_view()
    data["passes"] = load(DASH / "passlog.json", [])[-12:][::-1]
    sent = [(v["at"], k) for k, v in load(DASH / "notified.json", {}).items()
            if v.get("state") == "sent" and not v.get("by", "").startswith("baseline")]
    data["notes_sent"] = [{"at": at, "key": k} for at, k in sorted(sent, reverse=True)[:12]]
    data["messages"] = messages_status()
    live = json.dumps([data["desk_sessions"], data["cli"], refresh, data["autopilot"], data["passes"], data["notes_sent"],
                       data["messages"], data["made_by"], data["ticket_queue"]], sort_keys=True)
    data["version"] = hashlib.sha1((stamp + live).encode()).hexdigest()[:12]
    return data


class ActionError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def find_claude():
    """The newest claude CLI: on PATH or in any of nvm's Node folders (a login item's PATH has no nvm).

    Several Node versions can each carry their own copy (say a current one under Node 22 and an old
    one under Node 23), so pick by the CLI's own version, not by folder name."""
    home = pathlib.Path.home()
    found = {p for p in [shutil.which("claude"), str(home / ".local/bin/claude"),  # the standalone install
                         *glob.glob(str(home / ".nvm/versions/node/*/bin/claude"))] if p and os.path.exists(p)}

    def version(path):
        try:
            env = dict(os.environ, PATH=str(pathlib.Path(path).parent) + os.pathsep + os.environ.get("PATH", ""))
            out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=20, env=env).stdout
            return tuple(int(x) for x in re.match(r"(\d+)\.(\d+)\.(\d+)", out).groups())
        except (OSError, subprocess.SubprocessError, AttributeError, ValueError):
            return (0,)
    return max(found, key=version, default=None)


CLAUDE = find_claude()
SESSIONS = HERE / "sessions.json"
_auth = {"ok": None, "at": 0.0, "noes": 0}  # ok is None until the first check
_agents = {"list": [], "at": 0.0}


def cli_env():
    env = dict(os.environ)
    env["PATH"] = str(pathlib.Path(CLAUDE).parent) + os.pathsep + env.get("PATH", "")  # node sits beside claude
    return env


def cli(args, cwd=None, timeout=60):
    return subprocess.run([CLAUDE, *args], cwd=cwd, env=cli_env(), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)


def signed_in(cached=False):
    """Whether the CLI can run sessions: True, False, or None before the first check. The page reads
    what the watcher last saw; anything else checks again once a yes is a minute old.

    `claude auth status` sometimes fails for a moment (a dropped connection, a slow refresh of the
    login), so an error keeps the last answer, and only two plain "not logged in" answers in a row
    count as signed out."""
    if not CLAUDE:
        return False
    if cached or (_auth["ok"] and time.time() - _auth["at"] < 60):
        return _auth["ok"]
    _auth["at"] = time.time()
    try:
        r = cli(["auth", "status"], timeout=20)
        answer = json.loads(r.stdout).get("loggedIn")
    except Exception as e:  # noqa: BLE001
        print(f"[auth] check failed, keeping the last answer: {type(e).__name__}: {str(e)[:120]}", flush=True)
        return _auth["ok"]
    if answer:
        _auth.update(ok=True, noes=0)
    else:
        _auth["noes"] += 1
        if _auth["noes"] >= 2 and _auth["ok"] is not False:
            print(f"[auth] signed out: exit {r.returncode}, {r.stderr.strip()[-160:]!r}", flush=True)
            _auth["ok"] = False
    return _auth["ok"]


def agents(fresh=False):
    """Background sessions the CLI knows about, as the watcher last saw them (or right now, if fresh)."""
    if CLAUDE and fresh:
        try:
            _agents["list"] = json.loads(cli(["agents", "--json", "--all"], timeout=20).stdout or "[]")
        except Exception:  # noqa: BLE001
            pass
        _agents["at"] = time.time()
    return _agents["list"]


def watch_cli():
    """Keep the session list and sign-in state current off the request path. The CLI usually answers in
    half a second but sometimes takes ten, and the page shouldn't wait for it."""
    while True:
        agents(fresh=True)
        if time.time() - _auth["at"] > (60 if _auth["ok"] else 10):  # a no, or no answer yet, is checked sooner
            signed_in()
        time.sleep(4)


# A background session's `state`: working, blocked (stopped to ask you something), done, stopped.
STATES = {"working": "working", "blocked": "blocked", "done": "done"}
_transcripts = {}  # session id -> [path or None, mtime, progress, looked-at]
EMAIL = re.compile(r"[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}")
TOOL_STEP = {"Read": "Reading a file", "Edit": "Editing a file", "Write": "Writing a file",
             "Grep": "Searching files", "Glob": "Searching files", "WebFetch": "Reading a web page"}


def progress(sid):
    """What a session is doing, from the tail of its transcript: its latest step and its latest words."""
    if not sid or not re.fullmatch(r"[0-9a-f-]{36}", sid):
        return {}
    entry = _transcripts.setdefault(sid, [None, 0.0, {}, 0.0])
    if not entry[0]:
        if time.time() - entry[3] < 30:  # not written yet; look again in a bit
            return {}
        entry[3] = time.time()
        hits = glob.glob(str(pathlib.Path.home() / ".claude" / "projects" / "*" / f"{sid}.jsonl"))
        if not hits:
            return {}
        entry[0] = hits[0]
    try:
        size, mtime = os.path.getsize(entry[0]), os.path.getmtime(entry[0])
        if mtime == entry[1]:
            return entry[2]
        with open(entry[0], "rb") as f:
            f.seek(max(0, size - 262144))
            lines = f.read().decode("utf-8", "replace").splitlines()[1 if size > 262144 else 0:]  # a seek can cut the first line
    except OSError:
        return {}
    step = said = None
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("type") != "assistant":
            continue
        for block in reversed((row.get("message") or {}).get("content") or []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and step is None:
                name = block.get("name", "")
                step = ((block.get("input") or {}).get("description") or TOOL_STEP.get(name)
                        or ("Working in Gmail" if "COMPOSIO" in name.upper() else f"Using {name.split('__')[-1]}"))
            elif block.get("type") == "text" and said is None and block.get("text", "").strip():
                said = block["text"].strip()
        if step and said:
            break
    clean = lambda s: EMAIL.sub("[email]", re.sub(r"\s+", " ", s))[:400] if s else None  # noqa: E731
    entry[1:3] = [mtime, {"step": clean(step), "said": clean(said),
                          "touched": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime))}]
    return entry[2]


def desk_sessions():
    """Sessions this desk started, newest first, with their live state and latest step."""
    live = {a.get("id"): a for a in agents()}
    out, seen = [], set()
    rows = load(SESSIONS, [])
    # The newest 30, plus any older one still working or waiting for you, so its ticket or conversation keeps it.
    older = [s for s in rows[:-30] if not s.get("in_app") and (live.get(s["short"]) or {}).get("state") in ("working", "blocked")]
    for s in reversed(older + rows[-30:]):
        a = live.get(s["short"])
        status = STATES.get(a.get("state"), "ended") if a else "ended"
        if a and not a.get("state"):  # an older CLI reports only busy / idle
            status = "working" if a.get("status") == "busy" else "done"
        if s["short"] in seen:  # a retried session can be recorded twice
            continue
        seen.add(s["short"])
        if s.get("in_app"):  # handed to the Claude app: the CLI no longer runs it, but its transcript still grows
            status = "app"
        row = {**s, "status": status}
        if status != "ended":
            row.update(progress(s.get("session_id")))
        if status == "blocked" and (row.get("said") or "").startswith("API Error"):
            row["status"] = "failed"  # it didn't stop to ask anything; the connection dropped
        out.append(row)
    return out


# ---------- the session that made each open PR ----------
# A merge card opens the session that made its PR, in the Claude app. A cloud session (claude.ai/code)
# signs the PR's description with its link; a Fix or Build session the desk started has the PR's address
# in its transcript. Who made a PR never changes, so each is looked up once, and again every few minutes
# until something is found. Anything else (a PR made by hand, or by a session that's gone) keeps GitHub.
CLOUD_SESSION = re.compile(r"claude\.ai/code/(session_[A-Za-z0-9]{8,64})(?![A-Za-z0-9])")
PR_URL = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)")
MAKERS = ("fix", "build")  # the desk's sessions that open PRs
MADE_BY_EVERY, MADE_BY_RETRY = 60, 10 * 60
_made_by = {}  # PR url -> {"found": {"app": claude:// link} | {"short": desk session} | None, "at": time}
_made_by_lock = threading.Lock()


def pr_maker(p):
    """The session that made PR row `p` (prs.json): {"app": a claude:// link} for a cloud session,
    {"short": ...} for one the desk started, or None."""
    m = PR_URL.fullmatch(p.get("url", ""))
    gh = gh_cli()
    if not m or not gh:
        return None
    r = subprocess.run([gh, "pr", "view", m.group(3), "--repo", f"{m.group(1)}/{m.group(2)}", "--json", "body", "--jq", ".body"],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError("gh pr view failed")  # try again next round rather than settle on "nobody"
    cloud = CLOUD_SESSION.search(r.stdout)
    if cloud:
        return {"app": f"claude://claude.ai/epitaxy/{cloud.group(1)}"}
    # Oldest first: a retry also mentions the PR, but the first session to name it is the one that opened it.
    mention = re.compile(re.escape(p["url"]).encode() + rb"(?!\d)")  # pull/6 isn't pull/64
    for s in load(SESSIONS, []):
        if s.get("action") not in MAKERS or s.get("target") != p.get("conversation") or not s.get("session_id"):
            continue
        for path in glob.glob(str(pathlib.Path.home() / ".claude" / "projects" / "*" / f"{s['session_id']}.jsonl")):
            try:
                with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
                    if mention.search(data):
                        return {"short": s["short"]}
            except (OSError, ValueError):  # gone, or empty (mmap refuses a 0-byte file)
                continue
    return None


def watch_pr_makers():
    """Look up the maker of each open PR in the background, so the desk's polls never wait on GitHub."""
    while True:
        now = time.time()
        for p in load(FILES["prs"], []):
            url = p.get("url", "")
            if p.get("pr_state") != "open" or not url:
                continue
            with _made_by_lock:
                known = _made_by.get(url)
            if known and (known["found"] or now - known["at"] < MADE_BY_RETRY):
                continue
            try:
                found = pr_maker(p)
            except Exception as e:  # noqa: BLE001 (GitHub or a transcript hiccup: the next round tries again)
                print(f"[made-by] {p.get('pr')}: {type(e).__name__}", flush=True)
                continue
            with _made_by_lock:
                _made_by[url] = {"found": found, "at": now}
        time.sleep(MADE_BY_EVERY)


def made_by():
    """PR url -> how to open the session that made it, for the PRs where that's known."""
    with _made_by_lock:
        return {url: v["found"] for url, v in _made_by.items() if v["found"]}


# A GitHub link in a ticket names one of your repos (github.owner in config.json; any owner if that's unset).
TICKET_LINK = re.compile(r"https://github\.com/" + (re.escape(GITHUB_OWNER) if GITHUB_OWNER else r"[\w.-]+")
                         + r"/(\w[\w.-]*)/", re.I)


def ticket_folder(t):
    """The folder a ticket's session starts in: the project folder of the repo its key (example-app#56) or GitHub
    link names, else its product's folder, else CODE itself (see TICKET_REPO_DIR)."""
    m = re.fullmatch(r"(\w[\w.-]*)#\d+", t.get("key") or "") or TICKET_LINK.match(t.get("link") or "")
    for name in ((TICKET_REPO_DIR.get(m.group(1)) if m else None), TICKET_PRODUCT_DIR.get(t.get("product"))):
        if not name:
            continue
        folder = KIT if name == KIT.name else CODE / name  # an absolute path stays as it is
        if folder.is_dir():
            return folder
    return CODE


def tilde(folder):
    """A folder as it reads to a person: ~/code/example-app rather than the full path."""
    return str(folder).replace(str(pathlib.Path.home()), "~", 1)


def start_session(folder, prompt, title, action, target, auto=False, resume=None):
    """Start a background Claude Code session that works on `prompt` in `folder` right away
    (or continue an earlier one, `resume` being its session id)."""
    if not CLAUDE:
        raise ActionError(500, "Couldn't find the claude command-line tool on this Mac.")
    if signed_in() is False:  # unsure is worth a try: the CLI says so itself if it really is signed out
        raise ActionError(409, "The Claude command-line tool is signed out, so it can't run sessions. "
                               "Run `claude auth login` once in Terminal with your Claude account, then press the button again.")
    r = cli(["--bg", "--permission-mode", "auto", "-n", title, *(["--resume", resume] if resume else []), prompt],
            cwd=folder, timeout=90)
    m = re.search(r"backgrounded\W+([0-9a-f]{6,})", r.stdout)
    if r.returncode != 0 or not m:
        raise ActionError(500, "Couldn't start the session: " + (r.stderr or r.stdout).strip()[-200:])
    short = m.group(1)
    sid = next((a.get("sessionId") for a in agents(fresh=True) if a.get("id") == short), None)
    rows = load(SESSIONS, [])
    rows.append({"short": short, "session_id": sid, "title": title, "folder": str(folder), "action": action, "auto": auto,
                 "target": target, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    SESSIONS.write_text(json.dumps(rows[-200:], indent=1) + "\n")
    return short


def start_ticket(t):
    """Start a session on ticket t in its folder. Returns the folder, written with ~."""
    folder = ticket_folder(t)
    start_session(folder, f"Work on ticket {t['id']}. Follow {TICKET_RUNBOOK}. {OWNER} started this from the "
                  f"{DESK_NAME}'s Tickets view.", f"Ticket: {t['title'][:70]}", "ticket", t["id"])
    return tilde(folder)


def ticket_busy(tid):
    """This ticket's session that is working or waiting for your answer, if there is one."""
    return next((s for s in desk_sessions() if s["target"] == tid and s["action"] == "ticket"
                 and s["status"] in ("working", "blocked")), None)


def working_tickets():
    """Ticket sessions the CLI is running now, counting one started in the last minute that it doesn't list yet.
    One handed to the Claude app isn't counted: you drive it there."""
    live = {a.get("id"): a for a in agents()}
    recent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
    n = 0
    for s in {r["short"]: r for r in load(SESSIONS, []) if r["action"] == "ticket"}.values():
        a = live.get(s["short"])
        if s.get("in_app"):
            continue
        if a is None:
            n += s["started"] >= recent
        elif a.get("state") == "working" or (not a.get("state") and a.get("status") == "busy"):
            n += 1
    return n


def ticket_queue():
    q = load(TICKET_QUEUE, {})
    return {"waiting": q.get("waiting", []), "failed": q.get("failed", {})}


def save_ticket_queue(q):
    TICKET_QUEUE.write_text(json.dumps(q, indent=1) + "\n")


def queue_view():
    q = ticket_queue()
    return {**q, "cap": TICKET_CAP, "working": working_tickets()}


def start_queued():
    """Start waiting tickets, oldest first, while fewer than TICKET_CAP ticket sessions are working. One that's
    done by now, or already has a session, just leaves the line; one that fails to start says why on its card."""
    with _queue_lock:
        q = ticket_queue()
        while q["waiting"] and working_tickets() < TICKET_CAP:
            tid = q["waiting"].pop(0)["id"]
            save_ticket_queue(q)  # out of the line before it starts, so nothing starts it twice
            t = find_ticket(tid)
            if not t or t["status"] != "open" or ticket_busy(tid):
                continue
            try:
                start_ticket(t)
            except Exception as e:  # noqa: BLE001 (the next ticket still gets its turn)
                q["failed"][tid] = getattr(e, "message", None) or f"Couldn't start the session ({type(e).__name__})."
                save_ticket_queue(q)


def watch_ticket_queue():
    """Start queued tickets as slots free up. Each is a press of yours, so this runs with the autopilot paused too,
    but not while the CLI is signed out (every start would fail)."""
    time.sleep(30)
    while True:
        try:
            if ticket_queue()["waiting"] and signed_in() is not False:
                start_queued()
        except Exception as e:  # noqa: BLE001 (keep watching)
            print(f"[tickets] queue: {type(e).__name__}: {str(e)[:200]}", flush=True)
        time.sleep(20)


ATTACH = cache_dir()


def known_session(short):
    if not re.fullmatch(r"[0-9a-f]{6,12}", short) or not any(x["short"] == short for x in load(SESSIONS, [])):
        raise ActionError(404, "That session isn't known any more.")


def open_in_app(short, live):
    """Hand a desk-started session to the Claude app's Code tab, where you can read it and answer it.

    `claude --desktop --resume` won't take a session that is still running, so a background one is
    stopped first. Its conversation is kept, and one cut off mid-step waits in the app to be told to
    keep going. The app carries it on under the same id, so its card keeps showing the latest step from the same transcript. The CLI only hands over
    with a terminal attached, so it runs under `script`, which gives it one. After that the app holds
    the session, and Open just brings it forward."""
    rows = load(SESSIONS, [])
    row = next(x for x in reversed(rows) if x["short"] == short)
    sid = row.get("session_id") or (live or {}).get("sessionId")
    if not sid or not re.fullmatch(r"[0-9a-f-]{36}", sid):
        raise ActionError(409, "It hasn't started its conversation yet. Try again in a moment.")
    if row.get("in_app"):
        subprocess.run(["open", f"claude://claude.ai/epitaxy/local_{sid}"], check=True, timeout=20)
        return
    if live and (live.get("pid") or live.get("state") in ("working", "blocked")):  # a starting session has no pid yet
        try:  # the list can be stale and stop can hang on a busy Mac; the hand-over below refuses one still running
            cli(["stop", short], timeout=30)
        except subprocess.TimeoutExpired:
            pass
    folder = row["folder"] if os.path.isdir(row["folder"]) else str(pathlib.Path.home())
    r = subprocess.run(["script", "-q", "/dev/null", CLAUDE, "--desktop", "--resume", sid], cwd=folder, env=cli_env(),
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    said = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|[\x00-\x08\x0b-\x1f]", "", r.stdout).strip()
    if "in Claude Desktop" not in said:  # script echoes the closed stdin as ^D
        raise ActionError(500, "Couldn't open it in the Claude app: " + said.replace("^D", "").strip()[-200:])
    for x in rows:
        if x["short"] == short:
            x["in_app"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    SESSIONS.write_text(json.dumps(rows[-200:], indent=1) + "\n")


# ---------- drafts in other languages ----------
TRANSLATOR = (f"You translate {USE_CASE} emails written in {OWNER}'s voice: warm, plain and short. Keep the meaning, "
              "names, product names, numbers, line breaks and any [Owner: …] gap. Output only what the schema asks for.")
_translating = set()


def ask_claude(prompt, schema):
    """One structured answer from the CLI: no tools, no MCP servers, no saved session."""
    r = cli(["-p", "--model", "sonnet", "--tools", "", "--no-session-persistence", "--strict-mcp-config",
             "--setting-sources", "", "--system-prompt", TRANSLATOR, "--output-format", "json",
             "--json-schema", json.dumps(schema), prompt], timeout=150)
    try:
        out = json.loads(r.stdout)
    except ValueError:
        out = {}
    if r.returncode != 0 or out.get("is_error") or not isinstance(out.get("structured_output"), dict):
        raise ActionError(502, "Couldn't translate it just now. Try again in a minute.")
    return out["structured_output"]


def update_draft(did, expect_body=None, **fields):
    """Set fields on a pending draft under drafts.py's lock. None if it isn't pending (or has changed)."""
    with open(DASH / ".drafts.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        drafts = load(FILES["drafts"], [])
        d = next((x for x in drafts if x["id"] == did and x["status"] == "pending"), None)
        if not d or (expect_body is not None and d["body"] != expect_body):
            return None
        d.update(fields, updated=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        FILES["drafts"].write_text(json.dumps(drafts, ensure_ascii=False, indent=1) + "\n")
        return d


def watch_drafts():
    """Give each pending draft its language, and an English version when it isn't English. Drafting
    sessions save both themselves now; this covers drafts made before, or without them."""
    while True:
        for d in load(FILES["drafts"], []):
            if d.get("status") != "pending" or d.get("lang") or d["id"] in _translating or not CLAUDE:
                continue
            _translating.add(d["id"])  # one try per draft per run of the desk
            try:
                out = ask_claude("Name the language of this email in English (for example Indonesian or German), and "
                                 "translate it into natural English.\n\n<email>\n" + d["body"] + "\n</email>",
                                 {"type": "object", "required": ["lang", "english"],
                                  "properties": {"lang": {"type": "string"}, "english": {"type": "string"}}})
            except Exception:  # noqa: BLE001
                continue
            lang = str(out.get("lang", "")).strip()[:40] or "English"
            extra = {} if lang.lower() == "english" else {"body_en": str(out.get("english", "")).strip()[:4000]}
            update_draft(d["id"], expect_body=d["body"], lang="English" if not extra else lang, **extra)
        time.sleep(10)


# ---------- what customers wrote ----------
MESSAGES = ATTACH / "messages"  # <conversation id>.json: its emails, kept on this Mac only
FETCHER = HERE / "thread_fetch.py"
WORKBENCH = SUPPORT.get("composio_workbench_tool") or "mcp__claude_ai_Composio__COMPOSIO_REMOTE_WORKBENCH"
GMAIL_ACCOUNT = SUPPORT.get("composio_gmail_account") or ""  # the support mailbox's Composio account id
OUR_DOMAINS = sorted({str(d).lower() for d in SUPPORT.get("our_domains") or []})  # mail from these is us
RELAY_SENDERS = [str(r) for r in SUPPORT.get("relay_senders") or []]  # names that forward a customer's in-app form
_reading = {}  # conversation id -> {"started": t} while a read runs, {"error": "..."} after one fails
_reading_lock = threading.Lock()
_read_slots = threading.Semaphore(2)


def threads_of(cid):
    """A conversation's Gmail thread ids, from the curated list (the derived one keeps only a count)."""
    rows = load(DASH / "conversations.json", {}).get("conversations", [])
    row = next((c for c in rows if c.get("id") == cid), None) or {}
    return [t for t in row.get("threads", []) if re.fullmatch(r"[0-9a-f]{16}", str(t))]


def workbench_output(stream, code):
    """The fetch script's JSON, read from the workbench call's own result in the CLI's event stream.
    Refused if the code that ran isn't ours (the model only drops trailing whitespace)."""
    call, result = None, None
    for line in stream.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        content = (d.get("message") or {}).get("content")
        for b in content if isinstance(content, list) else []:
            if b.get("type") == "tool_use" and b.get("name") == WORKBENCH:
                if call or str((b.get("input") or {}).get("code_to_execute", "")).strip() != code.strip():
                    raise ActionError(502, "The read didn't run the desk's own script, so nothing from it is shown.")
                call = b.get("id")
            elif b.get("type") == "tool_result" and call and b.get("tool_use_id") == call:
                result = b.get("content")
    if not call:
        raise ActionError(502, "Couldn't reach Composio just now. Try again in a minute.")
    txt = result if isinstance(result, str) else "".join(x.get("text", "") for x in result or [] if isinstance(x, dict))
    # The workbench answers {"data": {"stdout", "results", "error", ...}}. Printed output used to arrive in
    # stdout; since 6 Nov 2026 it comes back empty and only the cell's last expression returns, in results.
    # The script gives both (the last expression as compressed base64, since results is Python's repr of
    # it and escapes quotes), so look in both.
    problem = ""
    try:
        data = json.loads(txt)["data"]
        res = data.get("results")
        txt = "\n".join([str(data.get("stdout") or ""), res if isinstance(res, str) else json.dumps(res or "")])
        problem = str(data.get("error") or data.get("stderr") or "").strip()
    except (ValueError, KeyError, TypeError, AttributeError):
        pass
    z = re.search(r"DESK_MESSAGES_Z ([A-Za-z0-9+/=]+)", txt)
    if z:
        return json.loads(zlib.decompress(base64.b64decode(z.group(1))).decode("utf-8"))
    m = re.search(r"^DESK_MESSAGES (\{.*\})\s*$", txt, re.M)
    if not m:
        why = EMAIL.sub("[email]", problem or txt.strip())[:160]
        raise ActionError(502, f"Gmail didn't answer: {why}" if why else "Gmail sent nothing back. Try again in a minute.")
    return json.loads(m.group(1))


def read_messages(cid):
    """Read a conversation's emails through a one-shot Claude call, and keep them on this Mac.

    The CLI reaches Composio only through a model, so a small one loads the workbench tool and passes
    thread_fetch.py to it unchanged. Those two tools and two turns are all it gets, so it never has a
    turn after the email text comes back; the desk reads the script's output straight from the tool
    result. Addresses are blanked in the workbench and again here."""
    try:
        threads = threads_of(cid)
        if not threads:
            raise ActionError(404, "This conversation has no Gmail thread on record.")
        if signed_in() is False:
            raise ActionError(503, "The Claude command-line tool is signed out, so the desk can't read Gmail.")
        if not GMAIL_ACCOUNT:
            raise ActionError(503, "Set support.composio_gmail_account in config.json so the desk knows which mailbox to read.")
        # The script holds placeholders, never real values: these fill them in (and the check below holds the
        # workbench call to exactly this text).
        code = FETCHER.read_text()
        for line, value in (("THREADS = []", "THREADS = " + json.dumps(threads)),
                            ('ACC = ""', "ACC = " + json.dumps(GMAIL_ACCOUNT)),
                            ("OURS = set()", "OURS = set(" + json.dumps(OUR_DOMAINS) + ")"),
                            ("RELAYS = []", "RELAYS = " + json.dumps(RELAY_SENDERS))):
            if line not in code:
                raise ActionError(500, f"thread_fetch.py has lost its {line.split()[0]} line.")
            code = code.replace(line, value, 1)
        prompt = (f"Load the tool with ToolSearch (query select:{WORKBENCH}). Then call it once, with code_to_execute set "
                  "to exactly the Python between the BEGIN and END lines, unchanged. Then stop.\nBEGIN\n" + code + "\nEND")
        MESSAGES.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _read_slots:
            r = cli(["-p", "--model", "haiku", "--output-format", "stream-json", "--verbose", "--max-turns", "2",
                     "--tools", "ToolSearch", "--allowedTools", f"ToolSearch,{WORKBENCH}", "--permission-mode", "dontAsk",
                     "--no-session-persistence", "--system-prompt",
                     "You load one tool, pass one piece of code to it exactly as given, and do nothing else.", prompt],
                    cwd=MESSAGES, timeout=240)
        data = workbench_output(r.stdout, code)
        scrub = lambda v, n: EMAIL.sub("[email]", str(v or ""))[:n]  # noqa: E731
        msgs = [{"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(m.get("ts") or 0) / 1000)),
                 "from": scrub(m.get("from"), 30) or "?", "us": bool(m.get("us")), "auto": bool(m.get("auto")),
                 "thread": scrub(m.get("thread"), 16), "subject": scrub(m.get("subject"), 200),
                 "text": scrub(m.get("text"), 4000), "atts": [scrub(a, 120) for a in (m.get("atts") or [])[:10] if a]}
                for m in data.get("messages") or [] if isinstance(m, dict)]
        doc = {"id": cid, "messages": msgs, "trimmed": int(data.get("trimmed") or 0),
               "errors": [scrub(e, 200) for e in (data.get("errors") or [])[:5]]}
        tmp = MESSAGES / f".{cid}.tmp"
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump(doc, f, ensure_ascii=False)
        os.replace(tmp, MESSAGES / f"{cid}.json")
        _reading.pop(cid, None)
    except ActionError as e:
        _reading[cid] = {"error": e.message}
    except subprocess.TimeoutExpired:
        _reading[cid] = {"error": "Reading the emails took too long. Try again."}
    except Exception as e:  # noqa: BLE001 (shown on the page; details go to the log)
        print(f"[messages] {cid}: {type(e).__name__}: {e}", flush=True)
        _reading[cid] = {"error": "Couldn't read the emails just now. Try again."}


def messages_status():
    """Per conversation: when its emails were last read (at, t), and a read running (started) or failed (error)."""
    out = {}
    for p in MESSAGES.glob("*.json") if MESSAGES.exists() else []:
        t = p.stat().st_mtime
        out[p.stem] = {"t": t, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))}
    for cid, r in list(_reading.items()):
        out.setdefault(cid, {}).update(r)
    return out


def start_reading(cid):
    with _reading_lock:
        if "started" in _reading.get(cid, {}):
            return False
        _reading[cid] = {"started": time.time()}
    threading.Thread(target=read_messages, args=(cid,), daemon=True).start()
    return True


def conversation(cid):
    return next((c for c in load(FILES["conversations"], []) if c["id"] == cid), None)


def known_pr(key):
    """A PR key (repo#n) the dashboard knows, from its fixes or its unreleased lists."""
    rows = load(FILES["prs"], []) + load(FILES["unreleased"], [])
    return any(r.get("pr") == key for r in rows)


def run_quick_refresh():
    refresh.update(running=True, started=time.time(), finished=None, ok=None, tail="Checking GitHub and the stores…")
    try:
        r = subprocess.run(["python3", str(DASH / "refresh.py"), "--stores"], cwd=DASH,
                           capture_output=True, text=True, timeout=420)
        refresh.update(ok=r.returncode == 0, tail=(r.stdout + r.stderr).strip()[-600:])
    except Exception as e:  # noqa: BLE001 (any failure is shown in the app)
        refresh.update(ok=False, tail=str(e)[-600:])
    refresh.update(running=False, finished=time.time())
    if autopilot()["on"] and notes_due():
        try:
            start_pass("a fix reached review or went live")
        except ActionError:
            pass  # signed out, or a pass is already running: the next tick tries again


AUTOPILOT_CONFIG = CONFIG.get("autopilot", {})


def clock_time(value, default):
    """An "HH:MM" from config.json as (hour, minute); the default if it isn't one."""
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(value or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else default


PASS_EVERY = 3600          # a support pass about hourly…
# …starting in these local hours ([8, 23]: between 08:00 and 22:59), so customers aren't emailed at night
PASS_HOURS = range(*(AUTOPILOT_CONFIG.get("pass_hours") or (8, 23))[:2])
RELEASES_EVERY = 2 * 3600  # recheck releases (GitHub and stores, no Claude) every two hours
FIX_CAP = int(AUTOPILOT_CONFIG.get("fix_cap", 3))  # automatic fix sessions per day
# Two daily sessions: the tripwire checks what went out, the digest sums up the week.
# Each runs once a day at or after its scheduled local time.
DAILY = {"tripwire": (clock_time(AUTOPILOT_CONFIG.get("tripwire_at"), (10, 0)), "Support tripwire",
                      "Check what Support sent in the past day."),
         "digest": (clock_time(AUTOPILOT_CONFIG.get("digest_at"), (10, 15)), "Support digest",
                    f"Write {OWNER}'s support digest for the past 7 days.")}
TICK = 300                 # the loop wakes every five minutes
FIRST_TICK = 600           # and first ten minutes after the desk starts


def autopilot():
    a = load(AUTOPILOT, {})
    a.setdefault("on", True)
    return a


def save_autopilot(**changes):
    a = autopilot()  # re-read, so a switch flipped meanwhile isn't overwritten
    a.update(changes)
    AUTOPILOT.write_text(json.dumps(a, indent=1) + "\n")


def autopilot_view():
    a = autopilot()
    nxt = max(a.get("pass_at", 0) + PASS_EVERY, time.time())
    return {"on": a["on"], "pass_at": a.get("pass_at"), "releases_at": a.get("releases_at"),
            "next_pass_at": nxt if a["on"] else None, "hours": f"{PASS_HOURS.start:02d}:00–{PASS_HOURS.stop:02d}:00",
            "fix_cap": FIX_CAP, "error": a.get("error", ""),
            "daily": [{"name": title, "at": f"{h:02d}:{m:02d}", "last": a.get(f"{name}_on")}
                      for name, ((h, m), title, _) in DAILY.items()]}


def running(action):
    return any(s["action"] == action and s["status"] == "working" for s in desk_sessions())


def notes_due():
    try:
        out = subprocess.run(["python3", str(DASH / "notify.py"), "plan"], cwd=DASH, capture_output=True, text=True,
                             timeout=60).stdout
        return len(json.loads(out or "[]"))
    except (subprocess.SubprocessError, ValueError):
        return 0


def start_pass(reason, auto=True):
    """Start a support pass unless one is already running. Returns whether it started."""
    if running("pass"):
        return False
    start_session(KIT, f"Run a support pass. Follow {RUNBOOK['pass']}. Started by the {DESK_NAME}: {reason}.",
                  "Support pass", "pass", reason, auto=auto)
    save_autopilot(pass_at=time.time())
    return True


def auto_fixes():
    """Start fix sessions for bugs nothing is fixing yet: FIX_CAP a day, one try per conversation per day."""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    started = [s for s in load(SESSIONS, []) if s["action"] == "fix" and s["started"].startswith(today)]
    budget = FIX_CAP - sum(1 for s in started if s.get("auto"))
    tried = {s["target"] for s in started}
    busy = {w["conversation"] for w in load(FILES["work"], []) if w.get("state") in ("working", "pr-open")}
    for c in load(FILES["conversations"], []):
        if budget <= 0:
            return
        if c.get("needs_fix") and c["product"] in REPO_FOLDER and c["id"] not in busy | tried:
            folder = CODE / REPO_FOLDER[c["product"]]
            start_session(folder, f"Fix support conversation {c['id']}. Follow {RUNBOOK['fix']}. Started by the desk's autopilot.",
                          f"Fix: {c['customer']} ({c['product']})", "fix", c["id"], auto=True)
            budget -= 1


def daily_jobs():
    """Start each DAILY session once a local day, at or after its time. A day the Mac sleeps through the digest's
    time still gets its digest when it wakes; a day it's off gets none."""
    now = time.localtime()
    today = time.strftime("%Y-%m-%d", now)
    for name, ((h, m), title, ask) in DAILY.items():
        last = autopilot().get(f"{name}_on")
        if last == today or (now.tm_hour, now.tm_min) < (h, m) or running(name):
            continue
        start_session(KIT, f"{ask} Follow {RUNBOOK[name]}. Started by the {DESK_NAME}'s autopilot.",
                      title, name, today, auto=True)
        save_autopilot(**{f"{name}_on": today})  # only once it started: a failure is retried next tick


TICKET_PRS_EVERY = 15 * 60


def watch_ticket_prs():
    """Close tickets about a pull request once it's merged or closed (tickets.py check_prs). It only reads
    GitHub and writes the desk's own tickets file, so it runs whether or not the autopilot is paused."""
    time.sleep(60)
    while True:
        try:
            for t in check_ticket_prs():
                print(f"[tickets] done: {t['id']} ({t.get('done_note', '')})", flush=True)
        except Exception as e:  # noqa: BLE001 (keep watching; the next round tries again)
            print(f"[tickets] PR check failed: {type(e).__name__}", flush=True)
        time.sleep(TICKET_PRS_EVERY)


def loop():
    """The autopilot: keeps support moving while the desk runs (it starts at login)."""
    time.sleep(FIRST_TICK)
    while True:
        try:
            a = autopilot()
            if a["on"] and signed_in():
                if time.time() - a.get("releases_at", 0) >= RELEASES_EVERY and not refresh["running"]:
                    save_autopilot(releases_at=time.time())
                    run_quick_refresh()  # starts a pass by itself when a note became due
                if time.localtime().tm_hour in PASS_HOURS and time.time() - autopilot().get("pass_at", 0) >= PASS_EVERY:
                    start_pass("hourly check")
                auto_fixes()
                daily_jobs()
            save_autopilot(error="")
        except Exception as e:  # noqa: BLE001 (keep looping; the desk shows the error)
            save_autopilot(error=str(e)[:200])
        time.sleep(TICK)


def act(body):
    """Validate one action from the page and carry it out. Returns (status, message)."""
    action = body.get("action")
    if action == "refresh-quick":
        if refresh["running"]:
            return 409, "A refresh is already running."
        threading.Thread(target=run_quick_refresh, daemon=True).start()
        return 202, "Refreshing GitHub and store data. This takes about a minute."
    if action == "autopilot":
        save_autopilot(on=bool(body.get("on")))
        return 200, "Autopilot is on." if body.get("on") else "Autopilot is paused. Buttons still work."
    if action in ("ticket-done", "ticket-reopen"):
        t = set_ticket_status(str(body.get("id", "")), "done" if action == "ticket-done" else "open")
        if not t:
            return 404, "That ticket isn't here any more."
        if action == "ticket-done":
            with _queue_lock:
                q = ticket_queue()
                q["waiting"] = [w for w in q["waiting"] if w["id"] != t["id"]]
                q["failed"].pop(t["id"], None)
                save_ticket_queue(q)
        return 200, f"Done: {t['title']}" if action == "ticket-done" else f"Reopened: {t['title']}"
    if action == "ticket-work":
        t = find_ticket(str(body.get("id", "")))
        if not t or not re.fullmatch(r"t-[0-9a-f]{8}", t["id"]) or t["status"] != "open":
            return 404, "That ticket isn't open any more."
        same = ticket_busy(t["id"])
        if same:
            return 409, f"Claude is already {'on it' if same['status'] == 'working' else 'waiting for your answer'}. It shows on the ticket."
        with _queue_lock:
            q = ticket_queue()
            ahead = next((i for i, w in enumerate(q["waiting"]) if w["id"] == t["id"]), None)
            if ahead is not None:
                return 409, f"It's already waiting its turn{f', with {ahead} ahead of it' if ahead else ', next in line'}."
            q["failed"].pop(t["id"], None)
            if q["waiting"] or working_tickets() >= TICKET_CAP:  # behind anything already waiting, so the line keeps its order
                q["waiting"].append({"id": t["id"], "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
                save_ticket_queue(q)
                n = len(q["waiting"]) - 1
                threading.Thread(target=start_queued, daemon=True).start()  # in case a slot is free already
                return 200, (f"{TICKET_CAP} ticket sessions are already working, so this one waits its turn"
                             f"{f' behind {n} more' if n else ''}. It starts by itself once one of them finishes or stops "
                             "to ask you something.")
            save_ticket_queue(q)
            where = start_ticket(t)
        return 200, (f"Claude is on it in {where}. It does what it can and asks you before anything only you can do. "
                     "Watch it on the ticket.")
    if action == "ticket-unqueue":
        tid = str(body.get("id", ""))
        with _queue_lock:
            q = ticket_queue()
            left = [w for w in q["waiting"] if w["id"] != tid]
            if len(left) == len(q["waiting"]):
                return 404, "It isn't waiting any more."
            q["waiting"] = left
            save_ticket_queue(q)
        return 200, "Taken out of the line. Nothing was started."
    if action == "read-messages":
        cid = str(body.get("id", ""))
        if not (re.fullmatch(r"[0-9a-f]{16}", cid) and conversation(cid)):
            return 400, "Unknown conversation."
        return 200, "Reading the emails from Gmail." if start_reading(cid) else "Already reading them."
    if action == "translate-draft":
        did = str(body.get("id", ""))
        english = str(body.get("english", "")).replace("\r\n", "\n").strip()[:4000]
        d = re.fullmatch(r"[0-9a-f]{8}", did) and next((x for x in load(FILES["drafts"], [])
                                                        if x["id"] == did and x["status"] == "pending"), None)
        if not d or not d.get("lang") or d["lang"] == "English":
            return 409, "That draft isn't waiting in another language any more."
        if not english:
            return 400, "The English version is empty."
        out = ask_claude(f"Translate this reply into {d['lang']}, as {OWNER} would write it.\n\n<email>\n{english}\n</email>",
                         {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}})
        text = str(out.get("text", "")).strip()[:4000]
        if not text or not update_draft(did, body=text, body_en=english):
            return 409, "That draft changed while it was translating. Look again and retry."
        return 200, f"Translated into {d['lang']}. Check it on the {d['lang']} tab, then press Send."
    if action == "retry-session":
        short = str(body.get("short", ""))
        known_session(short)
        s = next(x for x in reversed(load(SESSIONS, [])) if x["short"] == short)
        cli(["stop", short], timeout=30)
        start_session(pathlib.Path(s["folder"]), "The last attempt stopped on a network error. Pick up where it "
                      "left off and finish the task.", s["title"], s["action"], s["target"], auto=s.get("auto", False),
                      resume=s.get("session_id"))
        return 200, f"Retrying {s['title']}. It shows on the card as it works."
    if action in ("send-draft", "discard-draft"):
        did = str(body.get("id", ""))
        if not re.fullmatch(r"[0-9a-f]{8}", did):
            return 400, "Unknown draft."
        text = str(body.get("body", "")).replace("\r\n", "\n").strip()[:4000]
        with open(DASH / ".drafts.lock", "w") as lock:  # drafts.py takes the same lock
            fcntl.flock(lock, fcntl.LOCK_EX)
            drafts = load(FILES["drafts"], [])
            d = next((x for x in drafts if x["id"] == did and x["status"] == "pending"), None)
            if not d:
                return 409, "That draft isn't waiting any more."
            if action == "discard-draft":
                d["status"] = "discarded"
            else:
                if not text:
                    return 400, "The reply is empty."
                if any(gap in text for gap in GAPS):
                    return 400, "Fill in the [Owner: …] part before sending."
                d.update(body=text, status="approved")
            d["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            FILES["drafts"].write_text(json.dumps(drafts, ensure_ascii=False, indent=1) + "\n")
        if action == "discard-draft":
            return 200, f"Discarded the draft to {d['customer']}."
        try:
            start_session(KIT, f"Send approved draft {did}. Follow {RUNBOOK['send']}.", f"Send: {d['customer']} ({d['product']})",
                          "send", d["conversation"])
        except ActionError:
            with open(DASH / ".drafts.lock", "w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                drafts = load(FILES["drafts"], [])
                for x in drafts:
                    if x["id"] == did:
                        x["status"] = "pending"
                FILES["drafts"].write_text(json.dumps(drafts, ensure_ascii=False, indent=1) + "\n")
            raise
        return 200, f"Sending your reply to {d['customer']}. It goes out in a minute or so."
    if action in ("pass-now", "check-now"):
        if not start_pass("started from the desk", auto=False):
            return 409, "A check is already running."
        return 200, "Checking now: reading Gmail, replying where it should, sending due notes and closing what's done."
    if action == "open-session":
        short = str(body.get("short", ""))
        known_session(short)
        live = next((a for a in agents(fresh=True) if a.get("id") == short), None)
        in_app = any(x.get("in_app") for x in load(SESSIONS, []) if x["short"] == short)
        open_in_app(short, live)
        if live and live.get("state") == "working" and not in_app:
            return 200, "Opened it in the Claude app. It was still working, so it paused on the way: tell it to keep going there."
        return 200, "Opened it in the Claude app. Answer it there; its card here keeps showing what it's doing."
    if action == "stop-session":
        short = str(body.get("short", ""))
        known_session(short)
        r = cli(["stop", short], timeout=30)
        if r.returncode != 0:
            return 500, "Couldn't stop it: " + (r.stderr or r.stdout).strip()[-200:]
        agents(fresh=True)
        return 200, "Stopped it. Its conversation is kept if you want it back."
    if action == "refresh-full":
        start_session(KIT, "Refresh the support desk data now. Follow every step of "
                      f"{REFRESH_TASK}. {OWNER} started this run from the {DESK_NAME} app.",
                      "Support refresh (Gmail, GitHub, stores)", action, "")
        return 200, "Started a full refresh session. It's reading Gmail now; the desk updates when it's done."

    if action in ("fix", "build", "close", "reply"):
        cid = str(body.get("id", ""))
        c = re.fullmatch(r"[0-9a-f]{16}", cid) and conversation(cid)
        if not c:
            return 400, "Unknown conversation."
        who = f"{c['customer']} ({c['product']})"
        same = next((s for s in desk_sessions() if s["target"] == cid and s["action"] == action
                     and s["status"] in ("working", "blocked")), None)
        if same:
            return 409, f"{same['title']} is already {'working' if same['status'] == 'working' else 'waiting for your answer'}. It shows on the card."
        for s in desk_sessions():  # a fresh start replaces attempts that dropped their connection
            if s["target"] == cid and s["action"] == action and s["status"] == "failed":
                cli(["stop", s["short"]], timeout=30)
        if action == "reply":
            start_session(KIT, f"Draft a reply to support conversation {cid}. Follow {RUNBOOK['reply']}.",
                          f"Reply: {who}", action, cid)
            return 200, f"Drafting a reply to {c['customer']}. Watch it on the card; the draft lands under Replies to approve, and nothing sends until you press Send."
        if action in ("fix", "build"):
            if c["product"] not in REPO_FOLDER:
                return 400, f"{c['product']} has no repo to {action} in."
            folder = CODE / REPO_FOLDER[c["product"]]
            if action == "fix":
                start_session(folder, f"Fix support conversation {cid}. Follow {RUNBOOK['fix']}.", f"Fix: {who}", action, cid)
                return 200, f"Started a fix session in {tilde(folder)}. It's working now."
            # A feature idea: the same runbook, held to the smallest version of what was asked.
            start_session(folder, f"Build the feature asked for in support conversation {cid}. Follow {RUNBOOK['fix']}, "
                          "using its rules for feature ideas.", f"Build: {who}", action, cid)
            return 200, (f"Started building it in {tilde(folder)}. Watch it on the card; it opens a PR, "
                         "or asks you first if the idea is bigger than a small change.")
        reason = re.sub(r"[\r\n\t]+", " ", str(body.get("reason", "")))[:200].strip()
        force = bool(body.get("force"))
        open_pr = next((p["pr"] for p in load(FILES["prs"], []) if p.get("conversation") == cid and p.get("pr_state") == "open"), None)
        w = next((x for x in load(FILES["work"], []) if x.get("conversation") == cid and x.get("state") in ("working", "pr-open")), None)
        if (open_pr or w) and not force:
            return 409, (f"{c['customer']} has {open_pr + ' open' if open_pr else 'a fix in progress'}. "
                         "To close it anyway, use Close on the conversation's page.")
        marks = load(CLOSED, {})
        marks[cid] = {"t": time.time(), "reason": reason}
        CLOSED.write_text(json.dumps(marks, indent=1) + "\n")
        try:
            start_session(KIT, f"Close support conversation {cid} as not applicable. Follow {RUNBOOK['close']}."
                          + (f" Reason {OWNER} gave: {reason}" if reason else "")
                          + (f" {OWNER} chose to close it even though work is in flight: skip step 2's question." if force else ""),
                          f"Close: {who}", action, cid)
        except ActionError:
            marks.pop(cid, None)
            CLOSED.write_text(json.dumps(marks, indent=1) + "\n")
            raise
        return 200, f"Closed {c['customer']}'s conversation. Gmail gets its label in the background."

    if action in ("merge-ship", "ship"):
        key = str(body.get("pr", ""))
        m = re.fullmatch(r"([\w.-]+)#(\d+)", key)
        if not m or m.group(1) not in REPOS or not known_pr(key):
            return 400, "Unknown pull request."
        verb = "Merge and ship" if action == "merge-ship" else "Ship"  # Ship also submits a built release
        folder = CODE / REPO_DIR[m.group(1)]
        start_session(folder, f"{verb} {key}. Follow {RUNBOOK['ship']}.", f"{verb} {key}", action, key)
        return 200, f"Started a session to {verb.lower()} {key} in {tilde(folder)}. It's working now."
    return 400, "Unknown action."


def theme_location():
    """desk.latitude and desk.longitude as "lat,lon" for theme.js, which turns the desk dark from sunset to
    sunrise there. Empty when they aren't set: the page then estimates them from the browser's time zone."""
    try:
        lat, lon = float(DESK["latitude"]), float(DESK["longitude"])
    except (KeyError, TypeError, ValueError):
        return ""
    return f"{lat:.2f},{lon:.2f}" if -90 <= lat <= 90 and -180 <= lon <= 180 else ""


def page(name):
    """A page from static/ with the desk's name and location filled in (escaped: they come from config.json)."""
    return ((STATIC / name).read_text().replace("{{DESK_NAME}}", html_lib.escape(DESK_NAME))
            .replace("{{DESK_LOCATION}}", theme_location()))


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    def send(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def request_origin(self):
        port = self.server.server_port
        host = self.headers.get('Host', '')
        if host in (f'127.0.0.1:{port}', f'localhost:{port}'):
            return 'http://' + host
        if PUBLIC_ORIGIN and host == urllib.parse.urlsplit(PUBLIC_ORIGIN).netloc:
            return PUBLIC_ORIGIN
        raise AuthError(403, f'Refused: this address is not configured for the {DESK_NAME}.')

    def local_setup_allowed(self, origin):
        return (origin.startswith('http://') and self.client_address[0] in ('127.0.0.1', '::1')
                and not any(self.headers.get(name) for name in ('Forwarded', 'X-Forwarded-For', 'X-Forwarded-Host', 'X-Forwarded-Proto', 'CF-Connecting-IP', 'CF-Ray')))

    @staticmethod
    def cookie_name(origin):
        return '__Host-desk_session' if origin.startswith('https://') else 'desk_session'

    def session_token(self, origin):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
        except CookieError:
            return ''
        value = cookie.get(self.cookie_name(origin))
        return value.value if value else ''

    def session_cookie(self, origin, token='', lifetime=None):
        cookie = SimpleCookie()
        name = self.cookie_name(origin)
        cookie[name] = token
        cookie[name]['path'] = '/'
        cookie[name]['httponly'] = True
        cookie[name]['samesite'] = 'Lax'
        if origin.startswith('https://'):
            cookie[name]['secure'] = True
        if lifetime is not None:
            cookie[name]['max-age'] = lifetime
        return cookie.output(header='').strip()

    def auth_error(self, error):
        headers = {'Retry-After': str(error.retry_after)} if error.retry_after else None
        self.send(error.status, json.dumps({'message': error.message}), extra=headers)

    def read_json(self, limit):
        if self.headers.get_content_type() != 'application/json' or self.headers.get('Transfer-Encoding'):
            raise AuthError(400, 'Send a JSON request.')
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if size <= 0 or size > limit:
                raise AuthError(413, 'The request is too large or empty.')
            self.connection.settimeout(10)
            body = json.loads(self.rfile.read(size))
        except (ValueError, TimeoutError):
            raise AuthError(400, 'The request could not be read.') from None
        if not isinstance(body, dict):
            raise AuthError(400, 'Send a JSON object.')
        return body

    def do_GET(self):
        try:
            self.get_page()
        except AuthError as error:
            self.auth_error(error)

    def get_page(self):
        origin = self.request_origin()
        url = urllib.parse.urlparse(self.path)
        path = url.path
        if path == '/auth/status':
            return self.send(200, json.dumps({'service': 'support-desk', 'configured': AUTH.configured(), 'can_setup': self.local_setup_allowed(origin)}))
        public_assets = (
            '/static/login.css', '/static/login.js', '/static/theme.js', '/static/favicon.svg',
            '/static/bot.js', '/static/bot-appearance.js', '/static/bot3d.js', '/static/bot2d.js', '/static/bot-still.png',
            '/static/vendor/three-0.186.1/three.webgpu.min.js', '/static/vendor/three-0.186.1/three.core.js',
            '/static/vendor/three-0.186.1/three.tsl.min.js', '/static/vendor/three-0.186.1/RoomEnvironment.js',
        )
        signed_in = AUTH.authenticated(self.session_token(origin), origin)
        if path == '/login':
            if signed_in:
                return self.send(303, '', extra={'Location': '/'})
            mode = 'login' if AUTH.configured() else 'setup' if self.local_setup_allowed(origin) else 'locked'
            nonce = secrets.token_urlsafe(24)
            html = page('login.html').replace('{{AUTH_MODE}}', mode).replace('{{CSP_NONCE}}', nonce)
            csp = f"default-src 'self'; script-src 'self' 'nonce-{nonce}'; style-src 'self'; img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
            return self.send(200, html, 'text/html; charset=utf-8', {'Content-Security-Policy': csp})
        if not signed_in and path not in public_assets:
            if path in ('/', '/index.html'):
                return self.send(303, '', extra={'Location': '/login'})
            raise AuthError(401, f'Sign in to the {DESK_NAME} to continue.')
        if path == "/api/messages":
            cid = urllib.parse.parse_qs(url.query).get("id", [""])[0]
            f = MESSAGES / f"{cid}.json"
            if not re.fullmatch(r"[0-9a-f]{16}", cid) or not f.exists():
                return self.send(404, json.dumps({"message": "Not read yet."}))
            return self.send(200, f.read_bytes())
        if path in ("/", "/index.html"):
            html = page("index.html").replace("{{TOKEN}}", TOKEN)
            return self.send(200, html, "text/html; charset=utf-8")
        if path == "/api/state":
            return self.send(200, json.dumps(state(), ensure_ascii=False))
        if path.startswith("/static/"):
            f = (STATIC / path[len("/static/"):]).resolve()
            if STATIC in f.parents and f.is_file():
                ctype = {".js": "text/javascript", ".css": "text/css", ".woff2": "font/woff2",
                         ".svg": "image/svg+xml", ".png": "image/png"}.get(f.suffix, "application/octet-stream")
                return self.send(200, f.read_bytes(), ctype)
        self.send(404, json.dumps({"message": "Not found"}))

    def do_POST(self):
        try:
            self.post_action()
        except AuthError as error:
            self.auth_error(error)

    def post_action(self):
        origin = self.request_origin()
        if self.headers.get('Origin') != origin:
            raise AuthError(403, f'Refused: this request did not come from the {DESK_NAME} page.')
        path = urllib.parse.urlparse(self.path).path
        if path in ('/auth/login', '/auth/setup'):
            if path == '/auth/setup' and not self.local_setup_allowed(origin):
                raise AuthError(403, f'Choose the password from the {DESK_NAME} on your Mac.')
            body = self.read_json(4096)
            remember = body.get('remember') is True
            if path == '/auth/setup':
                session, lifetime = AUTH.setup(body.get('password'), body.get('confirmation'), origin, remember)
            else:
                session, lifetime = AUTH.login(body.get('password'), origin, remember)
            return self.send(200, json.dumps({'ok': True}), extra={'Set-Cookie': self.session_cookie(origin, session, lifetime if remember else None)})
        session = self.session_token(origin)
        if not AUTH.authenticated(session, origin):
            raise AuthError(401, f'Sign in to the {DESK_NAME} to continue.')
        if path == '/auth/logout':
            AUTH.logout(session)
            return self.send(200, json.dumps({'ok': True}), extra={'Set-Cookie': self.session_cookie(origin, lifetime=0)})
        if not hmac.compare_digest(self.headers.get('X-Desk-Token', '').encode('utf-8'), TOKEN.encode('ascii')):
            raise AuthError(403, f'Refused: this request did not come from the {DESK_NAME} page.')
        if path != '/api/action':
            return self.send(404, json.dumps({'message': 'Not found'}))
        body = self.read_json(16384)
        try:
            code, message = act(body)
        except ActionError as e:
            code, message = e.code, e.message
        except subprocess.SubprocessError as e:
            code, message = 500, f"A command on this Mac didn't finish: {e}"
        except ValueError:
            code, message = 400, "Bad request."
        self.send(code, json.dumps({"message": message}))


def already_running():
    """True when a desk already answers on PORT (for example, the one in the Browser pane)."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/auth/status", timeout=3) as r:
            return json.load(r).get('service') == 'support-desk'
    except Exception:  # noqa: BLE001 (anything else on the port isn't us)
        return False


if __name__ == "__main__":
    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        if already_running():
            print(f"{DESK_NAME} is already running at http://127.0.0.1:{PORT}.")
            if sys.stdout.isatty():  # typed in a terminal: open it; under launchd: just leave it be
                subprocess.run(["open", f"http://127.0.0.1:{PORT}"], check=False)
            raise SystemExit(0)
        raise SystemExit(f"Port {PORT} is taken by something else. Stop it, or change desk.port in config.json.")
    print(f"{DESK_NAME} on http://127.0.0.1:{PORT}")
    threading.Thread(target=watch_cli, daemon=True).start()
    threading.Thread(target=watch_drafts, daemon=True).start()
    threading.Thread(target=watch_ticket_prs, daemon=True).start()
    threading.Thread(target=watch_ticket_queue, daemon=True).start()
    threading.Thread(target=watch_pr_makers, daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()
    server.serve_forever()
