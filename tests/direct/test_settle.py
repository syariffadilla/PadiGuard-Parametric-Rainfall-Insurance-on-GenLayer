"""settle(): 2-of-3 source consensus, tiered payouts, missing sources, timing, double-settle.

All web responses are real Open-Meteo / NASA POWER / NOAA CPC payloads captured
for Tasikmalaya (see fixtures/README.md). Reference numbers (hundredths of mm):

  window                    Open-Meteo        NASA POWER        NOAA CPC
                            total   max       total   max       total   max
  2023-08 (El Nino dry)      4840   880        1399   508         744   369
  2025-01 (wet season)      33430  3150       41246  8055       47083  5752
  2024-12-20..2025-01-10    25600  3130       21901  2723       25059  5752
  2026-10-10..22             2870   620   (incomplete, 11/13)     231   231

Milestone v1 behaviour changes vs the accepted version (2 sources):
  * a component is decided when 2 of the 3 sources put it in the same tier;
    the old "mean of two sources within 30 %" rule is gone;
  * one source down / incomplete no longer blocks settlement if the other two agree;
  * tiered terms can pay a partial fraction of the payout.
"""

import json

from tests.direct.conftest import (
    GEN,
    LAT,
    LON,
    pool_invariants,
    buy,
    fixture_text,
    mock_cpc,
    mock_month,
    mock_nasa,
    mock_open_meteo,
    to_hex,
)

FUNDED = 100 * GEN
AUG = ("2023-08-01", "2023-08-31")
JAN = ("2025-01-01", "2025-01-31")
XMAS = ("2024-12-20", "2025-01-10")


def _buy_jan(vm, c, clock, farmer, **kw):
    clock("2024-12-15")
    return buy(vm, c, clock, farmer, *JAN, **kw)


def _settle_jan(vm, c, clock, pid, anyone, **mocks):
    clock("2025-02-10")
    mock_month(vm, "2025-01", *JAN, **mocks)
    vm.sender = anyone
    return c.settle(pid)


def _settle_aug(vm, c, clock, pid, **mocks):
    clock("2023-09-08")
    mock_month(vm, "2023-08", *AUG, **mocks)
    return c.settle(pid)


def _tiers(ev):
    return [(s["name"], s["drought_tier"], s["flood_tier"]) for s in ev["source_tiers"]]


# ------------------------------------------------------------------ all three agree


def test_drought_payout_real_el_nino_august_2023(direct_vm, clock, guard, direct_alice, direct_bob, transfers):
    pid = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000, payout=10 * GEN)
    clock("2023-09-07")  # settlement opens end + 7 days
    mock_month(direct_vm, "2023-08", *AUG)
    direct_vm.sender = direct_bob  # anyone may settle
    ev = guard.settle(pid)

    assert ev["policy_id"] == pid
    assert (ev["lat_micro"], ev["lon_micro"]) == (LAT, LON)
    assert (ev["start_date"], ev["end_date"], ev["expected_days"]) == ("2023-08-01", "2023-08-31", 31)
    assert ev["terms"] == {"drought_mm100": 6000, "drought_severe_mm100": 0, "flood_mm100": 0,
                           "flood_severe_mm100": 0, "partial_bps": 0, "payout_wei": 10 * GEN}
    assert ev["sources"] == [
        {"name": "open-meteo", "status": "ok", "days": 31, "total_mm100": 4840, "max_mm100": 880},
        {"name": "nasa-power", "status": "ok", "days": 31, "total_mm100": 1399, "max_mm100": 508},
        {"name": "noaa-cpc", "status": "ok", "days": 31, "total_mm100": 744, "max_mm100": 369},
    ]
    assert ev["sources_ok"] == 3 and ev["sources_used"] == ["open-meteo", "nasa-power", "noaa-cpc"]
    assert _tiers(ev) == [("open-meteo", 2, -1), ("nasa-power", 2, -1), ("noaa-cpc", 2, -1)]
    assert ev["drought"] == "FULL" and ev["flood"] == "DISABLED"
    assert ev["drought_agree"] == ["open-meteo", "nasa-power", "noaa-cpc"] and ev["flood_agree"] == []
    assert ev["decision"] == "PAYOUT" and ev["defer_reason"] == ""
    assert ev["payout_bps"] == 10000 and ev["payout_wei"] == 10 * GEN

    p = guard.get_policy(pid)
    assert p["status"] == "PAID_OUT" and p["paid_amount"] == 10 * GEN and p["paid_bps"] == 10000
    assert p["settle_attempts"] == 1
    assert json.loads(p["evidence"])["decision"] == "PAYOUT"
    assert transfers == [(to_hex(direct_alice), 10 * GEN)]  # paid to the farmer, not the caller
    pool = pool_invariants(guard, "1", FUNDED)
    assert pool["reserved"] == 0
    assert pool["balance"] == FUNDED + GEN - 10 * GEN
    assert pool["total_paid_out"] == 10 * GEN


def test_flood_payout_all_sources_agree(direct_vm, clock, guard, direct_alice, transfers):
    # flood > 25 mm/day: 31.50 / 80.55 / 57.52 -> all FULL
    pid = _buy_jan(direct_vm, guard, clock, direct_alice, flood=2500, payout=5 * GEN)
    ev = _settle_jan(direct_vm, guard, clock, pid, direct_alice)
    assert ev["flood"] == "FULL" and ev["flood_agree"] == ["open-meteo", "nasa-power", "noaa-cpc"]
    assert ev["decision"] == "PAYOUT" and ev["payout_wei"] == 5 * GEN
    assert guard.get_policy(pid)["status"] == "PAID_OUT"
    assert transfers == [(to_hex(direct_alice), 5 * GEN)]
    pool_invariants(guard, "1", FUNDED)


def test_no_payout_wet_january_2025(direct_vm, clock, guard, direct_alice, direct_owner, transfers):
    pid = _buy_jan(direct_vm, guard, clock, direct_alice, drought=6000, payout=10 * GEN)
    ev = _settle_jan(direct_vm, guard, clock, pid, direct_owner)
    assert ev["drought"] == "NOT_TRIGGERED" and len(ev["drought_agree"]) == 3
    assert ev["decision"] == "NO_PAYOUT" and ev["payout_wei"] == 0 and ev["payout_bps"] == 0
    p = guard.get_policy(pid)
    assert p["status"] == "SETTLED_NO_PAYOUT" and p["paid_amount"] == 0
    assert transfers == []
    pool = pool_invariants(guard, "1", FUNDED)
    assert pool["reserved"] == 0
    assert pool["balance"] == FUNDED + GEN  # premium earned by the pool
    # released reserve is now withdrawable by the insurer
    direct_vm.sender = direct_owner
    guard.withdraw("1", FUNDED + GEN)
    assert transfers == [(to_hex(direct_owner), FUNDED + GEN)]
    pool_invariants(guard, "1", FUNDED)


# ------------------------------------------------------------------ 2-of-3: one source disagrees


def test_one_source_disagrees_majority_no_payout(direct_vm, clock, guard, direct_alice, transfers):
    # drought < 400 mm: Open-Meteo 334.30 (yes) vs NASA 412.46 (no) and CPC 470.83 (no).
    # (Accepted version: 2-source mean rule -> PAYOUT. v1: 2-of-3 majority -> NO_PAYOUT.)
    pid = _buy_jan(direct_vm, guard, clock, direct_alice, drought=40000)
    ev = _settle_jan(direct_vm, guard, clock, pid, direct_alice)
    assert _tiers(ev) == [("open-meteo", 2, -1), ("nasa-power", 0, -1), ("noaa-cpc", 0, -1)]
    assert ev["drought"] == "NOT_TRIGGERED" and ev["drought_agree"] == ["nasa-power", "noaa-cpc"]
    assert ev["decision"] == "NO_PAYOUT"
    assert guard.get_policy(pid)["status"] == "SETTLED_NO_PAYOUT"
    assert transfers == []


def test_one_source_disagrees_majority_pays(direct_vm, clock, guard, direct_alice, transfers):
    # flood > 50 mm/day: Open-Meteo 31.50 (no) vs NASA 80.55 (yes) and CPC 57.52 (yes).
    # (Accepted version: the two sources diverged -> DEFER. v1: the third source breaks the tie.)
    pid = _buy_jan(direct_vm, guard, clock, direct_alice, flood=5000)
    ev = _settle_jan(direct_vm, guard, clock, pid, direct_alice)
    assert ev["flood"] == "FULL" and ev["flood_agree"] == ["nasa-power", "noaa-cpc"]
    assert ev["decision"] == "PAYOUT" and ev["payout_wei"] == 10 * GEN
    assert transfers == [(to_hex(direct_alice), 10 * GEN)]
    pool_invariants(guard, "1", FUNDED)


# ------------------------------------------------------------------ 2-of-3: no two agree -> defer


def test_three_sources_in_three_tiers_defers(direct_vm, clock, guard, direct_alice, transfers):
    # tiered drought: < 450 mm pays 50 %, < 380 mm pays 100 %
    # OM 334.30 -> FULL, NASA 412.46 -> PARTIAL, CPC 470.83 -> NONE: no tier has 2 votes
    pid = _buy_jan(direct_vm, guard, clock, direct_alice, drought=45000, drought_severe=38000, partial_bps=5000)
    ev = _settle_jan(direct_vm, guard, clock, pid, direct_alice)
    assert _tiers(ev) == [("open-meteo", 2, -1), ("nasa-power", 1, -1), ("noaa-cpc", 0, -1)]
    assert ev["drought"] == "UNDECIDED" and ev["drought_agree"] == []
    assert ev["decision"] == "DEFER" and ev["defer_reason"] == "SOURCES_DIVERGE"
    assert ev["payout_wei"] == 0
    p = guard.get_policy(pid)
    assert p["status"] == "ACTIVE" and p["settle_attempts"] == 1
    assert json.loads(p["evidence"])["decision"] == "DEFER"
    pool = pool_invariants(guard, "1", FUNDED)
    assert pool["reserved"] == 10 * GEN  # reserve stays locked while deferred
    assert transfers == []


def test_one_down_and_remaining_two_disagree_defers(direct_vm, clock, guard, direct_alice, transfers):
    # drought < 400 mm with NASA down: OM yes vs CPC no -> no 2 votes -> DEFER (SOURCES_DIVERGE)
    pid = _buy_jan(direct_vm, guard, clock, direct_alice, drought=40000)
    clock("2025-02-10")
    mock_open_meteo(direct_vm, *JAN, fixture_text("om_2025-01.json"))
    mock_nasa(direct_vm, *JAN, "Service Unavailable", status=503)
    mock_cpc(direct_vm, *JAN, fixture_text("cpc_2025-01.txt"))
    ev = guard.settle(pid)
    assert ev["sources_ok"] == 2 and ev["sources_used"] == ["open-meteo", "noaa-cpc"]
    assert ev["decision"] == "DEFER" and ev["defer_reason"] == "SOURCES_DIVERGE"
    # NASA back -> majority NOT_TRIGGERED
    direct_vm.clear_mocks()
    clock("2025-02-11")
    mock_month(direct_vm, "2025-01", *JAN)
    ev = guard.settle(pid)
    assert ev["decision"] == "NO_PAYOUT" and guard.get_policy(pid)["settle_attempts"] == 2
    assert transfers == []


# ------------------------------------------------------------------ one source down / incomplete


def test_one_source_down_other_two_agree_settles(direct_vm, clock, guard, direct_alice, transfers):
    # (Accepted version: DEFER until NASA came back. v1: OM + CPC agree -> pays now.)
    pid = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000)
    clock("2023-09-08")
    mock_open_meteo(direct_vm, *AUG, fixture_text("om_2023-08.json"))
    mock_nasa(direct_vm, *AUG, "Service Unavailable", status=503)
    mock_cpc(direct_vm, *AUG, fixture_text("cpc_2023-08.txt"))
    ev = guard.settle(pid)
    assert ev["sources"][1] == {"name": "nasa-power", "status": "unavailable", "days": 0,
                                "total_mm100": 0, "max_mm100": 0}
    assert ev["sources_ok"] == 2 and ev["sources_used"] == ["open-meteo", "noaa-cpc"]
    assert _tiers(ev)[1] == ("nasa-power", -2, -2)  # no data: does not vote
    assert ev["drought"] == "FULL" and ev["drought_agree"] == ["open-meteo", "noaa-cpc"]
    assert ev["decision"] == "PAYOUT"
    assert transfers == [(to_hex(direct_alice), 10 * GEN)]
    pool_invariants(guard, "1", FUNDED)


def test_cpc_not_published_yet_other_two_agree(direct_vm, clock, guard, direct_alice, transfers):
    # Real CPC HTTP 400 "stop >= size" response (days not published yet) -> unavailable
    pid = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000)
    ev = _settle_aug(direct_vm, guard, clock, pid, cpc_body=fixture_text("cpc_out_of_range.txt"), cpc_status=400)
    assert ev["sources"][2]["status"] == "unavailable"
    assert ev["drought_agree"] == ["open-meteo", "nasa-power"] and ev["decision"] == "PAYOUT"
    assert len(transfers) == 1


def test_nasa_fill_values_incomplete_other_two_agree(direct_vm, clock, guard, direct_alice, transfers):
    # Real NASA POWER response with -999 fill values for the last two days.
    # (Accepted version: DEFER SOURCE_MISSING. v1: OM 28.70 mm and CPC 2.31 mm both < 60 mm -> pays.)
    start, end = "2026-10-10", "2026-10-22"
    clock("2026-09-01")
    pid = buy(direct_vm, guard, clock, direct_alice, start, end, drought=6000)
    clock("2026-10-30")
    mock_month(direct_vm, "recent", start, end)
    ev = guard.settle(pid)
    assert ev["sources"][1] == {"name": "nasa-power", "status": "incomplete", "days": 11,
                                "total_mm100": 43272, "max_mm100": 8208}
    assert ev["sources"][0] == {"name": "open-meteo", "status": "ok", "days": 13, "total_mm100": 2870, "max_mm100": 620}
    assert ev["sources"][2] == {"name": "noaa-cpc", "status": "ok", "days": 13, "total_mm100": 231, "max_mm100": 231}
    assert ev["sources_ok"] == 2 and ev["sources_used"] == ["open-meteo", "noaa-cpc"]
    assert ev["decision"] == "PAYOUT"
    assert transfers == [(to_hex(direct_alice), 10 * GEN)]


def test_two_sources_down_defers_then_settles_on_retry(direct_vm, clock, guard, direct_alice, transfers):
    pid = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000)
    clock("2023-09-08")
    mock_open_meteo(direct_vm, *AUG, fixture_text("om_2023-08.json"))
    mock_nasa(direct_vm, *AUG, "Service Unavailable", status=503)
    mock_cpc(direct_vm, *AUG, fixture_text("cpc_out_of_range.txt"), status=400)
    ev = guard.settle(pid)
    assert [s["status"] for s in ev["sources"]] == ["ok", "unavailable", "unavailable"]
    assert ev["sources_ok"] == 1 and ev["drought"] == "UNDECIDED"
    assert ev["decision"] == "DEFER" and ev["defer_reason"] == "SOURCE_MISSING"
    assert guard.get_policy(pid)["settle_attempts"] == 1

    # sources back online the next day -> settles normally
    direct_vm.clear_mocks()
    clock("2023-09-09")
    mock_month(direct_vm, "2023-08", *AUG)
    ev = guard.settle(pid)
    assert ev["decision"] == "PAYOUT"
    p = guard.get_policy(pid)
    assert p["status"] == "PAID_OUT" and p["settle_attempts"] == 2
    assert transfers == [(to_hex(direct_alice), 10 * GEN)]
    pool_invariants(guard, "1", FUNDED)


def test_all_sources_unavailable_or_garbage_defers(direct_vm, clock, guard, direct_alice):
    pid = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000)
    clock("2023-09-08")
    mock_open_meteo(direct_vm, *AUG, "<html>not json</html>")
    mock_nasa(direct_vm, *AUG, '{"messages": ["error"]}')
    mock_cpc(direct_vm, *AUG, "Error { code = 500; };", status=500)
    ev = guard.settle(pid)
    assert [s["status"] for s in ev["sources"]] == ["unavailable", "unavailable", "unavailable"]
    assert ev["sources_ok"] == 0 and ev["decision"] == "DEFER" and ev["defer_reason"] == "SOURCE_MISSING"


def test_open_meteo_null_day_is_incomplete(direct_vm, clock, guard, direct_alice):
    pid = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000)
    om = json.loads(fixture_text("om_2023-08.json"))
    om["daily"]["precipitation_sum"][30] = None  # real API returns null for not-yet-available days
    ev = _settle_aug(direct_vm, guard, clock, pid, om_body=json.dumps(om))
    assert ev["sources"][0]["status"] == "incomplete" and ev["sources"][0]["days"] == 30
    assert ev["sources_used"] == ["nasa-power", "noaa-cpc"] and ev["decision"] == "PAYOUT"


def test_open_meteo_wrong_window_rejected(direct_vm, clock, guard, direct_alice):
    pid = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000)
    om = json.loads(fixture_text("om_2023-08.json"))
    om["daily"]["time"][0] = "2023-07-31"  # response for a different window must not count
    ev = _settle_aug(direct_vm, guard, clock, pid, om_body=json.dumps(om))
    assert ev["sources"][0] == {"name": "open-meteo", "status": "incomplete", "days": 0,
                                "total_mm100": 0, "max_mm100": 0}
    assert ev["sources_ok"] == 2


def test_cpc_wrong_time_axis_rejected(direct_vm, clock, guard, direct_alice):
    # a CPC response whose time axis is one day off must not count
    body = fixture_text("cpc_2023-08.txt").replace("1083288.0, 1083312.0", "1083264.0, 1083312.0")
    pid = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000)
    ev = _settle_aug(direct_vm, guard, clock, pid, cpc_body=body)
    assert ev["sources"][2] == {"name": "noaa-cpc", "status": "incomplete", "days": 0,
                                "total_mm100": 0, "max_mm100": 0}
    assert ev["sources_used"] == ["open-meteo", "nasa-power"]


def test_cpc_wrong_grid_cell_rejected(direct_vm, clock, guard, direct_alice):
    body = fixture_text("cpc_2023-08.txt").replace("precip.lat[1]\n-7.25", "precip.lat[1]\n-7.75")
    assert body != fixture_text("cpc_2023-08.txt")
    pid = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000)
    ev = _settle_aug(direct_vm, guard, clock, pid, cpc_body=body)
    assert ev["sources"][2]["status"] == "incomplete" and ev["sources"][2]["days"] == 0


def test_cross_year_window_uses_two_cpc_files(direct_vm, clock, guard, direct_alice, transfers):
    # 2024-12-20..2025-01-10 (22 days): CPC needs precip.2024.nc and precip.2025.nc
    clock("2024-12-01")
    pid = buy(direct_vm, guard, clock, direct_alice, *XMAS, drought=30000)
    urls = guard.get_source_urls(pid)
    assert len(urls) == 4
    assert urls[2].endswith("precip.2024.nc.ascii?precip%5B354:1:365%5D%5B194:1:194%5D%5B216:1:216%5D")
    assert urls[3].endswith("precip.2025.nc.ascii?precip%5B0:1:9%5D%5B194:1:194%5D%5B216:1:216%5D")
    clock("2025-01-20")
    mock_open_meteo(direct_vm, *XMAS, fixture_text("om_2024-12-20_2025-01-10.json"))
    mock_nasa(direct_vm, *XMAS, fixture_text("nasa_2024-12-20_2025-01-10.json"))
    mock_cpc(direct_vm, "2024-12-20", "2024-12-31", fixture_text("cpc_2024-12-20_2024-12-31.txt"))
    mock_cpc(direct_vm, "2025-01-01", "2025-01-10", fixture_text("cpc_2025-01-01_2025-01-10.txt"))
    ev = guard.settle(pid)
    assert ev["expected_days"] == 22
    assert ev["sources"] == [
        {"name": "open-meteo", "status": "ok", "days": 22, "total_mm100": 25600, "max_mm100": 3130},
        {"name": "nasa-power", "status": "ok", "days": 22, "total_mm100": 21901, "max_mm100": 2723},
        {"name": "noaa-cpc", "status": "ok", "days": 22, "total_mm100": 9026 + 16033, "max_mm100": 5752},
    ]
    assert ev["decision"] == "PAYOUT" and transfers == [(to_hex(direct_alice), 10 * GEN)]


def test_cross_year_window_one_cpc_file_missing(direct_vm, clock, guard, direct_alice):
    clock("2024-12-01")
    pid = buy(direct_vm, guard, clock, direct_alice, *XMAS, drought=30000)
    clock("2025-01-20")
    mock_open_meteo(direct_vm, *XMAS, fixture_text("om_2024-12-20_2025-01-10.json"))
    mock_nasa(direct_vm, *XMAS, fixture_text("nasa_2024-12-20_2025-01-10.json"))
    mock_cpc(direct_vm, "2024-12-20", "2024-12-31", fixture_text("cpc_2024-12-20_2024-12-31.txt"))
    mock_cpc(direct_vm, "2025-01-01", "2025-01-10", fixture_text("cpc_out_of_range.txt"), status=400)
    ev = guard.settle(pid)
    assert ev["sources"][2]["status"] == "unavailable"
    assert ev["sources_used"] == ["open-meteo", "nasa-power"] and ev["decision"] == "PAYOUT"


# ------------------------------------------------------------------ tiered payouts


def test_tiered_drought_partial_payout(direct_vm, clock, guard, direct_alice, transfers):
    # < 60 mm pays 50 %, < 10 mm pays 100 %:
    # OM 48.40 -> PARTIAL, NASA 13.99 -> PARTIAL, CPC 7.44 -> FULL  => 2-of-3 PARTIAL -> 50 %
    pid = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000, drought_severe=1000,
              partial_bps=5000, payout=10 * GEN)
    p0 = guard.get_policy(pid)
    assert (p0["drought_trigger_mm100"], p0["drought_severe_mm100"], p0["partial_bps"]) == (6000, 1000, 5000)
    ev = _settle_aug(direct_vm, guard, clock, pid)
    assert _tiers(ev) == [("open-meteo", 1, -1), ("nasa-power", 1, -1), ("noaa-cpc", 2, -1)]
    assert ev["drought"] == "PARTIAL" and ev["drought_agree"] == ["open-meteo", "nasa-power"]
    assert ev["decision"] == "PAYOUT" and ev["payout_bps"] == 5000 and ev["payout_wei"] == 5 * GEN
    p = guard.get_policy(pid)
    assert p["status"] == "PAID_OUT" and p["paid_amount"] == 5 * GEN and p["paid_bps"] == 5000
    assert transfers == [(to_hex(direct_alice), 5 * GEN)]
    pool = pool_invariants(guard, "1", FUNDED)
    assert pool["reserved"] == 0  # the whole reserve is released, only 50 % paid
    assert pool["balance"] == FUNDED + GEN - 5 * GEN
    assert pool["total_paid_out"] == 5 * GEN


def test_tiered_drought_full_payout(direct_vm, clock, guard, direct_alice, transfers):
    # severe level 20 mm: NASA 13.99 and CPC 7.44 -> FULL (2 votes), OM 48.40 -> PARTIAL
    pid = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000, drought_severe=2000, partial_bps=5000)
    ev = _settle_aug(direct_vm, guard, clock, pid)
    assert ev["drought"] == "FULL" and ev["drought_agree"] == ["nasa-power", "noaa-cpc"]
    assert ev["payout_bps"] == 10000 and transfers == [(to_hex(direct_alice), 10 * GEN)]


def test_tiered_flood_partial_payout(direct_vm, clock, guard, direct_alice, transfers):
    # flood > 30 mm pays 25 %, > 60 mm pays 100 %: OM 31.50 PARTIAL, NASA 80.55 FULL, CPC 57.52 PARTIAL
    pid = _buy_jan(direct_vm, guard, clock, direct_alice, flood=3000, flood_severe=6000, partial_bps=2500,
                   payout=8 * GEN)
    ev = _settle_jan(direct_vm, guard, clock, pid, direct_alice)
    assert _tiers(ev) == [("open-meteo", -1, 1), ("nasa-power", -1, 2), ("noaa-cpc", -1, 1)]
    assert ev["flood"] == "PARTIAL" and ev["flood_agree"] == ["open-meteo", "noaa-cpc"]
    assert ev["payout_bps"] == 2500 and ev["payout_wei"] == 2 * GEN
    assert transfers == [(to_hex(direct_alice), 2 * GEN)]
    pool_invariants(guard, "1", FUNDED)


def test_full_component_beats_undecided_component(direct_vm, clock, guard, direct_alice, transfers):
    # flood > 50 mm: FULL (NASA, CPC); drought 450/380 tiers: UNDECIDED -> FULL pays 100 % anyway
    pid = _buy_jan(direct_vm, guard, clock, direct_alice, drought=45000, drought_severe=38000,
                   flood=5000, partial_bps=5000)
    ev = _settle_jan(direct_vm, guard, clock, pid, direct_alice)
    assert (ev["drought"], ev["flood"], ev["decision"]) == ("UNDECIDED", "FULL", "PAYOUT")
    assert ev["payout_bps"] == 10000 and ev["defer_reason"] == ""
    assert transfers == [(to_hex(direct_alice), 10 * GEN)]


def test_partial_component_plus_undecided_defers(direct_vm, clock, guard, direct_alice, transfers):
    # flood 30/60: PARTIAL; drought 450/380: UNDECIDED -> the undecided one could be FULL -> DEFER
    pid = _buy_jan(direct_vm, guard, clock, direct_alice, drought=45000, drought_severe=38000,
                   flood=3000, flood_severe=6000, partial_bps=5000)
    ev = _settle_jan(direct_vm, guard, clock, pid, direct_alice)
    assert (ev["drought"], ev["flood"], ev["decision"]) == ("UNDECIDED", "PARTIAL", "DEFER")
    assert ev["defer_reason"] == "SOURCES_DIVERGE" and ev["payout_wei"] == 0
    assert transfers == []


def test_not_triggered_plus_undecided_defers(direct_vm, clock, guard, direct_alice):
    pid = _buy_jan(direct_vm, guard, clock, direct_alice, drought=6000, flood=6000, flood_severe=9000,
                   partial_bps=5000)
    # flood 60/90: OM 31.50 NONE, NASA 80.55 PARTIAL, CPC 57.52 NONE -> NOT_TRIGGERED (OM, CPC)
    ev = _settle_jan(direct_vm, guard, clock, pid, direct_alice)
    assert (ev["drought"], ev["flood"], ev["decision"]) == ("NOT_TRIGGERED", "NOT_TRIGGERED", "NO_PAYOUT")
    # now make the drought component undecided: NASA down + OM vs CPC split at 400 mm
    pid2 = _buy_jan(direct_vm, guard, clock, direct_alice, drought=40000, flood=6000)
    clock("2025-02-10")
    direct_vm.clear_mocks()
    mock_open_meteo(direct_vm, *JAN, fixture_text("om_2025-01.json"))
    mock_nasa(direct_vm, *JAN, "down", status=503)
    mock_cpc(direct_vm, *JAN, fixture_text("cpc_2025-01.txt"))
    ev2 = guard.settle(pid2)
    assert (ev2["drought"], ev2["flood"], ev2["decision"]) == ("UNDECIDED", "NOT_TRIGGERED", "DEFER")
    assert guard.get_policy(pid2)["status"] == "ACTIVE"


# ------------------------------------------------------------------ timing / state machine


def test_settle_before_window_opens_reverts(direct_vm, clock, guard, direct_alice):
    pid = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000)
    direct_vm.warp("2023-09-06T23:59:59Z")  # end + 6 days
    with direct_vm.expect_revert("Settlement not open yet"):
        guard.settle(pid)
    assert guard.get_policy(pid)["settle_attempts"] == 0


def test_settle_on_deadline_day_allowed(direct_vm, clock, guard, direct_alice):
    pid = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000)
    direct_vm.warp("2023-11-06T12:00:00Z")  # deadline day itself is still allowed
    mock_month(direct_vm, "2023-08", *AUG)
    assert guard.settle(pid)["decision"] == "PAYOUT"


def test_settle_day_after_deadline_reverts(direct_vm, clock, guard, direct_alice):
    pid = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000)
    clock("2023-11-07")
    with direct_vm.expect_revert("Settlement deadline passed"):
        guard.settle(pid)


def test_double_settle_reverts_and_pays_once(direct_vm, clock, guard, direct_alice, transfers):
    pid = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000)
    clock("2023-09-10")
    mock_month(direct_vm, "2023-08", *AUG)
    guard.settle(pid)
    with direct_vm.expect_revert("Policy not active"):
        guard.settle(pid)
    assert transfers == [(to_hex(direct_alice), 10 * GEN)]
    pool = pool_invariants(guard, "1", FUNDED)
    assert pool["total_paid_out"] == 10 * GEN


def test_double_settle_after_partial_payout_reverts(direct_vm, clock, guard, direct_alice, transfers):
    pid = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000, drought_severe=1000, partial_bps=5000)
    _settle_aug(direct_vm, guard, clock, pid)
    with direct_vm.expect_revert("Policy not active"):
        guard.settle(pid)
    assert transfers == [(to_hex(direct_alice), 5 * GEN)]


def test_double_settle_after_no_payout_reverts(direct_vm, clock, guard, direct_alice):
    pid = _buy_jan(direct_vm, guard, clock, direct_alice, drought=6000)
    _settle_jan(direct_vm, guard, clock, pid, direct_alice)
    with direct_vm.expect_revert("Policy not active"):
        guard.settle(pid)


def test_multiple_policies_independent(direct_vm, clock, guard, direct_alice, direct_bob, transfers):
    p1 = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000, payout=20 * GEN)
    p2 = buy(direct_vm, guard, clock, direct_bob, *AUG, drought=500, payout=30 * GEN)  # 5 mm
    clock("2023-09-10")
    mock_month(direct_vm, "2023-08", *AUG)
    assert guard.settle(p1)["decision"] == "PAYOUT"
    ev2 = guard.settle(p2)
    # trigger 5 mm: 48.40 / 13.99 / 7.44 are all above it -> NOT_TRIGGERED
    assert ev2["decision"] == "NO_PAYOUT"
    assert transfers == [(to_hex(direct_alice), 20 * GEN)]
    pool = pool_invariants(guard, "1", FUNDED)
    assert pool["reserved"] == 0
    assert pool["balance"] == FUNDED + 2 * GEN + 3 * GEN - 20 * GEN


def test_source_urls_view_lists_three_sources(direct_vm, clock, guard, direct_alice):
    pid = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000)
    urls = guard.get_source_urls(pid)
    assert len(urls) == 3
    assert urls[0].startswith("https://archive-api.open-meteo.com/v1/archive?latitude=-7.327400")
    assert urls[1].startswith("https://power.larc.nasa.gov/api/temporal/daily/point?parameters=PRECTOTCORR")
    assert urls[2] == (
        "https://psl.noaa.gov/thredds/dodsC/Datasets/cpc_global_precip/precip.2023.nc.ascii"
        "?precip%5B212:1:242%5D%5B194:1:194%5D%5B216:1:216%5D"
    )
