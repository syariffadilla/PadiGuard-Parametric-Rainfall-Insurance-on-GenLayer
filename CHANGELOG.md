# Changelog

## v1 milestone (2026-10-26): third rainfall source, 2-of-3 settlement, tiered payouts

Delta against the accepted Project version, commit
[`2b11724`](https://github.com/syariffadilla/PadiGuard-Parametric-Rainfall-Insurance-on-GenLayer/commit/2b117249779c3c8000924840a66c5102f0f479f1).
New deployment on GenLayer Studionet: `0x4dA4E888B9Eb59227bC948767a0a424A0a8310Bd`
(the accepted version's deployment `0xE33aaE2B162663688E0FcB21F030C5BB87B5656F` is left as it was).

### Why

In the accepted version, settlement needed **both** Open-Meteo and NASA POWER to be
complete, and when the two disagreed beyond 30 % the policy was deferred. One
slow or broken source (NASA POWER often reports `-999` for its most recent days)
blocked a payout. Payouts were also all-or-nothing, so a farmer could not buy
cheaper cover that pays part of the sum for a moderate shortfall.

### Contract (`contracts/padi_guard.py`)

* **Third settlement source: NOAA CPC Unified Gauge-Based Analysis of Global Daily
  Precipitation (V1.0 RT)**, read through NOAA PSL's public THREDDS OPeNDAP
  server (no API key, ~2-day latency, 0.5° grid). New helpers `_cpc_cell`,
  `_cpc_cell_center`, `_cpc_segments`, `_cpc_url(s)`, `_parse_cpc_segment`,
  `_fetch_cpc`. A window that crosses New Year is read from two yearly files. The
  parser checks the response's own time axis (hours since 1900-01-01) against the
  expected dates and its grid cell centre against the policy location; anything
  else counts as missing. HTTP errors (e.g. 400 when the days are not published
  yet) make the source `unavailable`.
* **2-of-3 settlement rule** (`_decide`, rewritten). Only complete sources vote.
  Each puts each enabled component (drought: season total; flood: wettest day)
  into a tier. A component is decided when at least 2 sources put it in the same
  tier. One source down or disagreeing no longer blocks settlement. If no two
  sources agree → `DEFER` (`SOURCES_DIVERGE`, or `SOURCE_MISSING` with fewer than
  2 complete sources). **Removed:** the accepted version's "two sources disagree
  but within 30 % → decide on their mean" rule; the majority of three replaces it.
* **Tiered (partial) payouts.** New terms `drought_severe_mm100`,
  `flood_severe_mm100`, `partial_bps`. Crossing the trigger pays `partial_bps`
  of the payout, crossing the severe level pays 100 %. Validated in
  `_check_tier_terms` (severe drought level below the trigger, severe flood level
  above it, `partial_bps` 1–9999 when tiered, 0 when not). Severe levels 0 and
  `partial_bps` 0 = untiered = the accepted version's behaviour. Payout in wei
  = `payout * payout_bps // 10000`; the whole reserve is released at settlement.
* **Validator check binds every money-driving field** (`_evidence_matches`):
  identity and the full terms (exact), each source's name, order, status and day
  count (exact) and totals (within 2 mm / 5 %), and — recomputed from the
  leader's own numbers AND compared with the validator's own result — which
  sources were complete, each source's tier per component, which sources agreed,
  both component outcomes, `payout_bps` and the exact `payout_wei`. After
  consensus every node runs `_check_evidence` (identity, terms, source list,
  recomputed decision) before any GEN moves, and `settle()` re-derives the amount.
* **Quotes price the expected payout fraction.** `request_quote` now takes the
  tier terms, and validators agree on what each of the 5 history years would have
  paid (`year_payout_bps`), the hit count, and `expected_payout_bps` (their
  average). `rate = max(pool.min_rate, expected_payout + loading)`, capped at 100 %.
  For untiered terms this equals the old hit-rate price. Quote history is still
  Open-Meteo ERA5 only (see Limitations).
* **`dry_run_settlement(...)`** (new write method). Runs exactly the settlement
  consensus for given terms on a past window (ended ≥ 7 days ago), with no
  policy and no money, and stores the agreed evidence (`get_dry_run(id)`). This is
  how the 3-source consensus was exercised live before any real policy can settle.
* **Storage/views:** `Quote` + `Policy` gain the tier fields; `Quote` gains
  `expected_payout_bps`; `Policy` gains `paid_bps`; new `DryRun` records;
  `get_stats` adds `dry_run_count`, `settlement_sources`, `min_agreeing_sources`;
  `get_source_urls` also returns the CPC URL(s).
* **Interface change:** `request_quote(pool_id, lat, lon, start, end,
  drought_trigger_mm100, drought_severe_mm100, flood_trigger_mm100,
  flood_severe_mm100, partial_bps, payout)` (was 8 arguments).

### Tests (`tests/direct/`)

* 141 → **228** direct-mode tests, all passing; `genvm-lint check` clean.
* New real captures (2026-10-26) in `fixtures/`: `cpc_2023-08.txt`,
  `cpc_2025-01.txt`, `cpc_recent.txt`, `cpc_2024-12-20_2024-12-31.txt`,
  `cpc_2025-01-01_2025-01-10.txt`, `cpc_out_of_range.txt` (real HTTP 400), and
  Open-Meteo / NASA POWER for 2024-12-20..2025-01-10.
* New file `test_tiers_and_dry_run.py` (tiered pricing on real history, tier terms
  through quote → policy, odd-wei partial payout accounting, dry runs).
* `test_settle.py` and `test_rules.py` rewritten for the 2-of-3 + tier rules (all
  three agree, one disagrees → majority, three different tiers → defer, one down
  and two agree, one down and two disagree → defer, two down → defer, CPC time
  axis / grid cell checks, cross-year CPC files, partial and full tier payouts).
* `test_validator.py`: CPC tolerance cases, tamper cases for every new bound field
  (terms, CPC source, `source_tiers`, `*_agree`, `payout_bps`), hidden/dropped
  source attacks, partial-payout lies.
* Old tests changed **only where behaviour intentionally changed**: the mean-rule
  tests (now majority results), "NASA down → defer" (now settles with 2 sources),
  the `-999` NASA case (now settles on Open-Meteo + CPC), the deferred-expiry test
  (now uses a real three-tier split), `_rate_for`/`_history_decide` signatures,
  and the positional `request_quote` arguments.

### Frontend (`frontend/`)

* Quote form: severe drought level, severe flood level, partial payout %.
* Quote card: tier terms, expected payout (% and GEN), per-year "would have paid X %".
* Policy page: tier terms, the three sources with each source's tier, which
  sources agreed, payout fraction; links to all source URLs (incl. CPC).
* Policies tab: public list of settlement dry runs with their evidence.
* Config points to the new contract `0x4dA4E888B9Eb59227bC948767a0a424A0a8310Bd`.
* `scripts/e2e.mjs`: tiered demo quote, `dry-run <case>` step; `tools/browser_check.py`:
  tiered quote and dry-run screens.

### Docs

* `docs/LIVE_RUN.md`: new v1 section with every transaction of the v1 live run.
* `README.md`, `CONSENSUS.md`, `tests/direct/fixtures/README.md` updated.

### Not done in this milestone

* **No live `settle()` decision yet.** Policies can only cover future windows;
  the v1 demo policies cover 2026-11-01..30, so the first live settlement is
  possible from **2026-12-07**. The 3-source settlement consensus was run live
  only through `dry_run_settlement` on past windows (no money moves there).
* Quotes still price from Open-Meteo ERA5 history only (one request); the three
  sources are used at settlement.
* Still Studionet only (no testnet deployment, no GenVM v0.3 migration).
