#!/usr/bin/env python3
"""Low-fare flight inspector: price checks with fluctuation alerting.

Usage:
    fare_watch.py run       --config config.yaml [--watch ID] [--format FMT]
    fare_watch.py validate  --config config.yaml [--watch ID] [--show-body]
    fare_watch.py history   --config config.yaml [--watch ID] [--limit 20]
    fare_watch.py airports  --config config.yaml --keyword shanghai
    fare_watch.py providers --config config.yaml

`run` is the command a scheduler calls. `validate` builds and capability-checks
every request without spending a single API call. Add `-v` for per-probe logging.

stdout carries only the report; progress goes to stderr.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import notify
import providers
import store as store_mod
import strategies
import visa_cache
from providers import base as provider_base

DEFAULT_STATE_DIR = "~/.flight-fare-scanner"


# ------------------------------------------------------------------- config

def load_config(path):
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        raise SystemExit("Config not found: {0}".format(path))
    with open(path, "r") as handle:
        raw = handle.read()

    if path.endswith((".yaml", ".yml")):
        try:
            import yaml
        except ImportError:
            raise SystemExit(
                "PyYAML is required for YAML configs. Either `pip3 install pyyaml` "
                "or convert the config to JSON (.json is supported natively).")
        config = yaml.safe_load(raw)
    else:
        config = json.loads(raw)

    if not isinstance(config, dict) or not config.get("watches"):
        raise SystemExit("Config must be a mapping containing a non-empty 'watches' list.")

    state_dir = os.path.expanduser(config.get("state_dir", DEFAULT_STATE_DIR))
    config["state_dir"] = state_dir
    defaults = config.setdefault("defaults", {})
    defaults.setdefault("currency", "CNY")
    config.setdefault("alerting", {})
    config.setdefault("quota", {})
    config.setdefault("notify", {})
    traveler = config.setdefault("traveler", {})
    traveler.setdefault("passport_nationality", "CN")
    visa = config.setdefault("visa", {})
    visa.setdefault("enabled", True)
    visa.setdefault("cache_file", os.path.join(state_dir, "visa-cache.json"))
    visa.setdefault("cache_max_age_hours", 24 * 30)
    visa.setdefault("entries", [])

    # Provider block: `provider: google_flights` or a mapping with settings.
    provider = config.get("provider") or providers.DEFAULT_PROVIDER
    if isinstance(provider, str):
        provider = {"name": provider}
    if not isinstance(provider, dict) or not provider.get("name"):
        raise SystemExit(
            "`provider` must be a name or a mapping containing `name`. "
            "Available: " + ", ".join(sorted(providers.PROVIDERS)))
    if provider["name"] == "amadeus":
        provider.setdefault("token_cache",
                            os.path.join(state_dir, ".token.json"))
    config["provider"] = provider

    seen = set()
    for watch in config["watches"]:
        watch_id = watch.get("id")
        if watch_id in seen:
            raise SystemExit("Duplicate watch id: {0}".format(watch_id))
        seen.add(watch_id)
    return config


def _provider_source(provider_name):
    """Human-readable data provenance included in every saved result."""
    if provider_name == "google_flights":
        return "Google Flights via SerpApi"
    if provider_name == "amadeus":
        return "Amadeus Flight Offers"
    if provider_name == "mock":
        return "Offline fixture (mock provider)"
    return provider_name


def _search_policy(probe_set):
    """Summarise the first query's meaningful filters for report consumers."""
    query = probe_set.probes[0].query
    if query.airlines:
        airline_policy = "Explicit airline allow-list: {0}".format(
            ", ".join(query.airlines))
    elif query.alliances:
        airline_policy = "Alliance filter: {0}".format(
            ", ".join(query.alliances))
    elif query.exclude_airlines:
        airline_policy = "Excluded airlines: {0}".format(
            ", ".join(query.exclude_airlines))
    else:
        airline_policy = "No airline filter"

    if query.max_stops is None:
        connection_policy = "Connection limit not specified"
    elif query.max_stops == 0:
        connection_policy = "Non-stop only"
    else:
        connection_policy = "At most {0} connection(s); actual stops are shown per offer".format(
            query.max_stops)
    return airline_policy, connection_policy


_INTERVAL_RE = re.compile(r"^(\d+)([mhdw])$")


def _interval_seconds(value):
    """Parse a scheduling interval used for quota estimation and installers."""
    match = _INTERVAL_RE.match(str(value or "").strip().lower())
    if not match:
        raise strategies.ConfigError(
            "schedule.interval must look like 30m, 6h, 1d or 7d")
    number = int(match.group(1))
    if number < 1:
        raise strategies.ConfigError("schedule.interval must be positive")
    unit_seconds = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
    return number * unit_seconds[match.group(2)]


def _estimate_probe_calls(probe_set, provider_config):
    """Conservative request count, including optional return-leg resolution."""
    calls = 0
    resolve_return = (provider_config.get("name") == "google_flights"
                      and provider_config.get("resolve_return"))
    for probe in probe_set.probes:
        calls += 1
        if resolve_return and len(probe.query.legs) == 2 \
                and not probe.query.is_open_jaw:
            calls += 1
    return calls


def _offer_datetime(offer, field, first):
    """Read a local departure/arrival timestamp from a one-way offer."""
    itineraries = offer.get("itineraries") or []
    if not itineraries:
        return None
    segments = itineraries[0].get("segments") or []
    if not segments:
        return None
    value = segments[0 if first else -1].get(field)
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value).replace(" ", "T"))
    except ValueError:
        return None


def _hub_component(probe, offer):
    """Auditable component data for a positioned-hub candidate."""
    return {"role": probe.metadata["role"], "label": probe.label,
            "price": offer["price"], "currency": offer["currency"],
            "itineraries": offer.get("itineraries") or []}


def inspect(probe_set, provider, log):
    """Run every probe in a set and combine them per the set's semantics.

    Returns (best_price, currency, offers, offer_count, meta) or raises.
    Offers are already provider-neutral: each backend parses its own response.
    """
    if probe_set.combine == "hub":
        groups = {}
        count = 0
        notes = []
        for probe in probe_set.probes:
            group = probe.metadata["group"]
            try:
                offers, meta = provider.search(probe.query)
            except (provider_base.ProviderError, provider_base.CapabilityError) as exc:
                notes.append("{0}: {1}".format(probe.label, exc))
                continue
            count += len(offers)
            groups.setdefault(group, {})[probe.metadata["role"]] = (probe, offers)
            for note in ([meta.get("note")] + list(meta.get("notes") or [])):
                if note and note not in notes:
                    notes.append(note)
            log("  {0}: {1} offers".format(probe.label, len(offers)))

        candidates = []
        for group, parts in groups.items():
            if "direct" in parts:
                probe, offers = parts["direct"]
                if not offers:
                    notes.append("{0}: no direct HKG offer".format(group))
                    continue
                offer = offers[0]
                extras = dict(offer.get("extras") or {})
                extras.update({"journey_dates": probe.metadata["journey_dates"],
                               "hub": probe.metadata["hub"], "ticket_count": 1,
                               "main_price": offer["price"], "positioning_price": 0,
                               "risk": "Single main-ticket candidate. Google may omit the "
                                       "return flight detail; confirm it at booking."})
                offer["extras"] = extras
                offer["probe"] = probe.key
                offer["probe_label"] = probe.label
                candidates.append(offer)
                continue

            required = ("position_out", "main_out", "main_in", "position_back")
            if any(role not in parts or not parts[role][1] for role in required):
                notes.append("{0}: no complete positioned-ticket set".format(group))
                continue
            min_buffer = parts["position_out"][0].metadata["min_buffer_minutes"]
            pos_out, main_out = parts["position_out"], parts["main_out"]
            main_in, pos_back = parts["main_in"], parts["position_back"]
            outbound_pairs = []
            for position, main in ((p, m) for p in pos_out[1] for m in main_out[1]):
                arrival = _offer_datetime(position, "arrive_at", False)
                departure = _offer_datetime(main, "depart_at", True)
                if arrival and departure:
                    gap = int((departure - arrival).total_seconds() // 60)
                    if gap >= min_buffer and position["currency"] == main["currency"]:
                        outbound_pairs.append((position["price"] + main["price"], gap,
                                               position, main))
            inbound_pairs = []
            for main, position in ((m, p) for m in main_in[1] for p in pos_back[1]):
                arrival = _offer_datetime(main, "arrive_at", False)
                departure = _offer_datetime(position, "depart_at", True)
                if arrival and departure:
                    gap = int((departure - arrival).total_seconds() // 60)
                    if gap >= min_buffer and main["currency"] == position["currency"]:
                        inbound_pairs.append((main["price"] + position["price"], gap,
                                              main, position))
            if not outbound_pairs or not inbound_pairs:
                notes.append("{0}: no same-day ICN combination meets the {1} minute "
                             "buffer".format(group, min_buffer))
                continue
            outbound_pairs.sort(key=lambda item: item[0])
            inbound_pairs.sort(key=lambda item: item[0])
            out_total, out_gap, out_pos, out_main = outbound_pairs[0]
            in_total, in_gap, in_main, in_pos = inbound_pairs[0]
            if out_pos["currency"] != in_main["currency"]:
                notes.append("{0}: positioned components use different currencies".format(group))
                continue
            component_offers = (out_pos, out_main, in_main, in_pos)
            component_probes = (pos_out[0], main_out[0], main_in[0], pos_back[0])
            components = [_hub_component(probe, offer)
                          for probe, offer in zip(component_probes, component_offers)]
            itineraries = [itinerary for offer in component_offers
                           for itinerary in offer.get("itineraries") or []]
            total = out_total + in_total
            extras = {"journey_dates": pos_out[0].metadata["journey_dates"],
                      "hub": pos_out[0].metadata["hub"], "ticket_count": 4,
                      "main_price": out_main["price"] + in_main["price"],
                      "positioning_price": out_pos["price"] + in_pos["price"],
                      "connection_buffers": {"outbound_minutes": out_gap,
                                             "return_minutes": in_gap},
                      "components": components,
                      "risk": "FOUR separate PNRs: no missed-connection protection, "
                              "no guaranteed through-checked baggage, and transit/entry "
                              "requirements remain the traveller's responsibility."}
            synthetic = provider_base.make_offer(
                price=total, currency=out_pos["currency"], itineraries=itineraries,
                validating_airlines=sorted({code for offer in component_offers
                                             for code in offer.get("validating_airlines") or []}),
                extras=extras)
            synthetic["fingerprint"] = hashlib.sha1(json.dumps(
                {"group": group, "components": [(item["role"], item["price"])
                                                   for item in components]},
                sort_keys=True).encode("utf-8")).hexdigest()[:12]
            candidates.append(synthetic)
        if not candidates:
            raise RuntimeError("; ".join(notes) or "no valid hub candidate")
        candidates.sort(key=lambda item: item["price"])
        return candidates[0]["price"], candidates[0]["currency"], candidates, count, {"notes": notes}

    if probe_set.combine == "sum":
        total = 0.0
        currency = None
        combined_itineraries = []
        parts = []
        count = 0
        notes = []
        for probe in probe_set.probes:
            offers, meta = provider.search(probe.query)
            if not offers:
                raise RuntimeError(
                    "no offers for '{0}' - a split ticket needs every leg priced, "
                    "so this watch cannot be evaluated".format(probe.label))
            cheapest = offers[0]
            if currency is None:
                currency = cheapest["currency"]
            elif cheapest["currency"] != currency:
                raise RuntimeError(
                    "cannot sum split-ticket legs priced in different currencies "
                    "({0} vs {1})".format(currency, cheapest["currency"]))
            total += cheapest["price"]
            combined_itineraries.extend(cheapest["itineraries"])
            parts.append({"probe": probe.key, "label": probe.label,
                          "price": cheapest["price"]})
            count += len(offers)
            if meta.get("note") and meta["note"] not in notes:
                notes.append(meta["note"])
            for note in meta.get("notes") or []:
                if note not in notes:
                    notes.append(note)
            log("  {0}: {1:.0f} {2} ({3} offers)".format(
                probe.label, cheapest["price"], currency, len(offers)))

        synthetic = provider_base.make_offer(
            price=total, currency=currency, itineraries=combined_itineraries,
            validating_airlines=sorted({
                segment["flight"][:2] for itinerary in combined_itineraries
                for segment in itinerary["segments"] if segment.get("flight")
            }),
            extras={"split_parts": parts})
        # Identify the combination, not just one routing.
        synthetic["fingerprint"] = hashlib.sha1(
            json.dumps(parts, sort_keys=True).encode("utf-8")).hexdigest()[:12]
        return total, currency, [synthetic], count, {"notes": notes}

    # combine == "min": cheapest across all probes wins.
    all_offers = []
    count = 0
    errors = []
    notes = []
    extra_meta = {}
    for probe in probe_set.probes:
        try:
            offers, meta = provider.search(probe.query)
        except (provider_base.ProviderError, provider_base.CapabilityError) as exc:
            # One bad date in a sweep must not sink the whole watch.
            errors.append("{0}: {1}".format(probe.label, exc))
            log("  {0}: FAILED {1}".format(probe.label, exc))
            continue
        count += len(offers)
        for offer in offers:
            offer["probe"] = probe.key
            offer["probe_label"] = probe.label
        all_offers.extend(offers)
        if meta.get("note") and meta["note"] not in notes:
            notes.append(meta["note"])
        # Providers may return several caveats (truncation, coverage limits).
        for note in meta.get("notes") or []:
            if note not in notes:
                notes.append(note)
        for key in ("price_level", "typical_price_range"):
            if meta.get(key) is not None:
                extra_meta[key] = meta[key]
        log("  {0}: {1} offers, best {2}".format(
            probe.label, len(offers),
            "{0:.0f}".format(offers[0]["price"]) if offers else "-"))

    if not all_offers:
        detail = "; ".join(errors) if errors else "the search returned zero offers"
        raise RuntimeError(detail)

    if probe_set.sort_mode == "relative_deal":
        all_offers.sort(key=lambda item: (
            -float((item.get("extras") or {}).get("discount_percentage")
                   if (item.get("extras") or {}).get("discount_percentage") is not None
                   else float("-inf")),
            item["price"]))
    else:
        all_offers.sort(key=lambda item: item["price"])
    best = all_offers[0]
    extra_meta["notes"] = notes
    return best["price"], best["currency"], all_offers, count, extra_meta


def run(config, args):
    log = _make_logger(args.verbose)
    state_dir = config["state_dir"]
    db_path = os.path.join(state_dir, "fares.sqlite3")
    store = store_mod.Store(db_path)

    if args.mock:
        provider = providers.build("mock", {"fixture_path": args.mock,
                                           "jitter_pct": args.mock_jitter}, log)
    else:
        provider = providers.build(config["provider"]["name"],
                                   config["provider"], log)
    print("[provider] {0}".format(provider.name), file=sys.stderr)

    watches = config["watches"]
    if args.watch:
        watches = [w for w in watches if w.get("id") in args.watch]
        if not watches:
            raise SystemExit("No watch matched: {0}".format(", ".join(args.watch)))

    # A focus watch can consume a scout snapshot created earlier in the same
    # process. Keep user order otherwise, but always run selected scouts first.
    focus_sources = {str(w.get("focus_from")) for w in watches
                     if w.get("strategy") == "focus_roundtrip"
                     and w.get("focus_from")}
    watches = sorted(watches, key=lambda item: 0 if item.get("id") in focus_sources
                     else 1)

    alerting = config["alerting"]
    visa_settings = config["visa"]
    if visa_settings.get("enabled"):
        visa_cache.merge_entries(visa_settings["cache_file"], visa_settings.get("entries"))
    scheduled_run = os.environ.get("FARE_WATCH_SCHEDULED") == "1"
    results = []
    for watch in watches:
        label = watch.get("label") or watch.get("id")
        # Progress goes to stderr so stdout holds only the report - that keeps
        # `--format json` parseable and the console output clean to paste.
        print("[watch] {0}".format(label), file=sys.stderr)
        result = {
            "watch_id": watch.get("id"), "label": label,
            "strategy": watch.get("strategy"), "provider": provider.name,
            "source": _provider_source(provider.name),
            "triggers": [], "notes": [],
        }
        try:
            effective_watch = watch
            focus_meta = None
            if watch.get("strategy") == "focus_roundtrip":
                scout_id = strategies.validate_focus(watch)
                effective_watch, focus_meta = strategies.materialize_focus_roundtrip(
                    watch, store.latest_snapshot(scout_id))
                result["focus"] = focus_meta
                result["notes"].append(
                    "Focus dates {0} → {1}, resolved from scout '{2}' captured "
                    "at {3}.".format(focus_meta["departure_date"],
                                       focus_meta["return_date"], scout_id,
                                       focus_meta.get("scout_captured_at") or "unknown"))

            probe_set = strategies.expand(effective_watch, config["defaults"])
            result["notes"].extend(note for note in probe_set.notes
                                   if note not in result["notes"])
            result["route"] = probe_set.route
            result["probe_count"] = len(probe_set.probes)
            result["estimated_calls"] = _estimate_probe_calls(
                probe_set, config["provider"])
            result["airline_policy"], result["connection_policy"] = _search_policy(
                probe_set)
            # Reject unsupported filters instead of silently ignoring them.
            for probe in probe_set.probes:
                provider.check(probe.query)

            price, currency, offers, offer_count, meta = inspect(
                probe_set, provider, log)
            for note in meta.get("notes", []):
                if note not in result["notes"]:
                    result["notes"].append(note)
            if meta.get("discovery"):
                result["discovery"] = True
            if meta.get("price_level"):
                result["price_level"] = meta["price_level"]
            if meta.get("typical_price_range"):
                result["typical_price_range"] = meta["typical_price_range"]
            if visa_settings.get("enabled"):
                passport = watch.get("passport_nationality") or \
                    config["traveler"]["passport_nationality"]
                for offer in offers:
                    offer.setdefault("extras", {})["visa_context"] = \
                        visa_cache.context_for_offer(
                            offer, passport, visa_settings["cache_file"],
                            visa_settings["cache_max_age_hours"], scheduled_run)
                result["visa_mode"] = "缓存（非实时）" if scheduled_run else "缓存核验信息"
            for offer in offers:
                extras = offer.setdefault("extras", {})
                extras["travel_summary_cn"] = {
                    "主程往返日期": notify._offer_dates(offer),
                    "托运行李": notify._baggage_cn(offer),
                    "航段与中转": [notify._itinerary_cn(item)
                                   for item in offer.get("itineraries") or []],
                    "签证与过境": notify._visa_cn(offer),
                }
            previous = store.latest_snapshot(watch["id"])
            hist_min = store.historical_min(watch["id"])

            result.update({
                "price": price,
                "currency": currency or config["defaults"]["currency"],
                "offers": offers[:10],
                "offer_count": offer_count,
                "previous_price": float(previous["best_price"]) if previous else None,
                "hist_min": hist_min,
                "target_price": watch.get("target_price"),
            })
            result["triggers"] = store_mod.evaluate(
                store, watch["id"], price, result["currency"],
                watch.get("target_price"), alerting, previous, hist_min,
                price_level=result.get("price_level"))

            captured_at = store.record_snapshot(
                watch_id=watch["id"], strategy=watch["strategy"],
                currency=result["currency"], best_price=price,
                offer_count=offer_count, fingerprint=offers[0].get("fingerprint", ""),
                detail={"best": offers[0], "route": result["route"],
                        "notes": result["notes"], "source": result["source"],
                        "airline_policy": result["airline_policy"],
                        "connection_policy": result["connection_policy"],
                        "probe_count": result["probe_count"],
                        "estimated_calls": result["estimated_calls"],
                        "focus": result.get("focus")})
            result["captured_at"] = captured_at
            for trigger in result["triggers"]:
                if trigger.severity != "info":
                    store.record_alert(watch["id"], trigger.kind, price)

            series = store.recent_series(watch["id"], limit=20)
            result["trend"] = store_mod.sparkline(series)
            print("  best {0:.0f} {1} from {2} offer(s)".format(
                price, result["currency"], offer_count), file=sys.stderr)
            for trigger in result["triggers"]:
                print("  -> [{0}] {1}".format(trigger.kind, trigger.message),
                      file=sys.stderr)

        except (strategies.ConfigError, provider_base.ProviderError,
                provider_base.CapabilityError, RuntimeError) as exc:
            result["error"] = str(exc)
            print("  FAILED: {0}".format(exc), file=sys.stderr)
            if args.verbose:
                traceback.print_exc()
        results.append(result)

    _deliver(config, results, args)
    store.close()

    failures = [r for r in results if r.get("error")]
    if failures and len(failures) == len(results):
        return 1
    return 0


def _deliver(config, results, args):
    """Fan the run out to every enabled sink.

    Console and the local report are local and always safe. Email and DingTalk
    are outbound, so `--no-notify` suppresses those two only. A failure in any
    one sink never discards the data or blocks the others.
    """
    notify_cfg = config["notify"]

    # --- console (default sink; this is what gets read back in conversation)
    console_cfg = notify_cfg.get("console")
    if console_cfg is None:
        console_cfg = {"enabled": True}
    if console_cfg.get("enabled", True) and not args.quiet:
        fmt = args.format or console_cfg.get("format", "markdown")
        shown = notify.select(results, console_cfg.get("only_on_alert", False))
        if shown:
            print()
            print(notify.render_console(
                shown, fmt=fmt,
                include_offers=int(console_cfg.get("include_offers", 3))))
        else:
            print("\nNothing actionable this run ({0} watch(es) checked).".format(
                len(results)))

    # --- local PDF report + JSON audit
    pdf_path = None
    report_cfg = notify_cfg.get("report") or {}
    if report_cfg.get("enabled", True):
        directory = report_cfg.get("dir") or os.path.join(
            config["state_dir"], "reports")
        pdf_path, json_path = notify.write_report(
            directory, results,
            include_offers=int(report_cfg.get("include_offers", 10)))
        print("report: {0}".format(pdf_path), file=sys.stderr)
        print("raw:    {0}".format(json_path), file=sys.stderr)

    if args.no_notify:
        return

    # --- email
    email_cfg = notify_cfg.get("email") or {}
    if args.email or email_cfg.get("enabled"):
        to_send = notify.select(results, email_cfg.get("only_on_alert", True)
                                and not args.email)
        if to_send:
            try:
                recipients = notify.send_email(
                    email_cfg, to_send, attachment_path=pdf_path)
                print("email: sent {0} item(s) to {1}".format(
                    len(to_send), ", ".join(recipients)), file=sys.stderr)
            except notify.NotifyError as exc:
                print("email: FAILED {0}".format(exc), file=sys.stderr)
        else:
            print("email: nothing actionable, skipped", file=sys.stderr)

    # --- dingtalk (optional)
    ding_cfg = notify_cfg.get("dingtalk") or {}
    if ding_cfg.get("enabled"):
        to_send = notify.select(results, ding_cfg.get("only_on_alert", True))
        if not to_send:
            print("dingtalk: nothing actionable, skipped", file=sys.stderr)
            return
        # The keyword must survive into the body when the robot uses keyword security.
        body = notify.build_summary(results)
        if ding_cfg.get("keyword"):
            body += "\n> {0}\n".format(ding_cfg["keyword"])
        body += "\n".join(notify.render_result_markdown(r) for r in to_send)
        try:
            notify.send_dingtalk(ding_cfg, ding_cfg.get("title", "Fare watch"), body)
            print("dingtalk: sent {0} item(s)".format(len(to_send)), file=sys.stderr)
        except notify.NotifyError as exc:
            print("dingtalk: FAILED {0}".format(exc), file=sys.stderr)


# ------------------------------------------------------------ other commands

def validate(config, args):
    """Build and capability-check every probe without spending an API call."""
    watches = config["watches"]
    if args.watch:
        watches = [w for w in watches if w.get("id") in args.watch]
        if not watches:
            print("No watch matched: {0}".format(", ".join(args.watch)))
            return 1

    provider_name = config["provider"]["name"]
    # Capability checks are class-level, so no credentials are needed here.
    provider_cls = providers.PROVIDERS.get(provider_name)
    if provider_cls is None:
        print("unknown provider '{0}'; available: {1}".format(
            provider_name, ", ".join(sorted(providers.PROVIDERS))))
        return 1
    capabilities = set(provider_cls.CAPABILITIES)
    configured_ids = {watch.get("id") for watch in config["watches"]}
    print("provider: {0}".format(provider_name))

    ok = True
    total_calls = 0
    monthly_calls = 0.0
    scheduled = []
    for watch in watches:
        watch_id = watch.get("id")
        schedule = watch.get("schedule") or {}
        interval_seconds = None
        try:
            if schedule and schedule.get("enabled", True):
                interval_seconds = _interval_seconds(schedule.get("interval"))

            if watch.get("strategy") == "focus_roundtrip":
                scout_id = strategies.validate_focus(watch)
                if scout_id not in configured_ids:
                    raise strategies.ConfigError(
                        "focus_from '{0}' is not a configured watch id".format(scout_id))
                calls = 2 if (provider_name == "google_flights"
                              and config["provider"].get("resolve_return")) else 1
                total_calls += calls
                print("✓ {0} [focus_roundtrip] dates from scout '{1}' -> up to {2} "
                      "API call(s)".format(watch_id, scout_id, calls))
                print("    note: resolves the newest stored scout dates at run time.")
                if args.show_body:
                    print("    (request dates are populated from the scout snapshot at run time)")
            else:
                probe_set = strategies.expand(watch, config["defaults"])
                unsupported = None
                for probe in probe_set.probes:
                    missing = sorted(probe.query.required_capabilities() - capabilities)
                    if missing:
                        unsupported = missing
                        break
                if unsupported:
                    details = []
                    for token in unsupported:
                        hint = provider_base.CAPABILITY_HINTS.get(token)
                        details.append("`{0}`{1}".format(
                            token, " - " + hint if hint else ""))
                    raise strategies.ConfigError(
                        "provider '{0}' does not support: {1}".format(
                            provider_name, "; ".join(details)))

                calls = _estimate_probe_calls(probe_set, config["provider"])
                total_calls += calls
                print("✓ {0} [{1}] {2} -> up to {3} API call(s)".format(
                    watch_id, probe_set.strategy, probe_set.route, calls))
                for note in probe_set.notes:
                    print("    note: {0}".format(note))
                if args.show_body:
                    # Rendering the real request needs an instance; fall back to
                    # the neutral query when credentials are absent.
                    renderer = None
                    try:
                        renderer = providers.build(provider_name, config["provider"])
                    except provider_base.ProviderError as exc:
                        print("    (no credentials, showing neutral queries: {0})".format(exc))
                    for probe in probe_set.probes:
                        print("    --- {0} ---".format(probe.label))
                        body = (renderer.describe_request(probe.query) if renderer
                                else json.dumps(probe.query.to_dict(), indent=2,
                                                ensure_ascii=False))
                        print(_indent(body))

            if interval_seconds:
                estimate = calls * (30.0 * 86400 / interval_seconds)
                monthly_calls += estimate
                scheduled.append((watch_id, schedule.get("interval"), estimate))
                print("    schedule: every {0}; estimated {1:.1f} calls/30-day month".format(
                    schedule.get("interval"), estimate))
        except strategies.ConfigError as exc:
            ok = False
            print("✗ {0}: {1}".format(watch_id, exc))

    print("\n{0} watch(es), up to {1} API call(s) for one complete run.".format(
        len(watches), total_calls))
    if scheduled:
        limit = config["quota"].get("monthly_limit")
        print("Scheduled estimate: {0:.1f} API call(s) per 30-day month.".format(
            monthly_calls))
        if limit is not None:
            ratio = monthly_calls / float(limit) * 100.0 if float(limit) else 0.0
            print("Quota budget: {0:.1f}/{1} calls ({2:.0f}%).".format(
                monthly_calls, limit, ratio))
            if monthly_calls > float(limit):
                print("WARNING: scheduled watches exceed the configured monthly budget.")
    return 0 if ok else 1


def show_providers(config, args):
    rows = providers.capability_table()
    widths = [max(len(str(row[i])) for row in rows) for i in range(len(rows[0]))]
    for index, row in enumerate(rows):
        print("  ".join(str(cell).ljust(widths[i])
                        for i, cell in enumerate(row)))
        if index == 0:
            print("  ".join("-" * width for width in widths))
    print("\nconfigured: {0}".format(config["provider"]["name"]))
    print("default:    {0}".format(providers.DEFAULT_PROVIDER))
    return 0


def _indent(text, prefix="    "):
    return "\n".join(prefix + line for line in text.splitlines())


def history(config, args):
    store = store_mod.Store(os.path.join(config["state_dir"], "fares.sqlite3"))
    watch_ids = args.watch or store.watch_ids()
    if not watch_ids:
        print("No history yet. Run `fare_watch.py run` first.")
        return 0
    for watch_id in watch_ids:
        rows = store.history(watch_id, limit=args.limit)
        if not rows:
            print("\n{0}: no data".format(watch_id))
            continue
        series = store.recent_series(watch_id, limit=args.limit)
        prices = [row["best_price"] for row in rows]
        print("\n=== {0} ===".format(watch_id))
        print("min {0:.0f} / avg {1:.0f} / max {2:.0f} {3}   trend {4}".format(
            min(prices), sum(prices) / len(prices), max(prices),
            rows[0]["currency"], store_mod.sparkline(series)))
        for row in rows:
            detail = json.loads(row["detail_json"])
            best = detail.get("best") or {}
            route = " | ".join(leg["path"] for leg in best.get("itineraries", []))
            print("  {0}  {1:>9.0f} {2}  {3}".format(
                row["captured_at"], row["best_price"], row["currency"], route))
    store.close()
    return 0


def airports(config, args):
    """IATA lookup. Only the Amadeus backend exposes a location endpoint."""
    provider = providers.build("amadeus", {
        "environment": config["provider"].get("environment", "test"),
        "token_cache": config["provider"].get(
            "token_cache", os.path.join(config["state_dir"], ".token.json")),
    })
    payload = provider.search_locations(args.keyword)
    rows = payload.get("data", [])
    if not rows:
        print("No match for '{0}'".format(args.keyword))
        return 1
    print("{0:<6} {1:<12} {2:<28} {3}".format("CODE", "TYPE", "NAME", "CITY/COUNTRY"))
    for row in rows:
        address = row.get("address", {})
        print("{0:<6} {1:<12} {2:<28} {3}".format(
            row.get("iataCode", "-"), row.get("subType", "-"),
            (row.get("name") or "")[:28],
            "{0}/{1}".format(address.get("cityName", "-"),
                             address.get("countryCode", "-"))))
    return 0


def _make_logger(verbose):
    if not verbose:
        return lambda msg: None

    def _log(msg):
        print("  [{0}] {1}".format(
            datetime.datetime.now().strftime("%H:%M:%S"), msg), file=sys.stderr)
    return _log


def main(argv=None):
    # --config/--verbose live on every subcommand so that both
    # `run --config x -v` and the reverse order work.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", required=True, help="path to the watch config")
    common.add_argument("-v", "--verbose", action="store_true")

    parser = argparse.ArgumentParser(
        prog="fare_watch.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", parents=[common],
                               help="inspect fares and report")
    run_parser.add_argument("--watch", action="append",
                            help="limit to this watch id (repeatable)")
    run_parser.add_argument("--format", choices=("markdown", "text", "json"),
                            help="console output format (default from config)")
    run_parser.add_argument("--quiet", action="store_true",
                            help="suppress console output")
    run_parser.add_argument("--email", action="store_true",
                            help="force an email for this run, even if disabled "
                                 "or nothing is actionable")
    run_parser.add_argument("--no-notify", action="store_true",
                            help="skip outbound sinks (email, DingTalk); still "
                                 "prints and writes the report")
    run_parser.add_argument("--mock", metavar="FIXTURE",
                            help="replay a fixture instead of calling Amadeus")
    run_parser.add_argument("--mock-jitter", type=float, default=0.0,
                            help="percent price drift in mock mode, to test alerts")

    validate_parser = sub.add_parser(
        "validate", parents=[common],
        help="check config and build request bodies without any API call")
    validate_parser.add_argument("--watch", action="append")
    validate_parser.add_argument("--show-body", action="store_true")

    history_parser = sub.add_parser("history", parents=[common],
                                    help="show recorded price history")
    history_parser.add_argument("--watch", action="append")
    history_parser.add_argument("--limit", type=int, default=20)

    airports_parser = sub.add_parser("airports", parents=[common],
                                     help="look up IATA codes (needs Amadeus creds)")
    airports_parser.add_argument("--keyword", required=True)

    sub.add_parser("providers", parents=[common],
                   help="show the backend capability matrix")

    args = parser.parse_args(argv)
    config = load_config(args.config)

    try:
        if args.command == "run":
            return run(config, args)
        if args.command == "validate":
            return validate(config, args)
        if args.command == "history":
            return history(config, args)
        if args.command == "airports":
            return airports(config, args)
        if args.command == "providers":
            return show_providers(config, args)
    except (provider_base.ProviderError, provider_base.CapabilityError) as exc:
        # Setup problems (missing credentials, unsupported filters) are routine.
        print("error: {0}".format(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    return 1


if __name__ == "__main__":
    sys.exit(main())
