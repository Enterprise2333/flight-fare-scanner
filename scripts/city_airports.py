"""Curated IATA city-to-airport expansion for Google Flights queries.

SerpApi's Google Flights engine accepts airport IATA codes reliably, but some
IATA metropolitan city codes (notably LON) yield an empty response. The map is
kept local so scheduled runs never need a network lookup or consume API quota.

When configuring a city not present here, look up its common airports first
(e.g. with WebSearch in an interactive assistant session) and provide those
airport codes explicitly in the watch configuration.
"""

CITY_AIRPORTS = {
    # Searched against the destination's official visitor information / airport
    # references. Keep only commercial airports relevant to fare comparison.
    "LON": ("LHR", "LGW"),
    "NYC": ("JFK", "LGA", "EWR"),
    "TYO": ("HND", "NRT"),
    "PAR": ("CDG", "ORY", "BVA"),
    "ROM": ("FCO", "CIA"),
}


def expand_codes(codes):
    """Expand known city codes, preserving order and removing duplicates.

    Returns `(expanded_codes, expansions)`, where `expansions` is a list of
    `(city_code, airports)` tuples for rendering into validation/result notes.
    Airport codes and unknown codes pass through unchanged; callers must not
    guess a city's airport set.
    """
    expanded = []
    expansions = []
    for raw_code in codes:
        code = str(raw_code).upper()
        airports = CITY_AIRPORTS.get(code)
        if airports:
            expansions.append((code, airports))
            expanded.extend(airports)
        else:
            expanded.append(code)

    seen = set()
    unique = []
    for code in expanded:
        if code not in seen:
            seen.add(code)
            unique.append(code)
    return unique, expansions


def expansion_notes(legs):
    """Unique, user-facing notes for expansions applied to a query's legs."""
    notes = []
    seen = set()
    for leg in legs:
        for codes in (leg.origins, leg.destinations):
            _, expansions = expand_codes(codes)
            for city, airports in expansions:
                if city not in seen:
                    seen.add(city)
                    notes.append(
                        "Expanded city code {0} to airports: {1}.".format(
                            city, ", ".join(airports)))
    return notes
