#!/usr/bin/env python3
"""Fictional customer table for the PII exports, built from French open data.

  customers.py build --db pii_samples.db [--rows 50] [--seed 42]
  customers.py truth --db pii_samples.db --dir EXPORTS_DIR

`build` writes a `customers` table (one row per customer, one column per field): 70% French
rows (INSEE names, Base Adresse Nationale addresses), 10% each Spanish, Italian, English.
`truth` locates every cell of export.csv / export.md / export.json (rendered by export.sh)
and writes truth.json: the character span of each PII cell with its entity type, plus the
spans of the safe cells. Positions come from the row/column structure, so values may repeat.
Stdlib only.
"""
import argparse
import csv
import json
import random
import sqlite3
import string
import unicodedata
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"

COLUMNS = ["id", "customer_ref", "firstname", "lastname", "customer_email", "phone_number",
           "address", "zipcode", "city", "country", "iban", "last_login_ip", "birth_date",
           "created_at", "status", "plan"]
# Entity each column holds. ZIP_CODE and DATE_TIME are PII the gateway doesn't mask by design.
COLUMN_ENTITY = {
    "firstname": "PERSON", "lastname": "PERSON", "customer_email": "EMAIL_ADDRESS",
    "phone_number": "PHONE_NUMBER", "address": "LOCATION", "zipcode": "ZIP_CODE",
    "city": "LOCATION", "country": "LOCATION", "iban": "IBAN_CODE",
    "last_login_ip": "IP_ADDRESS", "birth_date": "DATE_TIME",
}
SAFE_COLUMNS = ["customer_ref", "created_at", "status", "plan"]
LANG_MIX = {"fr": 0.7, "es": 0.1, "it": 0.1, "en": 0.1}

ARCEP_FICTION = ["01 99 00", "02 61 91", "03 53 01", "04 65 71", "05 36 49", "06 39 98"]
FR_EMAIL_DOMAINS = ["atelier-numerique.fr", "transports-lumiere.fr", "banque-horizon.fr",
                    "studio-azur.fr", "logistique-rhone.fr", "armor-conseil.fr", "courriel-demo.fr"]


def read_csv(name):
    with open(DATA / name, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def ascii_slug(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return "".join(c for c in s.lower() if c.isalnum() or c == "-")


def iban_check(country, bban):
    digits = "".join(str(int(c, 36)) for c in bban + country + "00")
    return f"{98 - int(digits) % 97:02d}"


def make_iban(rng, country):
    d = lambda n: "".join(rng.choice(string.digits) for _ in range(n))  # noqa: E731
    if country == "FR":
        bank, branch, account = d(5), d(5), d(11)
        rib = 97 - (89 * int(bank) + 15 * int(branch) + 3 * int(account)) % 97
        bban = f"{bank}{branch}{account}{rib:02d}"
    elif country == "ES":
        bban = d(20)
    elif country == "IT":
        bban = rng.choice(string.ascii_uppercase) + d(10) + d(12)
    else:  # GB
        bban = "".join(rng.choice(string.ascii_uppercase) for _ in range(4)) + d(14)
    iban = country + iban_check(country, bban) + bban
    return " ".join(iban[i:i + 4] for i in range(0, len(iban), 4)) if rng.random() < 0.5 else iban


def make_phone(rng, lang, prefix):
    if lang == "fr":
        national = ARCEP_FICTION[rng.randrange(len(ARCEP_FICTION))].replace(" ", "") + \
            "".join(rng.choice(string.digits) for _ in range(4))
        pairs = [national[i:i + 2] for i in range(0, 10, 2)]
        return rng.choice([
            "+33 " + national[1] + " " + " ".join(pairs[1:]),   # +33 6 39 98 12 34
            " ".join(pairs),                                      # 06 39 98 12 34
            national,                                             # 0639981234
            ".".join(pairs),                                      # 06.39.98.12.34
        ])
    if lang == "en":  # Ofcom drama range
        return f"+44 7700 900{rng.randrange(1000):03d}"
    first = "6" if lang == "es" else "3"
    n = first + "".join(rng.choice(string.digits) for _ in range(8 if lang == "es" else 9))
    groups = [n[:3], n[3:6], n[6:]]
    return f"{prefix} " + " ".join(groups)


def make_ip(rng):
    if rng.random() < 0.7:  # RFC 5737 documentation ranges
        return f"{rng.choice(['192.0.2', '198.51.100', '203.0.113'])}.{rng.randrange(1, 255)}"
    return f"2001:db8:{rng.randrange(0x10000):x}::{rng.randrange(1, 0x10000):x}"  # RFC 3849


def casing_plan(rng, rows):
    """Exact proportions instead of per-row chance: 30% of first names start lowercase;
    last names are split evenly between UPPER, lower and Capitalized."""
    first_lower = [i < round(rows * 0.3) for i in range(rows)]
    last_style = [("upper", "lower", "title")[i % 3] for i in range(rows)]
    rng.shuffle(first_lower)
    rng.shuffle(last_style)
    return first_lower, last_style


def cased_firstname(name, lower_first):
    return name[0].lower() + name[1:] if lower_first else name


def cased_lastname(name, style):
    return {"upper": name.upper(), "lower": name.lower(), "title": name}[style]


def build(db, rows, seed):
    rng = random.Random(seed)
    fr_first = read_csv("fr_firstnames.csv")
    fr_last = read_csv("fr_lastnames.csv")
    fr_addr = read_csv("fr_addresses.csv")
    foreign = json.loads((DATA / "foreign.json").read_text(encoding="utf-8"))

    langs = [lang for lang, share in LANG_MIX.items() for _ in range(round(rows * share))]
    rng.shuffle(langs)
    first_lower, last_style = casing_plan(rng, len(langs))
    records = []
    for i, lang in enumerate(langs, 1):
        if lang == "fr":
            first = rng.choices(fr_first, weights=[int(r["count"]) for r in fr_first])[0]["firstname"]
            last = rng.choices(fr_last, weights=[int(r["count"]) for r in fr_last])[0]["lastname"]
            a = rng.choice(fr_addr)
            address, zipcode, city, country = a["address"], a["zipcode"], a["city"], "France"
            domains, prefix, iban_country = FR_EMAIL_DOMAINS, "+33", "FR"
        else:
            f = foreign[lang]
            first = rng.choice(f["firstnames"])[0]
            last = rng.choice(f["lastnames"])
            address, zipcode, city = rng.choice(f["addresses"])
            country, domains, prefix = f["country"], f["email_domains"], f["phone_prefix"]
            iban_country = {"es": "ES", "it": "IT", "en": "GB"}[lang]
        local = rng.choice([f"{ascii_slug(first)}.{ascii_slug(last)}",
                            f"{ascii_slug(first)[0]}.{ascii_slug(last)}",
                            f"{ascii_slug(first)}{ascii_slug(last)}{rng.randrange(10, 99)}"])
        birth = date(1955, 1, 1) + timedelta(days=rng.randrange(365 * 50))
        created = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=rng.randrange(86400 * 990))
        records.append({
            "id": i,
            "customer_ref": str(uuid.UUID(int=rng.getrandbits(128), version=4)),
            "firstname": cased_firstname(first, first_lower[i - 1]),
            "lastname": cased_lastname(last, last_style[i - 1]),
            "customer_email": f"{local}@{rng.choice(domains)}",
            "phone_number": make_phone(rng, lang, prefix),
            "address": address, "zipcode": zipcode, "city": city, "country": country,
            "iban": make_iban(rng, iban_country),
            "last_login_ip": make_ip(rng),
            "birth_date": birth.isoformat(),
            "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": rng.choices(["active", "inactive", "suspended"], weights=[8, 2, 1])[0],
            "plan": rng.choice(["free", "pro", "enterprise"]),
        })

    Path(db).unlink(missing_ok=True)
    con = sqlite3.connect(db)
    con.execute(f"CREATE TABLE customers ({', '.join(c + (' INTEGER PRIMARY KEY' if c == 'id' else ' TEXT') for c in COLUMNS)})")
    con.executemany(f"INSERT INTO customers VALUES ({', '.join('?' * len(COLUMNS))})",
                    [[r[c] for c in COLUMNS] for r in records])
    con.commit()
    con.close()


def locate_row_cells(text, start, end, record, value_of):
    """Find each column's rendered value in order within text[start:end]."""
    cells, pos = [], start
    for col in COLUMNS[1:]:  # id is a plain integer, never PII
        needle = value_of(col, record[col])
        idx = text.find(needle, pos, end)
        if idx == -1:
            raise SystemExit(f"cell {col}={record[col]!r} of row {record['id']} not found")
        cells.append((col, idx, idx + len(needle), record[col]))
        pos = idx + len(needle)
    return cells


def truth(db, out_dir):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    records = [dict(r) for r in con.execute("SELECT * FROM customers ORDER BY id")]
    entries = []
    for fmt, header_lines in (("csv", 1), ("md", 2), ("json", None)):
        text = (Path(out_dir) / f"export.{fmt}").read_text(encoding="utf-8")
        cells = []
        if header_lines is not None:  # one row per line
            lines = text.splitlines(keepends=True)
            offsets = [sum(len(l) for l in lines[:i]) for i in range(len(lines))]
            for i, rec in enumerate(records):
                n = header_lines + i
                cells += [(rec["id"], *c) for c in locate_row_cells(
                    text, offsets[n], offsets[n] + len(lines[n]), rec, lambda col, v: str(v))]
        else:  # minified JSON: find each object, then '"column":' before each value
            pos = 0
            for rec in records:
                obj = text.find(f'{{"id":{rec["id"]},', pos)
                end = text.find("}", obj) + 1
                for col in COLUMNS[1:]:
                    key = text.find(f'"{col}":', obj, end) + len(col) + 3
                    value = json.dumps(rec[col], ensure_ascii=False)[1:-1]
                    idx = text.find(value, key, end)
                    if idx != key + 1:
                        raise SystemExit(f"cell {col} of row {rec['id']} not found in JSON")
                    cells.append((rec["id"], col, idx, idx + len(value), rec[col]))
                pos = end
        for row, col, s, e, value in cells:
            assert text[s:e] == (value if fmt != "json" else json.dumps(value, ensure_ascii=False)[1:-1])
        entries.append({
            "file": f"export.{fmt}", "lang": "exports",
            "spans": [{"start": s, "end": e, "entity": COLUMN_ENTITY[c], "value": v, "column": c, "row": r}
                      for r, c, s, e, v in cells if c in COLUMN_ENTITY],
            "safe": [{"start": s, "end": e, "value": v, "column": c, "row": r}
                     for r, c, s, e, v in cells if c in SAFE_COLUMNS],
        })
    (Path(out_dir) / "truth.json").write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--db", required=True)
    b.add_argument("--rows", type=int, default=50)
    b.add_argument("--seed", type=int, default=42)
    t = sub.add_parser("truth")
    t.add_argument("--db", required=True)
    t.add_argument("--dir", required=True)
    args = ap.parse_args()
    if args.cmd == "build":
        build(args.db, args.rows, args.seed)
    else:
        truth(args.db, args.dir)


if __name__ == "__main__":
    main()
