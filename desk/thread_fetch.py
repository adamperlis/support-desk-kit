"""Read one support conversation's Gmail threads, for the desk's message view.

Never run this locally: it needs the Composio workbench's run_composio_tool. The desk server
fills in THREADS, ACC, OURS and RELAYS from config.json and sends the whole file as code_to_execute
(see read_messages in server.py).

Read-only: it only calls GMAIL_FETCH_MESSAGE_BY_THREAD_ID on the support mailbox's Composio account. Same
rules as dashboard/gmail_digest.py: email addresses become [email] and a sender is a first name,
before anything prints. Quoted earlier replies are cut, line breaks are kept. Its result is one line:
DESK_MESSAGES and JSON when printed, DESK_MESSAGES_Z and compressed base64 as the cell's last expression.
"""
import base64, json, re, zlib
from html import unescape
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime

THREADS = []  # set by the desk: the conversation's thread ids
ACC = ""  # set by the desk: support.composio_gmail_account, the support mailbox; never another account
OURS = set()  # set by the desk: support.our_domains, mail from these is us
RELAYS = []  # set by the desk: support.relay_senders, sender names that forward a customer's in-app form
EMRE = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
PER_MESSAGE = 4000  # characters; the workbench turns long output into a file the desk can't read
TOTAL = 60000


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
    h = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h\d)>", "\n", h)
    return unescape(re.sub(r"<[^>]+>", " ", h))


def strip_quoted(t):
    lines = []
    for ln in t.replace("\r\n", "\n").splitlines():
        s = ln.strip()
        if s.startswith(">"):
            continue
        if re.match(r"^(On .+wrote:|Le .+a écrit|El .+escribió|Am .+schrieb|-----Original|From: .*Sent:)", s):
            break
        lines.append(re.sub(r"[ \t]+", " ", ln).strip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def hdr(m, name):
    for h in (m.get("payload") or {}).get("headers") or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def fetch(tid):
    r, e = run_composio_tool("GMAIL_FETCH_MESSAGE_BY_THREAD_ID", {"thread_id": tid, "user_id": "me"}, account=ACC)  # noqa: F821 (workbench builtin)
    if e:
        return tid, None, str(e)[:200]
    out = []
    for m in (r.get("data", r).get("messages") or []):
        frm = hdr(m, "From") or m.get("sender", "")
        dom = (re.search(r"@([\w.-]+)", frm) or [None, ""])[1].lower()
        raw = to_text(m.get("payload")) or m.get("messageText") or ""
        form = re.search(r"From:\s*([^<\n]+?)\s*<", raw)  # the in-app form: the customer's name is in the body
        name = EMRE.sub("", re.sub(r"<.*?>", "", frm)).strip().strip('"')
        relay = any(x in name for x in RELAYS)  # sent from our domain, but written by a customer
        us = dom in OURS and not relay
        who = (EMRE.sub("", form.group(1)).split() or ["?"])[0] if form else "us" if us else (name.split() or ["?"])[0]
        ts = int(m.get("internalDate") or 0)
        if not ts:
            try:
                ts = int(parsedate_to_datetime(hdr(m, "Date")).timestamp() * 1000)
            except (TypeError, ValueError):
                pass
        out.append({
            "ts": ts, "thread": tid, "from": who[:30], "us": us,
            "auto": hdr(m, "Auto-Submitted") not in ("", "no") or hdr(m, "Precedence") in ("bulk", "list"),
            "subject": EMRE.sub("[email]", hdr(m, "Subject") or m.get("subject", ""))[:200],
            "text": EMRE.sub("[email]", strip_quoted(raw))[:PER_MESSAGE],
            "atts": [a.get("filename") for a in m.get("attachmentList") or [] if a.get("filename")][:10],
        })
    return tid, out, None


with ThreadPoolExecutor(4) as ex:
    res = list(ex.map(fetch, THREADS))
msgs = sorted((m for _, out, _ in res for m in out or []), key=lambda m: m["ts"])
errors = [f"{tid}: {e}" for tid, _, e in res if e]
trimmed = 0
while len(json.dumps(msgs)) > TOTAL and len(msgs) > 1:  # keep the newest messages
    msgs.pop(0)
    trimmed += 1
# The workbench stopped returning printed output on 6 Nov 2026. It returns a cell's last expression (in
# data.results) instead, but as Python's repr of it, which escapes quotes and backslashes. So the result
# is printed as before and also left last as compressed base64, which no escaping can touch; the desk
# reads whichever arrives.
desk_messages_json = json.dumps({"messages": msgs, "errors": errors, "trimmed": trimmed}, ensure_ascii=False)
print("DESK_MESSAGES " + desk_messages_json)
"DESK_MESSAGES_Z " + base64.b64encode(zlib.compress(desk_messages_json.encode("utf-8"), 9)).decode("ascii")
