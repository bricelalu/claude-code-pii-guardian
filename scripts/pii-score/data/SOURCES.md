# Data sources

Samples used by `customers.py` to build the fictional `customers` table. Regenerate the French
samples with `python3 scripts/pii-score/fetch_opendata.py`.

| File | Source | Licence |
|---|---|---|
| `fr_firstnames.csv` | INSEE, *Fichier des prénoms* (`prenoms-2025-nat_csv.zip`): top 250 first names per sex for births 1955–2004 | Licence Ouverte 2.0 |
| `fr_lastnames.csv` | data.gouv.fr, *Liste de prénoms et patronymes* (`patronymes.csv`, derived from INSEE SIRENE): top 800 surnames | Licence Ouverte 2.0 |
| `fr_addresses.csv` | Base Adresse Nationale (adresse.data.gouv.fr), 40 random addresses from each of départements 75, 69, 13, 33, 59, 44, 31, 67 | Licence Ouverte 2.0 |
| `foreign.json` | Hand-curated Spanish, Italian and English names and addresses (not open data) | — |

Generated, not sourced: emails (fictional domains), IBANs (valid checksum, random account
numbers), IP addresses (documentation ranges RFC 5737 / RFC 3849), and phone numbers. French
numbers use the ranges ARCEP reserves for fiction (01 99 00, 02 61 91, 03 53 01, 04 65 71,
05 36 49, 06 39 98); UK numbers use Ofcom's drama range (07700 900xxx).

Combining a random first name, surname and address produces fictional people. The addresses are
real places from the national address base; on their own they are not personal data.
