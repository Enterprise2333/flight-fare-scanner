"""Visa/transit verification cache used by travel-decision reports.

This module intentionally never decides whether a traveller may enter or
transit a country. Interactive workflows record a conclusion only after an
agent has checked an official immigration, foreign-affairs, or consular source.
Scheduled jobs only read that cached evidence and label it non-real-time.
"""

import datetime
import json
import os

# Small, auditable airport-country map for report context. Unknown codes remain
# unknown rather than being guessed. Extend it only after verifying the airport.
AIRPORT_COUNTRIES = {
    "HKG": ("HK", "中国香港"), "CAN": ("CN", "中国"), "SZX": ("CN", "中国"),
    "PVG": ("CN", "中国"), "SHA": ("CN", "中国"), "PEK": ("CN", "中国"),
    "ICN": ("KR", "韩国"), "GMP": ("KR", "韩国"), "NRT": ("JP", "日本"),
    "HND": ("JP", "日本"), "DOH": ("QA", "卡塔尔"), "DXB": ("AE", "阿联酋"),
    "AUH": ("AE", "阿联酋"), "IST": ("TR", "土耳其"), "LHR": ("GB", "英国"),
    "LGW": ("GB", "英国"), "CDG": ("FR", "法国"), "AMS": ("NL", "荷兰"),
    "FRA": ("DE", "德国"), "MUC": ("DE", "德国"), "MAD": ("ES", "西班牙"),
    "JFK": ("US", "美国"), "LAX": ("US", "美国"), "DFW": ("US", "美国"),
    "LIM": ("PE", "秘鲁"), "GIG": ("BR", "巴西"), "SCL": ("CL", "智利"),
    "EZE": ("AR", "阿根廷"), "AEP": ("AR", "阿根廷"), "LPB": ("BO", "玻利维亚"),
    "MVD": ("UY", "乌拉圭"), "BOG": ("CO", "哥伦比亚"), "MEX": ("MX", "墨西哥"),
    "YYZ": ("CA", "加拿大"), "YVR": ("CA", "加拿大"),
}


def _parse_time(value):
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def load_cache(path):
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return {"version": 1, "entries": []}
    try:
        with open(path, "r") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and isinstance(data.get("entries"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"version": 1, "entries": []}


def merge_entries(path, entries):
    """Persist verified entries supplied explicitly through configuration."""
    if not entries:
        return
    cache = load_cache(path)
    index = {}
    for entry in cache["entries"] + list(entries):
        key = (str(entry.get("passport_nationality", "")).upper(),
               str(entry.get("country_code", "")).upper(),
               str(entry.get("kind", "")).lower())
        index[key] = entry
    cache["entries"] = list(index.values())
    cache["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    directory = os.path.dirname(os.path.expanduser(path))
    if directory and not os.path.exists(directory):
        os.makedirs(directory)
    with open(os.path.expanduser(path), "w") as handle:
        json.dump(cache, handle, ensure_ascii=False, indent=2)


def _route_countries(offer):
    """Return ordered destination/transit countries represented by an offer."""
    targets = []
    transits = []
    for itinerary in offer.get("itineraries") or []:
        segments = itinerary.get("segments") or []
        for segment in segments[:-1]:
            code = segment.get("to")
            if code and code not in transits:
                transits.append(code)
        if segments:
            code = segments[-1].get("to")
            if code and code not in targets:
                targets.append(code)
    return targets, transits


def context_for_offer(offer, passport_nationality, cache_path, max_age_hours,
                      scheduled=False):
    """Build report context from verified cache entries; never infer eligibility."""
    passport = str(passport_nationality or "CN").upper()
    cache = load_cache(cache_path)
    entries = {}
    for entry in cache.get("entries", []):
        key = (str(entry.get("passport_nationality", "")).upper(),
               str(entry.get("country_code", "")).upper(),
               str(entry.get("kind", "")).lower())
        entries[key] = entry
    targets, transits = _route_countries(offer)
    now = datetime.datetime.now()
    checks = []
    for kind, airports in (("destination", targets), ("transit", transits)):
        for airport in airports:
            country = AIRPORT_COUNTRIES.get(airport)
            if not country:
                checks.append({"kind": kind, "airport": airport, "status": "unknown",
                               "summary": "机场国家未收录，未完成官方核验"})
                continue
            code, name = country
            entry = entries.get((passport, code, kind))
            if not entry:
                checks.append({"kind": kind, "airport": airport, "country_code": code,
                               "country_name": name, "status": "missing",
                               "summary": "未完成官方核验"})
                continue
            checked_at = _parse_time(entry.get("checked_at"))
            stale = not checked_at or (now - checked_at.replace(tzinfo=None)).total_seconds() > \
                int(max_age_hours) * 3600
            checks.append({"kind": kind, "airport": airport, "country_code": code,
                           "country_name": name, "status": "stale" if stale else "verified",
                           "summary": entry.get("summary") or "未提供结论摘要",
                           "source_url": entry.get("source_url"),
                           "checked_at": entry.get("checked_at"),
                           "needs_final_confirmation": True})
    return {"passport_nationality": passport, "mode": "cached_non_realtime"
            if scheduled else "cached", "checks": checks,
            "final_confirmation_required": True}
