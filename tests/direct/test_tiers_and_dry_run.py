"""Milestone v1: tiered quotes priced from real history, tier terms through
quote -> policy, and dry_run_settlement() (the settle() consensus on a past
window without a policy or money).

Real Open-Meteo history (Tasikmalaya, hundredths of mm, newest first):
  August 2022..2018 totals: 23520 34460 13470 4490 4930
  January 2024..2020 maxima: 4860 2150 3130 6070 10000
"""

import copy
import json

import pytest

from tests.direct.conftest import (
    FUNDED,
    GEN,
    LAT,
    LON,
    buy,
    contract_module,
    fixture_text,
    mock_cpc,
    mock_month,
    mock_nasa,
    mock_open_meteo,
    pool_invariants,
    quote,
    to_hex,
)

AUG = ("2023-08-01", "2023-08-31")
JAN = ("2025-01-01", "2025-01-31")
AUG_HIST = "om_hist_aug_2018-2022.json"
JAN_HIST = "om_hist_jan_2020-2024.json"


# ---------------------------------------------------------------- tiered quotes (real history)


def test_tiered_drought_quote_prices_expected_payout(direct_vm, clock, guard, direct_alice):
    # < 150 mm pays 50 %, < 45 mm pays 100 %: 2020 (134.70) 50 %, 2019 (44.90) 100 %, 2018 (49.30) 50 %
    q = quote(direct_vm, guard, clock, direct_alice, *AUG, drought=15000, drought_severe=4500, partial_bps=5000,
              payout=10 * GEN, body=fixture_text(AUG_HIST))
    h = json.loads(q["history"])
    assert h["year_payout_bps"] == [0, 0, 5000, 10000, 5000]
    assert h["hit_flags"] == [False, False, True, True, True] and q["hits"] == 3
    assert q["expected_payout_bps"] == 4000 == h["expected_payout_bps"]
    assert q["rate_bps"] == 4500  # 40 % expected payout + 5 % loading
    assert q["premium"] == 45 * GEN // 10
    assert (q["drought_trigger_mm100"], q["drought_severe_mm100"], q["partial_bps"]) == (15000, 4500, 5000)
    assert h["terms"] == {"drought_mm100": 15000, "drought_severe_mm100": 4500, "flood_mm100": 0,
                          "flood_severe_mm100": 0, "partial_bps": 5000, "payout_wei": 10 * GEN}


def test_same_trigger_untiered_costs_more_than_tiered(direct_vm, clock, guard, direct_alice):
    full = quote(direct_vm, guard, clock, direct_alice, *AUG, drought=15000, body=fixture_text(AUG_HIST))
    tiered = quote(direct_vm, guard, clock, direct_alice, *AUG, drought=15000, drought_severe=4500,
                   partial_bps=5000, body=fixture_text(AUG_HIST))
    assert full["expected_payout_bps"] == 6000 and full["rate_bps"] == 6500  # 3 of 5 years at 100 %
    assert tiered["expected_payout_bps"] == 4000 and tiered["rate_bps"] == 4500


def test_tiered_flood_quote_real_january_history(direct_vm, clock, guard, direct_alice):
    clock("2024-12-15")
    # > 30 mm pays 20 %, > 60 mm pays 100 %: 2024 48.60 20 %, 2023 21.50 0, 2022 31.30 20 %,
    # 2021 60.70 100 %, 2020 100.00 100 %  -> (2000+0+2000+10000+10000)/5 = 4800
    q = quote(direct_vm, guard, clock, direct_alice, *JAN, flood=3000, flood_severe=6000, partial_bps=2000,
              body=fixture_text(JAN_HIST))
    h = json.loads(q["history"])
    assert h["year_payout_bps"] == [2000, 0, 2000, 10000, 10000]
    assert q["expected_payout_bps"] == 4800 and q["rate_bps"] == 5300 and q["hits"] == 4


def test_untiered_quote_matches_accepted_version_pricing(direct_vm, clock, guard, direct_alice):
    # same numbers as the accepted version's test: 2 of 5 Augusts < 60 mm -> 40 % + 5 %
    q = quote(direct_vm, guard, clock, direct_alice, *AUG, drought=6000, body=fixture_text(AUG_HIST))
    assert (q["hits"], q["expected_payout_bps"], q["rate_bps"]) == (2, 4000, 4500)
    assert (q["drought_severe_mm100"], q["flood_severe_mm100"], q["partial_bps"]) == (0, 0, 0)


def test_tier_terms_validated_in_quote(direct_vm, guard, direct_alice):
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Severe drought level must be below the drought trigger"):
        guard.request_quote("1", LAT, LON, *AUG, 6000, 7000, 0, 0, 5000, 10 * GEN)
    with direct_vm.expect_revert("Severe flood level must be above the flood trigger"):
        guard.request_quote("1", LAT, LON, *AUG, 0, 0, 5000, 4000, 5000, 10 * GEN)
    with direct_vm.expect_revert("Partial payout must be 1-9999 bps"):
        guard.request_quote("1", LAT, LON, *AUG, 6000, 1000, 0, 0, 10000, 10 * GEN)
    with direct_vm.expect_revert("Partial payout needs a severe level"):
        guard.request_quote("1", LAT, LON, *AUG, 6000, 0, 0, 0, 5000, 10 * GEN)
    assert guard.get_stats()["quote_count"] == 0


def test_tiers_carried_from_quote_to_policy(direct_vm, clock, guard, direct_alice):
    pid = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000, drought_severe=1000,
              flood=15000, flood_severe=20000, partial_bps=3000)
    p = guard.get_policy(pid)
    assert (p["drought_trigger_mm100"], p["drought_severe_mm100"]) == (6000, 1000)
    assert (p["flood_trigger_mm100"], p["flood_severe_mm100"], p["partial_bps"]) == (15000, 20000, 3000)
    assert p["paid_bps"] == 0
    # the full payout stays reserved until settlement
    assert guard.get_pool("1")["reserved"] == 10 * GEN


def test_partial_payout_accounting_with_odd_amount(direct_vm, clock, guard, direct_alice, transfers):
    payout = 10 * GEN + 7  # 33.33 % of it is not a whole number of wei
    pid = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000, drought_severe=1000, partial_bps=3333,
              payout=payout)
    clock("2023-09-08")
    mock_month(direct_vm, "2023-08", *AUG)
    ev = guard.settle(pid)
    expected = payout * 3333 // 10000
    assert ev["payout_bps"] == 3333 and ev["payout_wei"] == expected
    assert transfers == [(to_hex(direct_alice), expected)]
    pool = pool_invariants(guard, "1", FUNDED)
    assert pool["reserved"] == 0 and pool["total_paid_out"] == expected


def test_stats_expose_sources(guard):
    s = guard.get_stats()
    assert s["settlement_sources"] == ["open-meteo", "nasa-power", "noaa-cpc"]
    assert s["min_agreeing_sources"] == 2 and s["dry_run_count"] == 0


# ---------------------------------------------------------------- dry_run_settlement


def _dry(vm, c, start, end, drought=0, flood=0, drought_severe=0, flood_severe=0, partial_bps=0, payout=10 * GEN):
    return c.dry_run_settlement(LAT, LON, start, end, drought, drought_severe, flood, flood_severe, partial_bps, payout)


def test_dry_run_on_real_august_2023(direct_vm, clock, guard, direct_bob, transfers):
    clock("2026-10-26")
    mock_month(direct_vm, "2023-08", *AUG)
    direct_vm.sender = direct_bob
    before = guard.get_pool("1")
    ev = _dry(direct_vm, guard, *AUG, drought=6000, drought_severe=1000, partial_bps=5000)
    assert ev["policy_id"] == "dry-run-1"
    assert [s["total_mm100"] for s in ev["sources"]] == [4840, 1399, 744]
    assert ev["drought"] == "PARTIAL" and ev["payout_bps"] == 5000 and ev["payout_wei"] == 5 * GEN
    # no money moves, no policy, nothing reserved
    assert transfers == []
    assert guard.get_pool("1") == before
    assert guard.get_stats()["policy_count"] == 0 and guard.get_stats()["dry_run_count"] == 1
    r = guard.get_dry_run("1")
    assert r["requester"] == to_hex(direct_bob)
    assert (r["start_date"], r["end_date"], r["created_on"]) == (*AUG, "2026-10-26")
    assert json.loads(r["evidence"]) == ev
    assert len(r["source_urls"]) == 3 and "precip.2023.nc.ascii" in r["source_urls"][2]


def test_dry_run_two_of_three_on_real_january_2025(direct_vm, clock, guard, direct_alice):
    clock("2026-10-26")
    mock_month(direct_vm, "2025-01", *JAN)
    direct_vm.sender = direct_alice
    ev = _dry(direct_vm, guard, *JAN, flood=5000)
    assert ev["flood"] == "FULL" and ev["flood_agree"] == ["nasa-power", "noaa-cpc"]
    assert ev["decision"] == "PAYOUT"
    assert guard.get_dry_run("1")["id"] == "1"


def test_dry_run_validator_binds_like_settle(direct_vm, clock, guard, direct_alice):
    clock("2026-10-26")
    mock_month(direct_vm, "2023-08", *AUG)
    direct_vm.sender = direct_alice
    ev = _dry(direct_vm, guard, *AUG, drought=6000)
    assert direct_vm.run_validator() is True
    t = copy.deepcopy(ev)
    t["payout_wei"] = 1
    assert direct_vm.run_validator(leader_result=t) is False
    t = copy.deepcopy(ev)
    t["policy_id"] = "dry-run-2"
    assert direct_vm.run_validator(leader_result=t) is False


def test_dry_run_rejects_recent_or_invalid_windows(direct_vm, clock, guard, direct_alice):
    clock("2023-09-06")  # Aug 2023 ended 6 days ago
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Dry run needs a window that ended at least 7 days ago"):
        _dry(direct_vm, guard, *AUG, drought=6000)
    clock("2026-10-26")
    with direct_vm.expect_revert("Coverage window too long"):
        _dry(direct_vm, guard, "2023-01-01", "2023-12-31", drought=6000)
    with direct_vm.expect_revert("end_date before start_date"):
        _dry(direct_vm, guard, "2023-08-31", "2023-08-01", drought=6000)
    with direct_vm.expect_revert("At least one trigger required"):
        _dry(direct_vm, guard, *AUG)
    with direct_vm.expect_revert("Invalid latitude"):
        guard.dry_run_settlement(95000000, LON, *AUG, 6000, 0, 0, 0, 0, GEN)
    with direct_vm.expect_revert("Unknown dry run"):
        guard.get_dry_run("1")
    assert guard.get_stats()["dry_run_count"] == 0


def test_dry_run_with_sources_down_defers(direct_vm, clock, guard, direct_alice):
    clock("2026-10-26")
    mock_open_meteo(direct_vm, *AUG, fixture_text("om_2023-08.json"))
    mock_nasa(direct_vm, *AUG, "down", status=503)
    mock_cpc(direct_vm, *AUG, "down", status=503)
    direct_vm.sender = direct_alice
    ev = _dry(direct_vm, guard, *AUG, drought=6000)
    assert ev["decision"] == "DEFER" and ev["defer_reason"] == "SOURCE_MISSING"
    assert guard.get_stats()["dry_run_count"] == 1  # the deferred evidence is recorded too
