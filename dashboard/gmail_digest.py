"""Read-only digest of the support mailbox's threads, for the Composio remote workbench.

Never run this locally: it needs the workbench's run_composio_tool, and it can't
import config.py. Before each run, the session fills in the three lines below
marked "set to", then sends the whole file as code_to_execute:

  SINCE    conversations.json's read_at
  KNOWN    every id in every row's "threads" in conversations.json
  SUPPORT  config.json's "support" object, pasted as it is (only the keys below are read)

It only lists labels, lists threads and reads them. It never sends, drafts or
labels anything. Email addresses become [email] before anything prints, and
senders are reduced to their first names.
"""
import base64, json, re, datetime as dt
from html import unescape
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime

SINCE = "2026-01-01T00:00:00Z"  # set to conversations.json read_at
KNOWN = []  # set to every id in every row's "threads"
SUPPORT = {"mailbox": "support@example.com", "composio_gmail_account": "gmail_your-connected-account-id", "our_domains": ["example.com"], "label_prefix": "support"}  # set to config.json's "support" object

# From config.json's "support" object:
ACC = SUPPORT["composio_gmail_account"]  # the connected Gmail account; never another account
MAILBOX = SUPPORT.get("mailbox", "")  # the address customers write to
OURS = {d.lower() for d in SUPPORT.get("our_domains", [])}  # mail from these domains is us
PREFIX = SUPPORT.get("label_prefix", "support").strip("/")  # the desk's Gmail labels are <prefix>/<status>
SKIP_DOMS = {d.lower() for d in SUPPORT.get("skip_domains", [])}  # optional: senders that are never support (vendors, pitches)
RELAYS = [str(r) for r in SUPPORT.get("relay_senders") or [] if r]  # optional: names an in-app contact form sends as. Its mail is a customer's
TEST_SENDERS = [a.lower() for a in SUPPORT.get("test_senders", []) if a]  # optional: your own addresses (or "name@") you test from
CRASH_SUBJECTS = SUPPORT.get("crash_subjects", [])  # optional: subjects of automated crash or log reports, shown one line each

EMRE = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
since_ms = int(dt.datetime.fromisoformat(SINCE.replace("Z", "+00:00")).timestamp() * 1000)


def call(tool, args):
    r, e = run_composio_tool(tool, args, account=ACC)  # noqa: F821 (workbench builtin)
    if e:
        raise RuntimeError(f"{tool}: {str(e)[:200]}")
    return r.get("data", r)


def dec(s):
    if not s:
        return ""
    s = s.replace("-", "+").replace("_", "/")
    s += "=" * ((4 - len(s) % 4) % 4)
    return base64.b64decode(s).decode("utf-8", "replace")


def collect(p, out):
    mt, data = p.get("mimeType", ""), (p.get("body") or {}).get("data")
    if data and mt.startswith("text/"):
        out.append((mt, dec(data)))
    for c in p.get("parts") or []:
        collect(c, out)


def to_text(payload):
    buf = []
    collect(payload or {}, buf)
    plain = next((t for mt, t in buf if mt == "text/plain" and t.strip()), "")
    if plain:
        return plain
    h = next((t for mt, t in buf if mt == "text/html"), "")
    h = re.sub(r"(?is)<(style|script).*?</\1>", " ", h)
    return unescape(re.sub(r"<[^>]+>", " ", h))


def strip_quoted(t):
    lines = []
    for ln in t.splitlines():
        s = ln.strip()
        if s.startswith(">"):
            continue
        if re.match(r"^(On .+wrote:|Le .+a écrit|El .+escribió|Am .+schrieb|-----Original|From: .*Sent:)", s):
            break
        lines.append(ln)
    return re.sub(r"\s+", " ", "\n".join(lines)).strip()


def hdr(m, name):
    for h in (m.get("payload") or {}).get("headers") or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def list_threads(q):
    out, tok = [], ""
    while True:
        args = {"query": q, "max_results": 200, "user_id": "me"}
        if tok:
            args["page_token"] = tok
        d = call("GMAIL_LIST_THREADS", args)
        out += [t["id"] for t in d.get("threads") or []]
        tok = d.get("nextPageToken") or ""
        if not tok:
            return out


def fetch(tid):
    try:
        msgs = call("GMAIL_FETCH_MESSAGE_BY_THREAD_ID", {"thread_id": tid, "user_id": "me"}).get("messages") or []
    except RuntimeError as e:
        return tid, {"error": str(e)}
    out = []
    for m in msgs:
        frm = hdr(m, "From") or m.get("sender", "")
        dom = (re.search(r"@([\w.-]+)", frm) or [None, ""])[1].lower()
        raw = to_text(m.get("payload")) or m.get("messageText") or ""
        form = re.search(r"From:\s*([^<\n]+?)\s*<", raw)  # in-app form: customer's name
        ts = int(m.get("internalDate") or 0)
        if not ts:
            try:
                ts = int(parsedate_to_datetime(hdr(m, "Date")).timestamp() * 1000)
            except (TypeError, ValueError):
                pass
        name = EMRE.sub("[email]", re.sub(r"<.*?>", "", frm).strip().strip('"'))
        via_form = next((r for r in RELAYS if r in name), "")
        out.append({
            "ts": ts, "dom": dom, "subject": EMRE.sub("[email]", hdr(m, "Subject") or m.get("subject", "")),
            "name": via_form or (name.split() or [""])[0],  # first name only
            "us": dom in OURS and not via_form, "test": any(a in frm.lower() for a in TEST_SENDERS),
            "form": EMRE.sub("", form.group(1)).strip() if form else "",
            "auto": bool(hdr(m, "Auto-Submitted") not in ("", "no") or hdr(m, "Precedence") in ("bulk", "list")),
            "labels": m.get("labelIds") or [],
            "text": EMRE.sub("[email]", strip_quoted(raw))[:600],
            "atts": [a.get("filename") for a in m.get("attachmentList") or []],
        })
    out.sort(key=lambda x: x["ts"])
    return tid, {"messages": out}


labels = call("GMAIL_LIST_LABELS", {"user_id": "me"}).get("labels") or []
SUP = {l["id"]: l["name"][len(PREFIX) + 1:] for l in labels if l["name"].lower().startswith(PREFIX.lower() + "/")}
# Gmail searches a nested label "support/needs-reply" as label:support-needs-reply.
terms = sorted({"label:" + re.sub(r"[/\s]+", "-", n.lower()) for n in (PREFIX + "/" + s for s in SUP.values())})
QUERIES = ([f"({' OR '.join(terms)}) newer_than:60d"] if terms else []) + \
          ([f"{{to:{MAILBOX} cc:{MAILBOX} deliveredto:{MAILBOX}}} newer_than:30d"] if MAILBOX else [])
ids = list(dict.fromkeys([t for q in QUERIES for t in list_threads(q)] + KNOWN))
with ThreadPoolExecutor(8) as ex:
    res = dict(ex.map(fetch, ids))


def when(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def who(m):
    w = "US" if m["us"] else "OWNER-TEST" if m["test"] else (m["name"][:24] or "?")
    return w + (f" [form:{m['form'].split()[0]}]" if m["form"] else "") + (" [auto]" if m["auto"] else "")


lines, fresh = [], 0
order = sorted(res.items(), key=lambda kv: -max([m["ts"] for m in kv[1].get("messages", [])] or [0]))
for tid, t in order:
    if "error" in t:
        lines.append(f"## {tid} ERROR {t['error']}")
        continue
    ms = t["messages"]
    if not ms or ms[0]["dom"] in SKIP_DOMS:
        continue
    subj, last = ms[0]["subject"][:80], ms[-1]
    L = sorted({SUP[l] for m in ms for l in m["labels"] if l in SUP})
    new = [m for m in ms if m["ts"] > since_ms]
    tag = "KNOWN" if tid in KNOWN else "NEW"
    if any(c and c in subj for c in CRASH_SUBJECTS):
        note = re.search(r"User note:\s*(.{0,300})", ms[0]["text"])
        lines.append(f"## {tid} {tag} CRASH {when(ms[0]['ts'])} {subj[:60]} note={note.group(1)[:200] if note else None}")
        continue
    lines.append(f"## {tid} {tag} | {subj} | {','.join(L)} | {len(ms)} msgs | last {when(last['ts'])} {'us' if last['us'] else 'customer'}")
    show = (ms[:1] + [m for m in new if m is not ms[0]]) if tag == "NEW" else new
    fresh += bool(new)
    for m in show[-6:]:
        atts = f" (attachments: {', '.join(a for a in m['atts'] if a)})" if any(m["atts"]) else ""
        lines.append(f"  {when(m['ts'])} {who(m)}: {m['text']}{atts}")
print(f"read_at {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} | {len(ids)} threads, {fresh} with messages since {SINCE}")
print("\n".join(lines))
