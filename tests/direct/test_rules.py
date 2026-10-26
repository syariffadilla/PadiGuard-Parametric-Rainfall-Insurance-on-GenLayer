"""Unit tests for the pure deterministic helpers (decision rules, tiers, parsing, units)."""

from decimal import Decimal

import pytest

from tests.direct.conftest import GEN, LAT, LON, contract_module, fixture_text


@pytest.fixture
def mod(guard):
    return contract_module()


def _src(name, total, mx, status="ok", days=31):
    return {"name": name, "status": status, "days": days, "total_mm100": total, "max_mm100": mx}


def _t(mod, drought=0, flood=0, payout=7, drought_severe=0, flood_severe=0, partial_bps=0):
    return mod._terms(drought, drought_severe, flood, flood_severe, partial_bps, payout)


def _three(om, nasa, cpc):
    return [_src("open-meteo", *om), _src("nasa-power", *nasa), _src("noaa-cpc", *cpc)]


AUG = ((4840, 880), (1399, 508), (744, 369))  # real Aug 2023 totals / maxima
JAN = ((33430, 3150), (41246, 8055), (47083, 5752))  # real Jan 2025


# ---------------------------------------------------------------- units / formatting


def test_micro_degree_formatting(mod):
    assert mod._micro_to_str(-7327400) == "-7.327400"
    assert mod._micro_to_str(108220700) == "108.220700"
    assert mod._micro_to_str(5) == "0.000005"
    assert mod._micro_to_str(-5) == "-0.000005"
    assert mod._micro_to_str(0) == "0.000000"


def test_mm100_conversion(mod):
    assert mod._to_mm100(Decimal("17.00")) == 1700
    assert mod._to_mm100(Decimal("0.005")) == 1  # half-up
    assert mod._to_mm100(Decimal("82.08")) == 8208
    assert mod._to_mm100(0) == 0
    assert mod._to_mm100(Decimal("-999.0")) == -1  # NASA fill value
    assert mod._to_mm100(Decimal("-9.96921E36")) == -1  # CPC missing value
    assert mod._to_mm100(None) == -1
    assert mod._to_mm100(True) == -1
    assert mod._to_mm100("3.2") == -1


def test_within_tolerance(mod):
    assert mod._within(1000, 1200, 200, 5)  # abs
    assert not mod._within(1000, 1201, 200, 5)
    assert mod._within(100000, 105000, 200, 5)  # 5 % of max
    assert not mod._within(100000, 105300, 200, 5)


def test_window_dates(mod):
    d = mod._window_dates("2024-02-27", "2024-03-01")
    assert d == ["2024-02-27", "2024-02-28", "2024-02-29", "2024-03-01"]


# ---------------------------------------------------------------- NOAA CPC helpers


def test_cpc_grid_cell(mod):
    assert mod._cpc_cell(LAT, LON) == [194, 216]  # -7.25 N, 108.25 E
    assert mod._cpc_cell_center(LAT, LON) == [-7250000, 108250000]
    assert mod._cpc_cell(90000000, 0) == [0, 0]
    assert mod._cpc_cell(-90000000, 180000000) == [359, 360]
    assert mod._cpc_cell(0, -100000) == [180, 719]  # western hemisphere wraps to 0..360
    assert mod._cpc_cell_center(0, -100000) == [-250000, 359750000]
    assert mod._cpc_cell(-7000000, 108000000) == [194, 216]  # cell edges belong to the cell below/east
    assert mod._cpc_cell(-6999999, 107999999) == [193, 215]


def test_cpc_segments_split_by_year(mod):
    assert mod._cpc_segments("2023-08-01", "2023-08-31") == [[2023, 212, 242, "2023-08-01", "2023-08-31"]]
    assert mod._cpc_segments("2024-12-20", "2025-01-10") == [
        [2024, 354, 365, "2024-12-20", "2024-12-31"],  # 2024 is a leap year
        [2025, 0, 9, "2025-01-01", "2025-01-10"],
    ]


def test_cpc_url(mod):
    assert mod._cpc_url(LAT, LON, 2025, 0, 30) == (
        "https://psl.noaa.gov/thredds/dodsC/Datasets/cpc_global_precip/precip.2025.nc.ascii"
        "?precip%5B0:1:30%5D%5B194:1:194%5D%5B216:1:216%5D"
    )


def test_cpc_parse_real_fixture(mod):
    dates = mod._window_dates("2025-01-01", "2025-01-31")
    v = mod._parse_cpc_segment(fixture_text("cpc_2025-01.txt").encode(), dates, LAT, LON)
    assert len(v) == 31 and min(v) >= 0
    assert sum(v) == 47083 and max(v) == 5752


def test_cpc_parse_rejects_wrong_length_cell_or_garbage(mod):
    body = fixture_text("cpc_2025-01.txt").encode()
    # asked for 30 days but the response has 31 -> all missing
    assert mod._parse_cpc_segment(body, mod._window_dates("2025-01-01", "2025-01-30"), LAT, LON) == [-1] * 30
    # same body read for a point in another grid cell -> all missing
    dates = mod._window_dates("2025-01-01", "2025-01-31")
    assert mod._parse_cpc_segment(body, dates, LAT + 600000, LON) == [-1] * 31
    assert mod._parse_cpc_segment(b"Error { code = 3; };", dates, LAT, LON) == [-1] * 31
    assert mod._parse_cpc_segment(b"", dates, LAT, LON) == [-1] * 31


def test_cpc_missing_value_is_missing(mod):
    dates = mod._window_dates("2025-01-01", "2025-01-31")
    lines = fixture_text("cpc_2025-01.txt").split("\n")
    lines = [("[0][0], -9.96921E36" if ln.startswith("[0][0],") else ln) for ln in lines]
    v = mod._parse_cpc_segment("\n".join(lines).encode(), dates, LAT, LON)
    assert v[0] == -1 and all(x >= 0 for x in v[1:])
    assert mod._summarize("noaa-cpc", v, 31)["status"] == "incomplete"


# ---------------------------------------------------------------- tier terms validation


@pytest.mark.parametrize(
    "kw,msg",
    [
        (dict(drought=0, flood=0), "At least one trigger required"),
        (dict(drought=-1), "Invalid trigger"),
        (dict(drought=6000, drought_severe=-1), "Invalid trigger"),
        (dict(drought=6000, partial_bps=-1), "Invalid trigger"),
        (dict(drought=6000, drought_severe=6000, partial_bps=5000), "Severe drought level must be below"),
        (dict(drought=6000, drought_severe=7000, partial_bps=5000), "Severe drought level must be below"),
        (dict(flood=5000, drought_severe=1000, partial_bps=5000), "Severe drought level must be below"),
        (dict(flood=5000, flood_severe=5000, partial_bps=5000), "Severe flood level must be above"),
        (dict(drought=6000, flood_severe=9000, partial_bps=5000), "Severe flood level must be above"),
        (dict(drought=6000, drought_severe=1000, partial_bps=0), "Partial payout must be 1-9999"),
        (dict(drought=6000, drought_severe=1000, partial_bps=10000), "Partial payout must be 1-9999"),
        (dict(drought=6000, partial_bps=5000), "Partial payout needs a severe level"),
        (dict(drought=6000, payout=0), "Invalid payout"),
    ],
)
def test_tier_terms_rejected(direct_vm, mod, kw, msg):
    with direct_vm.expect_revert(msg):
        mod._check_tier_terms(_t(mod, **kw))


def test_tier_terms_accepted(mod):
    mod._check_tier_terms(_t(mod, drought=6000))
    mod._check_tier_terms(_t(mod, drought=6000, drought_severe=1, partial_bps=1))
    mod._check_tier_terms(_t(mod, flood=5000, flood_severe=5001, partial_bps=9999))
    mod._check_tier_terms(_t(mod, drought=6000, drought_severe=2000, flood=5000, partial_bps=5000))


def test_tier_classification(mod):
    t = _t(mod, drought=6000, drought_severe=2000, flood=5000, flood_severe=8000, partial_bps=4000)
    assert [mod._drought_tier(v, t) for v in [6000, 5999, 2000, 1999, 0]] == [0, 1, 1, 2, 2]
    assert [mod._flood_tier(v, t) for v in [5000, 5001, 8000, 8001]] == [0, 1, 1, 2]
    assert [mod._tier_bps(x, t) for x in [0, 1, 2]] == [0, 4000, 10000]
    u = _t(mod, drought=6000)  # untiered: crossing the trigger is FULL
    assert mod._drought_tier(5999, u) == 2 and mod._flood_tier(99999, u) == -1


def test_payout_amount_floors(mod):
    assert mod._payout_amount(10 * GEN, 5000) == 5 * GEN
    assert mod._payout_amount(7, 3333) == 2
    assert mod._payout_amount(7, 10000) == 7


# ---------------------------------------------------------------- 2-of-3 decision rules


def test_all_agree_full(mod):
    d = mod._decide(_three(*AUG), _t(mod, drought=6000))
    assert (d["drought"], d["flood"], d["decision"], d["payout_wei"]) == ("FULL", "DISABLED", "PAYOUT", 7)
    assert d["drought_agree"] == ["open-meteo", "nasa-power", "noaa-cpc"] and d["payout_bps"] == 10000


def test_all_agree_not_triggered(mod):
    d = mod._decide(_three(*JAN), _t(mod, drought=6000))
    assert (d["decision"], d["payout_wei"], d["defer_reason"]) == ("NO_PAYOUT", 0, "")


def test_threshold_is_strict(mod):
    # drought: total < trigger ; flood: max > trigger (equality does not trigger)
    d = mod._decide(_three((6000, 5000), (6000, 5000), (6000, 5000)), _t(mod, drought=6000, flood=5000))
    assert (d["drought"], d["flood"], d["decision"]) == ("NOT_TRIGGERED", "NOT_TRIGGERED", "NO_PAYOUT")


def test_one_disagrees_majority_wins(mod):
    d = mod._decide(_three(*JAN), _t(mod, drought=40000))
    assert d["drought"] == "NOT_TRIGGERED" and d["drought_agree"] == ["nasa-power", "noaa-cpc"]
    d = mod._decide(_three(*JAN), _t(mod, flood=5000))
    assert d["flood"] == "FULL" and d["flood_agree"] == ["nasa-power", "noaa-cpc"]


def test_three_tiers_no_majority_defers(mod):
    d = mod._decide(_three(*JAN), _t(mod, drought=45000, drought_severe=38000, partial_bps=5000))
    assert (d["drought"], d["decision"], d["defer_reason"]) == ("UNDECIDED", "DEFER", "SOURCES_DIVERGE")
    assert d["payout_wei"] == 0


@pytest.mark.parametrize("status", ["incomplete", "unavailable"])
def test_one_source_missing_two_agree(mod, status):
    srcs = _three(*AUG)
    srcs[1] = _src("nasa-power", 0, 0, status=status, days=0)
    d = mod._decide(srcs, _t(mod, drought=6000))
    assert (d["sources_ok"], d["sources_used"]) == (2, ["open-meteo", "noaa-cpc"])
    assert d["source_tiers"][1] == {"name": "nasa-power", "drought_tier": -2, "flood_tier": -2}
    assert (d["drought"], d["decision"]) == ("FULL", "PAYOUT")


def test_one_source_missing_two_disagree(mod):
    srcs = _three(*JAN)
    srcs[1] = _src("nasa-power", 0, 0, status="unavailable", days=0)
    d = mod._decide(srcs, _t(mod, drought=40000))
    assert (d["drought"], d["decision"], d["defer_reason"]) == ("UNDECIDED", "DEFER", "SOURCES_DIVERGE")


@pytest.mark.parametrize("status", ["incomplete", "unavailable"])
def test_two_sources_missing_defers(mod, status):
    srcs = [_src("open-meteo", 100, 10), _src("nasa-power", 0, 0, status=status, days=0),
            _src("noaa-cpc", 0, 0, status=status, days=0)]
    d = mod._decide(srcs, _t(mod, drought=6000))
    assert (d["sources_ok"], d["sources_used"]) == (1, ["open-meteo"])
    assert (d["drought"], d["decision"], d["defer_reason"]) == ("UNDECIDED", "DEFER", "SOURCE_MISSING")


def test_no_sources_defers(mod):
    d = mod._decide([], _t(mod, drought=6000, flood=2000))
    assert (d["drought"], d["flood"], d["decision"], d["defer_reason"]) == (
        "UNDECIDED", "UNDECIDED", "DEFER", "SOURCE_MISSING")


def test_unknown_source_names_do_not_vote(mod):
    srcs = _three(*AUG) + [_src("made-up", 0, 0)]
    srcs[1]["name"] = "other"
    d = mod._decide(srcs, _t(mod, drought=6000))
    assert d["sources_used"] == ["open-meteo", "noaa-cpc"]


def test_order_of_sources_does_not_matter(mod):
    a = mod._decide(_three(*AUG), _t(mod, drought=6000, payout=GEN))
    b = mod._decide(list(reversed(_three(*AUG))), _t(mod, drought=6000, payout=GEN))
    assert a == b


def test_partial_tier_decision(mod):
    d = mod._decide(_three(*AUG), _t(mod, drought=6000, drought_severe=1000, partial_bps=5000, payout=10 * GEN))
    assert (d["drought"], d["payout_bps"], d["payout_wei"]) == ("PARTIAL", 5000, 5 * GEN)


def test_highest_decided_tier_wins(mod):
    # drought PARTIAL (2 votes) + flood FULL (2 votes) -> 100 %
    srcs = _three((5000, 9000), (5000, 9000), (8000, 100))
    d = mod._decide(srcs, _t(mod, drought=6000, drought_severe=1000, flood=5000, partial_bps=5000, payout=100))
    assert (d["drought"], d["flood"], d["payout_bps"], d["payout_wei"]) == ("PARTIAL", "FULL", 10000, 100)


def test_full_beats_undecided_partial_does_not(mod):
    # flood FULL decided, drought split three ways -> pay 100 %
    srcs = _three((500, 9000), (3000, 9000), (8000, 100))
    t = _t(mod, drought=6000, drought_severe=1000, flood=5000, partial_bps=5000)
    d = mod._decide(srcs, t)
    assert (d["drought"], d["flood"], d["decision"]) == ("UNDECIDED", "FULL", "PAYOUT")
    # flood only PARTIAL -> the undecided drought could be FULL -> DEFER
    t2 = _t(mod, drought=6000, drought_severe=1000, flood=5000, flood_severe=9500, partial_bps=5000)
    d2 = mod._decide(srcs, t2)
    assert (d2["drought"], d2["flood"], d2["decision"]) == ("UNDECIDED", "PARTIAL", "DEFER")


# ---------------------------------------------------------------- quote pricing helpers


def test_year_payout_bps(mod):
    t = _t(mod, drought=6000, drought_severe=2000, flood=5000, partial_bps=3000)
    assert mod._year_payout_bps(7000, 100, t) == 0
    assert mod._year_payout_bps(5000, 100, t) == 3000
    assert mod._year_payout_bps(1000, 100, t) == 10000
    assert mod._year_payout_bps(5000, 5001, t) == 10000  # untiered flood trigger is FULL


def test_history_decide_expected_payout(mod):
    years = [{"total_mm100": v, "max_mm100": 0} for v in [23520, 34460, 13470, 4490, 4930]]
    # untiered drought < 60 mm: 2 of 5 years -> expected 40 % (same as the old hit rate)
    h = mod._history_decide("ok", years, _t(mod, drought=6000))
    assert h == {"year_payout_bps": [0, 0, 0, 10000, 10000], "hit_flags": [False, False, False, True, True],
                 "hits": 2, "expected_payout_bps": 4000}
    # tiered < 150 mm pays 50 %, < 45 mm pays 100 %: 2020 50 %, 2019 100 %, 2018 50 % -> 200/5 = 40 %
    h = mod._history_decide("ok", years, _t(mod, drought=15000, drought_severe=4500, partial_bps=5000))
    assert h["year_payout_bps"] == [0, 0, 5000, 10000, 5000]
    assert h["hits"] == 3 and h["expected_payout_bps"] == 4000
