"""Google Flights data via SerpApi's `google_flights` engine.

Google shut down its own flight API (QPX) in 2018 and offers no replacement, so
structured Google Flights data has to come through a provider that renders the
page and returns JSON. SerpApi is used here because it has self-serve keys, a
documented contract and a free monthly allowance.

Notable differences from a GDS backend:
  * `departure_id`/`arrival_id` accept a comma-separated list, so several origin
    airports cost one call instead of one call each.
  * `include_airlines` accepts alliance names (STAR_ALLIANCE / SKYTEAM /
    ONEWORLD), which is the practical way to ask for full-service carriers.
  * There is no way to force or ban a connection point, no server-side date
    window, and no fare-condition (refundable) filter.
  * Round trip (`type=1`) returns outbound options priced as the FULL round-trip
    total; the return itinerary is only itemised after a second call using the
    option's `departure_token`.
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import city_airports
from . import base

ENDPOINT = "https://serpapi.com/search"

TRAVEL_CLASS = {"ECONOMY": 1, "PREMIUM_ECONOMY": 2, "BUSINESS": 3, "FIRST": 4}
# Google exposes a "max stops" bucket rather than an exact connection count.
STOPS_PARAM = {0: 1, 1: 2, 2: 3}

TYPE_ROUND_TRIP = 1
TYPE_ONE_WAY = 2
TYPE_MULTI_CITY = 3


class GoogleFlightsProvider(base.Provider):
    name = "google_flights"
    CAPABILITIES = frozenset({
        base.CAP_MULTI_CITY,
        base.CAP_OPEN_JAW,
        base.CAP_MULTI_AIRPORT,
        base.CAP_AIRLINE_FILTER,
        base.CAP_ALLIANCE_FILTER,
        base.CAP_MAX_PRICE,
        base.CAP_TRAVEL_CLASS,
        base.CAP_MAX_STOPS,
        base.CAP_LAYOVER_FILTER,
        base.CAP_PRICE_INSIGHTS,
        base.CAP_BOOKING_LINK,
        base.CAP_DEAL_DISCOVERY,
    })

    def __init__(self, api_key=None, logger=None, timeout=90, max_retries=3,
                 min_interval_ms=200, deep_search=False, resolve_return=False,
                 market="us", language="en"):
        super(GoogleFlightsProvider, self).__init__(logger=logger)
        self.api_key = api_key or os.environ.get("SERPAPI_KEY", "").strip()
        if not self.api_key:
            raise base.ProviderError(
                "Missing SerpApi key. Get one at https://serpapi.com/users/sign_up "
                "(100 free searches/month) and export SERPAPI_KEY, or switch "
                "provider in the config.")
        self.timeout = timeout
        self.max_retries = max_retries
        self.min_interval = max(min_interval_ms, 0) / 1000.0
        self.deep_search = deep_search
        # Round-trip return legs cost a second call each; opt-in.
        self.resolve_return = resolve_return
        self.market = market
        self.language = language
        self._last_call_at = 0.0

    # ------------------------------------------------------------- request

    def _params(self, query):
        legs = query.legs
        params = {
            "engine": "google_flights",
            "api_key": self.api_key,
            "currency": query.currency,
            "adults": query.adults,
            "hl": query.language or self.language,
            "gl": query.market or self.market,
            # Cheapest first: this is a low-fare tool, not a "best value" tool.
            "sort_by": 2,
        }
        if query.children:
            params["children"] = query.children
        if query.infants:
            params["infants_in_seat"] = query.infants
        if query.travel_class:
            params["travel_class"] = TRAVEL_CLASS[query.travel_class.upper()]
        if query.max_stops is not None:
            params["stops"] = STOPS_PARAM[int(query.max_stops)]
        if query.max_price is not None:
            params["max_price"] = int(query.max_price)
        if self.deep_search:
            params["deep_search"] = "true"
        if query.layover_minutes:
            low, high = query.layover_minutes
            params["layover_duration"] = "{0},{1}".format(int(low), int(high))

        # include_airlines and exclude_airlines are mutually exclusive.
        included = list(query.airlines) + list(query.alliances)
        if included and query.exclude_airlines:
            raise base.CapabilityError(
                "Google Flights cannot combine an airline allow-list with an "
                "exclude-list; keep only one of `airlines`/`alliances` or "
                "`exclude_airlines`.")
        if included:
            params["include_airlines"] = ",".join(included)
        elif query.exclude_airlines:
            params["exclude_airlines"] = ",".join(query.exclude_airlines)

        if len(legs) == 1:
            params["type"] = TYPE_ONE_WAY
            params["departure_id"] = self._airport_ids(legs[0].origins)
            params["arrival_id"] = self._airport_ids(legs[0].destinations)
            params["outbound_date"] = legs[0].date
            if legs[0].times:
                params["outbound_times"] = legs[0].times
        elif len(legs) == 2 and not query.is_open_jaw:
            params["type"] = TYPE_ROUND_TRIP
            params["departure_id"] = self._airport_ids(legs[0].origins)
            params["arrival_id"] = self._airport_ids(legs[0].destinations)
            params["outbound_date"] = legs[0].date
            params["return_date"] = legs[1].date
            if legs[0].times:
                params["outbound_times"] = legs[0].times
        else:
            # Open jaw and 3+ legs both go through multi-city.
            params["type"] = TYPE_MULTI_CITY
            params["multi_city_json"] = json.dumps([
                dict([("departure_id", self._airport_ids(leg.origins)),
                      ("arrival_id", self._airport_ids(leg.destinations)),
                      ("date", leg.date)]
                     + ([("times", leg.times)] if leg.times else []))
                for leg in legs
            ], separators=(",", ":"))
        return params

    @staticmethod
    def _airport_ids(codes):
        """SerpApi-safe comma-separated airport IDs for a config code list."""
        expanded, _ = city_airports.expand_codes(codes)
        return ",".join(expanded)

    def _deals_params(self, query):
        """Build an origin-to-anywhere Google Flights Deals request."""
        discovery = query.discovery
        params = {
            "engine": "google_flights_deals",
            "api_key": self.api_key,
            "departure_id": self._airport_ids(discovery["origins"]),
            "currency": query.currency,
            "adults": query.adults,
            "hl": query.language or self.language,
            "gl": query.market or self.market,
            "type": TYPE_ONE_WAY if discovery["type"] == "oneway"
                    else TYPE_ROUND_TRIP,
        }
        if query.children:
            params["children"] = query.children
        if query.infants:
            params["infants_in_seat"] = query.infants
        if query.travel_class:
            params["travel_class"] = TRAVEL_CLASS[query.travel_class.upper()]
        if query.max_stops is not None:
            params["stops"] = STOPS_PARAM[int(query.max_stops)]
        if query.max_price is not None:
            params["max_price"] = int(query.max_price)
        included = list(query.airlines) + list(query.alliances)
        if included and query.exclude_airlines:
            raise base.CapabilityError(
                "Google Flights Deals cannot combine an airline allow-list with "
                "an exclude-list.")
        if included:
            params["include_airlines"] = ",".join(included)
        elif query.exclude_airlines:
            params["exclude_airlines"] = ",".join(query.exclude_airlines)
        if discovery.get("outbound_window"):
            params["outbound_date"] = ",".join(discovery["outbound_window"])
        if discovery["type"] == "roundtrip":
            if discovery.get("trip_length"):
                params["trip_length"] = ",".join(
                    str(value) for value in discovery["trip_length"])
            elif discovery.get("travel_duration"):
                params["travel_duration"] = discovery["travel_duration"]
        return params

    @staticmethod
    def _deals_sort_key(offer):
        extras = offer.get("extras") or {}
        discount = extras.get("discount_percentage")
        discount = float(discount) if discount is not None else float("-inf")
        return (-discount, offer["price"])

    @staticmethod
    def _deal_dates(raw):
        """Return deal dates from fields or the provider's canonical search link.

        The Deals payload may omit `start_date`/`end_date`, while its
        `serpapi_flight_link` still contains `outbound_date`/`return_date`.
        Persist both dates so the JSON audit and every renderer agree.
        """
        departure = (raw.get("start_date") or raw.get("departure_date") or
                     raw.get("outbound_date"))
        returning = (raw.get("end_date") or raw.get("return_date"))
        link = raw.get("serpapi_flight_link") or ""
        if link and (not departure or not returning):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
            departure = departure or (query.get("outbound_date") or [None])[0]
            returning = returning or (query.get("return_date") or [None])[0]
        return departure, returning

    @staticmethod
    def _parse_deal(raw, currency, home_country):
        price = raw.get("price")
        destination = raw.get("arrival_airport_code") or raw.get("destination_id")
        origin = raw.get("departure_airport_code") or "-"
        if price is None or not destination:
            return None
        country = raw.get("country") or ""
        category = "unclassified"
        if home_country:
            category = "domestic" if country.casefold() == home_country else "international"
        airline_code = str(raw.get("airline_code") or "").upper()
        airline = raw.get("airline") or airline_code or "(unknown airline)"
        start_date, end_date = GoogleFlightsProvider._deal_dates(raw)
        segment = base.make_segment(
            flight=(airline_code or "DEAL") + "-" + str(destination),
            origin=origin, destination=destination, depart_at=start_date,
            arrive_at=end_date, duration=base.human_duration_minutes(
                raw.get("flight_duration")), operating=airline)
        itinerary = base.make_itinerary([segment], duration=segment["duration"])
        itinerary["stops"] = raw.get("stops")
        extras = {
            "deal_destination": raw.get("name") or destination,
            "deal_country": country or None,
            "deal_category": category,
            "average_price": raw.get("average_price"),
            "discount_percentage": raw.get("discount_percentage"),
            "flight_link": raw.get("flight_link"),
            "serpapi_flight_link": raw.get("serpapi_flight_link"),
            "departure_date": start_date,
            "return_date": end_date,
        }
        return base.make_offer(
            price=price, currency=currency, itineraries=[itinerary],
            validating_airlines=[airline_code] if airline_code else [],
            booking_link=raw.get("flight_link"), extras=extras)

    def _search_deals(self, query):
        params = self._deals_params(query)
        payload = self._call(params)
        discovery = query.discovery
        offers = []
        for raw in payload.get("deals") or []:
            offer = self._parse_deal(raw, query.currency,
                                     discovery.get("home_country"))
            if offer:
                offers.append(offer)
        offers, rejected = self._filter_allowlisted_offers(offers, query.airlines)
        offers.sort(key=self._deals_sort_key)
        offers = offers[:discovery["max_deals"]]
        meta = {
            "provider": self.name,
            "discovery": True,
            "notes": [
                "Relative low-fare discovery ranks reported discount percentage "
                "before price; results are destination-free deals, not a fixed "
                "route comparison."
            ],
        }
        if rejected:
            meta["notes"].append(
                "Local airline allow-list verification excluded {0} deal(s).".format(
                    rejected))
        return offers, meta

    def describe_request(self, query):
        params = self._deals_params(query) if query.discovery else self._params(query)
        params["api_key"] = "***"
        return json.dumps(params, indent=2, ensure_ascii=False)

    def _pace(self):
        elapsed = time.time() - self._last_call_at
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_call_at = time.time()

    def _call(self, params):
        url = ENDPOINT + "?" + urllib.parse.urlencode(params)
        attempt = 0
        while True:
            attempt += 1
            self._pace()
            try:
                with urllib.request.urlopen(url, timeout=self.timeout) as response:
                    self.call_count += 1
                    payload = json.loads(response.read().decode("utf-8"))
                    break
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", "replace")
                try:
                    detail = json.loads(raw).get("error", raw)
                except ValueError:
                    detail = raw
                if exc.code == 401:
                    raise base.ProviderError(
                        "SerpApi rejected the key (401). Check SERPAPI_KEY.")
                if exc.code == 429:
                    raise base.ProviderError(
                        "SerpApi quota or rate limit reached (429): {0}. The free "
                        "plan allows 100 searches/month.".format(detail[:200]))
                if exc.code >= 500 and attempt <= self.max_retries:
                    delay = min(2 ** attempt, 20)
                    self.log("HTTP {0}, retry {1}/{2} in {3}s".format(
                        exc.code, attempt, self.max_retries, delay))
                    time.sleep(delay)
                    continue
                raise base.ProviderError(
                    "SerpApi HTTP {0}: {1}".format(exc.code, detail[:300]),
                    status=exc.code)
            except urllib.error.URLError as exc:
                if attempt <= self.max_retries:
                    delay = min(2 ** attempt, 20)
                    self.log("network error {0}, retry {1}/{2}".format(
                        exc.reason, attempt, self.max_retries))
                    time.sleep(delay)
                    continue
                raise base.ProviderError("Network error: {0}".format(exc.reason))

        # SerpApi reports search-level problems in the body with HTTP 200.
        if payload.get("error"):
            raise base.ProviderError("SerpApi: {0}".format(payload["error"]))
        return payload

    # --------------------------------------------------------------- search

    def search(self, query):
        if query.discovery:
            return self._search_deals(query)
        params = self._params(query)
        payload = self._call(params)
        trip_type = params.get("type")

        options = (payload.get("best_flights") or []) + \
                  (payload.get("other_flights") or [])
        offers = []
        for option in options:
            offer = self._parse_option(option, query.currency, trip_type)
            if offer:
                # Requested dates are authoritative even when Google only
                # itemises a subset of a round-trip or multi-city response.
                extras = offer["extras"]
                if query.legs:
                    extras.setdefault("departure_date", query.legs[0].date)
                if len(query.legs) > 1:
                    extras.setdefault("return_date", query.legs[-1].date)
                offers.append(offer)
        offers, rejected = self._filter_allowlisted_offers(offers, query.airlines)
        offers.sort(key=lambda item: item["price"])

        if self.resolve_return and trip_type == TYPE_ROUND_TRIP and offers:
            self._attach_return(offers[0], params, query)

        offers = offers[:query.max_offers]
        meta = {"provider": self.name, "trip_type": trip_type}
        expansion_notes = city_airports.expansion_notes(query.legs)
        if expansion_notes:
            meta["notes"] = expansion_notes
        if rejected:
            meta.setdefault("notes", []).append(
                "Local airline allow-list verification excluded {0} offer(s) "
                "whose marketing carrier was missing from, or outside, the "
                "configured allow-list.".format(rejected)
            )
        insights = payload.get("price_insights") or {}
        if insights:
            meta["price_level"] = insights.get("price_level")
            meta["lowest_price"] = insights.get("lowest_price")
            typical = insights.get("typical_price_range") or []
            meta["typical_price_range"] = typical
        if trip_type == TYPE_ROUND_TRIP and not self.resolve_return:
            meta["note"] = (
                "Round-trip total price; the return itinerary is not itemised. "
                "Set provider.resolve_return: true to fetch it (costs one extra "
                "API call per watch).")
        return offers, meta

    @staticmethod
    def _filter_allowlisted_offers(offers, allowed_airlines):
        """Defence-in-depth validation for an explicit airline allow-list.

        SerpApi receives the same filter in ``include_airlines``, but a local
        check prevents a provider-side mismatch from silently turning a
        "full-service only" watch into an unfiltered result set.  Every
        marketing carrier extracted from the returned flight numbers must be
        in the configured list; an unidentifiable carrier is rejected too.
        Alliance filters remain server-side because the response does not
        reliably expose alliance membership.
        """
        allowed = {str(code).upper() for code in (allowed_airlines or [])}
        if not allowed:
            return offers, 0

        accepted = []
        rejected = 0
        for offer in offers:
            carriers = {str(code).upper()
                        for code in (offer.get("validating_airlines") or [])
                        if code}
            if carriers and carriers.issubset(allowed):
                accepted.append(offer)
            else:
                rejected += 1
        return accepted, rejected

    def _attach_return(self, offer, params, query):
        """Second call that itemises the return leg of the cheapest option."""
        token = offer.get("extras", {}).get("departure_token")
        if not token:
            return
        follow = dict(params)
        follow.pop("return_date", None)
        follow["departure_token"] = token
        try:
            payload = self._call(follow)
        except base.ProviderError as exc:
            self.log("return leg lookup failed: {0}".format(exc))
            return
        options = (payload.get("best_flights") or []) + \
                  (payload.get("other_flights") or [])
        if not options:
            return
        cheapest = min(options, key=lambda o: o.get("price") or float("inf"))
        parsed = self._parse_option(cheapest, query.currency, TYPE_ROUND_TRIP)
        if not parsed or len(parsed["itineraries"]) < 2:
            return
        # The follow-up response repeats the outbound then adds the return.
        offer["itineraries"] = parsed["itineraries"]
        offer["fingerprint"] = base.fingerprint(parsed["itineraries"])

    # -------------------------------------------------------------- parsing

    @staticmethod
    def _parse_option(option, currency, trip_type):
        segments_raw = option.get("flights") or []
        if not segments_raw:
            return None

        segments = []
        airlines = []
        for flight in segments_raw:
            departure = flight.get("departure_airport") or {}
            arrival = flight.get("arrival_airport") or {}
            number = (flight.get("flight_number") or "").replace(" ", "")
            if number[:2]:
                airlines.append(number[:2])
            segments.append(base.make_segment(
                flight=number or "-",
                origin=departure.get("id"),
                destination=arrival.get("id"),
                depart_at=departure.get("time"),
                arrive_at=arrival.get("time"),
                duration=base.human_duration_minutes(flight.get("duration")),
                cabin=(flight.get("travel_class") or "").upper() or None,
                aircraft=flight.get("airplane"),
                operating=flight.get("airline"),
            ))

        # Google returns all legs of a round trip in one flat `flights` array.
        # Split it back into itineraries on the layover boundaries it reports.
        itineraries = GoogleFlightsProvider._split_itineraries(
            segments, option, trip_type)

        baggage = GoogleFlightsProvider._baggage_summary(option.get("extensions") or [])
        for segment in segments:
            segment["checked_bags"] = baggage.get("segment_value")

        extras = {}
        if option.get("departure_token"):
            extras["departure_token"] = option["departure_token"]
        if option.get("carbon_emissions"):
            extras["carbon_g"] = option["carbon_emissions"].get("this_flight")
        if option.get("extensions"):
            extras["extensions"] = option["extensions"]
        if option.get("type"):
            extras["google_type"] = option["type"]

        # Google returns baggage as text in `extensions`; preserve its original
        # meaning and never infer a quantity that the provider did not return.
        extras["baggage_summary"] = baggage

        return base.make_offer(
            price=option.get("price") or 0,
            currency=currency,
            itineraries=itineraries,
            validating_airlines=sorted(set(airlines)),
            seats=None,               # Google Flights does not expose seat count
            booking_link=None,        # a booking_token needs a further call
            extras=extras,
        )

    @staticmethod
    def _baggage_summary(extensions):
        """Normalise Google Flights' non-structured baggage text conservatively."""
        note = next((str(item) for item in extensions
                     if "bag" in str(item).lower()), None)
        if not note:
            return {"status": "unknown", "text": "未返回托运行李信息",
                    "segment_value": None}
        match = re.search(r"(\d+)\s+checked\s+bags?\s+included", note, re.I)
        if match:
            count = int(match.group(1))
            return {"status": "explicit", "pieces": count,
                    "text": "含 {0} 件托运行李".format(count),
                    "raw": note, "segment_value": "{0} 件".format(count)}
        if re.search(r"checked\s+bag.*included|bags?\s+included", note, re.I):
            return {"status": "ambiguous", "text": "可能包含托运行李，须以出票页确认",
                    "raw": note, "segment_value": "可能包含"}
        return {"status": "ambiguous", "text": "托运行李信息不明确，须以出票页确认",
                "raw": note, "segment_value": "待确认"}

    @staticmethod
    def _split_itineraries(segments, option, trip_type):
        """Group flat segments into itineraries.

        A connection keeps you inside the same itinerary; a genuine leg break
        (return flight, next multi-city leg) does not appear in `layovers`, so
        the absence of a layover between consecutive segments marks the break.
        """
        total = base.human_duration_minutes(option.get("total_duration"))
        if len(segments) <= 1:
            return [base.make_itinerary(segments, duration=total)]

        layover_ids = [l.get("id") for l in (option.get("layovers") or [])]
        groups = [[segments[0]]]
        for segment in segments[1:]:
            joins_here = segment["from"] == groups[-1][-1]["to"]
            # Consume one layover marker per real connection.
            if joins_here and layover_ids and segment["from"] in layover_ids:
                layover_ids.remove(segment["from"])
                groups[-1].append(segment)
            elif joins_here and not layover_ids:
                # No layover data: treat a same-airport join as a connection.
                groups[-1].append(segment)
            else:
                groups.append([segment])

        if len(groups) == 1:
            return [base.make_itinerary(groups[0], duration=total)]
        # With several legs the reported total covers the whole trip only.
        return [base.make_itinerary(group) for group in groups]
