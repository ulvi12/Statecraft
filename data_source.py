"""Read real state GDP and population from BEA's public CSV archives."""

import csv
import io
import math
import threading
import time
import zipfile
from datetime import datetime, timezone

import requests

STATE_NAMES = dict(zip(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split(),
    ["Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
     "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
     "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine",
     "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi",
     "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey",
     "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio",
     "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina",
     "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia",
     "Washington", "West Virginia", "Wisconsin", "Wyoming"],
))
STATE_ALIASES = {name.casefold(): code for code, name in STATE_NAMES.items()}
STATE_ALIASES.update({code.casefold(): code for code in STATE_NAMES})
GDP_URL = "https://apps.bea.gov/regional/zip/SASUMMARY.zip"
POPULATION_URL = "https://apps.bea.gov/regional/zip/SAINC.zip"
INDUSTRY_URL = "https://apps.bea.gov/regional/zip/SAGDP.zip"
# Mutually exclusive broad sectors: do not add parent totals to their children.
# Line codes and NAICS classifications are checked against BEA's SAGDP2 table.
INDUSTRIES = {
    "3": ("11", "Agriculture, forestry, fishing and hunting"),
    "6": ("21", "Mining, quarrying, and oil and gas extraction"),
    "10": ("22", "Utilities"), "11": ("23", "Construction"),
    "12": ("31-33", "Manufacturing"), "34": ("42", "Wholesale trade"),
    "35": ("44-45", "Retail trade"), "36": ("48-49", "Transportation and warehousing"),
    "45": ("51", "Information"), "51": ("52", "Finance and insurance"),
    "56": ("53", "Real estate and rental and leasing"),
    "60": ("54", "Professional, scientific, and technical services"),
    "64": ("55", "Management of companies and enterprises"),
    "65": ("56", "Administrative, support, and waste management services"),
    "69": ("61", "Educational services"), "70": ("62", "Health care and social assistance"),
    "76": ("71", "Arts, entertainment, and recreation"),
    "79": ("72", "Accommodation and food services"),
    "82": ("81", "Other services (except government)"),
    "83": ("92", "Government and government enterprises"),
}


class DataUnavailable(Exception):
    """An external source is unavailable or its schema has changed."""


def normalize_states(states: list[str]) -> tuple[list[str], list[str]]:
    if not isinstance(states, list) or not 1 <= len(states) <= 50:
        raise ValueError("Provide a list of 1–50 US states, using names or two-letter codes.")
    codes, duplicates = [], []
    for value in states:
        if not isinstance(value, str) or value.strip().casefold() not in STATE_ALIASES:
            raise ValueError(f"Unknown state {value!r}. Use full US state names or codes such as CA. DC and territories are outside this version.")
        code = STATE_ALIASES[value.strip().casefold()]
        if code in codes:
            duplicates.append(code)
        else:
            codes.append(code)
    return codes, duplicates


def table_rows(content: bytes, table: str):
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        candidates = [name for name in archive.namelist()
                      if name.startswith(table + "_") and "ALL_AREAS" in name and name.endswith(".csv")]
        if len(candidates) != 1:
            raise DataUnavailable(f"BEA archive does not contain one {table} ALL_AREAS CSV.")
        info = archive.getinfo(candidates[0])
        if info.file_size > 15_000_000:
            raise DataUnavailable("BEA table exceeded the expected size.")
        text = archive.read(info).decode("utf-8-sig")
    return csv.DictReader(io.StringIO(text), skipinitialspace=True)


def read_table(content: bytes, table: str, line_code: str, expected_unit: str) -> dict:
    """Select an explicit table/line/unit; never mix nominal and real GDP."""
    result = {}
    for row in table_rows(content, table):
        # BEA marks Alaska and Hawaii with a trailing footnote asterisk.
        name = (row.get("GeoName") or "").strip().rstrip(" *")
        if name.casefold() not in STATE_ALIASES or (row.get("LineCode") or "").strip() != line_code:
            continue
        if (row.get("Unit") or "").strip() != expected_unit:
            raise DataUnavailable(f"Unexpected units for {table}: {row.get('Unit')!r}.")
        values = {}
        for year, raw in row.items():
            if year and year.isdigit() and raw:
                try:
                    number = float(raw.strip().replace(",", ""))
                except ValueError:
                    continue  # BEA marks missing observations (NA), (D), etc.
                if math.isfinite(number) and number > 0:
                    values[int(year)] = number
        result[STATE_ALIASES[name.casefold()]] = values
    if len(result) != 50:
        raise DataUnavailable(f"Expected 50 states in {table}, received {len(result)}.")
    return result


def read_industries(content: bytes) -> dict:
    sectors = {code: {} for code in STATE_NAMES}
    for row in table_rows(content, "SAGDP2"):
        name = (row.get("GeoName") or "").strip().rstrip(" *")
        line = (row.get("LineCode") or "").strip()
        if name.casefold() not in STATE_ALIASES or line not in INDUSTRIES:
            continue
        if (row.get("Unit") or "").strip() != "Millions of current dollars":
            raise DataUnavailable("Unexpected units for SAGDP2 industry GDP.")
        if (row.get("IndustryClassification") or "").strip() != INDUSTRIES[line][0]:
            raise DataUnavailable(f"Unexpected industry classification for SAGDP2 line {line}.")
        code = STATE_ALIASES[name.casefold()]
        if line in sectors[code]:
            raise DataUnavailable(f"Duplicate SAGDP2 industry row for {code}, line {line}.")
        values = {}
        for year, raw in row.items():
            if year and year.isdigit() and raw:
                try:
                    value = float(raw.strip().replace(",", ""))
                except ValueError:
                    continue  # Suppressed or unpublished is not zero.
                if math.isfinite(value):
                    values[int(year)] = round(value * 1_000_000)
        sectors[code][line] = values
    result = {}
    for code, lines in sectors.items():
        if set(lines) != set(INDUSTRIES):
            raise DataUnavailable(f"Incomplete broad industry categories in SAGDP2 for {code}.")
        years = set.intersection(*(set(values) for values in lines.values()))
        result[code] = {year: [{"code": naics, "name": name, "gdp_usd": lines[line][year]}
                              for line, (naics, name) in INDUSTRIES.items()] for year in years}
    return result


class BEADataSource:
    """One-hour process cache; every cold/expired fetch makes external requests."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cache = None
        self.expires_at = 0

    @staticmethod
    def download(url: str) -> bytes:
        with requests.get(url, timeout=(10, 60), stream=True) as response:
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > 30_000_000:
                    raise DataUnavailable("BEA download exceeded 30 MB.")
                chunks.append(chunk)
        content = b"".join(chunks)
        if not content.startswith(b"PK"):
            raise DataUnavailable("BEA returned a page instead of its CSV archive. Retry later; do not invent data.")
        return content

    def fetch(self, states: list[str], year: int | None = None) -> tuple[list[dict], dict]:
        if year is not None and (type(year) is not int or not 1998 <= year <= datetime.now(timezone.utc).year):
            raise ValueError("Use an integer reporting year from 1998 through the current year; unpublished years return an availability error.")
        with self.lock:
            if self.cache is None or time.monotonic() >= self.expires_at:
                try:
                    gdp = read_table(self.download(GDP_URL), "SASUMMARY", "4", "Millions of current dollars")
                    population = read_table(self.download(POPULATION_URL), "SAINC1", "2", "Number of persons")
                    industries = read_industries(self.download(INDUSTRY_URL))
                except (requests.RequestException, zipfile.BadZipFile, UnicodeError, KeyError, csv.Error) as exc:
                    raise DataUnavailable("Could not read BEA's public downloads. Check your network and retry; if the problem persists check BEA's download page.") from exc
                self.cache = (gdp, population, industries, datetime.now(timezone.utc).isoformat())
                self.expires_at = time.monotonic() + 3600
            gdp, population, industries, fetched_at = self.cache
            # Choose one complete year for ALL 50 states, not just this selection.
            # This keeps separately assembled countries directly comparable.
            available = sorted(set.intersection(*(set(gdp[code]) & set(population[code]) & set(industries[code]) for code in STATE_NAMES)))
            if not available:
                raise DataUnavailable("No complete common year of GDP, population, and industry data is available for all 50 states. Retry after BEA updates its downloads.")
            if year is None:
                year = max(available)
            if year not in available:
                raise ValueError(f"No complete GDP/population/industry data for {year}. Available common years: {available}.")
            records = [{"code": code, "name": STATE_NAMES[code], "year": year,
                        "gdp_usd": round(gdp[code][year] * 1_000_000),
                        "population": int(population[code][year]),
                        "industries": industries[code][year]} for code in states]
            for record in records:
                # BEA publishes each sector rounded to $0.1M. Allow that small
                # residual, but reject a changed hierarchy or mismatched vintage.
                if abs(sum(item["gdp_usd"] for item in record["industries"]) - record["gdp_usd"]) > max(1_000_000, record["gdp_usd"] * 0.00001):
                    raise DataUnavailable(f"Industry GDP does not reconcile with total GDP for {record['code']} in {year}.")
        return records, {"provider": "US Bureau of Economic Analysis", "gdp_table": "SASUMMARY, line 4",
                         "population_table": "SAINC1, line 2", "gdp_url": GDP_URL,
                         "industry_table": "SAGDP2, 20 non-overlapping broad industry sectors",
                         "industry_url": INDUSTRY_URL,
                         "population_url": POPULATION_URL, "fetched_at": fetched_at, "reporting_year": year,
                         "gdp_basis": "Annual nominal GDP in current US dollars",
                         "population_basis": "Annual resident population estimates reported by BEA"}


DATA_SOURCE = BEADataSource()
