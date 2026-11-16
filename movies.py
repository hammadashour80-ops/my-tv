#!/usr/bin/env python3
"""The movie card's list: IMDb's top 250, worked out from IMDb's own free datasets (datasets.imdbws.com, for personal
and non-commercial use), with each film's Arabic name and a short Arabic summary from Wikipedia (via Wikidata).
Writes data/movies.json. Run: python3 movies.py   (GitHub Actions runs it once a week)

IMDb's chart uses a weighted rating and counts only regular voters, which its datasets don't carry; the same idea here
(a feature film, at least 25,000 votes, ranked by its rating and then its votes) gives almost the same 250."""
import csv
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
csv.field_size_limit(10 ** 7)


def get(url, data=None, tries=4):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=UA)
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except Exception as e:
            if attempt == tries - 1:
                raise
            print("retry", url[:90], e)
            time.sleep(5 * (attempt + 1))


def tsv(name):
    """One of IMDb's .tsv.gz files, read row by row while it downloads."""
    req = urllib.request.Request(IMDB + name, headers=UA)
    resp = urllib.request.urlopen(req, timeout=300)
    text = io.TextIOWrapper(gzip.GzipFile(fileobj=resp), encoding="utf-8", newline="")
    return csv.DictReader(text, delimiter="\t", quoting=csv.QUOTE_NONE)


def top_movies():
    votes = {}
    for row in tsv("title.ratings.tsv.gz"):
        n = int(row["numVotes"])
        if n >= MIN_VOTES:
            votes[row["tconst"]] = (float(row["averageRating"]), n)
    print("titles with enough votes:", len(votes))
    films = []
    for row in tsv("title.basics.tsv.gz"):
        t = row["tconst"]
        if t in votes and row["titleType"] == "movie":
            rating, n = votes[t]
            films.append({"imdb": t, "title": row["primaryTitle"], "year": row["startYear"], "rating": rating, "votes": n,
                          "minutes": row["runtimeMinutes"] if row["runtimeMinutes"] != "\\N" else None,
                          "genres": [g for g in row["genres"].split(",") if g != "\\N"]})
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


def summary(lang, title):
    url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(title.replace(" ", "_"), safe="")
    d = json.loads(get(url))
    return d.get("extract") or "", (d.get("thumbnail") or {}).get("source")


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
    arabic = 0
    for f in films:
        label, ar, en = wd.get(f["imdb"], (None, None, None))
        f["title_ar"] = label
        f["about"], f["about_lang"], f["poster"] = "", None, None
        for lang, page in (("ar", ar), ("en", en)):
            if not page:
                continue
            try:
                text, poster = summary(lang, page)
            except Exception as e:
                print("!! summary", f["imdb"], lang, e)
                continue
            if text:
                f["about"], f["about_lang"], f["poster"] = short(text), lang, poster
                arabic += lang == "ar"
                break
        time.sleep(0.2)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"updated": time.strftime("%Y-%m-%d"), "films": films}, ensure_ascii=False, indent=0),
                   encoding="utf-8", newline="\n")
    print(f"films: {len(films)} | with an Arabic summary: {arabic} | first: {films[0]['title']} {films[0]['rating']}")


if __name__ == "__main__":
    main()
