#!/usr/bin/env python3
"""The movie card's list: IMDb's top 250, worked out from IMDb's own free datasets (datasets.imdbws.com, for personal
and non-commercial use), with each film's Arabic name and a short Arabic summary from Wikipedia (via Wikidata).
Writes data/movies.json. Run: python3 movies.py   (GitHub Actions runs it once a week)

IMDb's chart uses a weighted rating and counts only regular voters, which its datasets don't carry; the same idea here
(a feature film, at least 25,000 votes, ranked by its rating and then its votes) gives almost the same 250."""
import gzip
import io
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "movies.json"
IMDB = "https://datasets.imdbws.com/"
MIN_VOTES = 25000
TOP = 250
UA = {"User-Agent": "my-tv/1.0 (a personal TV page; weekly list of 250 films)"}


def get(url, data=None, tries=4):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=UA)
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except Exception as e:
            if attempt == tries - 1:
                raise
            wait = 5 * (attempt + 1)
            if getattr(e, "code", None) == 429:                   # too many requests: wait as long as asked
                wait = max(wait, int((e.headers.get("Retry-After") or "30").strip() or 30) if e.headers else 30)
            print("retry", url[:60], e, f"(in {wait} s)", flush=True)
            time.sleep(wait)


def tsv(name):
    """One of IMDb's .tsv.gz files, downloaded whole (one request), then read line by line as lists of fields."""
    t = time.time()
    raw = get(IMDB + name)
    print(f"{name}: {len(raw) // 1_000_000} MB in {time.time() - t:.0f} s", flush=True)
    lines = io.TextIOWrapper(gzip.GzipFile(fileobj=io.BytesIO(raw)), encoding="utf-8", newline="\n")
    next(lines)                                                 # the header
    for line in lines:
        yield line.rstrip("\n").split("\t")


def top_movies():
    votes = {}
    for tconst, rating, n in tsv("title.ratings.tsv.gz"):       # tconst, averageRating, numVotes
        if int(n) >= MIN_VOTES:
            votes[tconst] = (float(rating), int(n))
    print("titles with enough votes:", len(votes), flush=True)
    films = []
    # tconst, titleType, primaryTitle, originalTitle, isAdult, startYear, endYear, runtimeMinutes, genres
    for row in tsv("title.basics.tsv.gz"):
        if row[0] in votes and row[1] == "movie":
            rating, n = votes[row[0]]
            films.append({"imdb": row[0], "title": row[2], "year": row[5], "rating": rating, "votes": n,
                          "minutes": int(row[7]) if row[7].isdigit() else None,
                          "genres": [g for g in row[8].split(",") if g != "\\N"]})
    films.sort(key=lambda f: (-f["rating"], -f["votes"]))
    films = films[:TOP]
    for i, f in enumerate(films, 1):
        f["rank"] = i
    return films


def wikidata(ids):
    """IMDb id -> (Arabic name, Arabic Wikipedia title, English Wikipedia title)."""
    values = " ".join(f'"{i}"' for i in ids)
    q = f"""SELECT ?imdb ?label ?ar ?en WHERE {{
      VALUES ?imdb {{ {values} }}
      ?film wdt:P345 ?imdb .
      OPTIONAL {{ ?film rdfs:label ?label FILTER(lang(?label) = "ar") }}
      OPTIONAL {{ ?a schema:about ?film ; schema:isPartOf <https://ar.wikipedia.org/> ; schema:name ?ar }}
      OPTIONAL {{ ?e schema:about ?film ; schema:isPartOf <https://en.wikipedia.org/> ; schema:name ?en }}
    }}"""
    body = urllib.parse.urlencode({"query": q, "format": "json"}).encode()
    rows = json.loads(get("https://query.wikidata.org/sparql", data=body))["results"]["bindings"]
    out = {}
    for r in rows:
        v = lambda k: r[k]["value"] if k in r else None
        out.setdefault(v("imdb"), (v("label"), v("ar"), v("en")))
    return out


def summaries(lang, titles):
    """Wikipedia title -> (the article's opening as plain text, its main picture), 20 articles a request."""
    out = {}
    titles = list(dict.fromkeys(titles))
    for i in range(0, len(titles), 20):
        q = urllib.parse.urlencode({"action": "query", "format": "json", "formatversion": 2, "redirects": 1,
                                    "prop": "extracts|pageimages", "exintro": 1, "explaintext": 1, "exlimit": 20,
                                    "piprop": "thumbnail", "pithumbsize": 300, "pilimit": 20,
                                    "titles": "|".join(titles[i:i + 20])})
        try:
            d = json.loads(get(f"https://{lang}.wikipedia.org/w/api.php?{q}"))["query"]
        except Exception as e:                                  # these 20 films go without a summary this week
            print("!! summaries", lang, i, e, flush=True)
            continue
        back = {}                                               # the title the API answers with -> the one asked
        for k in ("normalized", "redirects"):
            for m in d.get(k, []):
                back[m["to"]] = back.get(m["from"], m["from"])
        for page in d.get("pages", []):
            out[back.get(page["title"], page["title"])] = (page.get("extract") or "",
                                                           (page.get("thumbnail") or {}).get("source"))
        time.sleep(2)
    print(f"{lang} summaries: {sum(1 for v in out.values() if v[0])} of {len(titles)}", flush=True)
    return out


def short(text, limit=320):
    """The first sentences, up to about `limit` characters: a simple explanation, not the whole article."""
    text = re.sub(r"\s*\([^()]*\)", "", text).strip()          # (years, other names) make it heavier to read
    parts = re.split(r"(?<=[.!؟?])\s+", text)
    out = ""
    for p in parts:
        if out and len(out) + len(p) > limit:
            break
        out = (out + " " + p).strip()
    return out[: limit + 80]


def main():
    films = top_movies()
    if len(films) < TOP:
        sys.exit(f"!! only {len(films)} films; the list on GitHub is kept")
    wd = {}
    for i in range(0, len(films), 50):
        wd.update(wikidata([f["imdb"] for f in films[i:i + 50]]))
        time.sleep(1)
    texts = {lang: summaries(lang, [w[k] for w in wd.values() if w[k]]) for lang, k in (("ar", 1), ("en", 2))}
    arabic = 0
    for f in films:
        label, ar, en = wd.get(f["imdb"], (None, None, None))
        f["title_ar"] = label
        f["about"], f["about_lang"], f["poster"] = "", None, None
        for lang, page in (("ar", ar), ("en", en)):
            text, poster = texts[lang].get(page, ("", None)) if page else ("", None)
            if text:
                f["about"], f["about_lang"], f["poster"] = short(text), lang, poster
                arabic += lang == "ar"
                break
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"updated": time.strftime("%Y-%m-%d"), "films": films}, ensure_ascii=False, indent=0),
                   encoding="utf-8", newline="\n")
    print(f"films: {len(films)} | with an Arabic summary: {arabic} | first: {films[0]['title']} {films[0]['rating']}")


if __name__ == "__main__":
    main()
