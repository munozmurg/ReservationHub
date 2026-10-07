"""Reservation hub CLI.

  python pipeline.py sync                 # pull Gmail (if configured) + ingest landing/ + rebuild + export
  python pipeline.py sync --every 900     # same, every 15 minutes (or use scheduling/ for launchd/cron)
  python pipeline.py export [--date D]    # CSV run sheet + all upcoming reservations
  python pipeline.py confirm [--date D] [--send]   # text guests (dry run unless --send)
  python pipeline.py reply --phone P --body YES    # simulate / record an inbound SMS
  python pipeline.py webhook [--port 8080]         # Twilio inbound SMS webhook
  python pipeline.py status               # last sync runs, counts, review queue
  python pipeline.py preshift [--as-of "YYYY-MM-DD HH:MM"]   # is the Resy snapshot fresh enough?
  python pipeline.py outage on|off        # outage mode: buffer tables, flag website bookings
  python pipeline.py availability [--date D] [--party N --at HH:MM]
  python pipeline.py book --name N --phone P --party N --at "YYYY-MM-DD HH:MM"   # website booking
  python pipeline.py outreach [--date D]  # who to text / email / reach via public notices
"""
import argparse
import csv
import fcntl
import glob
import os
import sys
import time
import traceback
import uuid
from datetime import date

from datetime import datetime

from hub import lakehouse
from hub.availability import is_available, print_grid
from hub.outage import (outage_on, outreach_plan, preshift_check, set_outage,
                        website_booking, write_public_notices)
from hub.calendar_export import write_ics
from hub.models import now_iso
from hub.sms import reply_event, send_confirmations
from hub.sources.files import detect_and_parse
from hub.sources.gmail_imap import fetch_new

HERE = os.path.dirname(os.path.abspath(__file__))
LANDING = os.path.join(HERE, "landing")
EXPORTS = os.path.join(HERE, "exports")


def _platform_from(events, path):
    return events[0]["source_platform"] if events else os.path.basename(os.path.dirname(path))


def ingest(con):
    """Ingest every landed file we haven't seen before (idempotent by content hash)."""
    files, events_written = 0, 0
    for path in sorted(glob.glob(os.path.join(LANDING, "**", "*"), recursive=True)):
        if os.path.isdir(path) or os.path.basename(path).startswith("."):
            continue
        digest = lakehouse.file_hash(path)
        if lakehouse.already_ingested(con, digest):
            continue
        try:
            events = detect_and_parse(path)
        except Exception as e:  # one bad file must not block the rest
            print(f"  ! could not parse {os.path.basename(path)}: {e}")
            continue
        platform = _platform_from(events, path)
        bronze = lakehouse.land_bronze(path, platform, digest)
        lakehouse.write_events(con, events)
        lakehouse.record_manifest(con, digest, path, platform, bronze, len(events))
        files += 1
        events_written += len(events)
        print(f"  + {os.path.basename(path)} -> {len(events)} event(s) [{platform}]")
    return files, events_written


def export_csv(con, service_date=None):
    os.makedirs(EXPORTS, exist_ok=True)
    out = {}
    q = {"upcoming_reservations": "SELECT * FROM gold_upcoming_reservations"}
    if service_date:
        q[f"run_sheet_{service_date}"] = (
            f"SELECT * FROM gold_upcoming_reservations WHERE CAST(starts_at AS DATE) = DATE '{service_date}'")
    for name, sql in q.items():
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        path = os.path.join(EXPORTS, f"{name}.csv")
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(cur.fetchall())
        out[name] = path
    return out


def sync_once():
    lock = open(os.path.join(HERE, ".sync.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another sync is running; skipping this tick.")
        return
    con = lakehouse.connect()
    run_id, started = uuid.uuid4().hex[:12], now_iso()
    con.execute("INSERT INTO sync_runs VALUES (?, ?, NULL, 'running', 0, 0, NULL)", [run_id, started])
    try:
        pulled = fetch_new(os.path.join(LANDING, "email"))
        if pulled:
            print(f"  gmail: {pulled} new email(s)")
        files, n = ingest(con)
        lakehouse.rebuild_silver_and_gold(con)
        export_csv(con, date.today().isoformat())
        ics, _ = write_ics(con)
        con.execute("UPDATE sync_runs SET finished_at=?, status='ok', files_ingested=?, events_written=? "
                    "WHERE run_id=?", [now_iso(), files, n, run_id])
        print(f"[{now_iso()}] sync ok: {files} new file(s), {n} event(s). Calendar: {os.path.relpath(ics, HERE)}")
    except Exception as e:
        con.execute("UPDATE sync_runs SET finished_at=?, status='error', error=? WHERE run_id=?",
                    [now_iso(), str(e), run_id])
        traceback.print_exc()
    finally:
        con.close()
        fcntl.flock(lock, fcntl.LOCK_UN)


def record(con, events):
    if events:
        lakehouse.write_events(con, [e for e in events if e])
        lakehouse.rebuild_silver_and_gold(con)
        write_ics(con)


def status():
    con = lakehouse.connect()
    lakehouse.rebuild_silver_and_gold(con)
    print("Last sync runs:")
    for r in con.execute("SELECT started_at, status, files_ingested, events_written, error "
                         "FROM sync_runs ORDER BY started_at DESC LIMIT 5").fetchall():
        print("  ", *r)
    last_ok = con.execute("SELECT max(finished_at) FROM sync_runs WHERE status='ok'").fetchone()[0]
    if last_ok:
        age = con.execute("SELECT epoch(now()::TIMESTAMP - ?::TIMESTAMP) / 60", [last_ok]).fetchone()[0]
        flag = "  ⚠ STALE (>20 min)" if age > 20 else ""
        print(f"Last successful sync: {last_ok} ({age:.0f} min ago){flag}")
    for title, sql in [
        ("Covers by day and source", "SELECT * FROM gold_covers_by_day_and_source"),
        ("Review queue", "SELECT guest_name, booked_via, review_reason FROM gold_review_queue"),
        ("Possible duplicates", "SELECT guest_name, via_a, starts_a, via_b, starts_b FROM gold_possible_duplicates"),
    ]:
        rows = con.execute(sql).fetchall()
        print(f"\n{title} ({len(rows)}):")
        for r in rows:
            print("  ", *r)


def webhook(port):
    """Minimal Twilio inbound-SMS endpoint: POST /sms with From & Body."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            form = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            con = lakehouse.connect()
            lakehouse.rebuild_silver_and_gold(con)
            ev = reply_event(con, form.get("From", [""])[0], form.get("Body", [""])[0])
            record(con, [ev])
            con.close()
            msg = "Thanks, see you tonight!" if ev and ev["event_type"] == "confirmed" else \
                  "Got it, your reservation is cancelled." if ev and ev["event_type"] == "declined" else \
                  "Thanks! A team member will follow up."
            self.send_response(200)
            self.send_header("Content-Type", "text/xml")
            self.end_headers()
            self.wfile.write(f"<Response><Message>{msg}</Message></Response>".encode())

    print(f"Listening on :{port}/sms (point your Twilio number's webhook here, e.g. via ngrok)")
    HTTPServer(("", port), H).serve_forever()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync"); s.add_argument("--every", type=int, help="loop, seconds between runs")
    e = sub.add_parser("export"); e.add_argument("--date", default=date.today().isoformat())
    c = sub.add_parser("confirm"); c.add_argument("--date", default=date.today().isoformat())
    c.add_argument("--send", action="store_true", help="actually send via Twilio")
    r = sub.add_parser("reply"); r.add_argument("--phone", required=True); r.add_argument("--body", required=True)
    w = sub.add_parser("webhook"); w.add_argument("--port", type=int, default=8080)
    sub.add_parser("status")
    pre = sub.add_parser("preshift"); pre.add_argument("--as-of")
    o = sub.add_parser("outage"); o.add_argument("state", choices=["on", "off"])
    av = sub.add_parser("availability"); av.add_argument("--date", default=date.today().isoformat())
    av.add_argument("--party", type=int); av.add_argument("--at")
    b = sub.add_parser("book")
    for arg in ("--name", "--phone", "--at"):
        b.add_argument(arg, required=True)
    b.add_argument("--email"); b.add_argument("--party", type=int, required=True); b.add_argument("--notes")
    ou = sub.add_parser("outreach"); ou.add_argument("--date", default=date.today().isoformat())
    a = p.parse_args()

    if a.cmd == "sync":
        while True:
            sync_once()
            if not a.every:
                break
            time.sleep(a.every)
    elif a.cmd == "status":
        status()
    elif a.cmd == "webhook":
        webhook(a.port)
    else:
        con = lakehouse.connect()
        lakehouse.rebuild_silver_and_gold(con)
        if a.cmd == "export":
            for name, path in export_csv(con, a.date).items():
                print(f"{name}: {os.path.relpath(path, HERE)}")
        elif a.cmd == "confirm":
            events, skipped = send_confirmations(con, a.date, send=a.send)
            record(con, events)
            print(f"{'Sent' if a.send else 'DRY RUN, queued'} {len(events)} confirmation text(s) -> outbox/sms_outbox.jsonl")
            if skipped:
                print(f"No phone number, call or email instead: {', '.join(skipped)}")
        elif a.cmd == "reply":
            ev = reply_event(con, a.phone, a.body)
            if not ev:
                sys.exit(f"No upcoming booking found for {a.phone}")
            record(con, [ev])
            print(f"Recorded {ev['event_type']} for {ev['reservation_key']}")
        elif a.cmd == "preshift":
            ok, msg = preshift_check(con, datetime.strptime(a.as_of, "%Y-%m-%d %H:%M") if a.as_of else None)
            print(msg)
            if ok:
                print(f"Run sheet: {os.path.relpath(export_csv(con, date.today().isoformat())['run_sheet_' + date.today().isoformat()], HERE)}")
        elif a.cmd == "outage":
            set_outage(con, a.state == "on")
            print(f"Outage mode {a.state.upper()}.")
            if a.state == "on":
                print(f"Public notices: {os.path.relpath(write_public_notices(date.today().isoformat()), HERE)}")
        elif a.cmd == "availability":
            if a.party and a.at:
                at = datetime.strptime(f"{a.date} {a.at}", "%Y-%m-%d %H:%M")
                ok, tables, reason = is_available(con, a.party, at, outage=outage_on(con))
                print(f"Party of {a.party} at {a.at}: {'AVAILABLE ' + str(tables) if ok else 'NOT AVAILABLE (' + reason + ')'}")
            else:
                print_grid(con, a.date, outage=outage_on(con))
        elif a.cmd == "book":
            at = datetime.strptime(a.at, "%Y-%m-%d %H:%M")
            ev, reason, alts = website_booking(con, a.name, a.phone, a.email, a.party, at, a.notes)
            if ev:
                record(con, [ev])
                print(f"Booked {ev['external_id']} for {a.name} ({a.party}) at {at:%H:%M} on {ev['table_id']}"
                      + (" [outage: flagged to re-enter in Resy]" if ev["needs_review"] else ""))
            else:
                print(f"Can't book {a.name} ({a.party}) at {at:%H:%M}: {reason}. "
                      f"Offer instead: {', '.join(t.strftime('%H:%M') for t in alts) or 'nothing tonight'}")
        elif a.cmd == "outreach":
            for bucket, names in outreach_plan(con, a.date).items():
                print(f"{bucket.upper()} ({len(names)}):")
                for n in names:
                    print("   ", n)
        con.close()


if __name__ == "__main__":
    main()
