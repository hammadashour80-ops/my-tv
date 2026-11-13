#!/usr/bin/env python3
"""Gathers everything the page shows into data/tv.json: the latest news, the team's next and last game, its place in the
league table (worked out from the season's results), and the prayer times. Settings are in config.json.
Run: python3 fetch.py   (GitHub Actions runs it every hour; see .github/workflows/update.yml)"""
import datetime as dt
import json
import time
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
OUT = ROOT / "data" / "tv.json"
SPORTS = "https://www.thesportsdb.com/api/v1/json/123/"   # the free public key
TABLE_EVERY = dt.timedelta(hours=6)                        # the table needs ~40 requests; the rest is cheap
UTC = dt.timezone.utc


def get(url, raw=False):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (my-tv)"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read()
            return body if raw else json.loads(body or b"{}")
        except Exception as e:
            if attempt == 3:
                raise
            print("retry", url, e)
            time.sleep(65 if "429" in str(e) else 5)   # 429: the minute's allowance is used up


def sports(path):
    time.sleep(3)                                           # the free key allows 30 requests a minute; stay well under
    return get(SPORTS + path)


# ---- news -----------------------------------------------------------------------------------------------------------
def news():
    root = ET.fromstring(get(CFG["news_rss"], raw=True))
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        if not title:
            continue
        when = it.findtext("pubDate")
        try:
            when = parsedate_to_datetime(when).astimezone(UTC).isoformat() if when else None
        except Exception:
            when = None
        items.append({"title": title, "link": (it.findtext("link") or "").strip(), "time": when})
    items.sort(key=lambda x: x["time"] or "", reverse=True)
    return items[: CFG["news_count"]]


# ---- the team ---------------------------------------------------------------------------------------------------------
def kickoff(ev):
    ts = ev.get("strTimestamp")
    if ts:
        return dt.datetime.fromisoformat(ts.replace("Z", "")).replace(tzinfo=UTC)
    if ev.get("dateEvent"):
        return dt.datetime.fromisoformat(ev["dateEvent"] + "T" + (ev.get("strTime") or "00:00:00")[:8]).replace(tzinfo=UTC)
    return None


def game(ev):
    k = kickoff(ev)
    return {"home_id": ev.get("idHomeTeam"), "away_id": ev.get("idAwayTeam"), "home": ev.get("strHomeTeam"), "away": ev.get("strAwayTeam"),
            "home_badge": ev.get("strHomeTeamBadge"), "away_badge": ev.get("strAwayTeamBadge"),
            "home_score": ev.get("intHomeScore"), "away_score": ev.get("intAwayScore"),
            "league": ev.get("strLeague"), "venue": ev.get("strVenue"),
            "time": k.isoformat() if k else None}


def played(ev):
    return ev.get("intHomeScore") not in (None, "") and ev.get("intAwayScore") not in (None, "") \
        and (ev.get("strPostponed") or "no") != "yes"


def league_table(league, season, now):
    """Every round from 1 until the first that has not started yet; 3 points a win, 1 a draw."""
    rows = {}
    for r in range(1, 50):
        events = sports(f"eventsround.php?id={league}&r={r}&s={season}").get("events") or []
        if not events:
            break
        for ev in events:
            if not played(ev):
                continue
            hs, as_ = int(ev["intHomeScore"]), int(ev["intAwayScore"])
            for tid, name, gf, ga in ((ev["idHomeTeam"], ev["strHomeTeam"], hs, as_), (ev["idAwayTeam"], ev["strAwayTeam"], as_, hs)):
                t = rows.setdefault(tid, {"id": tid, "name": name, "played": 0, "won": 0, "drawn": 0, "lost": 0, "gf": 0, "ga": 0})
                t["played"] += 1
                t["gf"] += gf
                t["ga"] += ga
                t["won" if gf > ga else "drawn" if gf == ga else "lost"] += 1
        starts = [k for k in map(kickoff, events) if k]
        if starts and min(starts) > now:
            break
    for t in rows.values():
        t["points"] = 3 * t["won"] + t["drawn"]
        t["gd"] = t["gf"] - t["ga"]
    table = sorted(rows.values(), key=lambda t: (-t["points"], -t["gd"], -t["gf"], t["name"]))
    for i, t in enumerate(table, 1):
        t["rank"] = i
    return table


def team(now, old):
    tid, league = CFG["team_id"], CFG["league_id"]
    nxt = sports(f"eventsnext.php?id={tid}").get("events") or []
    nxt = [e for e in nxt if (kickoff(e) or now) > now - dt.timedelta(hours=2)]
    nxt.sort(key=lambda e: kickoff(e) or now)
    last = sports(f"eventslast.php?id={tid}").get("results") or []
    last.sort(key=lambda e: kickoff(e) or now, reverse=True)
    out = {"next": game(nxt[0]) if nxt else None, "last": game(last[0]) if last else None}

    old_table = (old or {}).get("table") or {}
    fresh = old_table.get("updated") and now - dt.datetime.fromisoformat(old_table["updated"]) < TABLE_EVERY
    if fresh:
        out["table"] = old_table
    else:
        info = (sports(f"lookupleague.php?id={league}").get("leagues") or [{}])[0]
        season = info.get("strCurrentSeason")
        rows = league_table(league, season, now) if season else []
        out["table"] = {"updated": now.isoformat(), "league": info.get("strLeague"), "season": season,
                        "size": len(rows), "rows": rows}
    me = next((r for r in out["table"]["rows"] if r["id"] == tid), None)
    out["standing"] = {k: me[k] for k in ("rank", "points", "played", "won", "drawn", "lost", "gd")} if me else None
    return out


# ---- prayer times -----------------------------------------------------------------------------------------------------
def prayers(now):
    """The city's days from yesterday to 3 days ahead (the page picks today by the city's own clock), from the month
    calendars that hold them."""
    want = {(now + dt.timedelta(days=d)).date() for d in range(-2, 4)}
    days = {}
    for year, month in sorted({(d.year, d.month) for d in want}):
        url = (f"https://api.aladhan.com/v1/calendarByCity/{year}/{month}?city={CFG['city']}"
               f"&country={CFG['country']}&method={CFG['prayer_method']}")
        for data in get(url)["data"]:
            day = dt.datetime.strptime(data["date"]["gregorian"]["date"], "%d-%m-%Y").date()
            if day not in want:
                continue
            t, h = data["timings"], data["date"]["hijri"]
            days[day.isoformat()] = {
                "times": {k: t[k][:5] for k in ("Fajr", "Sunrise", "Dhuhr", "Asr", "Maghrib", "Isha")},
                "hijri": {"day": h["day"], "month": h["month"]["ar"], "year": h["year"]}}
    return dict(sorted(days.items()))


# ---- all of it --------------------------------------------------------------------------------------------------------
def main():
    now = dt.datetime.now(UTC).replace(microsecond=0)
    old = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    out = {"updated": now.isoformat(), "config": {k: CFG[k] for k in ("city_ar", "timezone", "team_id", "team_ar")}}
    # each part on its own: one source down keeps the last good answer of that part
    for key, fn in (("news", news), ("team", lambda: team(now, old.get("team"))), ("prayers", lambda: prayers(now))):
        try:
            out[key] = fn()
        except Exception as e:
            print(f"!! {key}: {e}; the last good answer is kept")
            out[key] = old.get(key)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    s = (out.get("team") or {}).get("standing")
    print("news:", len(out.get("news") or []), "| next:", ((out.get("team") or {}).get("next") or {}).get("time"),
          "| place:", s and (s["rank"], s["points"]), "| prayer days:", len(out.get("prayers") or {}))


if __name__ == "__main__":
    main()
