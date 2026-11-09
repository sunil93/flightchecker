"""
Daily cheapest Etihad fare finder: LHR / MAN -> COK, with flight times and cheapest booking site.
Uses SerpApi's Google Flights API (https://serpapi.com, free tier = 250 searches/month).

Environment variables (set as secrets):
  SERPAPI_KEY   your SerpApi key
  SMTP_USER     sending email address (e.g. a Gmail address)
  SMTP_PASS     Gmail app password (NOT your normal password)
  MAIL_TO       where to send the alert
Optional:
  SMTP_HOST (default smtp.gmail.com), SMTP_PORT (default 465)
  PAGE_URL      your dashboard URL (GitHub Pages) to include in the email
  FORCE_RUN=1   skip the "is it 6am in London?" check (for testing)
"""
import json
import os
import smtplib
import sys
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ----------------------------- SETTINGS -----------------------------------
ORIGINS = ["LHR", "MAN"]
DESTINATION = "COK"
AIRLINE = "EY"                    # Etihad
CURRENCY = "GBP"
ROUND_TRIP = True                 # False = one-way
# Each entry: (outbound date, return date or None).
# API calls per day = len(ORIGINS) * len(DATE_PAIRS) + 2 (round trip) or + 1 (one-way)
DATE_PAIRS = [
     ("2027-01-13", "2027-02-08"),
    ("2027-01-14", "2027-02-09"),
    ("2027-01-12", "2027-02-08"),
]
MAX_TOTAL_HOURS = 15              # skip any flight whose total journey time (incl. layovers) is longer; None = no limit
ETIHAD_URL = "https://www.etihad.com/en-gb"
DATA_FILE = Path("docs/data.json")
SENT_FILE = Path("docs/last_sent.txt")
# ---------------------------------------------------------------------------

API = "https://serpapi.com/search.json"
KEY = os.environ.get("SERPAPI_KEY", "")


def fmt_dt(s):
    """'2026-12-18 09:55' -> 'Fri 18 Dec 2026, 09:55' (local time at that airport)."""
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M").strftime("%a %d %b %Y, %H:%M")
    except Exception:
        return s or "?"


def fmt_dur(mins):
    if not mins:
        return ""
    return f"{mins // 60}h {mins % 60:02d}m"


def parse_itinerary(o):
    legs = []
    for l in o.get("flights", []):
        dep, arr = l.get("departure_airport", {}), l.get("arrival_airport", {})
        legs.append({
            "airline": l.get("airline", ""),
            "flight": l.get("flight_number", ""),
            "from": dep.get("id", ""),
            "from_name": dep.get("name", ""),
            "dep": fmt_dt(dep.get("time", "")),
            "to": arr.get("id", ""),
            "to_name": arr.get("name", ""),
            "arr": fmt_dt(arr.get("time", "")),
            "duration": fmt_dur(l.get("duration")),
        })
    layovers = [{"airport": v.get("name", ""), "duration": fmt_dur(v.get("duration"))}
                for v in o.get("layovers", [])]
    return {"legs": legs, "layovers": layovers, "total_duration": fmt_dur(o.get("total_duration"))}


def base_params(origin, out_date, ret_date):
    p = {
        "engine": "google_flights",
        "departure_id": origin,
        "arrival_id": DESTINATION,
        "outbound_date": out_date,
        "currency": CURRENCY,
        "hl": "en",
        "gl": "uk",
        "include_airlines": AIRLINE,
        "api_key": KEY,
    }
    if ROUND_TRIP and ret_date:
        p["type"] = "1"
        p["return_date"] = ret_date
    else:
        p["type"] = "2"
    return p


def get(params):
    r = requests.get(API, params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def within_time_limit(o):
    """True if the journey (including layovers) is within MAX_TOTAL_HOURS."""
    mins = o.get("total_duration")
    if MAX_TOTAL_HOURS is None or not mins:
        return True
    return mins <= MAX_TOTAL_HOURS * 60


def etihad_only(o):
    legs = o.get("flights", [])
    return bool(legs) and all(AIRLINE in (l.get("flight_number", "") or "") for l in legs)


def search(origin, out_date, ret_date):
    data = get(base_params(origin, out_date, ret_date))
    gf_url = (data.get("search_metadata") or {}).get("google_flights_url")
    offers = (data.get("best_flights") or []) + (data.get("other_flights") or [])
    results = []
    for o in offers:
        if "price" not in o or not etihad_only(o) or not within_time_limit(o):
            continue
        results.append({
            "price": o["price"],
            "origin": origin,
            "out": out_date,
            "ret": ret_date if ROUND_TRIP else None,
            "token": o.get("booking_token"),          # one-way: present now
            "dep_token": o.get("departure_token"),    # round trip: needed to get return flights
            "gf_url": gf_url,
            "outbound": parse_itinerary(o),
            "inbound": None,
        })
    return results


def add_return_flight(best):
    """Round trip: fetch return options for the chosen outbound and take the cheapest."""
    if not best.get("dep_token"):
        return
    p = base_params(best["origin"], best["out"], best["ret"])
    p["departure_token"] = best["dep_token"]
    data = get(p)
    offers = (data.get("best_flights") or []) + (data.get("other_flights") or [])
    offers = [o for o in offers if "price" in o and etihad_only(o) and within_time_limit(o)]
    if not offers:
        return
    cheapest = min(offers, key=lambda o: o["price"])
    best["price"] = cheapest["price"]                 # exact round-trip total
    best["token"] = cheapest.get("booking_token")
    best["inbound"] = parse_itinerary(cheapest)


def cheapest_booking_site(best):
    if not best.get("token"):
        return []
    p = base_params(best["origin"], best["out"], best["ret"])
    if best.get("dep_token"):
        p["departure_token"] = best["dep_token"]
    p["booking_token"] = best["token"]
    sites = []
    for opt in get(p).get("booking_options", []):
        t = opt.get("together") or {}
        if "price" in t:
            sites.append((t.get("book_with", "Unknown"), t["price"]))
    return sorted(sites, key=lambda s: s[1])


def itinerary_text(title, itin):
    if not itin:
        return []
    out = [f"{title} (total {itin['total_duration']}):"]
    for i, l in enumerate(itin["legs"]):
        out.append(f"  {l['flight']}  {l['from']} {l['dep']}  ->  {l['to']} {l['arr']}  ({l['duration']})")
        if i < len(itin["layovers"]):
            lo = itin["layovers"][i]
            out.append(f"     layover: {lo['airport']} {lo['duration']}")
    out.append("")
    return out


def short_dt(s):
    """'Fri 18 Dec 2026, 21:40' -> 'Fri 18 Dec 21:40'"""
    try:
        w = s.replace(",", "").split()
        return f"{w[0]} {w[1]} {w[2]} {w[4]}"
    except Exception:
        return s


def summary_line(itin):
    legs = itin["legs"]
    stops = len(legs) - 1
    via = ", ".join(l["airport"] for l in itin["layovers"]) if itin["layovers"] else ""
    stop_txt = "non-stop" if stops == 0 else f"{stops} stop" + ("s" if stops > 1 else "") + (f" ({via})" if via else "")
    flights = ", ".join(l["flight"].replace(" ", "") for l in legs)
    return (f"{short_dt(legs[0]['dep'])} -> {short_dt(legs[-1]['arr'])}  |  "
            f"{itin['total_duration']}  |  {stop_txt}  |  {flights}")


def group_all(results):
    """Group every Etihad option by (origin, outbound date, return date), cheapest first."""
    groups = {}
    for r in results:
        groups.setdefault((r["origin"], r["out"], r["ret"]), []).append(r)
    out = []
    for (origin, out_d, ret_d), items in groups.items():
        items.sort(key=lambda r: r["price"])
        out.append({
            "origin": origin, "out": out_d, "ret": ret_d,
            "flights": [{"price": r["price"], "outbound": r["outbound"]} for r in items],
        })
    out.sort(key=lambda g: (g["origin"], g["out"]))
    return out


def send_email(subject, body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.environ["MAIL_TO"]
    msg.set_content(body)
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port) as s:
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)


def save_data(best, sites, all_results):
    """Write docs/data.json for the dashboard (keeps 90 days of history)."""
    now = datetime.now(ZoneInfo("Europe/London"))
    history = []
    if DATA_FILE.exists():
        try:
            history = json.loads(DATA_FILE.read_text()).get("history", [])
        except Exception:
            history = []
    history.append({"date": now.strftime("%Y-%m-%d"), "price": best["price"], "origin": best["origin"]})
    history = list({h["date"]: h for h in history}.values())[-90:]
    data = {
        "updated": now.strftime("%Y-%m-%d %H:%M %Z"),
        "currency": CURRENCY,
        "etihad_url": ETIHAD_URL,
        "best": {k: best.get(k) for k in ("price", "origin", "out", "ret", "gf_url", "outbound", "inbound")},
        "sites": [{"name": n, "price": pr} for n, pr in sites[:8]],
        "top": [{k: r[k] for k in ("price", "origin", "out", "ret")} for r in all_results[:8]],
        "all": group_all(all_results),
        "history": history,
    }
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, indent=2))


def mark_sent(now):
    SENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    SENT_FILE.write_text(now.strftime("%Y-%m-%d"))


def main():
    # Two cron triggers run in UTC (one fits BST, one fits GMT). GitHub can start them late,
    # so: send once per day, as soon as it is 6am or later in London.
    london_now = datetime.now(ZoneInfo("Europe/London"))
    if not os.environ.get("FORCE_RUN"):
        if london_now.hour < 6:
            print("Before 6am in London, skipping.")
            return
        if SENT_FILE.exists() and SENT_FILE.read_text().strip() == london_now.strftime("%Y-%m-%d"):
            print("Already sent today, skipping.")
            return

    all_results = []
    for origin in ORIGINS:
        for out_date, ret_date in DATE_PAIRS:
            if ROUND_TRIP and ret_date and ret_date <= out_date:
                print(f"Skipping invalid pair {out_date} / {ret_date}: return is not after outbound.", file=sys.stderr)
                continue
            try:
                all_results += search(origin, out_date, ret_date)
            except Exception as e:
                print(f"Search failed {origin} {out_date}: {e}", file=sys.stderr)

    if not all_results:
        send_email("Etihad fare check: no fares found",
                   "No Etihad fares were returned today. The API may have failed or dates may be unavailable.")
        mark_sent(london_now)
        return

    all_results.sort(key=lambda r: r["price"])
    best = all_results[0]

    if ROUND_TRIP:
        try:
            add_return_flight(best)
        except Exception as e:
            print(f"Return-flight lookup failed: {e}", file=sys.stderr)
    try:
        sites = cheapest_booking_site(best)
    except Exception as e:
        print(f"Booking-options lookup failed: {e}", file=sys.stderr)
        sites = []

    lines = [
        f"Cheapest Etihad fare to Cochin today: {CURRENCY} {best['price']}",
        f"From {best['origin']}   |   Out {best['out']}   |   Return {best['ret'] or 'one-way'}",
        "",
    ]
    lines += itinerary_text("OUTBOUND", best["outbound"])
    lines += itinerary_text("RETURN", best["inbound"])

    if sites:
        lines.append("Where to book (cheapest first):")
        lines += [f"  {name}: {CURRENCY} {price}" for name, price in sites[:5]]
        lines.append(f"\nCheapest site: {sites[0][0]} at {CURRENCY} {sites[0][1]}")
    else:
        lines.append("Booking-site comparison unavailable for this fare.")

    page = os.environ.get("PAGE_URL", "")
    lines.append("\nBook / view:")
    lines.append(f"  Etihad site: {ETIHAD_URL}")
    if best.get("gf_url"):
        lines.append(f"  This fare on Google Flights: {best['gf_url']}")
    if page:
        lines.append(f"  Dashboard: {page}")

    lines.append("\n" + "=" * 50)
    lines.append("ALL ETIHAD FLIGHTS ON YOUR DATES (cheapest first)")
    if MAX_TOTAL_HOURS:
        lines.append(f"Only flights with a total journey time of {MAX_TOTAL_HOURS}h or less are shown.")
    if ROUND_TRIP:
        lines.append("Price = round-trip fare when choosing that outbound flight.")
    for g in group_all(all_results):
        lines.append(f"\n{g['origin']} -> {DESTINATION}   Out {g['out']}   Return {g['ret'] or 'one-way'}")
        for f in g["flights"]:
            lines.append(f"  {CURRENCY} {f['price']}   {summary_line(f['outbound'])}")

    send_email(f"Etihad to Cochin: {CURRENCY} {best['price']} from {best['origin']}", "\n".join(lines))
    print("Email sent.")
    save_data(best, sites, all_results)
    mark_sent(london_now)


if __name__ == "__main__":
    main()
