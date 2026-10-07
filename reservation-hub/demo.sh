#!/usr/bin/env bash
# End-to-end demo for a restaurant that takes bookings on Resy + its own website.
#   2:00pm / 4:00pm  scheduled Resy exports + website bookings land in the hub
#   4:30pm           pre-shift check: is our copy of the Resy book fresh?
#   5:00pm           Resy goes down -> outage mode
#   during outage    emails keep flowing in; website keeps booking against OUR grid
#   service          text guests to confirm, handle replies, reach the unreachable
set -euo pipefail
cd "$(dirname "$0")"
PY=.venv/bin/python
TODAY=2026-10-07
rm -rf landing lakehouse exports outbox .gmail_state
mkdir -p landing/resy landing/website landing/email

echo "== 2:00pm + 4:00pm: Resy exports and website bookings land =="
cp sample_data/wave1_before_outage/resy_*.csv landing/resy/
cp sample_data/wave1_before_outage/website_*.json landing/website/
$PY pipeline.py sync

echo; echo "== 4:30pm: pre-shift check =="
$PY pipeline.py preshift --as-of "$TODAY 16:30"

echo; echo "== 5:00pm: Resy is down. Turn on outage mode =="
$PY pipeline.py outage on

echo; echo "== Booking emails sent before/around the outage still arrive =="
cp sample_data/wave2_during_outage/email/*.eml landing/email/
$PY pipeline.py sync

echo; echo "== Tonight's floor, from OUR grid (not Resy's) =="
$PY pipeline.py availability --date $TODAY

echo; echo "== Website keeps taking bookings, checked against the grid + buffer =="
$PY pipeline.py book --name "Lee Park" --phone "917-555-0210" --party 6 --at "$TODAY 19:30" || true
$PY pipeline.py book --name "Lee Park" --phone "917-555-0210" --party 2 --at "$TODAY 21:30"

echo; echo "== Reach every guest tonight =="
$PY pipeline.py outreach --date $TODAY
$PY pipeline.py confirm --date $TODAY
$PY pipeline.py reply --phone "917-555-0123" --body "YES"
$PY pipeline.py reply --phone "(212) 555-0177" --body "no, sorry!"

echo; echo "== Run sheet + status =="
$PY pipeline.py export --date $TODAY
$PY - <<'PYEOF'
import csv
rows = list(csv.DictReader(open("exports/run_sheet_2026-10-07.csv")))
fmt = "{:<6} {:<15} {:>3}  {:<8} {:<10} {:<12} {}"
print(fmt.format("time", "guest", "pax", "via", "conf#", "confirmed?", "notes"))
for r in rows:
    print(fmt.format(r["starts_at"][11:16], r["guest_name"], r["party_size"], r["booked_via"],
                     r["external_id"], r["guest_confirmation"], r["notes"] or ""))
PYEOF
echo; $PY pipeline.py status
