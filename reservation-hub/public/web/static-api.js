/* In-browser version of the Reservation Hub engine.
 *
 * The Python prototype (prototype/app.py) serves /api/* from a DuckDB lakehouse.
 * Static hosting can't run that, so this file answers the same /api/* calls
 * inside the page by replacing window.fetch for those paths. The dashboard and
 * booking page run unchanged. State lives in localStorage, so both pages share
 * one reservation book. Sample data mirrors prototype/sample_data/.
 *
 * Same rules as the Python engine: append-only events, latest non-null value per
 * field wins (ties broken by source trust), own table grid with turn times, outage
 * buffer, review queue, duplicate detection, SMS confirmation and replies.
 */
(function () {
  "use strict";

  var D = "2026-10-07"; // service date
  var CFG = {
    name: "Casa Demo", phone: "(212) 555-0100", start: "17:00", last: "21:30", step: 15,
    bufferPct: 0.2, maxAge: 60,
    turn: [[2, 90], [4, 105], [6, 120], [8, 135]],
    tables: [["T1", 2], ["T2", 2], ["T3", 2], ["T4", 2], ["T5", 4], ["T6", 4], ["T7", 4], ["T8", 4],
             ["T9", 4], ["T10", 4], ["T11", 6], ["T12", 8]]
  };
  var TRUST = { csv: 0, json: 1, web: 1, email: 1, manual: 3, sms: 4 };

  // ---------- helpers ----------
  function pad(n, w) { n = String(n); while (n.length < (w || 2)) n = "0" + n; return n; }
  function mins(hhmm) { var p = hhmm.split(":"); return +p[0] * 60 + +p[1]; }
  function hhmm(m) { return pad(Math.floor(m / 60)) + ":" + pad(m % 60); }
  function dateOf(s) { return s.slice(0, 10); }
  function timeOf(s) { return s.slice(11, 16); }
  function startMin(s) { return mins(timeOf(s)); }
  function normPhone(raw) {
    if (!raw) return null;
    var d = String(raw).replace(/\D/g, "");
    if (d.length === 10) return "+1" + d;
    if (d.length === 11 && d[0] === "1") return "+" + d;
    return d ? "+" + d : null;
  }
  function hash(s) { var h = 5381; for (var i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) >>> 0; return h.toString(16); }
  function turnMinutes(party) {
    for (var i = 0; i < CFG.turn.length; i++) if (party <= CFG.turn[i][0]) return CFG.turn[i][1];
    return CFG.turn[CFG.turn.length - 1][1];
  }
  function overlaps(a0, a1, b0, b1) { return a0 < b1 && b0 < a1; }
  function slotList() {
    var out = [], t = mins(CFG.start), end = mins(CFG.last);
    for (; t <= end; t += CFG.step) out.push(hhmm(t));
    return out;
  }
  function realClock() { var n = new Date(); return pad(n.getHours()) + ":" + pad(n.getMinutes()); }

  // ---------- events ----------
  function E(o) {
    var e = { key: null, platform: null, channel: null, type: null, at: null, id: null, name: null, phone: null,
              email: null, party: null, starts: null, table: null, status: null, conf: null, notes: null,
              review: false, reason: null };
    for (var k in o) e[k] = o[k];
    e.phone = normPhone(e.phone);
    e.email = (e.email || "").trim().toLowerCase() || null;
    if (!e.key) e.key = e.id ? e.platform + ":" + e.id : e.platform + ":h" + hash((e.phone || "") + "|" + (e.email || "") + "|" + (e.starts || ""));
    return e;
  }
  function validated(e) {
    var r = [];
    if (["snapshot", "created", "request"].indexOf(e.type) >= 0) {
      if (!e.starts) r.push("missing date/time");
      if (!e.phone && !e.email) r.push("no way to contact guest");
      if (e.party != null && !(e.party >= 1 && e.party <= 20)) r.push("unusual party size " + e.party);
    }
    if (r.length) { e.review = true; e.reason = [e.reason].concat(r).filter(Boolean).join("; "); }
    return e;
  }

  function resyRows(at, withRobin) {
    var rows = [
      ["RSY-10421", "Avery Chen", "(212) 555-0101", "avery.chen@example.com", 2, D, "18:00", "T4", "booked", ""],
      ["RSY-10433", "Morgan Patel", "646-555-0117", "morgan.p@example.com", 2, D, "19:00", "T7", "booked", "Window seat"],
      ["RSY-10440", "Riley Thompson", "917-555-0123", "riley.t@example.com", 6, D, "19:30", "T12", "booked", "Birthday"],
      ["RSY-10447", "Casey Nguyen", "212-555-0131", "", 4, D, "20:15", "T9", "booked", "Nut allergy"],
      ["RSY-10450", "Taylor Brooks", "", "", 2, D, "20:30", "", "booked", ""],
      ["RSY-10455", "Drew Kim", "347-555-0166", "drew.kim@example.com", 2, D, "21:00", "", "cancelled", ""],
      ["RSY-10452", "Jamie Ortiz", "718-555-0150", "jamie.o@example.com", 3, "2026-10-08", "19:00", "T5", "booked", ""]
    ];
    if (withRobin) rows.splice(1, 0, ["RSY-10457", "Robin Diaz", "212-555-0161", "robin.d@example.com", 2, D, "18:45", "T2", "booked", ""]);
    return rows.map(function (r) {
      return validated(E({ platform: "resy", channel: "csv", type: "snapshot", id: r[0], at: at, name: r[1], phone: r[2],
        email: r[3], party: r[4], starts: r[5] + " " + r[6], table: r[7] || null, status: r[8], notes: r[9] || null }));
    });
  }
  function websiteRows() {
    return [
      ["W-2001", "Alex Romero", "917-555-0191", "alex.r@example.com", 2, D + " 19:15", "Patio if possible", "2026-10-06 21:12:00"],
      ["W-2002", "Blake Foster", "212-555-0193", "blake.f@example.com", 5, "2026-10-08 18:30", "", "2026-10-07 09:40:00"],
      ["W-2004", "Riley Thompson", "917-555-0123", "riley.t@example.com", 6, D + " 19:45", "", "2026-10-07 11:05:00"],
      ["W-2005", "Quinn Harper", "212-555-0177", "quinn.h@example.com", 4, D + " 18:30", "", "2026-10-07 12:30:00"]
    ].map(function (r) {
      return validated(E({ platform: "website", channel: "json", type: "request", id: r[0], at: r[7], name: r[1], phone: r[2],
        email: r[3], party: r[4], starts: r[5], status: "booked", notes: r[6] || null }));
    });
  }
  function emailEvents() {
    var day = D + " ";
    return [
      validated(E({ platform: "resy", channel: "email", type: "created", id: "RSY-10458", at: day + "17:12:00", name: "Jordan Lee",
        phone: "(212) 555-0142", email: "jordan.lee@example.com", party: 4, starts: day + "20:00", status: "booked", notes: "Anniversary" })),
      validated(E({ platform: "resy", channel: "email", type: "cancelled", id: "RSY-10421", at: day + "17:20:00", name: "Avery Chen", status: "cancelled" })),
      validated(E({ platform: "resy", channel: "email", type: "modified", id: "RSY-10433", at: day + "17:31:00", name: "Morgan Patel",
        party: 3, starts: day + "19:15", status: "booked" })),
      validated(E({ platform: "website", channel: "email", type: "request", id: "W-2003", at: day + "17:45:00", name: "Sky Martin",
        phone: "347-555-0199", email: "sky.m@example.com", party: 2, starts: day + "21:15", status: "booked", notes: "First time visiting!" })),
      E({ platform: "email", channel: "email", type: "request", at: day + "17:50:00", name: "Sam Rivera", email: "sam.rivera@example.com",
        notes: "Table tonight?: Any chance for a table for 3 around 7pm tonight?", review: true,
        reason: "free-text email, needs a human to confirm details" })
    ];
  }

  // ---------- state (localStorage, shared by dashboard and booking page) ----------
  var KEY = "reservation-hub-demo-v1", mem = null;
  function fresh() { return { events: [], seq: 0, s: { outage: false, clock: null, lastSync: null } }; }
  function load() {
    try { var raw = window.localStorage.getItem(KEY); if (raw) return JSON.parse(raw); } catch (e) { /* ignore */ }
    return mem || (mem = fresh());
  }
  function save(S) { mem = S; try { window.localStorage.setItem(KEY, JSON.stringify(S)); } catch (e) { /* ignore */ } }
  function nextAt(S) { S.seq += 1; return D + " 23:00:00." + pad(S.seq, 6); }
  function nowTime() { var n = new Date(); return pad(n.getHours()) + ":" + pad(n.getMinutes()) + ":" + pad(n.getSeconds()); }

  // ---------- derive current state from events ----------
  function derive(events) {
    var idx = events.map(function (e, i) { return [e, i]; });
    idx.sort(function (a, b) {
      if (a[0].at !== b[0].at) return a[0].at < b[0].at ? -1 : 1;
      var ta = TRUST[a[0].channel] || 0, tb = TRUST[b[0].channel] || 0;
      return ta !== tb ? ta - tb : a[1] - b[1];
    });
    var map = {}, order = [];
    idx.forEach(function (p) {
      var e = p[0], r = map[e.key];
      if (!r) { r = map[e.key] = { key: e.key, via: e.platform, ext: null, review: false, reasons: [], lastAt: e.at }; order.push(r); }
      ["name", "phone", "email", "party", "starts", "table", "notes", "status", "conf"].forEach(function (f) { if (e[f] != null) r[f] = e[f]; });
      if (!r.ext && e.id) r.ext = e.id;
      if (e.review) r.review = true;
      if (e.reason && r.reasons.indexOf(e.reason) < 0) r.reasons.push(e.reason);
      r.lastAt = e.at;
    });
    return order;
  }

  // ---------- availability grid ----------
  function buildGrid(res, date) {
    var grid = {};
    CFG.tables.forEach(function (t) { grid[t[0]] = []; });
    var rows = res.filter(function (r) { return r.status === "booked" && r.starts && dateOf(r.starts) === date; });
    rows = rows.map(function (r, i) { return [r, i]; }).sort(function (a, b) {
      var na = a[0].table ? 0 : 1, nb = b[0].table ? 0 : 1;
      if (na !== nb) return na - nb;
      var sa = a[0].starts, sb = b[0].starts;
      return sa !== sb ? (sa < sb ? -1 : 1) : a[1] - b[1];
    }).map(function (p) { return p[0]; });
    var unplaced = [];
    rows.forEach(function (r) {
      var party = r.party || 2, s = startMin(r.starts), e = s + turnMinutes(party);
      if (r.table && grid[r.table]) { grid[r.table].push([s, e, r.name]); return; }
      for (var i = 0; i < CFG.tables.length; i++) {
        var t = CFG.tables[i];
        if (t[1] >= party && !grid[t[0]].some(function (b) { return overlaps(s, e, b[0], b[1]); })) { grid[t[0]].push([s, e, r.name]); return; }
      }
      unplaced.push([r.name, party, s]);
    });
    return { grid: grid, unplaced: unplaced };
  }
  function freeTables(grid, party, s) {
    var e = s + turnMinutes(party);
    return CFG.tables.filter(function (t) {
      return t[1] >= party && !grid[t[0]].some(function (b) { return overlaps(s, e, b[0], b[1]); });
    }).map(function (t) { return t[0]; });
  }
  function heldBack() { return Math.ceil(CFG.tables.length * CFG.bufferPct); }
  function isAvailable(res, party, time, outage) {
    var g = buildGrid(res, D).grid, s = mins(time), fits = freeTables(g, party, s);
    if (!fits.length) return { ok: false, tables: [], reason: "no table that size is free" };
    if (outage) {
      var e = s + turnMinutes(party);
      var free = CFG.tables.filter(function (t) { return !g[t[0]].some(function (b) { return overlaps(s, e, b[0], b[1]); }); }).length;
      if (free <= heldBack()) return { ok: false, tables: fits, reason: "outage buffer: " + free + " free table(s), " + heldBack() + " held back" };
    }
    return { ok: true, tables: fits, reason: "ok" };
  }
  function alternatives(res, party, time, outage) {
    var want = mins(time);
    return slotList().filter(function (t) { return isAvailable(res, party, t, outage).ok; })
      .map(function (t, i) { return [t, i]; })
      .sort(function (a, b) { var da = Math.abs(mins(a[0]) - want), db = Math.abs(mins(b[0]) - want); return da !== db ? da - db : a[1] - b[1]; })
      .slice(0, 3).map(function (p) { return p[0]; }).sort();
  }

  // ---------- views ----------
  function noticesText() {
    return "PUBLIC NOTICES for " + D + "\n\nWEBSITE BANNER\nOur reservation system is having issues tonight. Your booking is still valid.\n" +
      "Text or call " + CFG.phone + " with your name and time to confirm, or just come in, we'll have your table.\n\n" +
      "GOOGLE BUSINESS PROFILE POST / INSTAGRAM STORY\nHeads up: our booking platform is down tonight. All reservations are honored.\n" +
      "Questions? Text " + CFG.phone + ". See you soon! — " + CFG.name + "\n\nPHONE GREETING / VOICEMAIL\n" +
      "Thanks for calling " + CFG.name + ". Our online booking system is temporarily down, but\nall reservations for tonight are still valid. " +
      "To confirm, text your name and\nreservation time to this number, or stay on the line.\n\nHOST STAND\n" +
      "If a guest isn't on the run sheet but shows a Resy confirmation: honor it, seat\nthem from the buffer tables, and add them in the hub as a host booking.\n";
  }
  function sortedBooked(res) {
    return res.filter(function (r) { return r.status === "booked" && r.starts && dateOf(r.starts) === D; })
      .sort(function (a, b) { return a.starts !== b.starts ? (a.starts < b.starts ? -1 : 1) : (a.name || "").localeCompare(b.name || ""); });
  }
  function outreach(res) {
    var plan = { text: [], email_only: [], unreachable: [] };
    sortedBooked(res).forEach(function (r) {
      var b = r.phone ? "text" : r.email ? "email_only" : "unreachable";
      plan[b].push(timeOf(r.starts) + " " + r.name + " (" + r.party + ") via " + r.via);
    });
    return plan;
  }
  function preshift(S) {
    var last = null;
    S.events.forEach(function (e) { if (e.platform === "resy" && e.type === "snapshot" && (!last || e.at > last)) last = e.at; });
    if (!last) return { ok: false, message: "⚠ NO RESY SNAPSHOT. Export the book from Resy OS now, before service." };
    var age = mins(S.s.clock || realClock()) - startMin(last);
    if (age > CFG.maxAge) return { ok: false, message: "⚠ STALE: last Resy snapshot is " + age + " min old (" + timeOf(last) + "). Export a fresh one before service." };
    return { ok: true, message: "✓ Resy snapshot is " + age + " min old (" + timeOf(last) + "). Safe to run service from the hub." };
  }

  function getState() {
    var S = load();
    var out = { service_date: D, restaurant: CFG.name, outage: !!S.s.outage, has_data: S.events.length > 0,
                clock: S.s.clock || realClock(), last_sync: S.s.lastSync || null };
    if (!out.has_data) return out;
    var res = derive(S.events), built = buildGrid(res, D), placed = {};
    CFG.tables.forEach(function (t) { built.grid[t[0]].forEach(function (b) { placed[b[2] + "|" + hhmm(b[0])] = t[0]; }); });
    out.preshift = preshift(S);
    out.run_sheet = sortedBooked(res).map(function (r) {
      var t = timeOf(r.starts);
      return { time: t, guest_name: r.name, party_size: r.party, phone: r.phone, email: r.email, booked_via: r.via,
               external_id: r.ext, table_id: r.table || placed[r.name + "|" + t] || null,
               guest_confirmation: r.conf || "unconfirmed", notes: r.notes, needs_review: r.review };
    });
    out.cancelled = res.filter(function (r) { return r.status === "cancelled" && r.starts && dateOf(r.starts) === D; })
      .sort(function (a, b) { return a.starts < b.starts ? -1 : a.starts > b.starts ? 1 : 0; })
      .map(function (r) { return { time: timeOf(r.starts), guest_name: r.name, party_size: r.party, booked_via: r.via }; });
    out.review = res.filter(function (r) { return r.review; })
      .sort(function (a, b) { return a.lastAt < b.lastAt ? -1 : a.lastAt > b.lastAt ? 1 : 0; })
      .map(function (r) { return { guest_name: r.name, booked_via: r.via, review_reason: r.reasons.join("; ") }; });
    var booked = res.filter(function (r) { return r.status === "booked" && r.starts; });
    out.duplicates = [];
    for (var i = 0; i < booked.length; i++) for (var j = 0; j < booked.length; j++) {
      var a = booked[i], b = booked[j];
      if (a.key < b.key && dateOf(a.starts) === dateOf(b.starts) && Math.abs(startMin(a.starts) - startMin(b.starts)) <= 90 &&
          ((a.phone && a.phone === b.phone) || (a.email && a.email === b.email))) {
        out.duplicates.push({ guest_name: a.name, via_a: a.via, time_a: timeOf(a.starts), via_b: b.via, time_b: timeOf(b.starts) });
      }
    }
    out.outreach = outreach(res);
    out.grid = {
      slots: slotList(),
      tables: CFG.tables.map(function (t) {
        return { table_id: t[0], seats: t[1], bookings: built.grid[t[0]].map(function (b) { return { start: hhmm(b[0]), end: hhmm(b[1]), guest: b[2] }; }) };
      }),
      unplaced: built.unplaced.map(function (u) { return { guest: u[0], party: u[1], time: hhmm(u[2]) }; }),
      buffer_tables: heldBack(), service_start: CFG.start
    };
    out.notices = out.outage ? noticesText() : null;
    return out;
  }

  // ---------- actions ----------
  function demoStep(step) {
    var S = load();
    if (step === "reset") { save(fresh()); return { message: "Demo reset" }; }
    if (step === "snapshot") {
      S.events = S.events.concat(resyRows(D + " 14:00:00", false), websiteRows(), resyRows(D + " 16:00:00", true));
      S.s.clock = "16:30"; S.s.lastSync = nowTime(); save(S);
      return { message: "4:00pm Resy export and website bookings synced" };
    }
    if (step === "outage") { S.s.outage = true; S.s.clock = "17:05"; save(S); return { message: "Resy is down: outage mode on" }; }
    if (step === "emails") { S.events = S.events.concat(emailEvents()); S.s.lastSync = nowTime(); save(S); return { message: "Booking emails ingested" }; }
    if (step === "recovered") { S.s.outage = false; S.s.clock = "19:00"; save(S); return { message: "Resy is back: outage mode off" }; }
    return { error: "Unknown demo step" };
  }
  function setOutage(on) { var S = load(); S.s.outage = !!on; save(S); return { message: "Outage mode " + (on ? "on" : "off") }; }
  function confirmTexts() {
    var S = load(), res = derive(S.events), n = 0, skipped = [];
    sortedBooked(res).forEach(function (r) {
      if (r.conf && r.conf !== "unconfirmed") return;
      if (!r.phone) { skipped.push(r.name); return; }
      S.events.push(E({ key: r.key, platform: "sms", channel: "sms", type: "confirmation_sent", at: nextAt(S), conf: "sent", phone: r.phone }));
      n++;
    });
    save(S);
    return { message: n + " confirmation texts queued (dry run)" + (skipped.length ? ". No phone: " + skipped.join(", ") : "") };
  }
  function reply(phone, body) {
    var S = load(), p = normPhone(phone), res = derive(S.events);
    var mine = sortedBooked(res).filter(function (r) { return r.phone === p; })[0];
    if (!mine) return { error: "No upcoming booking for that phone number" };
    var word = (String(body).toLowerCase().match(/[a-z]+/) || [""])[0], ev;
    if (["yes", "y", "confirm", "confirmed"].indexOf(word) >= 0) ev = E({ key: mine.key, platform: "sms", channel: "sms", type: "confirmed", at: nextAt(S), conf: "yes", phone: p });
    else if (["no", "n", "cancel"].indexOf(word) >= 0) ev = E({ key: mine.key, platform: "sms", channel: "sms", type: "declined", at: nextAt(S), conf: "no", status: "cancelled", phone: p });
    else ev = E({ key: mine.key, platform: "sms", channel: "sms", type: "reply_unclear", at: nextAt(S), phone: p, review: true, reason: "unclear SMS reply: '" + body + "'" });
    S.events.push(ev); save(S);
    return { message: "Recorded '" + ev.type + "' from " + phone };
  }
  function availability(party, time) {
    var S = load(), res = derive(S.events), outage = !!S.s.outage, a = isAvailable(res, +party, time, outage);
    return { available: a.ok, reason: a.reason, alternatives: a.ok ? [] : alternatives(res, +party, time, outage) };
  }
  function book(b) {
    var S = load(), res = derive(S.events), outage = !!S.s.outage, party = +b.party, a = isAvailable(res, party, b.time, outage);
    if (!a.ok) return { booked: false, reason: a.reason, alternatives: alternatives(res, party, b.time, outage) };
    var id = "W-" + Math.random().toString(16).slice(2, 8).toUpperCase();
    var ev = validated(E({ platform: "website", channel: "web", type: "created", id: id, at: nextAt(S), name: b.name, phone: b.phone,
      email: b.email, party: party, starts: D + " " + b.time, table: a.tables[0], status: "booked", notes: b.notes || null,
      review: outage, reason: outage ? "booked during Resy outage, re-enter in Resy when it's back" : null }));
    S.events.push(ev); save(S);
    return { booked: true, confirmation: id, table: ev.table, outage: ev.review };
  }

  function handle(path, method, body) {
    var b = {};
    try { b = body ? JSON.parse(body) : {}; } catch (e) { /* ignore */ }
    if (method === "GET") {
      if (path === "/api/state") return getState();
      if (path === "/api/slots") return slotList();
      return { error: "not found" };
    }
    if (path === "/api/demo") return demoStep(b.step);
    if (path === "/api/sync") return { message: "Sync complete" };
    if (path === "/api/outage") return setOutage(b.on);
    if (path === "/api/confirm") return confirmTexts();
    if (path === "/api/reply") return reply(b.phone, b.body);
    if (path === "/api/availability") return availability(b.party, b.time);
    if (path === "/api/book") return book(b);
    return { error: "not found" };
  }

  if (typeof window !== "undefined" && window.fetch) {
    var realFetch = window.fetch.bind(window);
    window.fetch = function (url, opts) {
      var u = typeof url === "string" ? url : (url && url.url) || "";
      var path = u.split("?")[0];
      if (path.indexOf("/api/") === 0) {
        var data = handle(path, ((opts && opts.method) || "GET").toUpperCase(), opts && opts.body);
        return Promise.resolve(new Response(JSON.stringify(data), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      return realFetch(url, opts);
    };
    window.addEventListener("storage", function () { if (typeof window.load === "function") window.load(); });
  }
  if (typeof module !== "undefined" && module.exports) module.exports = { handle: handle };
})();
