#!/usr/bin/env python3
"""Download French open data and write the small curated samples used by customers.py.

Run once (network); the outputs in scripts/pii-score/data/ are committed so dataset
generation stays offline and reproducible. Raw downloads are cached in
.pii-score-out/opendata/. Sources and licences: scripts/pii-score/data/SOURCES.md.
Stdlib only.
"""
import csv
import gzip
import io
import random
import re
import sys
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
CACHE = REPO / ".pii-score-out" / "opendata"
DATA = HERE / "data"

FIRSTNAMES_URL = "https://www.insee.fr/fr/statistiques/fichier/8595130/prenoms-2025-nat_csv.zip"
LASTNAMES_URL = ("https://static.data.gouv.fr/resources/liste-de-prenoms-et-patronymes/"
                 "20181014-162921/patronymes.csv")
BAN_URL = "https://adresse.data.gouv.fr/data/ban/adresses/latest/csv/adresses-{dep}.csv.gz"
DEPARTEMENTS = ["75", "69", "13", "33", "59", "44", "31", "67"]

BIRTH_YEARS = range(1955, 2005)  # adult customers
TOP_FIRSTNAMES_PER_SEX = 250
TOP_LASTNAMES = 800
ADDRESSES_PER_DEPARTEMENT = 40
ARRONDISSEMENT = re.compile(r"\s+\d+(?:er|e)\s+Arrondissement$")


def fetch(url, name):
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / name
    if not path.exists():
        print(f"downloading {url}", file=sys.stderr)
        with urllib.request.urlopen(url, timeout=300) as r:
            path.write_bytes(r.read())
    return path


def firstnames():
    with zipfile.ZipFile(fetch(FIRSTNAMES_URL, "prenoms-nat.zip")) as z:
        member = next(n for n in z.namelist() if n.endswith(".csv"))
        rows = csv.DictReader(io.TextIOWrapper(z.open(member), encoding="utf-8"), delimiter=";")
        counts = Counter()
        for r in rows:
            if r["prenom"].startswith("_") or not r["periode"].isdigit():
                continue
            if int(r["periode"]) in BIRTH_YEARS:
                counts[(r["prenom"], r["sexe"])] += int(r["valeur"])
    out = []
    for sexe in ("1", "2"):
        top = sorted(((n, c) for (n, s), c in counts.items() if s == sexe), key=lambda x: -x[1])
        out += [(n.title(), "M" if sexe == "1" else "F", c) for n, c in top[:TOP_FIRSTNAMES_PER_SEX]]
    return out


def lastnames():
    with open(fetch(LASTNAMES_URL, "patronymes.csv"), encoding="utf-8") as f:
        rows = [(r["patronyme"], int(r["count"])) for r in csv.DictReader(f)
                if re.fullmatch(r"[A-Z][A-Z' -]*[A-Z]", r["patronyme"])]
    rows.sort(key=lambda x: -x[1])
    return [(n.title(), c) for n, c in rows[:TOP_LASTNAMES]]


def addresses(rng):
    out = []
    for dep in DEPARTEMENTS:
        path = fetch(BAN_URL.format(dep=dep), f"adresses-{dep}.csv.gz")
        with gzip.open(path, "rt", encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f, delimiter=";") if r["numero"] and r["nom_voie"]]
        for r in rng.sample(rows, ADDRESSES_PER_DEPARTEMENT):
            street = f"{r['numero']}{(' ' + r['rep']) if r['rep'] else ''} {r['nom_voie']}"
            out.append((street, r["code_postal"], ARRONDISSEMENT.sub("", r["nom_commune"])))
    return out


def write_csv(name, header, rows):
    DATA.mkdir(exist_ok=True)
    with open(DATA / name, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"wrote {DATA / name} ({len(rows)} rows)", file=sys.stderr)


def main():
    rng = random.Random(1789)
    write_csv("fr_firstnames.csv", ["firstname", "sex", "count"], firstnames())
    write_csv("fr_lastnames.csv", ["lastname", "count"], lastnames())
    write_csv("fr_addresses.csv", ["address", "zipcode", "city"], addresses(rng))


if __name__ == "__main__":
    main()
