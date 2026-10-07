"""The model chooses these three tools. Python validates and calculates results."""

import json
import logging

from data_source import DATA_SOURCE, INDUSTRIES, DataUnavailable, normalize_states


class StatecraftTools:
    """Country definitions and fetched observations belong to one chat only."""

    def __init__(self, source=DATA_SOURCE):
        self.source = source
        self.observations = {}
        self.sources = {}
        self.countries = {}
        self.reporting_year = None

    def fetch_state_economies(self, states: list[str]) -> dict:
        codes, duplicates = normalize_states(states)
        # Pin the automatically selected year within a chat so border updates stay
        # comparable even if BEA publishes another year while the chat is open.
        records, provenance = self.source.fetch(codes, self.reporting_year)
        year = records[0]["year"]
        self.reporting_year = year
        for record in records:
            self.observations[(year, record["code"])] = record
        self.sources[year] = provenance
        return {"year": year, "states": records, "sources": provenance,
                "duplicate_states_ignored": duplicates}

    def assemble_country(self, name: str, states: list[str], previous_name: str | None = None) -> dict:
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
            raise ValueError("Country name must contain 1–60 characters.")
        name = name.strip()
        key = name.casefold()
        previous_key = key
        if previous_name is not None:
            if not isinstance(previous_name, str) or not previous_name.strip():
                raise ValueError("previous_name must name an existing country in this chat.")
            previous_key = previous_name.strip().casefold()
            if previous_key not in self.countries:
                raise ValueError(f"Country to rename not found: {previous_name}. Available: {[c['name'] for c in self.countries.values()]}.")
            if key != previous_key and key in self.countries:
                raise ValueError(f"A different country named {name} already exists. Choose another name.")
        previous = self.countries.get(previous_key)
        codes, duplicates = normalize_states(states)
        year = self.reporting_year
        missing = [code for code in codes if (year, code) not in self.observations]
        if missing:
            raise ValueError(f"First call fetch_state_economies for {missing}, then assemble this country again. The data year is selected automatically.")
        records = [self.observations[(year, code)] for code in codes]
        gdp, population = sum(r["gdp_usd"] for r in records), sum(r["population"] for r in records)
        industry_totals = {code: {"code": code, "name": label, "gdp_usd": 0}
                           for code, label in INDUSTRIES.values()}
        for record in records:
            for industry in record["industries"]:
                industry_totals[industry["code"]]["gdp_usd"] += industry["gdp_usd"]
        industry_mix = sorted(
            [{**item, "share_of_gdp_pct": round(item["gdp_usd"] / gdp * 100, 2)}
             for item in industry_totals.values()], key=lambda item: item["gdp_usd"], reverse=True)
        country = {"name": name, "states": codes, "year": year, "gdp_usd": gdp,
                   "population": population, "gdp_per_person_usd": round(gdp / population, 2),
                   "industry_mix": industry_mix,
                   "state_contributions": [{**{k: v for k, v in r.items() if k != "industries"}, "share_of_country_gdp_pct": round(r["gdp_usd"] / gdp * 100, 2)} for r in records],
                   "sources": self.sources[year],
                   "interpretation": "A sum of existing state economies, not a prediction of independence. GDP per person is output, not personal income."}
        # Commit only after validation and calculation succeed. A rename moves
        # the existing entry rather than leaving the original country behind.
        if previous_key != key:
            del self.countries[previous_key]
        self.countries[key] = country
        result = {"country": country, "duplicate_states_ignored": duplicates,
                  "action": "updated" if previous else "created"}
        if previous:
            result["previous_name"] = previous["name"]
            result["previous_definition"] = {"states": previous["states"], "year": previous["year"]}
            if previous["year"] == year:
                result["change"] = {"gdp_usd": gdp - previous["gdp_usd"],
                                    "population": population - previous["population"],
                                    "gdp_per_person_usd": round(country["gdp_per_person_usd"] - previous["gdp_per_person_usd"], 2)}
        return result

    def compare_countries(self, country_names: list[str], metric: str = "gdp_usd") -> dict:
        metrics = {"gdp_usd": "Total GDP (current US dollars)", "population": "Population (people)",
                   "gdp_per_person_usd": "GDP per person (current US dollars)",
                   "industry_mix": "Industry shares of nominal GDP"}
        if not isinstance(metric, str) or metric not in metrics:
            raise ValueError(f"Choose metric from {list(metrics)}.")
        if not isinstance(country_names, list) or not 2 <= len(country_names) <= 10:
            raise ValueError("Compare 2–10 existing countries by name.")
        if any(not isinstance(name, str) for name in country_names):
            raise ValueError("Every country name must be a string.")
        keys = [name.strip().casefold() for name in country_names]
        if len(set(keys)) != len(keys):
            raise ValueError("Provide different countries; a country cannot compete against itself.")
        missing = [name for name, key in zip(country_names, keys) if key not in self.countries]
        if missing:
            raise ValueError(f"Countries not found in this chat: {missing}. Create them with assemble_country first. Available: {[c['name'] for c in self.countries.values()]}.")
        countries = [self.countries[key] for key in keys]
        if len({country["year"] for country in countries}) != 1:
            raise ValueError("Countries use different reporting years. Fetch and reassemble them for the same year before comparing.")
        overlaps = sorted(code for code in set(code for c in countries for code in c["states"])
                          if sum(code in c["states"] for c in countries) > 1)
        if metric == "industry_mix":
            rows = []
            for code, name in INDUSTRIES.values():
                values = [{**next(item for item in country["industry_mix"] if item["code"] == code), "name": country["name"]}
                          for country in countries]
                row = {"code": code, "name": name, "countries": values}
                if len(values) == 2:
                    row["share_difference_percentage_points"] = round(values[0]["share_of_gdp_pct"] - values[1]["share_of_gdp_pct"], 2)
                rows.append(row)
            rows.sort(key=lambda row: sum(value["gdp_usd"] for value in row["countries"]), reverse=True)
            return {"year": countries[0]["year"], "metric": metric, "metric_label": metrics[metric],
                    "industry_comparison": rows,
                    "overlapping_states": overlaps,
                    "interpretation": "Shares equal combined industry GDP divided by combined country GDP, not averages of state percentages. Industries are not ranked as countries; larger shares indicate specialization, not better performance."}
        ordered = sorted(countries, key=lambda country: country[metric], reverse=True)
        ranking, previous_value, rank = [], None, 0
        for index, country in enumerate(ordered, 1):
            if country[metric] != previous_value:
                rank = index
            ranking.append({"rank": rank, "name": country["name"], "value": country[metric],
                            "gdp_usd": country["gdp_usd"], "population": country["population"],
                            "gdp_per_person_usd": country["gdp_per_person_usd"]})
            previous_value = country[metric]
        result = {"year": ordered[0]["year"], "metric": metric, "metric_label": metrics[metric],
                  "ranking": ranking, "overlapping_states": overlaps,
                  "interpretation": "Overlapping definitions are allowed, but they cannot form a disjoint partition of the US." if overlaps else "Countries have no overlapping states."}
        if len(ordered) == 2:
            high, low = ordered[0][metric], ordered[1][metric]
            result["difference"] = {"absolute": round(high - low, 2),
                                    "percent_relative_to_smaller": round((high / low - 1) * 100, 2)}
        return result

    def run_tool(self, name: str, args: dict) -> str:
        functions = {"fetch_state_economies": self.fetch_state_economies,
                     "assemble_country": self.assemble_country, "compare_countries": self.compare_countries}
        if name not in functions:
            result = {"error": "unknown_tool", "message": f"Use one of {list(functions)}."}
        elif not isinstance(args, dict):
            result = {"error": "invalid_arguments", "message": "Tool arguments must be a JSON object."}
        else:
            try:
                result = functions[name](**args)
            except (ValueError, TypeError) as exc:
                result = {"error": "invalid_arguments", "message": str(exc)}
            except DataUnavailable as exc:
                result = {"error": "data_unavailable", "message": str(exc), "retryable": True}
            except Exception:
                logging.exception("Unexpected failure in tool %s", name)
                result = {"error": "tool_failed", "message": "The tool failed unexpectedly. Retry with a smaller request; do not invent a result."}
        return json.dumps(result, ensure_ascii=False, allow_nan=False)


def declaration(name, description, properties, required):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": required, "additionalProperties": False}}}


STATE_ARGUMENT = {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 50,
                  "description": "US state names or postal codes, e.g. ['California', 'OR', 'WA']. Only the 50 states are supported; duplicates count once."}
TOOLS = [
    declaration("fetch_state_economies", "Fetch official BEA annual nominal GDP, resident population, and GDP in 20 non-overlapping broad industry sectors for US states. Automatically uses the latest complete annual reporting year across all 50 states and keeps it consistent within this chat. Required before assembling states not yet fetched. Makes external requests, with a one-hour source cache. Returns year, sources, timestamps, units, and availability errors.",
                {"states": STATE_ARGUMENT}, ["states"]),
    declaration("assemble_country", "Create or update a fictional country from already-fetched state observations in this chat. Also use to retrieve a country's current industry mix: reuse its existing name and complete state list. Returns 20 industries with GDP dollars and shares of country GDP, largest first. To rename, set previous_name to its current name and name to its new name; this replaces the old entry. Supply existing states for a name-only change, or the COMPLETE updated list for border changes. Fetch new states first. Sums GDP and population and calculates GDP per person and state contributions; does not predict independence effects.",
                {"name": {"type": "string", "description": "Country name, 1–60 characters. Reuse a name to update that country."},
                 "previous_name": {"type": "string", "description": "Current name of an existing country when renaming it. Omit for creation or a border-only update. Never infer a rename just because states match."},
                 "states": STATE_ARGUMENT}, ["name", "states"]),
    declaration("compare_countries", "Compare 2–10 previously assembled countries in a common reporting year. Choose industry_mix for a side-by-side comparison of 20 broad sectors, with GDP dollars, shares, and two-country percentage-point differences. Otherwise ranks by GDP, population, or GDP per person, with differences and ties. Flags overlapping states. GDP per person measures output per resident, not household wealth or income.",
                {"country_names": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 10,
                                   "description": "Names of existing fictional countries in this chat."},
                 "metric": {"type": "string", "enum": ["gdp_usd", "population", "gdp_per_person_usd", "industry_mix"],
                            "description": "Ranking metric; defaults to total nominal GDP."}}, ["country_names"]),
]
