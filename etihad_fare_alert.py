"""
Daily cheapest Etihad fare finder: LHR / MAN -> COK, with cheapest booking site.
Uses SerpApi's Google Flights API (https://serpapi.com, free tier = 250 searches/month).

Environment variables (set as secrets):
  SERPAPI_KEY   your SerpApi key
  SMTP_USER     sending email address (e.g. a Gmail address)
  SMTP_PASS     app password (Gmail: Account > Security > App passwords)
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
# Each entry: (outbound date, return date or None). Keep this list short to save API calls:
# calls per day = len(ORIGINS) * len(DATE_PAIRS) + 1
DATE_PAIRS = [
    ("2027-01-13", "2027-02-08"),
    ("2027-01-14", "2027-01-09"),
    ("2027-01-12", "2027-01-08"),
]
ETIHAD_URL = "https://www.etihad.com/en-gb"
DATA_FILE = Path("docs/data.json")
# ---------------------------------------------------------------------------

API = "https://serpapi.com/search.json"
KEY = os.environ.get("SERPAPI_KEY", "")


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


def search(origin, out_date, ret_date):
    r = requests.get(API, params=base_params(origin, out_date, ret_date), timeout=60)
    r.raise_for_status()
    data = r.json()
    gf_url = (data.get("search_metadata") or {}).get("google_flights_url")
    offers = (data.get("best_flights") or []) + (data.get("other_flights") or [])
    results = []
    for o in offers:
        if "price" not in o:
            continue
        legs = o.get("flights", [])
        # keep only itineraries actually operated/marketed by Etihad
        if legs and not all(AIRLINE in (l.get("flight_number", "") or "") for l in legs):
            continue
        results.append({
            "price": o["price"],
            "origin": origin,
            "out": out_date,
            "ret": ret_date,
            "token": o.get("booking_token"),
            "gf_url": gf_url,
            "duration": o.get("total_duration"),
            "legs": " > ".join(
                f"{l['departure_airport']['id']}-{l['arrival_airport']['id']} {l.get('flight_number','')}"
                for l in legs
            ),
        })
    return results


def cheapest_booking_site(best):
    """Return list of (site, price) sorted by price for the chosen itinerary."""
    if not best.get("token"):
        return []
    p = base_params(best["origin"], best["out"], best["ret"])
    p["booking_token"] = best["token"]
    r = requests.get(API, params=p, timeout=60)
    r.raise_for_status()
    sites = []
    for opt in r.json().get("booking_options", []):
        t = opt.get("together") or {}
        if "price" in t:
            sites.append((t.get("book_with", "Unknown"), t["price"]))
    return sorted(sites, key=lambda s: s[1])


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
    # one entry per day (latest wins)
    history = list({h["date"]: h for h in history}.values())[-90:]
    data = {
        "updated": now.strftime("%Y-%m-%d %H:%M %Z"),
        "currency": CURRENCY,
        "etihad_url": ETIHAD_URL,
        "best": {k: best[k] for k in ("price", "origin", "out", "ret", "legs", "gf_url")},
        "sites": [{"name": n, "price": pr} for n, pr in sites[:8]],
        "top": [{k: r[k] for k in ("price", "origin", "out", "ret")} for r in all_results[:8]],
        "history": history,
    }
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, indent=2))


def main():
    # Scheduler runs in UTC; only proceed at 06:xx London time (handles BST/GMT).
    if not os.environ.get("FORCE_RUN"):
        if datetime.now(ZoneInfo("Europe/London")).hour != 6:
            print("Not 6am in London, skipping.")
            return

    all_results = []
    for origin in ORIGINS:
        for out_date, ret_date in DATE_PAIRS:
            try:
                all_results += search(origin, out_date, ret_date)
            except Exception as e:
                print(f"Search failed {origin} {out_date}: {e}", file=sys.stderr)

    if not all_results:
        send_email("Etihad fare check: no fares found",
                   "No Etihad fares were returned today. The API may have failed or dates may be unavailable.")
        return

    all_results.sort(key=lambda r: r["price"])
    best = all_results[0]
    sites = cheapest_booking_site(best)

    lines = [
        f"Cheapest Etihad fare to Cochin today: {CURRENCY} {best['price']}",
        f"From: {best['origin']}   Out: {best['out']}   Return: {best['ret'] or 'one-way'}",
        f"Route: {best['legs']}",
        "",
    ]
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

    lines.append("\nTop 5 overall:")
    for r in all_results[:5]:
        lines.append(f"  {CURRENCY} {r['price']}  {r['origin']}  {r['out']} -> {r['ret'] or 'one-way'}")

    save_data(best, sites, all_results)
    send_email(f"Etihad to Cochin: {CURRENCY} {best['price']} from {best['origin']}", "\n".join(lines))
    print("Email sent.")


if __name__ == "__main__":
    main()
