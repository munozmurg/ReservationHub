"""Prototype web app: host dashboard (/) and guest booking page (/book).

  .venv/bin/python app.py            # http://localhost:8765

Everything reads from and writes to the same lakehouse the CLI uses. The
"Demo" buttons replay the outage story step by step for a live pitch.
"""
import glob
import json
import os
import shutil
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pipeline
from hub import lakehouse
from hub.availability import alternatives, build_grid, is_available, load_config, slots
from hub.outage import (_settings, outage_on, outreach_plan, preshift_check, set_outage,
                        website_booking, write_public_notices)
from hub.sms import reply_event, send_confirmations

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICE_DATE = os.environ.get("SERVICE_DATE", "2026-10-07")
PORT = int(os.environ.get("PORT", 8790))


def _clock(con):
    """Demo clock, so the pre-shift check reads like it's 4:30pm during the pitch."""
    _settings(con)
    row = con.execute("SELECT value FROM settings WHERE key = 'clock'").fetchone()
    return datetime.strptime(row[0], "%Y-%m-%d %H:%M") if row else datetime.now()


def _set_clock(con, hhmm):
    _settings(con)
    con.execute("INSERT OR REPLACE INTO settings VALUES ('clock', ?, now())", [f"{SERVICE_DATE} {hhmm}"])


def _rows(con, sql, params=()):
    cur = con.execute(sql, list(params))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def state():
    con = lakehouse.connect()
    try:
        lakehouse.rebuild_silver_and_gold(con)
        has_data = con.execute("SELECT count(*) FROM ingest_manifest").fetchone()[0] > 0
        cfg = load_config()
        out = {"service_date": SERVICE_DATE, "restaurant": cfg["restaurant_name"],
               "outage": outage_on(con), "has_data": has_data,
               "clock": _clock(con).strftime("%H:%M")}
        sync = con.execute("SELECT max(finished_at) FROM sync_runs WHERE status = 'ok'").fetchone()[0]
        out["last_sync"] = sync.strftime("%H:%M:%S") if sync else None
        if not has_data:
            return out
        ok, msg = preshift_check(con, _clock(con))
        out["preshift"] = {"ok": ok, "message": msg}
        out["run_sheet"] = _rows(con, """
            SELECT strftime(starts_at, '%H:%M') AS time, guest_name, party_size, phone, email,
                   booked_via, external_id, table_id, guest_confirmation, notes, needs_review
            FROM gold_upcoming_reservations WHERE CAST(starts_at AS DATE) = ?""", [SERVICE_DATE])
        out["cancelled"] = _rows(con, """
            SELECT strftime(starts_at, '%H:%M') AS time, guest_name, party_size, booked_via
            FROM reservations WHERE status = 'cancelled' AND CAST(starts_at AS DATE) = ?
            ORDER BY starts_at""", [SERVICE_DATE])
        out["review"] = _rows(con, "SELECT guest_name, booked_via, review_reason FROM gold_review_queue")
        out["duplicates"] = _rows(con, """
            SELECT guest_name, via_a, strftime(starts_a, '%H:%M') AS time_a,
                   via_b, strftime(starts_b, '%H:%M') AS time_b FROM gold_possible_duplicates""")
        out["outreach"] = outreach_plan(con, SERVICE_DATE)
        grid, unplaced = build_grid(con, SERVICE_DATE, cfg)
        placed = {(g, st.strftime("%H:%M")): tid for tid, bks in grid.items() for st, _, g in bks}
        for r in out["run_sheet"]:
            r["table_id"] = r["table_id"] or placed.get((r["guest_name"], r["time"]))
        out["grid"] = {
            "slots": [s.strftime("%H:%M") for s in slots(cfg, SERVICE_DATE)],
            "tables": [{"table_id": t["table_id"], "seats": t["seats"],
                        "bookings": [{"start": s.strftime("%H:%M"), "end": e.strftime("%H:%M"), "guest": g}
                                     for s, e, g in grid[t["table_id"]]]} for t in cfg["tables"]],
            "unplaced": [{"guest": g, "party": p, "time": s.strftime("%H:%M")} for g, p, s in unplaced],
            "buffer_tables": round(len(cfg["tables"]) * cfg["outage_buffer_pct"] + 0.4999),
            "service_start": cfg["service_start"],
        }
        notices = os.path.join(HERE, "exports", f"outage_notices_{SERVICE_DATE}.txt")
        out["notices"] = open(notices).read() if out["outage"] and os.path.exists(notices) else None
        return out
    finally:
        con.close()


def demo_step(step):
    """Replay the pitch: reset -> 4pm snapshot -> Resy down -> emails arrive."""
    if step == "reset":
        for d in ("landing", "lakehouse", "exports", "outbox"):
            shutil.rmtree(os.path.join(HERE, d), ignore_errors=True)
        return "Demo reset"
    for d in ("resy", "website", "email"):
        os.makedirs(os.path.join(HERE, "landing", d), exist_ok=True)
    if step == "snapshot":
        for f in glob.glob(os.path.join(HERE, "sample_data", "wave1_before_outage", "resy_*.csv")):
            shutil.copy(f, os.path.join(HERE, "landing", "resy"))
        for f in glob.glob(os.path.join(HERE, "sample_data", "wave1_before_outage", "website_*.json")):
            shutil.copy(f, os.path.join(HERE, "landing", "website"))
        pipeline.sync_once()
        con = lakehouse.connect(); _set_clock(con, "16:30"); con.close()
        return "4:00pm Resy export and website bookings synced"
    if step == "outage":
        con = lakehouse.connect()
        set_outage(con, True); _set_clock(con, "17:05")
        write_public_notices(SERVICE_DATE); con.close()
        return "Resy is down: outage mode on"
    if step == "emails":
        for f in glob.glob(os.path.join(HERE, "sample_data", "wave2_during_outage", "email", "*.eml")):
            shutil.copy(f, os.path.join(HERE, "landing", "email"))
        pipeline.sync_once()
        return "Booking emails ingested"
    if step == "recovered":
        con = lakehouse.connect(); set_outage(con, False); _set_clock(con, "19:00"); con.close()
        return "Resy is back: outage mode off"
    raise ValueError(step)


def api_post(path, body):
    if path == "/api/demo":
        return {"message": demo_step(body["step"])}
    if path == "/api/sync":
        pipeline.sync_once()
        return {"message": "Sync complete"}
    con = lakehouse.connect()
    try:
        lakehouse.rebuild_silver_and_gold(con)
        if path == "/api/outage":
            set_outage(con, bool(body["on"]))
            if body["on"]:
                write_public_notices(SERVICE_DATE)
            return {"message": f"Outage mode {'on' if body['on'] else 'off'}"}
        if path == "/api/confirm":
            events, skipped = send_confirmations(con, SERVICE_DATE, send=False)
            pipeline.record(con, events)
            return {"message": f"{len(events)} confirmation texts queued (dry run)"
                               + (f". No phone: {', '.join(skipped)}" if skipped else "")}
        if path == "/api/reply":
            ev = reply_event(con, body["phone"], body["body"])
            if not ev:
                return {"error": "No upcoming booking for that phone number"}
            pipeline.record(con, [ev])
            return {"message": f"Recorded '{ev['event_type']}' from {body['phone']}"}
        if path == "/api/availability":
            at = datetime.strptime(f"{SERVICE_DATE} {body['time']}", "%Y-%m-%d %H:%M")
            ok, tables, reason = is_available(con, int(body["party"]), at, outage=outage_on(con))
            alts = [] if ok else [t.strftime("%H:%M") for t in sorted(alternatives(con, int(body["party"]), at, outage_on(con)))]
            return {"available": ok, "reason": reason, "alternatives": alts}
        if path == "/api/book":
            at = datetime.strptime(f"{SERVICE_DATE} {body['time']}", "%Y-%m-%d %H:%M")
            ev, reason, alts = website_booking(con, body["name"], body["phone"], body.get("email"),
                                               int(body["party"]), at, body.get("notes"))
            if not ev:
                return {"booked": False, "reason": reason, "alternatives": [t.strftime("%H:%M") for t in sorted(alts)]}
            pipeline.record(con, [ev])
            return {"booked": True, "confirmation": ev["external_id"], "table": ev["table_id"],
                    "outage": bool(ev["needs_review"])}
    finally:
        con.close()
    raise KeyError(path)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload, ctype="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        pages = {"/": "dashboard.html", "/book": "book.html"}
        if path in pages:
            with open(os.path.join(HERE, "web", pages[path]), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path == "/web/style.css":
            with open(os.path.join(HERE, "web", "style.css"), "rb") as f:
                return self._send(200, f.read(), "text/css")
        if path == "/api/state":
            return self._send(200, state())
        if path == "/api/slots":
            return self._send(200, [s.strftime("%H:%M") for s in slots(load_config(), SERVICE_DATE)])
        self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        try:
            self._send(200, api_post(urlparse(self.path).path, body))
        except KeyError:
            self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(400, {"error": str(e)})

    def log_message(self, fmt, *args):
        print(f"[web] {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")


if __name__ == "__main__":
    print(f"Host dashboard: http://localhost:{PORT}/   Guest booking: http://localhost:{PORT}/book")
    HTTPServer(("", PORT), Handler).serve_forever()  # single-threaded: one DuckDB writer at a time
