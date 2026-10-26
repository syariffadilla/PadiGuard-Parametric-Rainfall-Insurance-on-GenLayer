# How PadiGuard reaches consensus

`contracts/padi_guard.py` has three methods that read the outside world. All use a
custom leader/validator pair through `gl.vm.run_nondet(leader_fn, validator_fn)`
(py-genlayer `1jb45aa8…`, GenVM v0.2.x; renamed `gl.vm.run_nondet_default` in v0.3).
This document describes the milestone v1 contract (`0x4dA4E888B9Eb59227bC948767a0a424A0a8310Bd`);
the accepted version's two-source rules are in git history at `2b11724`.

| Method | What validators fetch | What they must agree on | What it decides |
|---|---|---|---|
| `request_quote` | Open-Meteo ERA5 archive: daily precipitation for the same calendar window in each of the last 5 years (one request) | per-year day counts, totals and wettest day (within tolerance), the terms, per-year payout fraction, hit count and expected payout (exact) | the premium rate |
| `settle` | Open-Meteo ERA5 archive, NASA POWER daily point API **and** NOAA CPC gauge analysis (PSL OPeNDAP) for the coverage window | the terms, which sources were complete, per-source totals and wettest day (within tolerance), each source's tier, which sources agreed, the outcome, the payout fraction and the exact payout in wei | whether, and how much, GEN moves |
| `dry_run_settlement` (v1) | same as `settle`, for a past window | same as `settle` | nothing moves; the evidence is stored |

In every case the leader returns a structured evidence dict, every validator
re-fetches the data itself and compares, and after consensus every node
re-checks the evidence deterministically before anything is stored or paid.

---

## 1. Quote consensus (`request_quote`)

### Which years

`_history_windows(start, end, today)` is deterministic and runs **before** the
non-deterministic block, so leader and validators use the same list:

* the coverage window shifted back 1, 2, 3 … years;
* a year is used only if its shifted window ended at least 7 days before the
  transaction date (ERA5 latency);
* the first 5 usable years are taken, newest first (`QUOTE_YEARS = 5`);
* 29 February maps to 28 February in non-leap years.

All five windows are covered by **one** Open-Meteo request from the earliest
start to the latest end (`get_quote_source_url(quote_id)` returns it).

### Evidence

```json
{
  "lat_micro": -7327400, "lon_micro": 108220700,
  "windows": [["2025-11-01","2025-11-30"], ["2024-11-01","2024-11-30"], ...],
  "terms": {"drought_mm100": 30000, "drought_severe_mm100": 15000, "flood_mm100": 0,
            "flood_severe_mm100": 0, "partial_bps": 5000, "payout_wei": 2000000000000000000},
  "source": "open-meteo", "status": "ok",
  "years": [{"start_date":"2025-11-01","end_date":"2025-11-30","expected_days":30,"days":30,"total_mm100":38440,"max_mm100":3370}, ...],
  "year_payout_bps": [0, 0, 5000, 0, 0],
  "hit_flags": [false, false, true, false, false], "hits": 1,
  "expected_payout_bps": 1000
}
```

(Values from the live Studionet quote #1 on the v1 contract `0x4dA4E888B9Eb59227bC948767a0a424A0a8310Bd`:
drought < 300 mm pays 50 %, < 150 mm pays 100 %; Nov 2023 had 246.50 mm.)

### Equivalence check (`_history_matches`)

| # | Fields | Rule |
|---|---|---|
| 1 | `lat_micro`, `lon_micro`, `windows`, `terms`, `source`, `status` | exact |
| 2 | number of years, and per year `start_date`, `end_date`, `expected_days`, `days` | exact |
| 3 | per year `total_mm100`, `max_mm100` | non-negative int, \|a−b\| ≤ max(2.00 mm, 5 %) |
| 4 | `year_payout_bps`, `hit_flags`, `hits`, `expected_payout_bps` | exact vs `_history_decide()` re-run on the **leader's own** numbers |
| 5 | `year_payout_bps`, `hit_flags`, `hits`, `expected_payout_bps` | exact vs the validator's **own** result |
| 6 | leader returned an error | disagree |

Check 4 stops a leader from reporting real numbers with a cheaper payout history.
Check 5 stops a leader from nudging a year across the trigger inside the
tolerance band. Check 3 stops invented numbers. Validators also have to agree on
`status`: if the leader claims the history is incomplete but the validator can
read it (or the reverse), the round is rejected.

### After consensus

Every node checks the identity fields again, requires `status == "ok"` with every
day of every year present (otherwise the transaction reverts with *"Rainfall history
incomplete or unavailable, try again later"* and no quote is stored), recomputes
the flags, and only then prices:

```
year_payout_bps[i] = max over components of the tier that year reaches (0, partial_bps or 10000)
expected_bps       = sum(year_payout_bps) // 5
rate_bps           = min(10000, max(pool.min_rate_bps, expected_bps + pool.loading_bps))
premium            = ceil(payout * rate_bps / 10000)
```

For untiered terms `year_payout_bps` is 0 or 10000, so `expected_bps` equals the
old `hits * 10000 / 5`.

The quote (terms, rate, premium, hit count and the full agreed history JSON) is
stored on-chain and is valid for 3 days. `buy_policy(quote_id)` charges exactly
that premium. The pool's pricing at request time is locked into the quote.

**Edge of tolerance.** If ERA5 is revised between the leader's and a validator's
fetch and the revision flips a year across the trigger, check 5 fails and the
round rotates. A quote is not money-moving on its own, so the cost is only a
retry.

**Why Open-Meteo only for quotes.** NASA POWER and NOAA CPC are used at
settlement. For pricing, one consistent long series keeps the request to a single
call (CPC would need one request per year). A pricing model that blends the sources
is still on the roadmap.

---

## 2. Settlement consensus (`settle`, `dry_run_settlement`) — milestone v1

`settle()` and `dry_run_settlement()` share `_run_settlement_consensus`: the same
leader function (`_fetch_evidence`), validator check (`_evidence_matches`) and
deterministic re-check (`_check_evidence`). A dry run uses the id `dry-run-<n>`
instead of a policy id, requires a window that ended at least 7 days ago, and only
stores the evidence.

### Sources

| Name in evidence | Product | Request |
|---|---|---|
| `open-meteo` | ERA5 reanalysis (ECMWF) via the Open-Meteo archive API | `archive-api.open-meteo.com/v1/archive?...&daily=precipitation_sum&timezone=auto` |
| `nasa-power` | NASA POWER daily point API, `PRECTOTCORR` | `power.larc.nasa.gov/api/temporal/daily/point?parameters=PRECTOTCORR&community=AG...` |
| `noaa-cpc` (new) | NOAA CPC Unified Gauge-Based Analysis of Global Daily Precipitation V1.0 RT, 0.5° grid, via NOAA PSL THREDDS OPeNDAP (ASCII) | `psl.noaa.gov/thredds/dodsC/Datasets/cpc_global_precip/precip.<YEAR>.nc.ascii?precip[t0:1:t1][iy:1:iy][ix:1:ix]` |

All three are public and need no API key. CPC details:

* grid cell: `iy = (90° − lat) // 0.5°`, `ix = (lon mod 360°) // 0.5°` (integer
  micro-degree arithmetic); the response's own `lat`/`lon` map must equal that
  cell's centre;
* time: one file per year, `t` = day of year − 1; the response's own `time` axis
  (hours since 1900-01-01) must equal the expected dates; a window crossing New
  Year is read from two files;
* missing value `-9.96921E36` → missing day; an HTTP error (e.g. 400 *"stop >= size"*
  when the days are not published yet) → `unavailable`.

The three products are made differently (reanalysis, NASA's product, gauge
interpolation) and run by different operators. They are not fully independent
measurements: they share some inputs such as gauge networks.

### Evidence

```json
{
  "policy_id": "dry-run-1", "lat_micro": -7327400, "lon_micro": 108220700,
  "start_date": "2023-08-01", "end_date": "2023-08-31", "expected_days": 31,
  "terms": {"drought_mm100": 6000, "drought_severe_mm100": 1000, "flood_mm100": 0,
            "flood_severe_mm100": 0, "partial_bps": 5000, "payout_wei": 10000000000000000000},
  "sources": [
    {"name": "open-meteo", "status": "ok", "days": 31, "total_mm100": 4840, "max_mm100": 880},
    {"name": "nasa-power", "status": "ok", "days": 31, "total_mm100": 1399, "max_mm100": 508},
    {"name": "noaa-cpc",   "status": "ok", "days": 31, "total_mm100": 744,  "max_mm100": 369}
  ],
  "sources_ok": 3, "sources_used": ["open-meteo", "nasa-power", "noaa-cpc"],
  "source_tiers": [
    {"name": "open-meteo", "drought_tier": 1, "flood_tier": -1},
    {"name": "nasa-power", "drought_tier": 1, "flood_tier": -1},
    {"name": "noaa-cpc",   "drought_tier": 2, "flood_tier": -1}
  ],
  "drought": "PARTIAL", "drought_agree": ["open-meteo", "nasa-power"],
  "flood": "DISABLED", "flood_agree": [],
  "decision": "PAYOUT", "defer_reason": "", "payout_bps": 5000, "payout_wei": 5000000000000000000
}
```

(Live Studionet dry run #1, tx `0xabccf535…4fd2`, FINALIZED; tier codes: −2 no data,
−1 component disabled, 0 none, 1 partial, 2 full.)

### Equivalence check (`_evidence_matches`)

| # | Fields | Rule |
|---|---|---|
| 1 | `policy_id`, `lat_micro`, `lon_micro`, `start_date`, `end_date`, `expected_days`, `terms` (all six term values incl. `payout_wei`) | exact |
| 2 | number of sources, order, `name`, `status`, `days` | exact |
| 3 | per source `total_mm100`, `max_mm100` | non-negative int, \|a−b\| ≤ max(2.00 mm, 5 %) |
| 4 | `sources_ok`, `sources_used`, `source_tiers`, `drought`, `drought_agree`, `flood`, `flood_agree`, `decision`, `defer_reason`, `payout_bps`, `payout_wei` | exact vs `_decide()` re-run on the **leader's own** numbers |
| 5 | the same eleven fields | exact vs the validator's **own** evidence |
| 6 | leader returned an error | disagree |

Check 2 binds *which* sources were available: a leader cannot hide a source to
change who votes (a validator that can read it disagrees). Check 5 binds each
source's tier, so a leader cannot nudge one source across a tier boundary inside
the tolerance band. Checks 1 + 4 + 5 bind the exact payout in wei.

### Decision rules (`_decide`)

1. Only **complete** sources vote (every date present; no `null`, no NASA `-999`,
   no CPC missing value, HTTP 200, correct dates and grid cell).
2. Each complete source puts each enabled component into a tier: drought on the
   window total (`< trigger` → partial or full, `< severe` → full), flood on the
   wettest day (`> trigger` → partial or full, `> severe` → full). Untiered terms
   have only *none* and *full*.
3. A component is **decided** when at least **2** complete sources put it in the same
   tier (`*_agree` lists them). Otherwise it is `UNDECIDED`.
4. Payout fraction = the highest decided tier over the components. A decided `FULL`
   pays 100 % even if the other component is undecided. Otherwise any undecided
   component → `DEFER` (`SOURCE_MISSING` if fewer than 2 sources are complete,
   `SOURCES_DIVERGE` if they are but no two agree): no money moves, the policy stays
   `ACTIVE`, anyone can retry until the deadline, then `expire()` refunds the premium.
5. `payout_wei = payout * payout_bps // 10000`.

Removed in v1: the accepted version's rule "two sources disagree but within 30 % →
decide on their mean". With three sources the majority decides instead.

| Window / terms (real data, Tasikmalaya) | Open-Meteo | NASA POWER | NOAA CPC | Outcome |
|---|---|---|---|---|
| Aug 2023, drought < 60 mm | 48.40 mm | 13.99 mm | 7.44 mm | FULL (3 agree) → 100 % |
| Aug 2023, drought < 60 mm 50 % / < 10 mm 100 % | partial | partial | full | PARTIAL (OM + NASA) → 50 % *(live dry run #1)* |
| Jan 2025, drought < 60 mm | 334.30 mm | 412.46 mm | 470.83 mm | NOT_TRIGGERED → NO_PAYOUT |
| Jan 2025, drought < 400 mm | yes | no | no | NOT_TRIGGERED (NASA + CPC) → NO_PAYOUT *(v0: mean rule → PAYOUT)* |
| Jan 2025, flood > 50 mm/day | 31.50 (no) | 80.55 (yes) | 57.52 (yes) | FULL (NASA + CPC) → 100 % *(v0: DEFER; live dry run #2)* |
| Jan 2025, drought < 450 mm 50 % / < 380 mm 100 % | full | partial | none | DEFER (SOURCES_DIVERGE) *(live dry run #3)* |
| Jan 2025, flood > 30 mm 25 % / > 60 mm 100 % | partial | full | partial | PARTIAL (OM + CPC) → 25 % |
| 10–22 Oct 2026, drought < 60 mm | 28.70 mm | incomplete (2 days `-999`) | 2.31 mm | FULL (OM + CPC) → 100 % *(v0: DEFER)* |
| 2024-12-20 → 2025-01-10, drought < 300 mm | 256.00 mm | 219.01 mm | 250.59 mm (2 files) | FULL → 100 % *(live dry run #4)* |

### After consensus

Every node runs `_check_evidence`: identity and terms equal the stored policy (or
the dry-run request), the source list is exactly `open-meteo, nasa-power, noaa-cpc`,
and `_decide()` recomputed from the agreed numbers equals every decision field.
`settle()` then re-derives the amount (`payout * payout_bps // 10000`), checks it
equals `payout_wei` and does not exceed the policy payout, releases the full
reserve, and sends the amount to the farmer. The money path never trusts a verdict
string on its own.
