# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
PadiGuard - a multi-insurer marketplace for parametric rainfall crop insurance.

This is the contract behind the PadiGuard web dApp.

  * Insurer pools: anyone can open a pool with its own pricing parameters and
    an optional coverage region (lat/lon bounding box), fund it and withdraw
    unreserved liquidity. Each pool keeps its own balance and reserve.
  * Risk-priced quotes from rainfall history: request_quote() asks validators
    to fetch the last 5 years of daily rainfall for the same location and
    calendar window (Open-Meteo ERA5 archive) and to agree on what each of
    those years would have paid under the requested terms. The premium rate is
    max(pool.min_rate, expected_payout_fraction + pool.loading). The quote,
    with its per-year evidence, is stored on-chain and is what buy_policy()
    charges.
  * Tiered terms (milestone v1): drought and flood triggers can have a second,
    severe level. Crossing the trigger pays `partial_bps` of the payout,
    crossing the severe level pays 100 %. Untiered terms behave as before.
  * Settlement (milestone v1): after the window closes anyone calls
    settle(policy_id); validators re-fetch daily precipitation from THREE
    sources (Open-Meteo ERA5, NASA POWER, NOAA CPC gauge analysis) and the
    outcome is decided when at least 2 complete sources put the policy in the
    same tier. The validator check binds every field that drives money.
  * dry_run_settlement() (milestone v1) runs the same settlement consensus on a
    past window without a policy or any money, and stores the agreed evidence.
  * Read views for a UI: stats, paginated pool and policy lists, per-farmer
    policy and quote lookups, per-pool policy lists, dry-run records.

Units:
  * coordinates: micro-degrees (int), e.g. -7.327400 deg -> -7327400
  * rainfall: hundredths of a millimetre (int), e.g. 48.40 mm -> 4840
  * fractions: basis points (int), 10000 = 100 %
  * money: wei of native GEN (int / u256)
"""

import json
import datetime
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from genlayer import *


# --------------------------------------------------------------------------
# Constants (all integers)
# --------------------------------------------------------------------------

SOURCE_OPEN_METEO = "open-meteo"
SOURCE_NASA_POWER = "nasa-power"
SOURCE_NOAA_CPC = "noaa-cpc"  # new in v1: third settlement source
SOURCE_NAMES = [SOURCE_OPEN_METEO, SOURCE_NASA_POWER, SOURCE_NOAA_CPC]
MIN_AGREEING_SOURCES = 2  # v1: 2-of-3 rule

# Leader vs validator, SAME source (data revisions / rounding):
#   values agree if |a - b| <= max(VALIDATOR_TOL_ABS_MM100, VALIDATOR_TOL_PCT% of max(a, b))
VALIDATOR_TOL_ABS_MM100 = 200  # 2.00 mm
VALIDATOR_TOL_PCT = 5  # 5 %

# NOAA CPC Unified Gauge-Based Analysis of Global Daily Precipitation (V1.0 RT),
# served by NOAA PSL THREDDS as one file per year on a 0.5 degree grid
# (lat 89.75 .. -89.75, lon 0.25 .. 359.75, time = hours since 1900-01-01).
CPC_BASE_URL = "https://psl.noaa.gov/thredds/dodsC/Datasets/cpc_global_precip/precip."
CPC_CELL_MICRO = 500000  # 0.5 degree
CPC_EPOCH = datetime.date(1900, 1, 1)

SETTLEMENT_DELAY_DAYS = 7  # data latency after end_date before settle() opens
CLAIM_WINDOW_DAYS = 60  # settle() allowed for this many days after it opens
MIN_LEAD_DAYS = 1  # coverage must start at least 1 day after purchase
MAX_LEAD_DAYS = 365  # and at most 1 year ahead
MAX_WINDOW_DAYS = 184  # longest coverage window (~ one growing season)
BPS = 10000

# Quotes
QUOTE_YEARS = 5  # years of history used to price a quote
QUOTE_HISTORY_LAG_DAYS = 7  # a history year is usable once its window ended this long ago
QUOTE_VALID_DAYS = 3  # a quote can be used to buy for this many days
MAX_POOL_NAME = 64

STATUS_ACTIVE = "ACTIVE"
STATUS_PAID_OUT = "PAID_OUT"
STATUS_SETTLED_NO_PAYOUT = "SETTLED_NO_PAYOUT"
STATUS_CANCELLED = "CANCELLED"
STATUS_EXPIRED_REFUNDED = "EXPIRED_REFUNDED"

DECISION_PAYOUT = "PAYOUT"
DECISION_NO_PAYOUT = "NO_PAYOUT"
DECISION_DEFER = "DEFER"

# Per-component outcomes (v1: tiers)
DISABLED = "DISABLED"
UNDECIDED = "UNDECIDED"
NOT_TRIGGERED = "NOT_TRIGGERED"
PARTIAL = "PARTIAL"  # outer trigger crossed, severe level not crossed (tiered terms only)
FULL = "FULL"  # severe level crossed, or the single trigger of untiered terms

TIER_DISABLED = -1
TIER_NO_DATA = -2  # source not complete: it does not vote
TIER_NONE = 0
TIER_PARTIAL = 1
TIER_FULL = 2
TIER_OUTCOME = {TIER_NONE: NOT_TRIGGERED, TIER_PARTIAL: PARTIAL, TIER_FULL: FULL}

REASON_NONE = ""
REASON_SOURCE_MISSING = "SOURCE_MISSING"
REASON_SOURCES_DIVERGE = "SOURCES_DIVERGE"

SRC_OK = "ok"
SRC_INCOMPLETE = "incomplete"
SRC_UNAVAILABLE = "unavailable"

MAX_PAGE = 50


# --------------------------------------------------------------------------
# Pure helpers: dates, URLs, parsing
# --------------------------------------------------------------------------


def _parse_date(s: str) -> datetime.date:
    if not isinstance(s, str) or len(s) != 10 or s[4] != "-" or s[7] != "-":
        raise gl.vm.UserError("Invalid date format, expected YYYY-MM-DD")
    try:
        d = datetime.date.fromisoformat(s)
    except ValueError:
        raise gl.vm.UserError("Invalid date format, expected YYYY-MM-DD")
    if d.isoformat() != s:
        raise gl.vm.UserError("Invalid date format, expected YYYY-MM-DD")
    return d


def _window_dates(start_date: str, end_date: str) -> list:
    start = datetime.date.fromisoformat(start_date)
    end = datetime.date.fromisoformat(end_date)
    out = []
    d = start
    while d <= end:
        out.append(d.isoformat())
        d = d + datetime.timedelta(days=1)
    return out


def _micro_to_str(v: int) -> str:
    """Format micro-degrees as a fixed 6-decimal string without floats."""
    sign = "-" if v < 0 else ""
    a = -v if v < 0 else v
    return sign + str(a // 1000000) + "." + str(a % 1000000).rjust(6, "0")


def _open_meteo_url(lat_micro: int, lon_micro: int, start_date: str, end_date: str) -> str:
    return (
        "https://archive-api.open-meteo.com/v1/archive"
        + "?latitude=" + _micro_to_str(lat_micro)
        + "&longitude=" + _micro_to_str(lon_micro)
        + "&start_date=" + start_date
        + "&end_date=" + end_date
        + "&daily=precipitation_sum&timezone=auto"
    )


def _nasa_power_url(lat_micro: int, lon_micro: int, start_date: str, end_date: str) -> str:
    return (
        "https://power.larc.nasa.gov/api/temporal/daily/point"
        + "?parameters=PRECTOTCORR&community=AG"
        + "&longitude=" + _micro_to_str(lon_micro)
        + "&latitude=" + _micro_to_str(lat_micro)
        + "&start=" + start_date.replace("-", "")
        + "&end=" + end_date.replace("-", "")
        + "&format=JSON"
    )


def _cpc_cell(lat_micro: int, lon_micro: int) -> list:
    """[lat_index, lon_index] of the 0.5 degree CPC grid cell containing the point."""
    iy = (90000000 - lat_micro) // CPC_CELL_MICRO
    if iy < 0:
        iy = 0
    if iy > 359:
        iy = 359
    lon360 = lon_micro % 360000000
    ix = lon360 // CPC_CELL_MICRO
    if ix > 719:
        ix = 719
    return [iy, ix]


def _cpc_cell_center(lat_micro: int, lon_micro: int) -> list:
    """Cell centre in micro-degrees as the file reports it ([lat, lon 0..360))."""
    cell = _cpc_cell(lat_micro, lon_micro)
    return [89750000 - CPC_CELL_MICRO * cell[0], 250000 + CPC_CELL_MICRO * cell[1]]


def _cpc_segments(start_date: str, end_date: str) -> list:
    """Split a window by calendar year (CPC has one file per year).
    Returns [[year, first_index, last_index, seg_start, seg_end], ...]."""
    start = datetime.date.fromisoformat(start_date)
    end = datetime.date.fromisoformat(end_date)
    out = []
    s = start
    while s <= end:
        year_end = datetime.date(s.year, 12, 31)
        e = end if end <= year_end else year_end
        jan1 = datetime.date(s.year, 1, 1)
        out.append([s.year, (s - jan1).days, (e - jan1).days, s.isoformat(), e.isoformat()])
        s = e + datetime.timedelta(days=1)
    return out


def _cpc_url(lat_micro: int, lon_micro: int, year: int, t0: int, t1: int) -> str:
    cell = _cpc_cell(lat_micro, lon_micro)
    iy = str(cell[0])
    ix = str(cell[1])
    return (
        CPC_BASE_URL + str(year) + ".nc.ascii?precip"
        + "%5B" + str(t0) + ":1:" + str(t1) + "%5D"
        + "%5B" + iy + ":1:" + iy + "%5D"
        + "%5B" + ix + ":1:" + ix + "%5D"
    )


def _cpc_urls(lat_micro: int, lon_micro: int, start_date: str, end_date: str) -> list:
    return [_cpc_url(lat_micro, lon_micro, s[0], s[1], s[2]) for s in _cpc_segments(start_date, end_date)]


def _to_mm100(v) -> int:
    """mm (Decimal/int from JSON) -> integer hundredths of mm. -1 means missing."""
    if v is None or isinstance(v, bool):
        return -1
    if isinstance(v, int):
        d = Decimal(v)
    elif isinstance(v, Decimal):
        d = v
    else:
        return -1
    if d < 0:  # NASA POWER fill value is -999, CPC missing value is -9.96921E36
        return -1
    return int((d * 100).to_integral_value(rounding=ROUND_HALF_UP))


def _summarize(name: str, values: list, expected_days: int) -> dict:
    """values: list of mm100 ints (-1 = missing), one per expected date."""
    valid = [x for x in values if x >= 0]
    days = len(valid)
    total = sum(valid) if days > 0 else 0
    mx = max(valid) if days > 0 else 0
    status = SRC_OK if (days == expected_days and len(values) == expected_days) else SRC_INCOMPLETE
    return {"name": name, "status": status, "days": days, "total_mm100": total, "max_mm100": mx}


def _unavailable(name: str) -> dict:
    return {"name": name, "status": SRC_UNAVAILABLE, "days": 0, "total_mm100": 0, "max_mm100": 0}


def _parse_open_meteo(body: bytes, dates: list) -> dict:
    data = json.loads(body.decode("utf-8"), parse_float=Decimal)
    daily = data["daily"]
    times = daily["time"]
    precip = daily["precipitation_sum"]
    if list(times) != dates or len(precip) != len(dates):
        # wrong window / shape: treat as incomplete, count nothing
        return _summarize(SOURCE_OPEN_METEO, [-1] * len(dates), len(dates))
    return _summarize(SOURCE_OPEN_METEO, [_to_mm100(x) for x in precip], len(dates))


def _parse_nasa_power(body: bytes, dates: list) -> dict:
    data = json.loads(body.decode("utf-8"), parse_float=Decimal)
    series = data["properties"]["parameter"]["PRECTOTCORR"]
    values = []
    for d in dates:
        key = d.replace("-", "")
        values.append(_to_mm100(series.get(key)) if key in series else -1)
    return _summarize(SOURCE_NASA_POWER, values, len(dates))


def _decimal_or_none(s: str):
    try:
        return Decimal(s.strip())
    except Exception:
        return None


def _parse_cpc_segment(body: bytes, seg_dates: list, lat_micro: int, lon_micro: int) -> list:
    """Parse one OPeNDAP ASCII response for one grid cell. Returns one mm100 value
    per date in seg_dates (-1 = missing). The response's own time axis and grid
    cell are checked: wrong days or a wrong cell -> every value missing."""
    missing = [-1] * len(seg_dates)
    lines = body.decode("utf-8").split("\n")
    by_index = {}
    times = None
    lat_ok = False
    lon_ok = False
    center = _cpc_cell_center(lat_micro, lon_micro)
    section = ""
    for raw in lines:
        line = raw.strip()
        if line.startswith("precip.precip["):
            section = "values"
            continue
        if line.startswith("precip.time["):
            section = "time"
            continue
        if line.startswith("precip.lat["):
            section = "lat"
            continue
        if line.startswith("precip.lon["):
            section = "lon"
            continue
        if line == "":
            section = ""
            continue
        if section == "values" and line.startswith("["):
            head, sep, val = line.partition(",")
            if sep != "," or not head.endswith("[0]"):
                return missing
            idx_txt = head[1 : head.index("]")]
            if not idx_txt.isdigit():
                return missing
            d = _decimal_or_none(val)
            by_index[int(idx_txt)] = _to_mm100(d) if d is not None else -1
        elif section == "time":
            parts = [_decimal_or_none(x) for x in line.split(",")]
            if None in parts:
                return missing
            times = parts
            section = ""
        elif section == "lat":
            v = _decimal_or_none(line)
            lat_ok = v is not None and v * 1000000 == center[0]
            section = ""
        elif section == "lon":
            v = _decimal_or_none(line)
            lon_ok = v is not None and v * 1000000 == center[1]
            section = ""
    if not lat_ok or not lon_ok or times is None or len(times) != len(seg_dates):
        return missing
    for i, d in enumerate(seg_dates):
        hours = (datetime.date.fromisoformat(d) - CPC_EPOCH).days * 24
        if times[i] != hours:
            return missing
    if len(by_index) != len(seg_dates):
        return missing
    return [by_index[i] if i in by_index else -1 for i in range(len(seg_dates))]


def _fetch_cpc(lat_micro: int, lon_micro: int, start_date: str, end_date: str) -> dict:
    """Non-deterministic: one OPeNDAP request per calendar year in the window.
    Any failed request (e.g. HTTP 400 when the days are not published yet)
    makes the source unavailable."""
    dates = _window_dates(start_date, end_date)
    values = []
    for seg in _cpc_segments(start_date, end_date):
        try:
            r = gl.nondet.web.get(_cpc_url(lat_micro, lon_micro, seg[0], seg[1], seg[2]))
        except Exception:
            return _unavailable(SOURCE_NOAA_CPC)
        if r.status != 200 or r.body is None:
            return _unavailable(SOURCE_NOAA_CPC)
        try:
            values.extend(_parse_cpc_segment(r.body, _window_dates(seg[3], seg[4]), lat_micro, lon_micro))
        except Exception:
            values.extend([-1] * len(_window_dates(seg[3], seg[4])))
    return _summarize(SOURCE_NOAA_CPC, values, len(dates))


def _within(a: int, b: int, tol_abs: int, tol_pct: int) -> bool:
    diff = a - b if a >= b else b - a
    hi = a if a >= b else b
    return diff <= tol_abs or diff * 100 <= hi * tol_pct


# --------------------------------------------------------------------------
# Pure helpers: policy terms and tiers (v1)
# --------------------------------------------------------------------------


def _terms(drought: int, drought_severe: int, flood: int, flood_severe: int, partial_bps: int, payout: int) -> dict:
    return {
        "drought_mm100": int(drought),
        "drought_severe_mm100": int(drought_severe),
        "flood_mm100": int(flood),
        "flood_severe_mm100": int(flood_severe),
        "partial_bps": int(partial_bps),
        "payout_wei": int(payout),
    }


def _check_tier_terms(t: dict) -> None:
    """Trigger and tier rules (pure). Untiered terms: severe levels 0 and
    partial_bps 0 -> crossing a trigger pays 100 % (pre-v1 behaviour).
    Tiered: crossing the trigger pays partial_bps, crossing the severe level pays 100 %."""
    d = t["drought_mm100"]
    ds = t["drought_severe_mm100"]
    f = t["flood_mm100"]
    fs = t["flood_severe_mm100"]
    pb = t["partial_bps"]
    if d < 0 or f < 0 or ds < 0 or fs < 0 or pb < 0:
        raise gl.vm.UserError("Invalid trigger")
    if d == 0 and f == 0:
        raise gl.vm.UserError("At least one trigger required")
    if ds > 0 and (d == 0 or ds >= d):
        raise gl.vm.UserError("Severe drought level must be below the drought trigger")
    if fs > 0 and (f == 0 or fs <= f):
        raise gl.vm.UserError("Severe flood level must be above the flood trigger")
    tiered = ds > 0 or fs > 0
    if tiered and (pb <= 0 or pb >= BPS):
        raise gl.vm.UserError("Partial payout must be 1-9999 bps for tiered terms")
    if not tiered and pb != 0:
        raise gl.vm.UserError("Partial payout needs a severe level")
    if t["payout_wei"] <= 0:
        raise gl.vm.UserError("Invalid payout")


def _drought_tier(total_mm100: int, t: dict) -> int:
    trig = t["drought_mm100"]
    if trig <= 0:
        return TIER_DISABLED
    if total_mm100 >= trig:
        return TIER_NONE
    severe = t["drought_severe_mm100"]
    if severe > 0 and total_mm100 >= severe:
        return TIER_PARTIAL
    return TIER_FULL


def _flood_tier(max_mm100: int, t: dict) -> int:
    trig = t["flood_mm100"]
    if trig <= 0:
        return TIER_DISABLED
    if max_mm100 <= trig:
        return TIER_NONE
    severe = t["flood_severe_mm100"]
    if severe > 0 and max_mm100 <= severe:
        return TIER_PARTIAL
    return TIER_FULL


def _tier_bps(tier: int, t: dict) -> int:
    if tier == TIER_FULL:
        return BPS
    if tier == TIER_PARTIAL:
        return t["partial_bps"]
    return 0


def _payout_amount(payout_wei: int, payout_bps: int) -> int:
    return payout_wei * payout_bps // BPS


# --------------------------------------------------------------------------
# Settlement consensus (v1: three sources, 2-of-3, tiers)
# --------------------------------------------------------------------------


def _vote(tiers: list, names: list) -> list:
    """2-of-N vote on one component. tiers/names: complete sources only.
    Returns [tier, agreeing_names] or [None, []] when no tier has 2 votes."""
    for tier in [TIER_FULL, TIER_PARTIAL, TIER_NONE]:
        agree = [names[i] for i in range(len(tiers)) if tiers[i] == tier]
        if len(agree) >= MIN_AGREEING_SOURCES:
            return [tier, agree]
    return [None, []]


def _decide(sources: list, t: dict) -> dict:
    """Deterministic 2-of-3 settlement rules -> decision.

    1. Only complete sources (status ok: every day present) vote.
    2. Each complete source puts each enabled component into a tier:
       drought on the cumulative total, flood on the wettest day
       (NOT_TRIGGERED / PARTIAL / FULL).
    3. A component is decided when at least 2 complete sources put it in the
       same tier; otherwise it is UNDECIDED (SOURCE_MISSING if fewer than 2
       sources are complete, SOURCES_DIVERGE if they are but do not agree).
    4. payout fraction = highest decided tier over the components. A decided
       FULL pays 100 % even if the other component is undecided. Otherwise any
       undecided component -> DEFER (no money moves).
    """
    by_name = {}
    for s in sources:
        by_name[s["name"]] = s
    ok = [n for n in SOURCE_NAMES if n in by_name and by_name[n]["status"] == SRC_OK]
    source_tiers = []
    for n in SOURCE_NAMES:
        if n in ok:
            s = by_name[n]
            source_tiers.append(
                {"name": n, "drought_tier": _drought_tier(s["total_mm100"], t), "flood_tier": _flood_tier(s["max_mm100"], t)}
            )
        else:
            source_tiers.append({"name": n, "drought_tier": TIER_NO_DATA, "flood_tier": TIER_NO_DATA})

    voters = [st for st in source_tiers if st["name"] in ok]
    comps = {}
    for comp, key, enabled in [
        ["drought", "drought_tier", t["drought_mm100"] > 0],
        ["flood", "flood_tier", t["flood_mm100"] > 0],
    ]:
        if not enabled:
            comps[comp] = [DISABLED, TIER_DISABLED, []]
            continue
        v = _vote([st[key] for st in voters], [st["name"] for st in voters])
        if v[0] is None:
            comps[comp] = [UNDECIDED, TIER_NO_DATA, []]
        else:
            comps[comp] = [TIER_OUTCOME[v[0]], v[0], v[1]]

    decided = [comps[c][1] for c in ["drought", "flood"] if comps[c][0] not in [DISABLED, UNDECIDED]]
    undecided = len([c for c in ["drought", "flood"] if comps[c][0] == UNDECIDED]) > 0
    best = max(decided) if len(decided) > 0 else TIER_NONE
    reason = REASON_NONE
    if best == TIER_FULL or not undecided:
        payout_bps = _tier_bps(best, t)
        decision = DECISION_PAYOUT if payout_bps > 0 else DECISION_NO_PAYOUT
    else:
        payout_bps = 0
        decision = DECISION_DEFER
        reason = REASON_SOURCE_MISSING if len(ok) < MIN_AGREEING_SOURCES else REASON_SOURCES_DIVERGE
    return {
        "sources_ok": len(ok),
        "sources_used": ok,
        "source_tiers": source_tiers,
        "drought": comps["drought"][0],
        "drought_agree": comps["drought"][2],
        "flood": comps["flood"][0],
        "flood_agree": comps["flood"][2],
        "decision": decision,
        "defer_reason": reason,
        "payout_bps": payout_bps,
        "payout_wei": _payout_amount(t["payout_wei"], payout_bps),
    }


DECISION_KEYS = [
    "sources_ok",
    "sources_used",
    "source_tiers",
    "drought",
    "drought_agree",
    "flood",
    "flood_agree",
    "decision",
    "defer_reason",
    "payout_bps",
    "payout_wei",
]

IDENTITY_KEYS = ["policy_id", "lat_micro", "lon_micro", "start_date", "end_date", "expected_days", "terms"]


def _fetch_evidence(policy_id: str, lat_micro: int, lon_micro: int, start_date: str, end_date: str, t: dict) -> dict:
    """Non-deterministic: fetch the three sources and build the settlement evidence."""
    dates = _window_dates(start_date, end_date)
    sources = []

    try:
        r = gl.nondet.web.get(_open_meteo_url(lat_micro, lon_micro, start_date, end_date))
        if r.status == 200 and r.body is not None:
            sources.append(_parse_open_meteo(r.body, dates))
        else:
            sources.append(_unavailable(SOURCE_OPEN_METEO))
    except Exception:
        sources.append(_unavailable(SOURCE_OPEN_METEO))

    try:
        r = gl.nondet.web.get(_nasa_power_url(lat_micro, lon_micro, start_date, end_date))
        if r.status == 200 and r.body is not None:
            sources.append(_parse_nasa_power(r.body, dates))
        else:
            sources.append(_unavailable(SOURCE_NASA_POWER))
    except Exception:
        sources.append(_unavailable(SOURCE_NASA_POWER))

    try:
        sources.append(_fetch_cpc(lat_micro, lon_micro, start_date, end_date))
    except Exception:
        sources.append(_unavailable(SOURCE_NOAA_CPC))

    ev = {
        "policy_id": policy_id,
        "lat_micro": lat_micro,
        "lon_micro": lon_micro,
        "start_date": start_date,
        "end_date": end_date,
        "expected_days": len(dates),
        "terms": dict(t),
        "sources": sources,
    }
    ev.update(_decide(sources, t))
    return ev


def _evidence_matches(leader: dict, mine: dict, t: dict) -> bool:
    """Validator equivalence check. Every settlement-driving field is bound:
    identity and terms (exact), each source's status/day count (exact) and
    totals (within tolerance), and every decision field - which sources were
    complete, each source's tier per component, which sources agreed, the
    outcome, the payout fraction and the exact payout in wei - both recomputed
    from the leader's own numbers and equal to the validator's own result."""
    if not isinstance(leader, dict):
        return False
    # 1. identity and terms: exact
    for k in IDENTITY_KEYS:
        if leader.get(k) != mine[k]:
            return False
    # 2. per-source observations (same sources, same order)
    ls = leader.get("sources")
    if not isinstance(ls, list) or len(ls) != len(mine["sources"]):
        return False
    for lsrc, msrc in zip(ls, mine["sources"]):
        if not isinstance(lsrc, dict):
            return False
        if lsrc.get("name") != msrc["name"] or lsrc.get("status") != msrc["status"]:
            return False
        if lsrc.get("days") != msrc["days"]:
            return False
        for k in ["total_mm100", "max_mm100"]:
            lv = lsrc.get(k)
            if not isinstance(lv, int) or isinstance(lv, bool) or lv < 0:
                return False
            if not _within(lv, msrc[k], VALIDATOR_TOL_ABS_MM100, VALIDATOR_TOL_PCT):
                return False
    # 3. leader's decision must follow from leader's own numbers (no fake verdicts)
    recomputed = _decide(ls, t)
    for k in DECISION_KEYS:
        if leader.get(k) != recomputed[k]:
            return False
    # 4. and must equal the decision the validator reached independently
    for k in DECISION_KEYS:
        if leader.get(k) != mine[k]:
            return False
    return True


def _check_evidence(ev: dict, pid: str, lat: int, lon: int, start_date: str, end_date: str, t: dict) -> dict:
    """Deterministic post-consensus check run by every node before state changes."""
    if (
        ev["policy_id"] != pid
        or ev["lat_micro"] != lat
        or ev["lon_micro"] != lon
        or ev["start_date"] != start_date
        or ev["end_date"] != end_date
        or ev["expected_days"] != len(_window_dates(start_date, end_date))
        or ev["terms"] != t
    ):
        raise gl.vm.UserError("Evidence does not match policy")
    names = [s["name"] for s in ev["sources"]]
    if names != SOURCE_NAMES:
        raise gl.vm.UserError("Evidence sources do not match")
    recomputed = _decide(ev["sources"], t)
    for k in DECISION_KEYS:
        if ev[k] != recomputed[k]:
            raise gl.vm.UserError("Evidence decision inconsistent")
    return recomputed


# --------------------------------------------------------------------------
# Rainfall-history quotes (pure helpers; v1: expected payout with tiers)
# --------------------------------------------------------------------------


def _shift_years(d: datetime.date, years: int) -> datetime.date:
    """Same calendar day `years` years earlier; 29 Feb maps to 28 Feb."""
    y = d.year - years
    try:
        return datetime.date(y, d.month, d.day)
    except ValueError:
        return datetime.date(y, d.month, 28)


def _history_windows(start_date: str, end_date: str, today: datetime.date) -> list:
    """The QUOTE_YEARS most recent past copies of the coverage window whose end
    is at least QUOTE_HISTORY_LAG_DAYS before today, newest first.
    Returns [[start, end], ...] as ISO strings. Deterministic."""
    start = datetime.date.fromisoformat(start_date)
    end = datetime.date.fromisoformat(end_date)
    latest_ok = today - datetime.timedelta(days=QUOTE_HISTORY_LAG_DAYS)
    out = []
    k = 1
    while len(out) < QUOTE_YEARS and k <= QUOTE_YEARS + 3:
        s = _shift_years(start, k)
        e = _shift_years(end, k)
        if e <= latest_ok:
            out.append([s.isoformat(), e.isoformat()])
        k += 1
    return out


def _history_range(windows: list) -> list:
    """[first_start, last_end] covering every history window (one request)."""
    starts = [w[0] for w in windows]
    ends = [w[1] for w in windows]
    return [min(starts), max(ends)]


def _year_payout_bps(total_mm100: int, max_mm100: int, t: dict) -> int:
    """Fraction of the payout (bps) this history year would have paid."""
    dt = _drought_tier(total_mm100, t)
    ft = _flood_tier(max_mm100, t)
    a = _tier_bps(dt, t) if dt >= 0 else 0
    b = _tier_bps(ft, t) if ft >= 0 else 0
    return a if a >= b else b


def _summarize_years(by_date: dict, windows: list) -> list:
    years = []
    for w in windows:
        dates = _window_dates(w[0], w[1])
        values = [by_date[d] if d in by_date else -1 for d in dates]
        s = _summarize(SOURCE_OPEN_METEO, values, len(dates))
        years.append(
            {
                "start_date": w[0],
                "end_date": w[1],
                "expected_days": len(dates),
                "days": s["days"],
                "total_mm100": s["total_mm100"],
                "max_mm100": s["max_mm100"],
            }
        )
    return years


def _parse_history(body: bytes, windows: list) -> list:
    data = json.loads(body.decode("utf-8"), parse_float=Decimal)
    daily = data["daily"]
    times = daily["time"]
    precip = daily["precipitation_sum"]
    by_date = {}
    if len(times) == len(precip):
        for tm, v in zip(times, precip):
            if isinstance(tm, str):
                by_date[tm] = _to_mm100(v)
    return _summarize_years(by_date, windows)


def _empty_years(windows: list) -> list:
    return _summarize_years({}, windows)


def _history_decide(status: str, years: list, t: dict) -> dict:
    """Deterministic: what each history year would have paid (bps of the
    payout), how many years paid anything, and the average (expected payout
    fraction). Only meaningful when status == ok."""
    pays = []
    for y in years:
        pays.append(_year_payout_bps(y["total_mm100"], y["max_mm100"], t))
    flags = [p > 0 for p in pays]
    hits = len([f for f in flags if f])
    expected = sum(pays) // len(pays) if len(pays) > 0 else BPS
    return {"year_payout_bps": pays, "hit_flags": flags, "hits": hits, "expected_payout_bps": expected}


HISTORY_DECISION_KEYS = ["year_payout_bps", "hit_flags", "hits", "expected_payout_bps"]


def _history_status(years: list) -> str:
    for y in years:
        if y["days"] != y["expected_days"]:
            return SRC_INCOMPLETE
    return SRC_OK


def _fetch_history(lat_micro: int, lon_micro: int, windows: list, t: dict) -> dict:
    """Non-deterministic: fetch the history range once and summarise each year."""
    rng = _history_range(windows)
    years = _empty_years(windows)
    status = SRC_UNAVAILABLE
    try:
        r = gl.nondet.web.get(_open_meteo_url(lat_micro, lon_micro, rng[0], rng[1]))
        if r.status == 200 and r.body is not None:
            years = _parse_history(r.body, windows)
            status = _history_status(years)
    except Exception:
        years = _empty_years(windows)
        status = SRC_UNAVAILABLE
    ev = {
        "lat_micro": lat_micro,
        "lon_micro": lon_micro,
        "windows": windows,
        "terms": dict(t),
        "source": SOURCE_OPEN_METEO,
        "status": status,
        "years": years,
    }
    ev.update(_history_decide(status, years, t))
    return ev


def _history_matches(leader: dict, mine: dict, t: dict) -> bool:
    """Validator equivalence check for request_quote(): everything that drives
    the premium is bound (window list, terms, per-year coverage, per-year totals
    within tolerance, per-year payout fractions, hit count and expected payout)."""
    if not isinstance(leader, dict):
        return False
    for k in ["lat_micro", "lon_micro", "windows", "terms", "source", "status"]:
        if leader.get(k) != mine[k]:
            return False
    ly = leader.get("years")
    if not isinstance(ly, list) or len(ly) != len(mine["years"]):
        return False
    for lyr, myr in zip(ly, mine["years"]):
        if not isinstance(lyr, dict):
            return False
        for k in ["start_date", "end_date", "expected_days", "days"]:
            if lyr.get(k) != myr[k]:
                return False
        for k in ["total_mm100", "max_mm100"]:
            lv = lyr.get(k)
            if not isinstance(lv, int) or isinstance(lv, bool) or lv < 0:
                return False
            if not _within(lv, myr[k], VALIDATOR_TOL_ABS_MM100, VALIDATOR_TOL_PCT):
                return False
    # leader's payout fractions must follow from its own numbers ...
    recomputed = _history_decide(leader["status"], ly, t)
    for k in HISTORY_DECISION_KEYS:
        if leader.get(k) != recomputed[k]:
            return False
    # ... and equal the validator's own
    for k in HISTORY_DECISION_KEYS:
        if leader.get(k) != mine[k]:
            return False
    return True


def _rate_for(expected_payout_bps: int, min_rate_bps: int, loading_bps: int) -> int:
    """Premium rate in bps: historical expected payout fraction + loading,
    floored at the pool minimum, capped at 100 %. (Untiered terms: the
    expected payout fraction equals the historical hit rate, as before v1.)"""
    rate = expected_payout_bps + loading_bps
    if rate < min_rate_bps:
        rate = min_rate_bps
    if rate > BPS:
        rate = BPS
    return rate


def _premium(payout: int, rate_bps: int) -> int:
    # ceil(payout * rate / 10000), integers only
    return (payout * rate_bps + BPS - 1) // BPS


# --------------------------------------------------------------------------
# Storage types
# --------------------------------------------------------------------------


@allow_storage
@dataclass
class Pool:
    id: str
    insurer: Address
    name: str
    min_rate_bps: u32
    loading_bps: u32
    lat_min: i64
    lat_max: i64
    lon_min: i64
    lon_max: i64
    accepting: bool
    balance: u256  # deposits + premiums - payouts - refunds - withdrawals
    reserved: u256  # sum of payouts of ACTIVE policies in this pool
    total_deposits: u256
    total_premiums: u256
    total_paid_out: u256
    total_refunded: u256
    total_withdrawn: u256
    policy_count: u32
    active_policies: u32


@allow_storage
@dataclass
class Quote:
    id: str
    pool_id: str
    requester: Address
    lat_micro: i64
    lon_micro: i64
    start_date: str
    end_date: str
    drought_mm100: u256
    flood_mm100: u256
    payout: u256
    rate_bps: u32
    premium: u256
    hits: u32
    years: u32
    drought_severe_mm100: u256  # v1 tiers (0 = untiered)
    flood_severe_mm100: u256
    partial_bps: u32
    expected_payout_bps: u32  # v1: average historical payout fraction
    created_on: str
    valid_until: str
    policy_id: str  # "" until used
    history: str  # JSON of the agreed history evidence


@allow_storage
@dataclass
class Policy:
    id: str
    pool_id: str
    quote_id: str
    farmer: Address
    lat_micro: i64
    lon_micro: i64
    start_date: str
    end_date: str
    drought_mm100: u256
    flood_mm100: u256
    payout: u256
    premium: u256
    rate_bps: u32
    status: str
    purchased_on: str
    settle_attempts: u32
    paid_amount: u256
    evidence: str
    drought_severe_mm100: u256  # v1 tiers (0 = untiered)
    flood_severe_mm100: u256
    partial_bps: u32
    paid_bps: u32  # fraction of payout actually paid (v1)


@allow_storage
@dataclass
class DryRun:
    id: str
    requester: Address
    lat_micro: i64
    lon_micro: i64
    start_date: str
    end_date: str
    created_on: str
    evidence: str  # JSON of the agreed settlement evidence


@gl.evm.contract_interface
class _Recipient:
    class View:
        pass

    class Write:
        pass


# --------------------------------------------------------------------------
# Contract
# --------------------------------------------------------------------------


class PadiGuard(gl.Contract):
    pool_count: u256
    quote_count: u256
    policy_count: u256
    dry_run_count: u256
    pools: TreeMap[str, Pool]
    quotes: TreeMap[str, Quote]
    policies: TreeMap[str, Policy]
    farmer_policies: TreeMap[str, DynArray[str]]
    farmer_quotes: TreeMap[str, DynArray[str]]
    pool_policies: TreeMap[str, DynArray[str]]
    insurer_pools: TreeMap[str, DynArray[str]]
    dry_runs: TreeMap[str, DryRun]

    def __init__(self):
        self.pool_count = u256(0)
        self.quote_count = u256(0)
        self.policy_count = u256(0)
        self.dry_run_count = u256(0)

    # ---------------- internal helpers ----------------

    def _today(self) -> datetime.date:
        return datetime.datetime.now(datetime.timezone.utc).date()

    def _send(self, to: Address, amount: int) -> None:
        if amount > 0:
            _Recipient(to).emit_transfer(value=u256(amount))

    def _pool(self, pool_id: str) -> Pool:
        if pool_id not in self.pools:
            raise gl.vm.UserError("Unknown pool")
        return self.pools[pool_id]

    def _policy(self, policy_id: str) -> Policy:
        if policy_id not in self.policies:
            raise gl.vm.UserError("Unknown policy")
        return self.policies[policy_id]

    def _quote(self, quote_id: str) -> Quote:
        if quote_id not in self.quotes:
            raise gl.vm.UserError("Unknown quote")
        return self.quotes[quote_id]

    def _only_insurer(self, pool: Pool) -> None:
        if gl.message.sender_address != pool.insurer:
            raise gl.vm.UserError("Only pool insurer")

    def _free(self, pool: Pool) -> int:
        return int(pool.balance) - int(pool.reserved)

    def _index_append(self, index: TreeMap[str, DynArray[str]], key: str, value: str) -> None:
        index.get_or_insert_default(key).append(value)

    def _index_list(self, index: TreeMap[str, DynArray[str]], key: str) -> list:
        if key not in index:
            return []
        return [str(x) for x in index[key]]

    def _release(self, pool: Pool, p: Policy) -> None:
        pool.reserved = u256(int(pool.reserved) - int(p.payout))
        pool.active_policies = u32(int(pool.active_policies) - 1)

    def _check_rates(self, min_rate_bps: int, loading_bps: int) -> None:
        if min_rate_bps <= 0 or min_rate_bps > BPS:
            raise gl.vm.UserError("Invalid minimum rate")
        if loading_bps < 0 or loading_bps > BPS:
            raise gl.vm.UserError("Invalid loading")

    def _check_point(self, lat_micro: int, lon_micro: int) -> None:
        if lat_micro < -90000000 or lat_micro > 90000000:
            raise gl.vm.UserError("Invalid latitude")
        if lon_micro < -180000000 or lon_micro > 180000000:
            raise gl.vm.UserError("Invalid longitude")

    def _check_terms(
        self,
        pool: Pool,
        lat_micro: int,
        lon_micro: int,
        start_date: str,
        end_date: str,
        terms: dict,
    ) -> None:
        if not bool(pool.accepting):
            raise gl.vm.UserError("Pool is not accepting new policies")
        self._check_point(lat_micro, lon_micro)
        if (
            lat_micro < int(pool.lat_min)
            or lat_micro > int(pool.lat_max)
            or lon_micro < int(pool.lon_min)
            or lon_micro > int(pool.lon_max)
        ):
            raise gl.vm.UserError("Location outside pool coverage region")
        start = _parse_date(start_date)
        end = _parse_date(end_date)
        if end < start:
            raise gl.vm.UserError("end_date before start_date")
        if (end - start).days + 1 > MAX_WINDOW_DAYS:
            raise gl.vm.UserError("Coverage window too long")
        lead = (start - self._today()).days
        if lead < MIN_LEAD_DAYS:
            raise gl.vm.UserError("Coverage must start after purchase date")
        if lead > MAX_LEAD_DAYS:
            raise gl.vm.UserError("Coverage starts too far ahead")
        _check_tier_terms(terms)
        if self._free(pool) < terms["payout_wei"]:
            raise gl.vm.UserError("Insufficient pool liquidity")

    def _quote_terms(self, q: Quote) -> dict:
        return _terms(
            int(q.drought_mm100),
            int(q.drought_severe_mm100),
            int(q.flood_mm100),
            int(q.flood_severe_mm100),
            int(q.partial_bps),
            int(q.payout),
        )

    def _policy_terms(self, p: Policy) -> dict:
        return _terms(
            int(p.drought_mm100),
            int(p.drought_severe_mm100),
            int(p.flood_mm100),
            int(p.flood_severe_mm100),
            int(p.partial_bps),
            int(p.payout),
        )

    def _run_settlement_consensus(self, pid: str, lat: int, lon: int, start_date: str, end_date: str, terms: dict) -> dict:
        """Shared by settle() and dry_run_settlement(): leader/validator
        consensus on the 3-source evidence, then the deterministic re-check."""
        t = dict(terms)

        def leader_fn() -> dict:
            return _fetch_evidence(pid, lat, lon, start_date, end_date, t)

        def validator_fn(leaders_res) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return False
            mine = _fetch_evidence(pid, lat, lon, start_date, end_date, t)
            return _evidence_matches(leaders_res.calldata, mine, t)

        ev = gl.vm.run_nondet(leader_fn, validator_fn)
        _check_evidence(ev, pid, lat, lon, start_date, end_date, t)
        return ev

    # ---------------- insurers ----------------

    @gl.public.write.payable
    def create_pool(
        self,
        name: str,
        min_rate_bps: int,
        loading_bps: int,
        lat_min: int,
        lat_max: int,
        lon_min: int,
        lon_max: int,
    ) -> str:
        """Open a new insurer pool. msg.value (optional) is the first deposit."""
        if not isinstance(name, str) or len(name.strip()) == 0 or len(name) > MAX_POOL_NAME:
            raise gl.vm.UserError("Invalid pool name")
        self._check_rates(min_rate_bps, loading_bps)
        if lat_min < -90000000 or lat_max > 90000000 or lat_min > lat_max:
            raise gl.vm.UserError("Invalid latitude range")
        if lon_min < -180000000 or lon_max > 180000000 or lon_min > lon_max:
            raise gl.vm.UserError("Invalid longitude range")
        deposit = int(gl.message.value)
        new_count = int(self.pool_count) + 1
        pool_id = str(new_count)
        self.pool_count = u256(new_count)
        self.pools[pool_id] = Pool(
            id=pool_id,
            insurer=gl.message.sender_address,
            name=name.strip(),
            min_rate_bps=u32(min_rate_bps),
            loading_bps=u32(loading_bps),
            lat_min=i64(lat_min),
            lat_max=i64(lat_max),
            lon_min=i64(lon_min),
            lon_max=i64(lon_max),
            accepting=True,
            balance=u256(deposit),
            reserved=u256(0),
            total_deposits=u256(deposit),
            total_premiums=u256(0),
            total_paid_out=u256(0),
            total_refunded=u256(0),
            total_withdrawn=u256(0),
            policy_count=u32(0),
            active_policies=u32(0),
        )
        self._index_append(self.insurer_pools, gl.message.sender_address.as_hex, pool_id)
        return pool_id

    @gl.public.write.payable
    def fund_pool(self, pool_id: str) -> int:
        pool = self._pool(pool_id)
        self._only_insurer(pool)
        value = int(gl.message.value)
        if value <= 0:
            raise gl.vm.UserError("Send GEN to fund the pool")
        pool.balance = u256(int(pool.balance) + value)
        pool.total_deposits = u256(int(pool.total_deposits) + value)
        return int(pool.balance)

    @gl.public.write
    def withdraw(self, pool_id: str, amount: int) -> int:
        pool = self._pool(pool_id)
        self._only_insurer(pool)
        if amount <= 0:
            raise gl.vm.UserError("Invalid amount")
        if amount > self._free(pool):
            raise gl.vm.UserError("Amount exceeds unreserved funds")
        pool.balance = u256(int(pool.balance) - amount)
        pool.total_withdrawn = u256(int(pool.total_withdrawn) + amount)
        self._send(pool.insurer, amount)
        return int(pool.balance)

    @gl.public.write
    def set_pool_pricing(self, pool_id: str, min_rate_bps: int, loading_bps: int) -> None:
        """Applies to quotes requested after this call."""
        pool = self._pool(pool_id)
        self._only_insurer(pool)
        self._check_rates(min_rate_bps, loading_bps)
        pool.min_rate_bps = u32(min_rate_bps)
        pool.loading_bps = u32(loading_bps)

    @gl.public.write
    def set_pool_accepting(self, pool_id: str, accepting: bool) -> None:
        """Open/close the pool for new quotes and purchases. Existing policies
        are unaffected."""
        pool = self._pool(pool_id)
        self._only_insurer(pool)
        pool.accepting = bool(accepting)

    # ---------------- farmers: quote ----------------

    @gl.public.write
    def request_quote(
        self,
        pool_id: str,
        lat_micro: int,
        lon_micro: int,
        start_date: str,
        end_date: str,
        drought_trigger_mm100: int,
        drought_severe_mm100: int,
        flood_trigger_mm100: int,
        flood_severe_mm100: int,
        partial_bps: int,
        payout: int,
    ) -> dict:
        """Price a policy from the last QUOTE_YEARS years of rainfall at this
        location for the same calendar window. Validators fetch the history
        themselves and must agree on every year's numbers and on what each year
        would have paid under these (optionally tiered) terms.

        Tiers (v1): drought pays partial_bps of the payout when the season total
        is below drought_trigger_mm100 and 100 % when it is below
        drought_severe_mm100; flood pays partial_bps when the wettest day is
        above flood_trigger_mm100 and 100 % above flood_severe_mm100. Severe
        levels 0 and partial_bps 0 = untiered (a crossed trigger pays 100 %)."""
        pool = self._pool(pool_id)
        lat = int(lat_micro)
        lon = int(lon_micro)
        terms = _terms(
            drought_trigger_mm100, drought_severe_mm100, flood_trigger_mm100, flood_severe_mm100, partial_bps, payout
        )
        self._check_terms(pool, lat, lon, start_date, end_date, terms)
        today = self._today()
        windows = _history_windows(start_date, end_date, today)
        if len(windows) != QUOTE_YEARS:
            raise gl.vm.UserError("Not enough rainfall history for this window")

        t = dict(terms)

        def leader_fn() -> dict:
            return _fetch_history(lat, lon, windows, t)

        def validator_fn(leaders_res) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return False
            mine = _fetch_history(lat, lon, windows, t)
            return _history_matches(leaders_res.calldata, mine, t)

        ev = gl.vm.run_nondet(leader_fn, validator_fn)

        # ---- deterministic post-consensus checks ----
        if ev["lat_micro"] != lat or ev["lon_micro"] != lon or ev["windows"] != windows or ev["terms"] != terms:
            raise gl.vm.UserError("History evidence does not match request")
        if ev["status"] != SRC_OK or _history_status(ev["years"]) != SRC_OK:
            raise gl.vm.UserError("Rainfall history incomplete or unavailable, try again later")
        recomputed = _history_decide(ev["status"], ev["years"], terms)
        for k in HISTORY_DECISION_KEYS:
            if ev[k] != recomputed[k]:
                raise gl.vm.UserError("History evidence inconsistent")

        expected = recomputed["expected_payout_bps"]
        rate = _rate_for(expected, int(pool.min_rate_bps), int(pool.loading_bps))
        premium = _premium(terms["payout_wei"], rate)
        new_count = int(self.quote_count) + 1
        quote_id = str(new_count)
        self.quote_count = u256(new_count)
        valid_until = today + datetime.timedelta(days=QUOTE_VALID_DAYS)
        self.quotes[quote_id] = Quote(
            id=quote_id,
            pool_id=pool_id,
            requester=gl.message.sender_address,
            lat_micro=i64(lat),
            lon_micro=i64(lon),
            start_date=start_date,
            end_date=end_date,
            drought_mm100=u256(terms["drought_mm100"]),
            flood_mm100=u256(terms["flood_mm100"]),
            payout=u256(terms["payout_wei"]),
            rate_bps=u32(rate),
            premium=u256(premium),
            hits=u32(recomputed["hits"]),
            years=u32(QUOTE_YEARS),
            drought_severe_mm100=u256(terms["drought_severe_mm100"]),
            flood_severe_mm100=u256(terms["flood_severe_mm100"]),
            partial_bps=u32(terms["partial_bps"]),
            expected_payout_bps=u32(expected),
            created_on=today.isoformat(),
            valid_until=valid_until.isoformat(),
            policy_id="",
            history=json.dumps(ev, sort_keys=True),
        )
        self._index_append(self.farmer_quotes, gl.message.sender_address.as_hex, quote_id)
        return self._quote_view(self.quotes[quote_id])

    # ---------------- farmers: buy / cancel ----------------

    @gl.public.write.payable
    def buy_policy(self, quote_id: str) -> str:
        """Buy the policy described by an unused, unexpired quote. msg.value
        must equal the quoted premium."""
        q = self._quote(quote_id)
        if gl.message.sender_address != q.requester:
            raise gl.vm.UserError("Only the quote requester can buy")
        if q.policy_id != "":
            raise gl.vm.UserError("Quote already used")
        today = self._today()
        if today > datetime.date.fromisoformat(q.valid_until):
            raise gl.vm.UserError("Quote expired")
        pool = self._pool(q.pool_id)
        payout = int(q.payout)
        self._check_terms(pool, int(q.lat_micro), int(q.lon_micro), q.start_date, q.end_date, self._quote_terms(q))
        premium = int(q.premium)
        if int(gl.message.value) != premium:
            raise gl.vm.UserError("Premium must equal " + str(premium))

        new_count = int(self.policy_count) + 1
        policy_id = str(new_count)
        self.policy_count = u256(new_count)
        pool.balance = u256(int(pool.balance) + premium)
        pool.reserved = u256(int(pool.reserved) + payout)
        pool.total_premiums = u256(int(pool.total_premiums) + premium)
        pool.policy_count = u32(int(pool.policy_count) + 1)
        pool.active_policies = u32(int(pool.active_policies) + 1)
        q.policy_id = policy_id
        self.policies[policy_id] = Policy(
            id=policy_id,
            pool_id=q.pool_id,
            quote_id=quote_id,
            farmer=gl.message.sender_address,
            lat_micro=q.lat_micro,
            lon_micro=q.lon_micro,
            start_date=q.start_date,
            end_date=q.end_date,
            drought_mm100=q.drought_mm100,
            flood_mm100=q.flood_mm100,
            payout=u256(payout),
            premium=u256(premium),
            rate_bps=q.rate_bps,
            status=STATUS_ACTIVE,
            purchased_on=today.isoformat(),
            settle_attempts=u32(0),
            paid_amount=u256(0),
            evidence="",
            drought_severe_mm100=q.drought_severe_mm100,
            flood_severe_mm100=q.flood_severe_mm100,
            partial_bps=q.partial_bps,
            paid_bps=u32(0),
        )
        self._index_append(self.farmer_policies, gl.message.sender_address.as_hex, policy_id)
        self._index_append(self.pool_policies, q.pool_id, policy_id)
        return policy_id

    @gl.public.write
    def cancel_policy(self, policy_id: str) -> int:
        """Farmer cancels before coverage starts: full premium refund."""
        p = self._policy(policy_id)
        if p.status != STATUS_ACTIVE:
            raise gl.vm.UserError("Policy not active")
        if gl.message.sender_address != p.farmer:
            raise gl.vm.UserError("Only policy holder")
        if self._today() >= datetime.date.fromisoformat(p.start_date):
            raise gl.vm.UserError("Coverage already started")
        pool = self._pool(p.pool_id)
        refund = int(p.premium)
        p.status = STATUS_CANCELLED
        self._release(pool, p)
        pool.balance = u256(int(pool.balance) - refund)
        pool.total_refunded = u256(int(pool.total_refunded) + refund)
        self._send(p.farmer, refund)
        return refund

    # ---------------- settlement ----------------

    @gl.public.write
    def settle(self, policy_id: str) -> dict:
        p = self._policy(policy_id)
        if p.status != STATUS_ACTIVE:
            raise gl.vm.UserError("Policy not active")
        end = datetime.date.fromisoformat(p.end_date)
        opens = end + datetime.timedelta(days=SETTLEMENT_DELAY_DAYS)
        deadline = opens + datetime.timedelta(days=CLAIM_WINDOW_DAYS)
        today = self._today()
        if today < opens:
            raise gl.vm.UserError("Settlement not open yet")
        if today > deadline:
            raise gl.vm.UserError("Settlement deadline passed")

        # copy everything the nondet block needs into plain locals
        pid = str(p.id)
        lat = int(p.lat_micro)
        lon = int(p.lon_micro)
        start_date = str(p.start_date)
        end_date = str(p.end_date)
        terms = self._policy_terms(p)

        ev = self._run_settlement_consensus(pid, lat, lon, start_date, end_date, terms)

        # ---- money logic (evidence already re-checked deterministically) ----
        pool = self._pool(p.pool_id)
        p.settle_attempts = u32(int(p.settle_attempts) + 1)
        p.evidence = json.dumps(ev, sort_keys=True)
        decision = ev["decision"]
        if decision == DECISION_PAYOUT:
            amount = _payout_amount(terms["payout_wei"], ev["payout_bps"])
            if amount != ev["payout_wei"] or amount <= 0 or amount > int(p.payout):
                raise gl.vm.UserError("Payout amount inconsistent")
            p.status = STATUS_PAID_OUT
            p.paid_amount = u256(amount)
            p.paid_bps = u32(ev["payout_bps"])
            self._release(pool, p)  # releases the full reserved payout
            pool.balance = u256(int(pool.balance) - amount)
            pool.total_paid_out = u256(int(pool.total_paid_out) + amount)
            self._send(p.farmer, amount)
        elif decision == DECISION_NO_PAYOUT:
            p.status = STATUS_SETTLED_NO_PAYOUT
            self._release(pool, p)
        # DEFER: policy stays ACTIVE, reserve stays locked, evidence recorded
        return ev

    @gl.public.write
    def dry_run_settlement(
        self,
        lat_micro: int,
        lon_micro: int,
        start_date: str,
        end_date: str,
        drought_trigger_mm100: int,
        drought_severe_mm100: int,
        flood_trigger_mm100: int,
        flood_severe_mm100: int,
        partial_bps: int,
        payout: int,
    ) -> dict:
        """Run the exact settle() consensus (same fetch, same validator check,
        same 2-of-3 + tier rules) for terms over a PAST window, without any
        policy or money. The agreed evidence is stored as a public record so
        farmers and insurers can see what settlement would have decided."""
        lat = int(lat_micro)
        lon = int(lon_micro)
        self._check_point(lat, lon)
        start = _parse_date(start_date)
        end = _parse_date(end_date)
        if end < start:
            raise gl.vm.UserError("end_date before start_date")
        if (end - start).days + 1 > MAX_WINDOW_DAYS:
            raise gl.vm.UserError("Coverage window too long")
        today = self._today()
        if today < end + datetime.timedelta(days=SETTLEMENT_DELAY_DAYS):
            raise gl.vm.UserError("Dry run needs a window that ended at least 7 days ago")
        terms = _terms(
            drought_trigger_mm100, drought_severe_mm100, flood_trigger_mm100, flood_severe_mm100, partial_bps, payout
        )
        _check_tier_terms(terms)
        new_count = int(self.dry_run_count) + 1
        run_id = str(new_count)
        pid = "dry-run-" + run_id
        ev = self._run_settlement_consensus(pid, lat, lon, start_date, end_date, terms)
        self.dry_run_count = u256(new_count)
        self.dry_runs[run_id] = DryRun(
            id=run_id,
            requester=gl.message.sender_address,
            lat_micro=i64(lat),
            lon_micro=i64(lon),
            start_date=start_date,
            end_date=end_date,
            created_on=today.isoformat(),
            evidence=json.dumps(ev, sort_keys=True),
        )
        return ev

    @gl.public.write
    def expire(self, policy_id: str) -> int:
        """After the claim window closes without settlement (sources missing or
        diverging the whole time), anyone can expire the policy: the premium is
        refunded to the farmer and the reserve is released."""
        p = self._policy(policy_id)
        if p.status != STATUS_ACTIVE:
            raise gl.vm.UserError("Policy not active")
        end = datetime.date.fromisoformat(p.end_date)
        deadline = end + datetime.timedelta(days=SETTLEMENT_DELAY_DAYS + CLAIM_WINDOW_DAYS)
        if self._today() <= deadline:
            raise gl.vm.UserError("Claim window still open")
        pool = self._pool(p.pool_id)
        refund = int(p.premium)
        p.status = STATUS_EXPIRED_REFUNDED
        self._release(pool, p)
        pool.balance = u256(int(pool.balance) - refund)
        pool.total_refunded = u256(int(pool.total_refunded) + refund)
        self._send(p.farmer, refund)
        return refund

    # ---------------- view builders ----------------

    def _pool_view(self, pool: Pool) -> dict:
        return {
            "id": pool.id,
            "insurer": pool.insurer.as_hex,
            "name": pool.name,
            "min_rate_bps": int(pool.min_rate_bps),
            "loading_bps": int(pool.loading_bps),
            "lat_min": int(pool.lat_min),
            "lat_max": int(pool.lat_max),
            "lon_min": int(pool.lon_min),
            "lon_max": int(pool.lon_max),
            "accepting": bool(pool.accepting),
            "balance": int(pool.balance),
            "reserved": int(pool.reserved),
            "free_liquidity": self._free(pool),
            "total_deposits": int(pool.total_deposits),
            "total_premiums": int(pool.total_premiums),
            "total_paid_out": int(pool.total_paid_out),
            "total_refunded": int(pool.total_refunded),
            "total_withdrawn": int(pool.total_withdrawn),
            "policy_count": int(pool.policy_count),
            "active_policies": int(pool.active_policies),
        }

    def _quote_view(self, q: Quote) -> dict:
        return {
            "id": q.id,
            "pool_id": q.pool_id,
            "requester": q.requester.as_hex,
            "lat_micro": int(q.lat_micro),
            "lon_micro": int(q.lon_micro),
            "start_date": q.start_date,
            "end_date": q.end_date,
            "drought_trigger_mm100": int(q.drought_mm100),
            "flood_trigger_mm100": int(q.flood_mm100),
            "payout": int(q.payout),
            "rate_bps": int(q.rate_bps),
            "premium": int(q.premium),
            "hits": int(q.hits),
            "years": int(q.years),
            "drought_severe_mm100": int(q.drought_severe_mm100),
            "flood_severe_mm100": int(q.flood_severe_mm100),
            "partial_bps": int(q.partial_bps),
            "expected_payout_bps": int(q.expected_payout_bps),
            "created_on": q.created_on,
            "valid_until": q.valid_until,
            "policy_id": q.policy_id,
            "history": q.history,
        }

    def _policy_view(self, p: Policy) -> dict:
        end = datetime.date.fromisoformat(p.end_date)
        opens = end + datetime.timedelta(days=SETTLEMENT_DELAY_DAYS)
        return {
            "id": p.id,
            "pool_id": p.pool_id,
            "quote_id": p.quote_id,
            "farmer": p.farmer.as_hex,
            "lat_micro": int(p.lat_micro),
            "lon_micro": int(p.lon_micro),
            "start_date": p.start_date,
            "end_date": p.end_date,
            "drought_trigger_mm100": int(p.drought_mm100),
            "flood_trigger_mm100": int(p.flood_mm100),
            "drought_severe_mm100": int(p.drought_severe_mm100),
            "flood_severe_mm100": int(p.flood_severe_mm100),
            "partial_bps": int(p.partial_bps),
            "payout": int(p.payout),
            "premium": int(p.premium),
            "rate_bps": int(p.rate_bps),
            "status": p.status,
            "purchased_on": p.purchased_on,
            "settle_opens": opens.isoformat(),
            "settle_deadline": (opens + datetime.timedelta(days=CLAIM_WINDOW_DAYS)).isoformat(),
            "settle_attempts": int(p.settle_attempts),
            "paid_amount": int(p.paid_amount),
            "paid_bps": int(p.paid_bps),
            "evidence": p.evidence,
        }

    def _page(self, count: int, offset: int, limit: int) -> list:
        if offset < 0 or limit <= 0 or limit > MAX_PAGE:
            raise gl.vm.UserError("Invalid page (limit 1-50, offset >= 0)")
        first = offset + 1
        last = offset + limit
        if last > count:
            last = count
        return [str(i) for i in range(first, last + 1)]

    # ---------------- views ----------------

    @gl.public.view
    def get_stats(self) -> dict:
        balance = 0
        reserved = 0
        premiums = 0
        paid = 0
        for i in range(1, int(self.pool_count) + 1):
            pool = self.pools[str(i)]
            balance += int(pool.balance)
            reserved += int(pool.reserved)
            premiums += int(pool.total_premiums)
            paid += int(pool.total_paid_out)
        return {
            "pool_count": int(self.pool_count),
            "quote_count": int(self.quote_count),
            "policy_count": int(self.policy_count),
            "dry_run_count": int(self.dry_run_count),
            "total_balance": balance,
            "total_reserved": reserved,
            "total_premiums": premiums,
            "total_paid_out": paid,
            "quote_years": QUOTE_YEARS,
            "quote_valid_days": QUOTE_VALID_DAYS,
            "settlement_delay_days": SETTLEMENT_DELAY_DAYS,
            "claim_window_days": CLAIM_WINDOW_DAYS,
            "settlement_sources": list(SOURCE_NAMES),
            "min_agreeing_sources": MIN_AGREEING_SOURCES,
        }

    @gl.public.view
    def get_pool(self, pool_id: str) -> dict:
        return self._pool_view(self._pool(pool_id))

    @gl.public.view
    def list_pools(self, offset: int, limit: int) -> list:
        return [self._pool_view(self.pools[i]) for i in self._page(int(self.pool_count), offset, limit)]

    @gl.public.view
    def get_quote(self, quote_id: str) -> dict:
        return self._quote_view(self._quote(quote_id))

    @gl.public.view
    def get_policy(self, policy_id: str) -> dict:
        return self._policy_view(self._policy(policy_id))

    @gl.public.view
    def list_policies(self, offset: int, limit: int) -> list:
        return [self._policy_view(self.policies[i]) for i in self._page(int(self.policy_count), offset, limit)]

    @gl.public.view
    def get_farmer_policies(self, farmer: str) -> list:
        key = Address(farmer).as_hex
        return [self._policy_view(self.policies[i]) for i in self._index_list(self.farmer_policies, key)]

    @gl.public.view
    def get_farmer_quotes(self, farmer: str) -> list:
        key = Address(farmer).as_hex
        return [self._quote_view(self.quotes[i]) for i in self._index_list(self.farmer_quotes, key)]

    @gl.public.view
    def get_pool_policies(self, pool_id: str) -> list:
        self._pool(pool_id)
        return [self._policy_view(self.policies[i]) for i in self._index_list(self.pool_policies, pool_id)]

    @gl.public.view
    def get_insurer_pools(self, insurer: str) -> list:
        key = Address(insurer).as_hex
        return [self._pool_view(self.pools[i]) for i in self._index_list(self.insurer_pools, key)]

    @gl.public.view
    def get_source_urls(self, policy_id: str) -> list:
        p = self._policy(policy_id)
        lat = int(p.lat_micro)
        lon = int(p.lon_micro)
        return [
            _open_meteo_url(lat, lon, p.start_date, p.end_date),
            _nasa_power_url(lat, lon, p.start_date, p.end_date),
        ] + _cpc_urls(lat, lon, p.start_date, p.end_date)

    @gl.public.view
    def get_dry_run(self, run_id: str) -> dict:
        if run_id not in self.dry_runs:
            raise gl.vm.UserError("Unknown dry run")
        r = self.dry_runs[run_id]
        lat = int(r.lat_micro)
        lon = int(r.lon_micro)
        return {
            "id": r.id,
            "requester": r.requester.as_hex,
            "lat_micro": lat,
            "lon_micro": lon,
            "start_date": r.start_date,
            "end_date": r.end_date,
            "created_on": r.created_on,
            "evidence": r.evidence,
            "source_urls": [
                _open_meteo_url(lat, lon, r.start_date, r.end_date),
                _nasa_power_url(lat, lon, r.start_date, r.end_date),
            ]
            + _cpc_urls(lat, lon, r.start_date, r.end_date),
        }

    @gl.public.view
    def get_quote_source_url(self, quote_id: str) -> str:
        q = self._quote(quote_id)
        ev = json.loads(q.history)
        rng = _history_range(ev["windows"])
        return _open_meteo_url(int(q.lat_micro), int(q.lon_micro), rng[0], rng[1])
