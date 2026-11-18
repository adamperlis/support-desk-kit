# dashboard/: the desk's data and runbooks

Everything the desk (`../desk`) shows comes from the JSON files in this folder.
The Markdown runbooks here tell each Claude Code session what to do, and the
scripts are the only things that write the data.

Every data file is gitignored. They hold first names, one-line issues in our
own words, Gmail thread ids and PR keys. They never hold email addresses,
surnames or customer text, and the scripts refuse an `@`.

## How the data flows

```
mailbox ──(Composio workbench: gmail_digest.py)──▶ a session ──▶ conversations.json
Slack channel (optional, slack.md) ───────────────▶ a session ──┘
                                                                  │
GitHub (gh) ──────────────────────────────────▶ refresh.py ◀─────┘
                                                     │
                                                     ▼
                                            out/*.json ──▶ the desk
```

1. **Inbox to rows.** A session runs `gmail_digest.py` in the Composio
   workbench and curates `conversations.json` from what it prints: one row per
   conversation. `refresh-runbook.md` has the whole procedure and the field
   rules. Optional Slack rows follow `slack.md`.
2. **Rows to the desk.** `python3 dashboard/refresh.py` asks GitHub about every
   PR the rows link, places merged fixes on their release, and writes
   `out/*.json`. The desk reads those files from disk every few seconds.

The pass does both every hour. To do them by hand, start `claude` in the kit
root and ask it to follow `refresh-runbook.md`. The desk's **Recheck
releases** button and its two-hourly autopilot check rerun step 2 alone,
since it needs no Gmail.

## Rows

`conversations.json` is the one file sessions edit by hand. Its shape:

```json
{
  "read_at": "2026-11-18T09:00:12Z",
  "slack_read_ts": "1794320000.000100",
  "conversations": [
    {
      "id": "1e00000000000001",
      "first_name": "Maya",
      "product": "Example App",
      "issue": "CSV export gives a file with only the header row",
      "status": "triaged",
      "labels": ["bug", "acknowledged", "triaged"],
      "last_activity": "2026-11-18T08:41Z",
      "last_from": "us",
      "threads": ["1e00000000000001"],
      "prs": [],
      "owed": "",
      "needs_fix": true
    }
  ]
}
```

- **`status`** comes from the Gmail labels: `resolved`, `needs-owner`,
  `fix-in-review`, then `waiting-on-customer` when our last message asked for
  something, else `triaged`.
- **`prs`** are keys of the form `<repo>#<number>`, where `<repo>` is a
  product's `repo` in `config.json`.
- **`needs_fix: true`** puts **Fix it** on the conversation, and lets the
  autopilot start a fix session for it.
- **`owed`** puts it in the owner's For you list.

`refresh-runbook.md` has the full rules, including which threads get a row
and when a row is dropped. `sample/` has a complete example.

## Releases

`refresh.py` places each merged fix on a release *track* and says whether
that release is live. Out of the box, a product with no track ships when its
PR merges, so a merged fix is live. To wait on a real release (a store, an
updater feed, a deploy), give the product a track. The root README covers
this under "What's not included".

`notify.py` turns those states into the customer notes that are due, one per
conversation per milestone. It records each note in `notified.json` with the
fixes it covered, so a later release that brings a customer nothing new sends
nothing.

## Runbooks

| Runbook | Started by | What it may do |
| --- | --- | --- |
| `pass-runbook.md` | the autopilot, hourly; **Check now** | read the inbox, acknowledge, label, send due notes, close, refresh |
| `refresh-runbook.md` | the pass; you, by hand | read the inbox and rebuild the data. Sends nothing |
| `fix-runbook.md` | **Fix it**, **Build it**; the autopilot | open one PR in the product's repo, and add `fix-in-review` |
| `reply-runbook.md` | **Draft a reply** | save a draft for the owner. Sends nothing |
| `send-runbook.md` | **Send** on an approved draft | send exactly that draft, once |
| `notify-runbook.md` | the pass | send the release notes that are due |
| `close-runbook.md` | **Close as not applicable** | add `resolved`, and update the row |
| `ship-runbook.md` | **Merge & ship**, **Start the release**, **Submit for review** | merge one PR, and release by the repo's own process |
| `digest-runbook.md` | the autopilot, daily | insert one digest into the owner's inbox |
| `tripwire-runbook.md` | the autopilot, daily | read what was sent, and flag anything off |
| `slack.md` | the pass, when Slack is on | read one channel. Never posts |
| `rules.md` | every session | the rules above all runbooks |

`../desk/ticket-runbook.md` covers **Work on it** on a ticket.

## Files

| File | What it is | Written by |
| --- | --- | --- |
| `conversations.json` | the curated rows | sessions (refresh-runbook.md) |
| `out/conversations.json`, `out/prs.json`, `out/releases.json`, `out/unreleased.json`, `out/meta.json` | what the desk shows | `refresh.py` |
| `work.json`, `out/work.json` | what each fix, ship or close session is doing | `work.py` |
| `drafts.json` | reply drafts waiting for the owner, and their state | `drafts.py`, and the desk |
| `notified.json` | which notes were sent, claimed or baselined, with the fixes each covered | `notify.py` |
| `passlog.json` | the newest 200 passes, for the Autopilot view | `passlog.py` |
| `ship.json` | today's release per repo, so same-day fixes ship together | `ship.py` |
| `gmail_digest.py` | the read-only inbox digest, run in the Composio workbench and never locally | (code) |
| `refresh.py` | GitHub and release checks | (code) |
| `sample/` | demo data (see the root README) | (committed) |

## Scripts at a glance

All of them run from the kit root and take one writer at a time (a lock file
beside the data).

```bash
python3 dashboard/refresh.py                               # rebuild out/ from conversations.json and GitHub
python3 dashboard/work.py set <id> working|pr-open|blocked|done [--session T] [--pr URL] [--note TEXT]
python3 dashboard/drafts.py add|show|sent|failed|list ...  # reply drafts
python3 dashboard/notify.py plan|claim|sent|free|baseline|log
python3 dashboard/passlog.py add ... --summary "..." | show [N]
python3 dashboard/ship.py join|merged|drop|set|status ...
```
