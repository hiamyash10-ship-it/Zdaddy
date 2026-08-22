#!/usr/bin/env python3
"""
Flight price tracker — polls Google Flights for fixed routes/dates,
keeps a price history, and pings you on Telegram when fares drop.

Run once:      python tracker.py
Rebuild page:  python tracker.py --report-only
Test alerts:   python tracker.py --test-alert
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from fast_flights import FlightQuery, Passengers, create_query, get_flights

# ─────────────────────────── WHAT TO TRACK ───────────────────────────
# Delete or add blocks freely. Dates are YYYY-MM-DD.

ROUTES = [
    {
        "origin": "DEL",
        "destination": "BKK",          # Bangkok Suvarnabhumi
        "dates": ["2027-01-16", "2027-01-17", "2027-01-18"],
        "target": None,                # e.g. 16000 -> always alert at/below this
    },
    {
        "origin": "DEL",
        "destination": "DMK",          # Bangkok Don Mueang (AirAsia, Thai Lion, Nok)
        "dates": ["2027-01-16", "2027-01-17", "2027-01-18"],
        "target": None,
    },
    {
        "origin": "HKT",               # Phuket
        "destination": "DEL",
        "dates": ["2027-01-23", "2027-01-24", "2027-01-25"],
        "target": None,
    },
]

# ─────────────────────────── SETTINGS ───────────────────────────

CURRENCY = "INR"
SEAT = "economy"          # economy | premium-economy | business | first
ADULTS = 1
MAX_STOPS = 1             # None = allow any number of stops
HIDE_SELF_TRANSFER = True # skip risky self-transfer / separate-ticket itineraries

DROP_PCT = 3.0            # alert if the cheapest fare falls by this % vs last check
DROP_ABS = 800            # ...and by at least this many currency units
ALERT_ON_NEW_LOW = True   # always alert on an all-time low for that date
KEEP_FLIGHTS = 6          # how many itineraries per date to store in history

MIN_DELAY, MAX_DELAY = 4, 11   # polite random pause between searches (seconds)

HISTORY = Path(__file__).parent / "history.json"
DASHBOARD = Path(__file__).parent / "docs" / "index.html"

# ────────────────────────────────────────────────────────────────


def key_of(route: dict, date: str) -> str:
    return f"{route['origin']}-{route['destination']}|{date}"


def google_flights_url(origin: str, destination: str, date: str) -> str:
    q = f"Flights from {origin} to {destination} on {date} one way"
    return "https://www.google.com/travel/flights?q=" + urllib.parse.quote(q)


def fmt_money(v) -> str:
    if v is None:
        return "—"
    symbol = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "THB": "฿"}.get(CURRENCY, CURRENCY + " ")
    return f"{symbol}{v:,.0f}"


def hhmm(t) -> str:
    return f"{t[0]:02d}:{t[1]:02d}"


# ─────────────────────────── FETCHING ───────────────────────────


def search(origin: str, destination: str, date: str, attempts: int = 3) -> list[dict]:
    """Return a list of itineraries, cheapest first."""
    query = create_query(
        flights=[
            FlightQuery(
                date=date,
                from_airport=origin,
                to_airport=destination,
                max_stops=MAX_STOPS,
            )
        ],
        trip="one-way",
        seat=SEAT,
        passengers=Passengers(adults=ADULTS),
        currency=CURRENCY,
        language="en-US",
        hide_separate_and_self_transfer=HIDE_SELF_TRANSFER,
    )

    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            results = get_flights(query)
            break
        except Exception as exc:                      # network hiccup, block, parse change
            last_error = exc
            if attempt == attempts:
                print(f"    ! failed after {attempts} tries: {exc}", file=sys.stderr)
                return []
            time.sleep(5 * attempt)
    else:
        print(f"    ! {last_error}", file=sys.stderr)
        return []

    itineraries = []
    for f in results:
        if not f.flights:
            continue
        legs = f.flights
        itineraries.append(
            {
                "price": int(f.price),
                "airlines": list(f.airlines),
                "stops": len(legs) - 1,
                "depart": hhmm(legs[0].departure.time),
                "arrive": hhmm(legs[-1].arrival.time),
                "duration_min": sum(l.duration for l in legs if l.duration),
                "route": " → ".join(
                    [legs[0].from_airport.code] + [l.to_airport.code for l in legs]
                ),
            }
        )

    itineraries.sort(key=lambda x: x["price"])
    return itineraries[:KEEP_FLIGHTS]


# ─────────────────────────── ALERTS ───────────────────────────


def telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    payload = urllib.parse.urlencode(
        {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "false"}
    ).encode()
    try:
        with urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage", data=payload, timeout=20
        ) as r:
            return r.status == 200
    except Exception as exc:
        print(f"! telegram failed: {exc}", file=sys.stderr)
        return False


def send_email(subject: str, body: str) -> bool:
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    to = os.environ.get("ALERT_EMAIL")
    if not all([host, user, pw, to]):
        return False
    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL(host, int(os.environ.get("SMTP_PORT", 465))) as s:
            s.login(user, pw)
            s.send_message(msg)
        return True
    except Exception as exc:
        print(f"! email failed: {exc}", file=sys.stderr)
        return False


def notify(alerts: list[dict]) -> None:
    if not alerts:
        return

    lines = ["<b>✈️ Fare drop</b>", ""]
    plain = ["Fare drop", ""]
    for a in alerts:
        head = f"{a['origin']} → {a['destination']} · {a['date']}"
        move = (
            f"{fmt_money(a['previous'])} → <b>{fmt_money(a['price'])}</b>"
            if a["previous"]
            else f"<b>{fmt_money(a['price'])}</b>"
        )
        tag = " 🔻 all-time low" if a["new_low"] else ""
        detail = f"{', '.join(a['best']['airlines'])} · {a['best']['depart']}–{a['best']['arrive']} · {a['best']['stops']} stop(s)"
        lines += [f"{head}", f"{move}{tag}", detail, f'<a href="{a["url"]}">open in Google Flights</a>', ""]
        plain += [head, move.replace("<b>", "").replace("</b>", "") + tag.replace(" 🔻", ""), detail, a["url"], ""]

    sent = telegram("\n".join(lines))
    sent = send_email("Fare drop on your tracked routes", "\n".join(plain)) or sent
    if not sent:
        print("\n".join(plain))


# ─────────────────────────── DASHBOARD ───────────────────────────


def build_dashboard(history: dict) -> None:
    DASHBOARD.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"currency": CURRENCY, "updated": datetime.now(timezone.utc).isoformat(), "series": history},
        separators=(",", ":"),
    )
    DASHBOARD.write_text(HTML_TEMPLATE.replace("__DATA__", payload), encoding="utf-8")


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fare tracker</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--line:#262b36;--txt:#e6e9ef;--dim:#8a93a6;--up:#ff6b6b;--down:#4ade80;--acc:#7aa2ff}
*{box-sizing:border-box}
body{margin:0;padding:24px 16px 64px;background:var(--bg);color:var(--txt);font:15px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:920px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px;letter-spacing:-.01em}
.sub{color:var(--dim);font-size:13px;margin-bottom:24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin-bottom:14px}
.top{display:flex;flex-wrap:wrap;align-items:baseline;gap:10px;justify-content:space-between}
.route{font-weight:600;letter-spacing:-.01em}
.date{color:var(--dim);font-size:13px;font-weight:400;margin-left:6px}
.price{font-size:22px;font-weight:650;font-variant-numeric:tabular-nums}
.delta{font-size:13px;font-variant-numeric:tabular-nums}
.down{color:var(--down)}.up{color:var(--up)}.flat{color:var(--dim)}
.meta{color:var(--dim);font-size:13px;margin-top:6px}
svg{display:block;width:100%;height:78px;margin-top:12px;overflow:visible}
a{color:var(--acc);text-decoration:none;font-size:13px}
a:hover{text-decoration:underline}
.lo{color:var(--dim);font-size:12px;margin-top:8px;font-variant-numeric:tabular-nums}
.empty{color:var(--dim);font-size:13px;padding:8px 0}
</style></head><body><div class="wrap">
<h1>Fare tracker</h1><div class="sub" id="sub"></div><div id="out"></div>
</div><script>
const D = __DATA__;
const sym = {INR:"₹",USD:"$",EUR:"€",GBP:"£",THB:"฿"}[D.currency] || D.currency+" ";
const money = v => v==null ? "—" : sym + Math.round(v).toLocaleString("en-IN");
document.getElementById("sub").textContent =
  "Last checked " + new Date(D.updated).toLocaleString() + " · " + Object.keys(D.series).length + " searches tracked";

function spark(points){
  if(points.length < 2) return "";
  const W=880,H=78,vals=points.map(p=>p.min).filter(v=>v!=null);
  if(!vals.length) return "";
  const lo=Math.min(...vals),hi=Math.max(...vals),span=(hi-lo)||1;
  const xy=points.map((p,i)=>[i/(points.length-1)*W, H-((p.min-lo)/span)*(H-14)-7]);
  const line=xy.map((p,i)=>(i?"L":"M")+p[0].toFixed(1)+","+p[1].toFixed(1)).join(" ");
  const area=line+` L${W},${H} L0,${H} Z`;
  const last=xy[xy.length-1];
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    <defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%" stop-color="#7aa2ff" stop-opacity=".28"/>
    <stop offset="100%" stop-color="#7aa2ff" stop-opacity="0"/></linearGradient></defs>
    <path d="${area}" fill="url(#g)"/>
    <path d="${line}" fill="none" stroke="#7aa2ff" stroke-width="2" vector-effect="non-scaling-stroke"/>
    <circle cx="${last[0].toFixed(1)}" cy="${last[1].toFixed(1)}" r="3.5" fill="#7aa2ff"/></svg>`;
}

const out=document.getElementById("out");
const keys=Object.keys(D.series).sort();
if(!keys.length) out.innerHTML='<div class="empty">No data yet — run the tracker once.</div>';
for(const k of keys){
  const s=D.series[k], pts=(s.points||[]).filter(p=>p.min!=null);
  const [route,date]=k.split("|");
  const cur=pts.length?pts[pts.length-1]:null;
  const prev=pts.length>1?pts[pts.length-2]:null;
  const allLow=pts.length?Math.min(...pts.map(p=>p.min)):null;
  let delta='<span class="flat">first reading</span>';
  if(cur&&prev){
    const d=cur.min-prev.min;
    delta = d===0 ? '<span class="flat">no change</span>'
      : `<span class="${d<0?'down':'up'}">${d<0?"▼":"▲"} ${money(Math.abs(d))} (${(Math.abs(d)/prev.min*100).toFixed(1)}%)</span>`;
  }
  const b=cur&&cur.best;
  out.insertAdjacentHTML("beforeend", `<div class="card">
    <div class="top"><div class="route">${route.replace("-"," → ")}<span class="date">${date}</span></div>
    <div style="text-align:right"><div class="price">${money(cur&&cur.min)}</div><div class="delta">${delta}</div></div></div>
    ${b?`<div class="meta">${b.airlines.join(", ")} · ${b.depart}–${b.arrive} · ${b.stops} stop${b.stops===1?"":"s"} · ${b.route}</div>`:""}
    ${spark(pts)}
    <div class="lo">low ${money(allLow)} · ${pts.length} reading${pts.length===1?"":"s"} · <a href="${s.url}" target="_blank" rel="noopener">book →</a></div>
  </div>`);
}
</script></body></html>
"""


# ─────────────────────────── MAIN ───────────────────────────


def run() -> None:
    history = json.loads(HISTORY.read_text()) if HISTORY.exists() else {}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    alerts: list[dict] = []

    jobs = [(r, d) for r in ROUTES for d in r["dates"]]
    for i, (route, date) in enumerate(jobs, 1):
        k = key_of(route, date)
        print(f"[{i}/{len(jobs)}] {route['origin']}→{route['destination']} {date} …", flush=True)

        flights = search(route["origin"], route["destination"], date)
        if not flights:
            print("    no results")
        else:
            cheapest = flights[0]
            entry = history.setdefault(
                k,
                {"url": google_flights_url(route["origin"], route["destination"], date), "points": []},
            )
            entry["url"] = google_flights_url(route["origin"], route["destination"], date)
            points = entry["points"]

            prior = [p["min"] for p in points if p.get("min") is not None]
            previous = prior[-1] if prior else None
            all_time_low = min(prior) if prior else None
            price = cheapest["price"]

            points.append({"t": now, "min": price, "best": cheapest, "offers": flights})
            entry["points"] = points[-400:]     # keep it from growing forever

            new_low = ALERT_ON_NEW_LOW and all_time_low is not None and price < all_time_low
            big_drop = (
                previous is not None
                and price < previous
                and (previous - price) >= DROP_ABS
                and (previous - price) / previous * 100 >= DROP_PCT
            )
            hit_target = route.get("target") is not None and price <= route["target"]

            arrow = "" if previous is None else (" ▼" if price < previous else (" ▲" if price > previous else " ="))
            print(f"    {fmt_money(price)}{arrow}  ({len(flights)} itineraries)")

            if new_low or big_drop or hit_target:
                alerts.append(
                    {
                        "origin": route["origin"],
                        "destination": route["destination"],
                        "date": date,
                        "price": price,
                        "previous": previous,
                        "new_low": new_low,
                        "best": cheapest,
                        "url": entry["url"],
                    }
                )

        if i < len(jobs):
            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    HISTORY.write_text(json.dumps(history, indent=1), encoding="utf-8")
    build_dashboard(history)
    notify(alerts)
    print(f"\ndone · {len(alerts)} alert(s) · history: {HISTORY.name}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-only", action="store_true", help="rebuild the HTML page from saved history")
    ap.add_argument("--test-alert", action="store_true", help="send a dummy notification to check credentials")
    args = ap.parse_args()

    if args.report_only:
        build_dashboard(json.loads(HISTORY.read_text()) if HISTORY.exists() else {})
        print(f"wrote {DASHBOARD}")
    elif args.test_alert:
        ok = telegram("✅ Flight tracker is wired up correctly.") or send_email(
            "Flight tracker test", "Flight tracker is wired up correctly."
        )
        print("sent" if ok else "no notification channel configured — set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID")
    else:
        run()
