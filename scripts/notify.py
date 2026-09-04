"""Notification sinks.

Four independent sinks, each switchable in config:

  console    render to stdout (default ON) - this is what gets relayed into a
             conversation, so it must be self-contained and readable as-is
  report     writes one PDF report plus a per-run JSON audit file
  email      SMTP, multipart text + HTML + optional PDF attachment
  dingtalk   optional robot webhook, off by default

Credentials for every sink come from environment variables, never the config
file, so the config stays safe to share.
"""

import base64
import datetime
import hashlib
import hmac
import json
import os
import smtplib
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage

import airport_transit

SEVERITY_MARK = {"deal": "🟢", "warn": "🔴", "info": "⚪"}
SEVERITY_WORD = {"deal": "DEAL", "warn": "RISE", "info": "base"}


class NotifyError(Exception):
    pass


# ------------------------------------------------------------------ helpers

def _fmt_money(value, currency):
    return "{0:,.0f} {1}".format(float(value), currency)


def is_actionable(result):
    """True when a result is worth pushing: an alert fired, or it failed."""
    return bool(result.get("error")) or any(
        t.severity != "info" for t in result["triggers"])


def select(results, only_on_alert):
    return [r for r in results if is_actionable(r)] if only_on_alert else results


def serialise(results):
    """Plain-data form of results, safe for json.dump."""
    out = []
    for result in results:
        item = {key: value for key, value in result.items() if key != "triggers"}
        item["triggers"] = [
            {"kind": t.kind, "severity": t.severity, "message": t.message}
            for t in result["triggers"]
        ]
        out.append(item)
    return out


def _counts(results):
    deals = sum(1 for r in results
                if any(t.severity == "deal" for t in r["triggers"]))
    failures = sum(1 for r in results if r.get("error"))
    return deals, failures


def build_subject(results, prefix="[Fare watch]"):
    """A subject line that is useful without opening the message."""
    deals, failures = _counts(results)
    cheapest = None
    for result in results:
        if result.get("error") or result.get("price") is None:
            continue
        if cheapest is None or result["price"] < cheapest["price"]:
            cheapest = result
    parts = []
    if deals:
        parts.append("{0} deal(s)".format(deals))
    if failures:
        parts.append("{0} failed".format(failures))
    if cheapest:
        parts.append("{0} {1}".format(
            cheapest.get("route", ""),
            _fmt_money(cheapest["price"], cheapest["currency"])))
    if not parts:
        parts.append("no change")
    return "{0} {1}".format(prefix, " · ".join(parts)).strip()


# ---------------------------------------------------------------- Markdown

def build_summary(results):
    deals, failures = _counts(results)
    return ("## ✈️ Fare watch — {0}\n\n{1} watch(es) · {2} deal signal(s) · "
            "{3} failure(s)\n".format(
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                len(results), deals, failures))


def _stops(leg):
    """Stop count, or '-' when the backend gave no routing to count."""
    value = leg.get("stops")
    return "-" if value is None else str(value)


def _offer_dates(offer):
    """Outbound (and return) date of an offer, short form.

    For a flexible-window search this is the actual answer - which dates are
    cheap - so it gets its own column rather than being buried in a timestamp.
    """
    extras = offer.get("extras") or {}
    journey = extras.get("journey_dates") or {}
    out = journey.get("departure_date") or extras.get("departure_date")
    back = journey.get("return_date") or extras.get("return_date")
    # Historic Deals audit files may predate provider-side date normalisation.
    # Their SerpApi hand-off URL is still authoritative and lets regenerated
    # PDFs show the actual outbound/return pair without another API call.
    deal_link = extras.get("serpapi_flight_link") or ""
    if deal_link and (not out or not back):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(deal_link).query)
        out = out or (query.get("outbound_date") or [None])[0]
        back = back or (query.get("return_date") or [None])[0]
    if not out:
        itineraries = offer.get("itineraries") or []
        if itineraries:
            out = (itineraries[0].get("depart_at") or "")[:10] or None
        if len(itineraries) > 1:
            back = (itineraries[-1].get("depart_at") or "")[:10] or None
    if not out:
        return "-"
    return "{0} → {1}".format(str(out)[:10], str(back)[:10]) if back else str(out)[:10]


def _offer_plan(offer, currency):
    """Display relative-deal details or hub/positioning composition metadata."""
    extras = offer.get("extras") or {}
    if extras.get("hub"):
        parts = ["Hub {0}".format(extras["hub"]),
                 "{0} PNR".format(extras.get("ticket_count", "?"))]
        if extras.get("main_price") is not None:
            parts.append("main {0}".format(_fmt_money(extras["main_price"], currency)))
        if extras.get("positioning_price"):
            parts.append("positioning {0}".format(
                _fmt_money(extras["positioning_price"], currency)))
        buffers = extras.get("connection_buffers") or {}
        if buffers:
            parts.append("ICN buffers {0}/{1}m".format(
                buffers.get("outbound_minutes", "-"), buffers.get("return_minutes", "-")))
        return " · ".join(parts)
    return _deal_summary(offer, currency)


def _deal_summary(offer, currency):
    """Short relative-deal metadata, or '-' for ordinary fixed-route offers."""
    extras = offer.get("extras") or {}
    destination = extras.get("deal_destination")
    if not destination:
        return "-"
    country = extras.get("deal_country") or "?"
    category = extras.get("deal_category") or "unclassified"
    discount = extras.get("discount_percentage")
    average = extras.get("average_price")
    parts = ["{0} ({1}, {2})".format(destination, country, category)]
    if discount is not None:
        parts.append("{0}% off".format(discount))
    if average is not None:
        parts.append("avg {0}".format(_fmt_money(average, currency)))
    return " · ".join(parts)


def _minutes_cn(minutes):
    if minutes is None:
        return "时间未返回"
    minutes = int(minutes)
    hours, remain = divmod(minutes, 60)
    return ("{0}小时{1}分".format(hours, remain) if hours else "{0}分".format(remain))


def _parse_datetime(value):
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _itinerary_cn(itinerary):
    """中文航段叙述：机场中文名、总时长与实际中转停留。"""
    segments = itinerary.get("segments") or []
    if not segments:
        return "航段信息未返回"
    path = [airport_transit.display_name(segments[0].get("from"))]
    layovers = []
    for index, segment in enumerate(segments):
        path.append(airport_transit.display_name(segment.get("to")))
        if index < len(segments) - 1:
            arrival = _parse_datetime(segment.get("arrive_at"))
            departure = _parse_datetime(segments[index + 1].get("depart_at"))
            gap = None if not arrival or not departure else int(
                (departure - arrival).total_seconds() // 60)
            layovers.append("{0} 实际停留{1}".format(
                airport_transit.display_name(segment.get("to")), _minutes_cn(gap)))
    total = itinerary.get("duration") or "未返回"
    connection = "直飞" if not layovers else "中转：" + "；".join(layovers)
    return " → ".join(path) + "（全程 " + total + "，" + connection + "）"


def _transit_guidance_cn(itinerary):
    """显示公开来源支持的保守预留建议，和实际停留并列而非替代。"""
    segments = itinerary.get("segments") or []
    guidance = []
    for index, segment in enumerate(segments[:-1]):
        airport = segment.get("to")
        guide = airport_transit.guide_for(airport)
        if not guide:
            guidance.append("{0}：未收录公开建议预留时间".format(
                airport_transit.display_name(airport)))
            continue
        arrival = _parse_datetime(segment.get("arrive_at"))
        departure = _parse_datetime(segments[index + 1].get("depart_at"))
        actual = None if not arrival or not departure else int(
            (departure - arrival).total_seconds() // 60)
        recommended = guide["recommended_minutes"]
        status = "满足建议" if actual is not None and actual >= recommended else "短于建议，偏紧"
        actual_text = _minutes_cn(actual)
        guidance.append("{0}：实际{1}；建议≥{2}（{3}）。{4} 来源：{5}".format(
            airport_transit.display_name(airport), actual_text,
            _minutes_cn(recommended), status, guide["reason"], guide["source_url"]))
    return "；".join(guidance) if guidance else "直飞，无中转预留要求"


def _baggage_cn(offer):
    summary = (offer.get("extras") or {}).get("baggage_summary") or {}
    text = summary.get("text")
    if text:
        return text
    values = []
    for itinerary in offer.get("itineraries") or []:
        for segment in itinerary.get("segments") or []:
            value = segment.get("checked_bags")
            if isinstance(value, dict):
                value = value.get("text")
            if value and value not in values:
                values.append(str(value))
    return " / ".join(values) if values else "未返回托运行李信息"


# Public lowest-economy references only. They are deliberately separate from
# provider-returned baggage because a ticket's fare brand and most-significant
# carrier can impose a different final allowance.
PUBLIC_LOWEST_BAGGAGE = {
    "QR": ("卡塔尔航空 Economy Lite：美洲航线公开参考为托运 2 件×23kg；手提 1 件≤7kg。",
           "https://www.qatarairways.com/en/baggage/allowance.html"),
    "LA": ("LATAM Basic：通常不含托运行李；不含 12kg 手提大件，仅保留小型随身物品额度。",
           "https://www.latamairlines.com/gb/en/experience/prepare-your-trip/baggage"),
    "IB": ("Iberia Basic：通常不含托运行李；手提 1 件≤10kg，另可带 1 件个人物品。",
           "https://www.iberia.com/gb/luggage/hand-luggage/"),
    "CX": ("国泰 Economy Light：托运 1 件≤23kg；手提 1 件≤7kg。",
           "https://www.cathaypacific.com/cx/en_HK/book-a-trip/book-flights/new-economy-fares.html"),
}


def _public_baggage_cn(offer):
    """Lowest published economy-tier reference, never a ticket entitlement."""
    codes = offer.get("validating_airlines") or []
    entries = []
    for code in codes:
        reference = PUBLIC_LOWEST_BAGGAGE.get(code)
        if reference:
            entries.append("{0} 来源：{1}".format(reference[0], reference[1]))
    if not entries:
        return "未收录该承运航司的最低经济舱公开行李参考"
    return "；".join(entries) + "。仅供参考，最终以出票页行李额度为准。"


def _visa_cn(offer):
    context = (offer.get("extras") or {}).get("visa_context") or {}
    checks = context.get("checks") or []
    if not checks:
        return "未完成官方签证/过境核验"
    items = []
    for item in checks:
        kind = "目的地" if item.get("kind") == "destination" else "过境地"
        place = item.get("country_name") or item.get("airport") or "未知地区"
        status = item.get("status")
        prefix = "已缓存" if status == "verified" else ("缓存已过期" if status == "stale" else "未核验")
        source = item.get("source_url")
        source_note = "；官方来源：{0}".format(source) if source else ""
        items.append("{0}{1}：{2}（{3}）{4}".format(
            kind, place, item.get("summary", "未核验"), prefix, source_note))
    suffix = "；须以官方最新规则和承运航司要求为准"
    if context.get("mode") == "cached_non_realtime":
        suffix += "（定时任务引用缓存，非实时）"
    return "；".join(items) + suffix


def _typical_suffix(result, currency):
    """Render the backend's typical price band, when it provides one."""
    band = result.get("typical_price_range") or []
    values = [v for v in band if isinstance(v, (int, float))]
    if len(values) >= 2:
        return " (typical {0}-{1})".format(
            _fmt_money(min(values), currency), _fmt_money(max(values), currency))
    if len(values) == 1:
        return " (typical ~{0})".format(_fmt_money(values[0], currency))
    return ""


def render_result_markdown(result, include_offers=3):
    lines = []
    marks = "".join(SEVERITY_MARK.get(t.severity, "") for t in result["triggers"])
    lines.append("### {0} {1}".format(marks, result["label"]))
    lines.append("")

    if result.get("error"):
        lines.append("> ❌ Inspection failed: {0}".format(result["error"]))
        lines.append("")
        return "\n".join(lines)

    currency = result["currency"]
    lines.append("- Strategy: `{0}`  Route: {1}".format(
        result["strategy"], result["route"]))
    lines.append("- Source: {0}{1}".format(
        result.get("source", result.get("provider", "-")),
        "  Captured: {0}".format(result["captured_at"])
        if result.get("captured_at") else ""))
    if result.get("airline_policy"):
        lines.append("- Airline policy: {0}".format(result["airline_policy"]))
    if result.get("connection_policy"):
        lines.append("- Connections: {0}".format(result["connection_policy"]))
    lines.append("- **Best price: {0}**{1}".format(
        _fmt_money(result["price"], currency),
        "  (target {0})".format(_fmt_money(result["target_price"], currency))
        if result.get("target_price") else ""))

    previous = result.get("previous_price")
    if previous is not None:
        delta = result["price"] - previous
        arrow = "▼" if delta < 0 else ("▲" if delta > 0 else "＝")
        lines.append("- Change: {0} {1} (was {2})".format(
            arrow, _fmt_money(abs(delta), currency), _fmt_money(previous, currency)))
    else:
        lines.append("- Change: first observation (baseline)")

    if result.get("hist_min") is not None:
        lines.append("- History low: {0}".format(
            _fmt_money(result["hist_min"], currency)))
    if result.get("price_level"):
        lines.append("- Market level: **{0}**{1}".format(
            result["price_level"], _typical_suffix(result, currency)))
    if result.get("trend"):
        lines.append("- Trend: `{0}`".format(result["trend"]))

    for trigger in result["triggers"]:
        lines.append("- {0} **{1}**: {2}".format(
            SEVERITY_MARK.get(trigger.severity, ""), trigger.kind, trigger.message))
    for note in result.get("notes", []):
        lines.append("- ⚠️ {0}".format(note))

    offers = result.get("offers") or []
    if offers and include_offers:
        lines.append("")
        lines.append("| 序号 | 价格 | 主程往返日期 | 航司 | 方案/低价 | 航段 | 总时长 | 中转次数 | 托运行李 |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for index, offer in enumerate(offers[:include_offers], start=1):
            legs = offer.get("itineraries", [])
            lines.append("| {0} | {1} | {2} | {3} | {4} | {5} | {6} | {7} | {8} |".format(
                index,
                _fmt_money(offer["price"], offer.get("currency", currency)),
                _offer_dates(offer),
                ",".join(offer.get("validating_airlines") or []) or "-",
                _offer_plan(offer, offer.get("currency", currency)),
                "<br>".join(_itinerary_cn(leg) for leg in legs) or "-",
                "<br>".join(leg["duration"] or "-" for leg in legs) or "-",
                "/".join(_stops(leg) for leg in legs) or "-",
                _baggage_cn(offer),
            ))
    lines.append("")
    return "\n".join(lines)


# -------------------------------------------------------------- plain text

def render_text(results, include_offers=3):
    """Terminal-friendly output with no Markdown syntax."""
    deals, failures = _counts(results)
    out = ["=" * 72,
           "FARE WATCH  {0}".format(datetime.datetime.now().strftime("%Y-%m-%d %H:%M")),
           "{0} watch(es), {1} deal signal(s), {2} failure(s)".format(
               len(results), deals, failures),
           "=" * 72]

    for result in results:
        out.append("")
        out.append("* {0}".format(result["label"]))
        if result.get("error"):
            out.append("    FAILED: {0}".format(result["error"]))
            continue

        currency = result["currency"]
        out.append("    {0}  [{1}]".format(result["route"], result["strategy"]))
        out.append("    source: {0}{1}".format(
            result.get("source", result.get("provider", "-")),
            " | captured {0}".format(result["captured_at"])
            if result.get("captured_at") else ""))
        if result.get("airline_policy"):
            out.append("    airline policy: {0}".format(result["airline_policy"]))
        if result.get("connection_policy"):
            out.append("    connections: {0}".format(result["connection_policy"]))
        line = "    best {0}".format(_fmt_money(result["price"], currency))
        previous = result.get("previous_price")
        if previous is not None:
            delta = result["price"] - previous
            sign = "-" if delta < 0 else ("+" if delta > 0 else "=")
            line += "   change {0}{1}".format(
                sign, _fmt_money(abs(delta), currency))
        else:
            line += "   (baseline)"
        if result.get("hist_min") is not None:
            line += "   low {0}".format(_fmt_money(result["hist_min"], currency))
        out.append(line)
        if result.get("price_level"):
            out.append("    market level: {0}{1}".format(
                result["price_level"], _typical_suffix(result, currency)))
        if result.get("trend"):
            out.append("    trend {0}".format(result["trend"]))

        for trigger in result["triggers"]:
            out.append("    [{0}] {1}".format(
                SEVERITY_WORD.get(trigger.severity, trigger.severity),
                trigger.message))
        for note in result.get("notes", []):
            out.append("    ! {0}".format(note))

        offers = result.get("offers") or []
        for index, offer in enumerate(offers[:include_offers], start=1):
            legs = offer.get("itineraries", [])
            out.append("      {0}. {1:>12}  {2:<13} {3:<6} {4}  {5}".format(
                index,
                _fmt_money(offer["price"], offer.get("currency", currency)),
                _offer_dates(offer),
                ",".join(offer.get("validating_airlines") or []) or "-",
                " | ".join("{0} {1} ({2} stop)".format(
                    leg["path"], leg["duration"] or "-", _stops(leg))
                    for leg in legs),
                _offer_plan(offer, offer.get("currency", currency))))
    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------- HTML mail

_HTML_STYLE = """
body{font:14px/1.5 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#222}
h2{margin:0 0 4px}h3{margin:20px 0 6px}
table{border-collapse:collapse;margin:8px 0;font-size:13px}
th,td{border:1px solid #ddd;padding:5px 9px;text-align:left}
th{background:#f4f6f8}
.deal{color:#0a7d32;font-weight:bold}.warn{color:#c0392b;font-weight:bold}
.muted{color:#777}.note{color:#8a6d3b}
.price{font-size:17px;font-weight:bold}
"""


def _esc(value):
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def render_html(results, include_offers=5):
    deals, failures = _counts(results)
    parts = ["<html><head><meta charset='utf-8'><style>", _HTML_STYLE,
             "</style></head><body>",
             "<h2>✈️ Fare watch</h2>",
             "<p class='muted'>{0} &middot; {1} watch(es), {2} deal signal(s), "
             "{3} failure(s)</p>".format(
                 datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                 len(results), deals, failures)]

    for result in results:
        parts.append("<h3>{0}</h3>".format(_esc(result["label"])))
        if result.get("error"):
            parts.append("<p class='warn'>Inspection failed: {0}</p>".format(
                _esc(result["error"])))
            continue

        currency = result["currency"]
        parts.append("<p class='muted'>{0} &middot; <code>{1}</code></p>".format(
            _esc(result["route"]), _esc(result["strategy"])))
        source = result.get("source", result.get("provider", "-"))
        captured = result.get("captured_at")
        parts.append("<p class='muted'>source: {0}{1}</p>".format(
            _esc(source),
            " &middot; captured " + _esc(captured) if captured else ""))
        if result.get("airline_policy"):
            parts.append("<p class='muted'>airline policy: {0}</p>".format(
                _esc(result["airline_policy"])))
        if result.get("connection_policy"):
            parts.append("<p class='muted'>connections: {0}</p>".format(
                _esc(result["connection_policy"])))
        parts.append("<p><span class='price'>{0}</span>".format(
            _esc(_fmt_money(result["price"], currency))))
        previous = result.get("previous_price")
        if previous is not None:
            delta = result["price"] - previous
            cls = "deal" if delta < 0 else ("warn" if delta > 0 else "muted")
            arrow = "&#9660;" if delta < 0 else ("&#9650;" if delta > 0 else "=")
            parts.append(" <span class='{0}'>{1} {2}</span> "
                         "<span class='muted'>(was {3})</span>".format(
                             cls, arrow, _esc(_fmt_money(abs(delta), currency)),
                             _esc(_fmt_money(previous, currency))))
        else:
            parts.append(" <span class='muted'>(baseline)</span>")
        parts.append("</p>")

        if result.get("hist_min") is not None or result.get("trend") \
                or result.get("price_level"):
            bits = []
            if result.get("hist_min") is not None:
                bits.append("history low {0}".format(
                    _esc(_fmt_money(result["hist_min"], currency))))
            if result.get("price_level"):
                bits.append("market level <b>{0}</b>{1}".format(
                    _esc(result["price_level"]),
                    _esc(_typical_suffix(result, currency))))
            if result.get("trend"):
                bits.append("trend <code>{0}</code>".format(_esc(result["trend"])))
            parts.append("<p class='muted'>{0}</p>".format(" &middot; ".join(bits)))

        for trigger in result["triggers"]:
            cls = {"deal": "deal", "warn": "warn"}.get(trigger.severity, "muted")
            parts.append("<p class='{0}'>{1}: {2}</p>".format(
                cls, _esc(trigger.kind), _esc(trigger.message)))
        for note in result.get("notes", []):
            parts.append("<p class='note'>&#9888; {0}</p>".format(_esc(note)))

        offers = result.get("offers") or []
        if offers and include_offers:
            parts.append("<table><tr><th>#</th><th>Price</th><th>Main trip dates</th>"
                         "<th>Airline</th><th>Plan / deal</th><th>Routing</th><th>Duration</th>"
                         "<th>Stops</th></tr>")
            for index, offer in enumerate(offers[:include_offers], start=1):
                legs = offer.get("itineraries", [])
                parts.append(
                    "<tr><td>{0}</td><td>{1}</td><td>{2}</td><td>{3}</td>"
                    "<td>{4}</td><td>{5}</td><td>{6}</td><td>{7}</td></tr>".format(
                        index,
                        _esc(_fmt_money(offer["price"],
                                        offer.get("currency", currency))),
                        _esc(_offer_dates(offer)),
                        _esc(",".join(offer.get("validating_airlines") or []) or "-"),
                        _esc(_offer_plan(offer, offer.get("currency", currency))),
                        "<br>".join(_esc(leg["path"]) for leg in legs) or "-",
                        "<br>".join(_esc(leg["duration"] or "-") for leg in legs) or "-",
                        _esc("/".join(_stops(leg) for leg in legs) or "-")))
            parts.append("</table>")

    parts.append("<p class='muted'>Prices are indicative. Confirm on the airline "
                 "or agency site before booking.</p></body></html>")
    return "".join(parts)


# -------------------------------------------------------------- console sink

def render_console(results, fmt="markdown", include_offers=3):
    if fmt == "json":
        return json.dumps(
            {"generated_at": datetime.datetime.now().isoformat(),
             "results": serialise(results)}, ensure_ascii=False, indent=2)
    if fmt == "text":
        return render_text(results, include_offers=include_offers)
    body = build_summary(results) + "\n"
    body += "\n".join(render_result_markdown(r, include_offers) for r in results)
    return body


# ---------------------------------------------------------------- email sink

def send_email(config, results, attachment_path=None):
    """Send the run as a multipart text+HTML message with an optional PDF."""
    host = config.get("host") or os.environ.get(
        config.get("host_env", "SMTP_HOST"), "").strip()
    if not host:
        raise NotifyError(
            "email is enabled but no SMTP host is configured. Set notify.email.host "
            "or export ${0}.".format(config.get("host_env", "SMTP_HOST")))

    recipients = config.get("to") or []
    if isinstance(recipients, str):
        recipients = [recipients]
    recipients = [r.strip() for r in recipients if str(r).strip()]
    if not recipients:
        raise NotifyError("email is enabled but notify.email.to is empty.")

    user = os.environ.get(config.get("user_env", "SMTP_USER"), "").strip()
    password = os.environ.get(config.get("password_env", "SMTP_PASSWORD"), "").strip()
    sender = config.get("from") or user
    if not sender:
        raise NotifyError(
            "email needs a sender: set notify.email.from or export ${0}.".format(
                config.get("user_env", "SMTP_USER")))

    security = str(config.get("security", "starttls")).lower()
    if security not in ("starttls", "ssl", "none"):
        raise NotifyError("notify.email.security must be starttls, ssl or none")
    port = int(config.get("port") or (465 if security == "ssl" else 587))

    message = EmailMessage()
    message["Subject"] = build_subject(
        results, config.get("subject_prefix", "[Fare watch]"))
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(render_text(results))
    message.add_alternative(render_html(results), subtype="html")
    if attachment_path:
        try:
            with open(attachment_path, "rb") as handle:
                message.add_attachment(
                    handle.read(), maintype="application", subtype="pdf",
                    filename=os.path.basename(attachment_path))
        except OSError as exc:
            raise NotifyError("Could not attach PDF report: {0}".format(exc))

    try:
        if security == "ssl":
            context = ssl.create_default_context()
            server = smtplib.SMTP_SSL(host, port, timeout=30, context=context)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
        with server:
            server.ehlo()
            if security == "starttls":
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            if user and password:
                server.login(user, password)
            server.send_message(message, from_addr=sender, to_addrs=recipients)
    except smtplib.SMTPAuthenticationError as exc:
        raise NotifyError(
            "SMTP authentication failed ({0}). Most providers reject your normal "
            "account password here - generate an app-specific password and put it "
            "in ${1}.".format(exc.smtp_code,
                              config.get("password_env", "SMTP_PASSWORD")))
    except smtplib.SMTPException as exc:
        raise NotifyError("SMTP error: {0}".format(exc))
    except (OSError, ssl.SSLError) as exc:
        raise NotifyError(
            "Could not reach {0}:{1} ({2}). Check the host/port and whether "
            "security should be 'ssl' (465) instead of 'starttls' (587).".format(
                host, port, exc))
    return recipients


# ------------------------------------------------------------- DingTalk sink

def _sign(webhook, secret):
    """DingTalk custom-robot signing: HMAC-SHA256 over "<timestamp>\\n<secret>"."""
    timestamp = str(round(time.time() * 1000))
    payload = "{0}\n{1}".format(timestamp, secret)
    digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"),
                      hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest))
    separator = "&" if "?" in webhook else "?"
    return "{0}{1}timestamp={2}&sign={3}".format(webhook, separator, timestamp, sign)


def send_dingtalk(config, title, markdown, at_mobiles=None):
    webhook = os.environ.get(config.get("webhook_env", "DINGTALK_WEBHOOK"), "").strip()
    if not webhook:
        raise NotifyError(
            "DingTalk is enabled but ${0} is empty. Export the robot webhook or "
            "set notify.dingtalk.enabled=false.".format(
                config.get("webhook_env", "DINGTALK_WEBHOOK")))

    secret = os.environ.get(config.get("secret_env", "DINGTALK_SECRET"), "").strip()
    url = _sign(webhook, secret) if secret else webhook

    at_mobiles = at_mobiles or config.get("at_mobiles") or []
    text = markdown
    if at_mobiles:
        # DingTalk only renders an @ if the mobile also appears in the body.
        text += "\n\n" + " ".join("@" + str(m) for m in at_mobiles)

    body = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": text},
        "at": {"atMobiles": [str(m) for m in at_mobiles],
               "isAtAll": bool(config.get("at_all", False))},
    }
    request = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise NotifyError("DingTalk HTTP {0}: {1}".format(
            exc.code, exc.read().decode("utf-8", "replace")[:300]))
    except urllib.error.URLError as exc:
        raise NotifyError("DingTalk network error: {0}".format(exc.reason))

    if payload.get("errcode") != 0:
        hint = ""
        if payload.get("errcode") == 310000:
            hint = (" Hint: the robot's security setting rejected this message - "
                    "the keyword must appear in the text, or signing/IP allowlist "
                    "must match.")
        raise NotifyError("DingTalk rejected the message: {0}{1}".format(payload, hint))
    return payload


# --------------------------------------------------------------- report sink

def _pdf_escape(value):
    """Escape text before placing it in ReportLab Paragraph markup."""
    return (str(value or "-").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _write_pdf_report(path, results, include_offers):
    """Render a Chinese travel-decision PDF with candidate cards."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.platypus import (KeepTogether, Paragraph, SimpleDocTemplate,
                                        Spacer, Table, TableStyle)
    except ImportError as exc:
        raise NotifyError("PDF reports require ReportLab. Install it with `pip3 install "
                          "reportlab` ({0}).".format(exc))

    font_name = "STSong-Light"
    try:
        pdfmetrics.registerFont(UnicodeCIDFont(font_name))
    except Exception as exc:
        raise NotifyError("Could not initialise the PDF Chinese font: {0}".format(exc))

    styles = getSampleStyleSheet()
    title = ParagraphStyle("FareTitle", parent=styles["Title"], fontName=font_name,
                           fontSize=18, leading=24, textColor=colors.HexColor("#163A5F"),
                           spaceAfter=6)
    heading = ParagraphStyle("FareHeading", parent=styles["Heading2"], fontName=font_name,
                             fontSize=13, leading=18, textColor=colors.HexColor("#163A5F"),
                             spaceBefore=10, spaceAfter=4)
    card_title = ParagraphStyle("FareCardTitle", parent=styles["Heading3"],
                                fontName=font_name, fontSize=11, leading=15,
                                textColor=colors.HexColor("#163A5F"), spaceAfter=3)
    body = ParagraphStyle("FareBody", parent=styles["BodyText"], fontName=font_name,
                          fontSize=8.5, leading=12)
    small = ParagraphStyle("FareSmall", parent=body, fontSize=7.5, leading=10)
    notice = ParagraphStyle("FareNotice", parent=small, textColor=colors.HexColor("#8A4B08"))
    doc = SimpleDocTemplate(path, pagesize=A4, leftMargin=13 * mm,
                            rightMargin=13 * mm, topMargin=12 * mm,
                            bottomMargin=12 * mm, title="机票旅行决策报告")

    deals, failures = _counts(results)
    story = [Paragraph("机票旅行决策报告", title),
             Paragraph("生成时间：{0}　巡检项目：{1}　低价信号：{2}　失败：{3}".format(
                 datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 len(results), deals, failures), body),
             Paragraph("行李、签证及过境信息仅供出行前核对；请以承运航司与官方最新规则为准。", notice),
             Spacer(1, 5)]

    for result in results:
        story.append(Paragraph(_pdf_escape(result.get("label")), heading))
        if result.get("error"):
            story.append(Paragraph("巡检失败：{0}".format(_pdf_escape(result["error"])), body))
            continue
        summary_rows = [
            ("数据源", result.get("source", result.get("provider", "-"))),
            ("检索时间", result.get("captured_at", "-")),
            ("查询路线", result.get("route", "-")),
            ("当前最低价", _fmt_money(result["price"], result["currency"])),
            ("航司筛选", result.get("airline_policy", "-")),
            ("中转限制", result.get("connection_policy", "-")),
        ]
        info = Table([[Paragraph(_pdf_escape(k), small), Paragraph(_pdf_escape(v), small)]
                      for k, v in summary_rows], colWidths=[27 * mm, 157 * mm])
        info.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#EAF2F8")),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#C8D6E5")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.extend([info, Spacer(1, 4)])
        for trigger in result.get("triggers", []):
            story.append(Paragraph("价格信号：{0}".format(
                _pdf_escape(trigger.message)), notice))

        for index, offer in enumerate((result.get("offers") or [])[:include_offers], 1):
            extras = offer.get("extras") or {}
            heading_text = "候选 #{0}　{1}　主程：{2}".format(
                index, _fmt_money(offer["price"], offer.get("currency", result["currency"])),
                _offer_dates(offer))
            meta = [
                ("航司", ", ".join(offer.get("validating_airlines") or []) or "未返回"),
                ("报价返回的托运行李", _baggage_cn(offer)),
                ("最低经济舱公开参考", _public_baggage_cn(offer)),
                ("方案", _offer_plan(offer, offer.get("currency", result["currency"]))),
                ("PNR/接驳", "{0} 张独立票".format(extras.get("ticket_count"))
                 if extras.get("ticket_count") else "主票候选"),
            ]
            card = [Paragraph(_pdf_escape(heading_text), card_title)]
            card.append(Table([[Paragraph(_pdf_escape(k), small), Paragraph(_pdf_escape(v), small)]
                               for k, v in meta], colWidths=[27 * mm, 157 * mm],
                              style=TableStyle([
                                  ("FONTNAME", (0, 0), (-1, -1), font_name),
                                  ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F8F9F9")),
                                  ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D5DBDB")),
                                  ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                  ("LEFTPADDING", (0, 0), (-1, -1), 4),
                                  ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                                  ("TOPPADDING", (0, 0), (-1, -1), 3),
                                  ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                              ])))
            for leg_index, itinerary in enumerate(offer.get("itineraries") or [], 1):
                label = "航段 {0}".format(leg_index)
                card.append(Paragraph("{0}：{1}".format(label, _pdf_escape(_itinerary_cn(itinerary))), body))
                card.append(Paragraph("中转预留建议：{0}".format(
                    _pdf_escape(_transit_guidance_cn(itinerary))), notice))
            card.append(Paragraph("签证与过境：{0}".format(_pdf_escape(_visa_cn(offer))), notice))
            if extras.get("risk"):
                card.append(Paragraph("风险提示：{0}".format(_pdf_escape(extras["risk"])), notice))
            link = extras.get("flight_link") or offer.get("booking_link")
            if link:
                card.append(Paragraph("预订链接：{0}".format(_pdf_escape(link)), small))
            container = Table([[item] for item in card], colWidths=[184 * mm],
                              style=TableStyle([
                                  ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                                  ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#AAB7B8")),
                                  ("LEFTPADDING", (0, 0), (-1, -1), 6),
                                  ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                                  ("TOPPADDING", (0, 0), (-1, -1), 4),
                                  ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                              ]))
            story.extend([KeepTogether([container, Spacer(1, 5)])])
        story.append(Spacer(1, 5))

    story.append(Paragraph("价格为检索时的指示价。签证、过境、行李、回程航段和退改规则均须在出行/出票前再次核对。", notice))
    doc.build(story)


def write_report(directory, results, include_offers=10):
    """Write one self-contained PDF report and its per-run JSON audit file."""
    directory = os.path.expanduser(directory)
    os.makedirs(directory, exist_ok=True)
    today = datetime.date.today().isoformat()
    stamp = datetime.datetime.now().strftime("%H%M%S")
    pdf_path = os.path.join(directory, "fare-watch-{0}-{1}.pdf".format(today, stamp))
    json_path = os.path.join(directory, "fare-watch-{0}-{1}.json".format(today, stamp))

    with open(json_path, "w") as handle:
        json.dump({"generated_at": datetime.datetime.now().isoformat(),
                   "results": serialise(results)}, handle,
                  ensure_ascii=False, indent=2)
    _write_pdf_report(pdf_path, results, int(include_offers))
    return pdf_path, json_path
