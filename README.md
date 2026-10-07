# Reservation Hub

One live reservation book for a restaurant that takes bookings on **Resy and its own website**, built to keep service running when Resy goes down.

## Live site

`index.html` at the top of this repo is the pitch page with the clickable host-stand demo (`public/index.html` is a copy). It's plain static HTML, so it deploys on Vercel with no build step. The Python prototype lives in `prototype/`, away from the top level on purpose: it stores data in local files and runs a long-lived server, so it runs on your laptop or a normal server host. If it sits at the top level, Vercel mistakes the repo for a Python app and serves a broken dashboard instead of the page.

## Run the prototype

```bash
cd prototype
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

- **Host dashboard:** http://localhost:8790
- **Guest booking page:** http://localhost:8790/book

Use the **Pitch demo** buttons to replay the story:

| Step | What happens |
|---|---|
| 1 · 4pm Resy export lands | The 2pm and 4pm Resy exports and the website bookings sync into the hub. The pre-shift check reports that the snapshot is 30 minutes old. |
| 2 · Resy goes down | Outage mode turns on: 3 tables are held back as a buffer and the public notices are generated. |
| 3 · Booking emails arrive | Resy new, cancel and modify emails, a website form email and a free-text guest email are ingested. Avery's booking is cancelled, Morgan's changes to 3 people at 7:15, and Jordan and Sky are added. |
| Guest booking page | A party of 6 at 7:30 is refused and offered 5:30, 5:45 or 9:30. A party of 2 at 9:30 is booked and flagged to re-enter in Resy. |
| Text guests to confirm | Confirmation texts are queued (dry run). "Simulate reply" sends a YES or NO back in. |
| 4 · Resy back | Outage mode ends. |

`./demo.sh` (inside `prototype/`) runs the same story in the terminal.

## How it works

```
 Resy export (CSV, 2pm/4pm/pre-shift) ─┐
 Resy notification emails (Gmail)    ─┤                bronze/   raw files, never edited
 Website bookings (our own form)     ─┼─► landing/ ──► silver/   event log ─► current state
 Guest SMS replies (Twilio)          ─┤  every 15 min  gold/     run sheet · review · duplicates · covers
 Host edits                          ─┘
                                                │
              ┌─────────────────────────────────┼──────────────────────────────┐
         Pre-shift check                 Our availability grid            Guest outreach
   "Is our copy of the book fresh?"  tables × slots, buffer in outage  text → email → public notices
```

1. **Know the bookings:** Resy exports are the backbone. The pre-shift check warns if the latest one is more than 60 minutes old. Emails fill in what changed since.
2. **Check availability:** the hub's own table grid (`config/tables.csv`) has turn times by party size. In outage mode, 20% of tables are held back for bookings that may have been missed.
3. **Reach guests:** text anyone with a phone number, email anyone without one, and cover unreachable guests with public notices (website banner, Google post, phone greeting) plus the buffer tables.

## CLI

| Command | What it does |
|---|---|
| `pipeline.py sync [--every 900]` | Ingests new files once, or every 15 minutes |
| `pipeline.py preshift` | Checks whether the Resy snapshot is fresh |
| `pipeline.py outage on\|off` | Turns outage mode on or off |
| `pipeline.py availability [--party N --at HH:MM]` | Shows the floor grid, or checks one slot |
| `pipeline.py book --name … --phone … --party N --at "YYYY-MM-DD HH:MM"` | Makes a website booking |
| `pipeline.py outreach` | Lists guests to text, guests with email only, and unreachable guests |
| `pipeline.py confirm [--send]` | Sends confirmation texts (dry run unless `--send`) |
| `pipeline.py reply --phone … --body YES` | Records an inbound reply |
| `pipeline.py export` | Writes the run sheet and upcoming reservations as CSV |
| `pipeline.py status` | Shows sync health, review queue and duplicates |

Scheduling: `scheduling/com.reservationhub.sync.plist` (macOS launchd) or `scheduling/crontab.txt`.

## Design rules

- **Idempotent:** each file is fingerprinted with SHA-256 and ingested exactly once.
- **Event-sourced:** sources append events and never overwrite. Current state is rebuilt from the log.
- **Latest event wins** (by when it happened at the source). Ties are broken by trust: SMS > host > email/web > CSV.
- **Field-level merge:** a cancel email with only a confirmation number doesn't erase the guest's phone.
- **Never guess silently:** free-text emails, guests with no contact info, unclear replies and outage bookings go to "Needs a human".
- **Duplicates are flagged, not merged:** the same phone or email within 90 minutes on Resy and the website is surfaced for a human.

## Live configuration (optional)

```bash
export GMAIL_USER=you@gmail.com GMAIL_APP_PASSWORD=…        # reads Gmail label "Reservations"
export TWILIO_ACCOUNT_SID=… TWILIO_AUTH_TOKEN=… TWILIO_FROM_NUMBER=+1…
```

## Known limits

- Resy offers restaurants no public API. Snapshots come from Resy OS exports or official partner integrations, never scraping.
- Sample emails and exports are mock data. Parsers must be checked against real Resy emails.
- A booking that vanishes from a later export without a cancel email isn't detected yet (the next step is a snapshot diff).
- US business texting needs A2P 10DLC registration before going live.
- OpenTable parsers exist (`sample_data/extra_opentable/`) but aren't part of this demo.
