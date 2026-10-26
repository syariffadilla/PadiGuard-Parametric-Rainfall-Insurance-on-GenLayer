# PadiGuard: multi-insurer parametric rainfall insurance on GenLayer

PadiGuard is a web dApp plus a GenLayer Intelligent Contract for **parametric
rainfall cover for rice farmers** (*padi* is Indonesian for rice). Insurers open
capital pools. A farmer picks a pool, a field location, a coverage window and a
drought and/or flood trigger, optionally **tiered** (a moderate shortfall pays part
of the cover, a severe one pays all of it). **GenLayer validators price the policy
from five years of rainfall history at that location**, the farmer buys it, and after
the season **validators settle it from three public rainfall datasets, deciding when
at least two of them agree**. The payout goes straight to the farmer. There are no claim forms.

> **Status: working prototype on GenLayer Studionet** (test GEN, no real value).
> It is not a licensed insurance product. Current contract (milestone v1)
> `0x4dA4E888B9Eb59227bC948767a0a424A0a8310Bd`; see [docs/LIVE_RUN.md](docs/LIVE_RUN.md)
> for every transaction of the live runs and [CHANGELOG.md](CHANGELOG.md) for what
> changed in v1. No policy has been settled live yet (first possible: 2026-12-07).

![Quote screen: rainfall history agreed by validators](docs/screenshots/quote.png)

## Use case

Smallholder rice farmers in West Java lose harvests to dry spells (El Niño years)
and to extreme rain days. Indemnity insurance needs loss adjusters, and that is
too expensive for small plots. Parametric cover pays on a measurable index
(rainfall) instead, but it needs two things a normal smart contract cannot do on
its own:

1. **Price** a policy for an arbitrary field from historical weather data.
2. **Settle** it from weather data for that exact field and window, without a
   trusted oracle operator.

PadiGuard does both inside GenLayer consensus.

## How GenLayer is central

The app has no backend. The browser talks only to the GenLayer contract, through
`genlayer-js`. The two steps that make the product work are non-deterministic
GenLayer transactions:

| Step | Contract method | What validators do |
|---|---|---|
| **Quote** | `request_quote` | Each validator fetches daily precipitation for the same calendar window in each of the last 5 years from the Open-Meteo ERA5 archive. They must agree on every year's totals (within tolerance) and on exactly what each year would have paid under the (tiered) terms. The contract prices the premium from the average, the expected payout fraction. |
| **Settle** | `settle` | Each validator fetches the coverage window from Open-Meteo ERA5, NASA POWER **and** NOAA CPC (gauge analysis). A component is decided when at least 2 complete sources put it in the same tier. Validators must agree on which sources were complete, every total (within tolerance), each source's tier, which sources agreed, the payout fraction and the exact payout in wei. Then the contract pays (fully or partly), closes, or defers. |
| **Dry run** (v1) | `dry_run_settlement` | The same settlement consensus on a past window, with no policy and no money; the agreed evidence is stored publicly. |

Without GenLayer there would be no price and no payout. Both would need a trusted
off-chain service. The equivalence checks are documented in
[CONSENSUS.md](CONSENSUS.md).

## Milestone v1 (delta from the accepted version `2b11724`)

Full list with reasons: [CHANGELOG.md](CHANGELOG.md).

* **Third settlement source:** NOAA CPC Unified Gauge-Based Analysis of Global Daily
  Precipitation (NOAA PSL OPeNDAP, no API key, ~2-day latency).
* **2-of-3 settlement:** one source down or disagreeing no longer blocks a payout;
  no two sources in the same tier → defer. Replaces the old 2-source "mean within 30 %" rule.
* **Tiered payouts:** optional severe drought / flood levels and a partial payout
  fraction; quotes price the expected payout fraction from the 5-year history.
* **`dry_run_settlement`:** the settlement consensus on past windows, recorded
  on-chain, used for the v1 live run (4 rounds finalized on Studionet).
* Frontend: tier inputs, expected payout, three-source evidence with per-source
  tiers, dry-run list. 228 direct-mode tests (was 141).

## What is new compared with my earlier contract

This project builds on my earlier, single-insurer Intelligent Contract
[genlayer-rainfall-insurance](https://github.com/syariffadilla/genlayer-rainfall-insurance)
(Studionet `0x6c84A4F9B0F00c12214FBECec8cBe421Bd0CE3a3`). The accepted PadiGuard
version reused its two-source settlement helpers unchanged; milestone v1 replaced
them with the three-source, tiered version described above. New in PadiGuard:

* **A multi-insurer pool marketplace.** Any account can open a pool with its own
  capital, reserve, minimum rate, loading and coverage region (lat/lon box). The
  earlier contract had one insurer, the deployer, and one flat rate.
* **Risk-priced quotes from rainfall history.** A new non-deterministic method,
  `request_quote`, has its own validator equivalence check. The quote, with the
  agreed per-year history, is stored on-chain and `buy_policy(quote_id)` charges
  exactly that premium.
* **Per-pool accounting.** Payouts, refunds and withdrawals come from the policy's
  own pool, and pools are isolated from each other (tested).
* **Read views for a UI:** stats, paginated pool and policy lists, per-farmer
  policies and quotes, per-insurer pools, per-pool policies, and the source URLs
  for quotes and settlement.
* **The web dApp** (`frontend/`): wallet or burner account, Studio faucet,
  insurer and farmer flows, policy pages with settlement evidence, and a
  transaction log.

## Architecture

```
 Browser (static Vite app, frontend/)                 GenLayer Studionet
 ┌───────────────────────────────────┐   genlayer-js   ┌──────────────────────────────────────┐
 │ Overview  · get_stats, list_pools │ ──────────────► │ PadiGuard (contracts/padi_guard.py)   │
 │ Farmer    · request_quote ────────┼─ write ───────► │  pools ─ quotes ─ policies (TreeMaps) │
 │           · buy_policy (+premium) │                 │                                      │
 │ Insurer   · create/fund/withdraw  │                 │  request_quote ── run_nondet ──┐      │
 │ Policies  · settle / expire /     │                 │  settle ───────── run_nondet ──┤      │
 │             cancel, evidence      │ ◄── views ───── │                                │      │
 └───────────────────────────────────┘                 └────────────────────────────────┼──────┘
   account: burner key (localStorage)                    leader + validators each fetch: │
   or MetaMask (window.ethereum)                         Open-Meteo ERA5 archive ◄───────┤
                                                         NASA POWER daily API  ◄─────────┤ (settle, dry run)
                                                         NOAA CPC via PSL OPeNDAP ◄──────┘ (settle, dry run)
```

Policy lifecycle:

```
 request_quote ─► Quote (valid 3 days) ─► buy_policy ─► ACTIVE ─┬─ cancel (before start) ─► CANCELLED (premium refunded)
                                                                ├─ settle → PAYOUT ───────► PAID_OUT (full or tier % to farmer)
                                                                ├─ settle → NO_PAYOUT ────► SETTLED_NO_PAYOUT
                                                                ├─ settle → DEFER ────────► stays ACTIVE, retry later
                                                                └─ expire (after deadline) ► EXPIRED_REFUNDED
 settle window: end_date + 7 days … end_date + 67 days
```

Pricing: `expected = average over the 5 history years of the fraction that year would
have paid` (0, `partial_bps` or 100 %), `rate = min(100 %, max(pool.min_rate, expected
+ pool.loading))`, and `premium = ceil(payout × rate)`. For untiered terms `expected`
is the hit rate, as before v1. All money and rainfall values are integers: wei,
micro-degrees, hundredths of a mm, and basis points.

Tiers: drought pays `partial_bps` when the season total is below
`drought_trigger_mm100` and 100 % below `drought_severe_mm100`; flood pays
`partial_bps` when the wettest day is above `flood_trigger_mm100` and 100 % above
`flood_severe_mm100`. Severe levels 0 and `partial_bps` 0 = untiered. The payout
fraction is the highest decided tier over the two components.

## Contract API (`contracts/padi_guard.py`)

| Method | Type | Who | Notes |
|---|---|---|---|
| `create_pool(name, min_rate_bps, loading_bps, lat_min, lat_max, lon_min, lon_max)` | write, payable | anyone | `msg.value` = optional first deposit. Returns the pool id. |
| `fund_pool(pool_id)` | write, payable | pool insurer | |
| `withdraw(pool_id, amount)` | write | pool insurer | Unreserved funds only. |
| `set_pool_pricing(pool_id, min_rate_bps, loading_bps)` | write | pool insurer | Applies to new quotes. |
| `set_pool_accepting(pool_id, bool)` | write | pool insurer | Opens or closes the pool to new quotes and purchases. |
| `request_quote(pool_id, lat, lon, start, end, drought_mm100, drought_severe_mm100, flood_mm100, flood_severe_mm100, partial_bps, payout)` | write, **non-deterministic** | farmer | Validates the (tiered) terms, agrees on 5 years of history and per-year payout fractions, stores the quote. |
| `buy_policy(quote_id)` | write, payable | quote requester | `msg.value` must equal the quoted premium. Re-checks dates, pool status and liquidity. |
| `cancel_policy(policy_id)` | write | farmer | Before coverage starts: full refund. |
| `settle(policy_id)` | write, **non-deterministic** | anyone | Three-source, 2-of-3 settlement; pays (full or tier %), closes or defers. |
| `dry_run_settlement(lat, lon, start, end, drought_mm100, drought_severe_mm100, flood_mm100, flood_severe_mm100, partial_bps, payout)` | write, **non-deterministic** | anyone | Same consensus on a window that ended ≥ 7 days ago; no policy, no money; stores the evidence. |
| `expire(policy_id)` | write | anyone | After the claim deadline, if still ACTIVE: refunds the premium. |
| `get_stats()`, `get_pool(id)`, `list_pools(offset, limit)` | view | | |
| `get_quote(id)`, `get_farmer_quotes(addr)`, `get_quote_source_url(id)` | view | | |
| `get_policy(id)`, `list_policies(offset, limit)`, `get_farmer_policies(addr)`, `get_pool_policies(pool_id)`, `get_insurer_pools(addr)`, `get_source_urls(policy_id)` | view | | |
| `get_dry_run(id)` | view | | Dry-run evidence and its source URLs. |

Rules carried over from the earlier contract: coverage starts 1–365 days after
purchase and lasts at most 184 days. At least one trigger is required (0 means
disabled). The pool must have free liquidity of at least the payout.

## Deployed contract

| | |
|---|---|
| Network | GenLayer Studionet (`https://studio.genlayer.com/api`) |
| Address (v1, current) | `0x4dA4E888B9Eb59227bC948767a0a424A0a8310Bd` |
| Deploy tx | `0x269c47f2c3a5427a51bcb515ecbcee4b8292eaa1b125164577bb3350c2330167` (FINALIZED, 5/5 agree) |
| Source sha256 | `5db6a2cd3c98c1219d922f80b886e15cf7ad0b59e8618272fe3eea32f54e8818` (`npm run verify-code` compares it with the on-chain code: MATCH) |
| SDK | `py-genlayer:1jb45aa8…` (GenVM v0.2.x) |
| Accepted version (v0) | `0xE33aaE2B162663688E0FcB21F030C5BB87B5656F`, deployed 2026-10-25, unchanged |

**v1 live run (2026-10-26):** four `dry_run_settlement` rounds on past windows
(FINALIZED, MAJORITY_AGREE) in which validators fetched all three sources, including
a 2-of-3 decision where Open-Meteo disagreed, a 50 % partial tier, a three-way split
that deferred, and a window crossing New Year; two tiered `request_quote` rounds
(Node script and web UI, FINALIZED) and two tiered policies bought; `settle()`
correctly refused before the window. **v0 live run (2026-10-25)** on the accepted
deployment: pools from two insurers, quotes, purchases, a withdrawal. Full tables with
hashes: [docs/LIVE_RUN.md](docs/LIVE_RUN.md).

No policy has been settled live yet: the v1 demo policies cover 2026-11-01..30, so
the first live `settle()` can run from **2026-12-07**.

## Run it

### Contract tests (direct mode, no Studio needed)

```bash
python3 -m pip install -r requirements.txt
genvm-lint check contracts/padi_guard.py       # lint + SDK validation
genvm-lint typecheck contracts/padi_guard.py   # pyright with the GenLayer SDK
pytest tests/direct -q
```

The first run downloads the GenVM v0.2.16 SDK into `~/.cache/gltest-direct`
(`tests/direct/conftest.py` pins it). Weather responses are real API captures; see
[tests/direct/fixtures/README.md](tests/direct/fixtures/README.md).

Result on 2026-10-26 (v1): **228 passed**. `genvm-lint check`: lint passed (3
checks), validation passed (24 methods: 13 view, 11 write).

| File | Tests | Covers |
|---|---|---|
| `test_pools.py` | 12 | create/fund/withdraw/pricing/open-close, region box, isolation between pools, paging, unknown ids |
| `test_quotes.py` | 15 | history-window helper (lag, leap day), rate formula, real-data quotes (drought, flood, both), pool minimum, pricing lock-in, missing day / HTTP 503 / garbage / truncated history refused, input validation |
| `test_quote_validator.py` | 36 | quote equivalence check: agree, tolerance, out-of-tolerance, flipped year, unreachable source, missing day, leader error, 25 tampered fields (incl. terms, per-year payout, expected payout), dropped year, self-consistent lie, small drift |
| `test_buy.py` | 8 | buy from quote, exact premium, requester-only, single use, expiry, re-checks at purchase, lookups |
| `test_settle.py` | 32 | 3 sources agree, 2-of-3 majority (pays / does not pay), three tiers → defer, one source down → other two decide, one down + two disagree → defer, two down → defer, CPC not published (real HTTP 400), NASA `-999`, CPC time-axis / grid-cell checks, cross-year CPC files, partial and full tier payouts, FULL beats undecided, timing, double settle |
| `test_validator.py` | 60 | settlement equivalence check incl. CPC tolerance, 40 tampered fields (terms, every source, `source_tiers`, `*_agree`, `payout_bps`, `payout_wei`), dropped / hidden source, stall attack, partial-payout lies, 2-of-3 and partial-payout rounds |
| `test_refund_expire.py` | 8 | cancel, expire (deferred by a real three-tier split), full multi-policy lifecycle |
| `test_rules.py` | 44 | pure helpers: units, CPC grid/segments/URL/parser, tier validation and classification, 2-of-3 decision rules |
| `test_tiers_and_dry_run.py` | 13 | tiered pricing on real history, tier terms through quote → policy, odd-wei partial payouts, `dry_run_settlement` (real data, validator binding, input checks, deferred record) |

### Frontend

```bash
cd frontend
npm ci
npm run dev        # http://localhost:5173
npm run build      # static files in frontend/dist (relative base, works on GitHub Pages or Vercel)
```

The default contract address is in `frontend/src/config.js`. You can override it
at build time with `VITE_PADIGUARD_ADDRESS=0x… npm run build`, or in the app under
**Settings**.

Hosting:

* **GitHub Pages:** `.github/workflows/pages.yml` builds `frontend/` and publishes
  it. Turn it on under *Settings → Pages → Source: GitHub Actions*.
* **Vercel:** root directory `frontend`, build command `npm run build`, output
  directory `dist`.

When tested on 2026-10-25, the Studio RPC answered CORS preflight requests from
`localhost`, `127.0.0.1`, a `github.io` origin and a `vercel.app` origin.

### Live demo walkthrough

1. Open the app (local `npm run dev` or the hosted build).
2. Click **Use burner account**, then **+10 test GEN** (Studio faucet). A burner
   key is created in your browser's localStorage. Use it for Studionet test GEN
   only. **Connect MetaMask** also works through `genlayer-js`; it adds or switches
   to the Studionet network.
3. **Insurer: pools** (optional): open a pool with a name, a minimum rate, a
   loading, a region and a deposit.
4. **Farmer: get cover**: pick a pool and a location (presets for West Java rice
   districts or your own coordinates), a window starting tomorrow or later, a
   drought and/or flood trigger, optionally a severe level and a partial payout %,
   and a payout. Then click **Request quote**. After validator consensus (about 1–2
   minutes on Studionet) the quote shows the 5-year history, what each year would
   have paid and the expected payout.
5. **Buy policy**: this pays the exact quoted premium.
6. **Policies & settlement**: open the policy. Once the settlement window is
   open, **Settle now** runs the three-source consensus and shows the recorded
   evidence (each source's numbers and tier, which sources agreed, payout fraction).
   Before then the contract rejects it, and you can see that with **Try settle
   anyway**. The same tab lists the public **settlement dry runs**.

### Scripts (Node, same client library as the UI)

```bash
cd frontend
npm run deploy:contract -- --fund-gen 100   # deploy contracts/padi_guard.py (throwaway key, Studio faucet)
node scripts/e2e.mjs pool|faucet|quote|wait <tx>|buy|settle-early|read   # live end-to-end steps
node scripts/e2e.mjs dry-run aug2023-tiered|jan2025-flood|jan2025-split|xmas2024-cross-year
npm run read                                # print live contract state (no key)
npm run verify-code                         # on-chain code == contracts/padi_guard.py ?
```

Keys are read from `PADIGUARD_<ROLE>_KEY`, or from
`../.studionet-padiguard-<role>.key` **outside** the repository, which is created
on first use. `.gitignore` excludes `*.key` and `.env*`. No key is committed.

`tools/browser_check.py` drives the built frontend in headless Chromium
(Playwright) against the live contract. It was used for section B of the live run.

## Limitations

* **Prototype on Studionet.** Test GEN only. This is not an insurance product and
  has no regulatory status.
* **Basis risk.** Gridded products (about 9–25 km for ERA5 via Open-Meteo, about
  50 km for NASA POWER, 0.5° ≈ 55 km for NOAA CPC) are not a rain gauge on the
  farm. Parametric cover pays on the index, not on the actual loss. The three
  products are built differently (reanalysis, NASA's assimilation/satellite-based
  product, gauge interpolation) and run by different operators, but they are not
  fully independent measurements (they share some input data such as gauge
  networks), and CPC's gauge coverage in parts of Indonesia is sparse.
* **Source latency.** CPC is usually ~2 days behind; if a source has not
  published the whole window yet it does not vote, and with fewer than two
  complete sources settlement defers (retry until the deadline, then `expire()`).
* **Simple pricing.** Five years of history give a coarse loss estimate. Quotes use
  Open-Meteo ERA5 only (one request), while settlement uses three sources.
* **Two tiers per component.** One partial level and one full level; no
  continuous payout curve.
* **Pools are single-insurer.** One account funds and manages each pool. Pooled LP
  capital is on the roadmap.
* **Quotes cost a consensus round.** Every quote is a non-deterministic
  transaction (about 1–2 minutes on Studionet).
* **Tolerance edge.** A data revision that flips a year or a trigger between the
  leader's and a validator's fetch makes the round rotate (intended).
* **MetaMask path not tested end to end here.** It is implemented with
  `genlayer-js` (`provider: window.ethereum`, `client.connect("studionet")`). The
  burner path is the tested one.
* **SDK version.** Pinned to `py-genlayer:1jb45aa8…` (GenVM v0.2.x). Moving to
  v0.3 needs the documented renames (`run_nondet` → `run_nondet_default`, …).

## Roadmap / future milestones

Planned follow-ups, each meant to be a separately verifiable milestone:

1. **Testnet deployment and first live settlement.** Migrate to the GenVM v0.3
   SDK, deploy to the GenLayer public testnet (Bradbury), host the frontend, and
   record the first live `settle()` rounds (payout or no-payout) with their
   on-chain evidence. The current Studionet demo policies become settleable on
   2026-12-07.
2. **Liquidity-provider capital in pools.** Share-based deposits so several
   accounts can back one pool, with pro-rata premiums and losses and withdrawals
   limited to unreserved capital. UI for LPs (position, share price, utilisation).
3. **Better index and pricing.** *Partly done in milestone v1:* a third keyless
   source (NOAA CPC), 2-of-3 settlement and tiered payouts. Still open: quotes that
   use all sources and a longer history, and more than two tiers.
4. **Farmer UX for the field.** Indonesian-language UI, a map picker for the
   field location, mobile layout, and a printable policy certificate.
5. **Distribution partners.** Let cooperatives or MFIs buy and manage policies on
   behalf of member farmers (bulk quotes and purchases, member lookup).

## Repository layout

```
contracts/padi_guard.py           the Intelligent Contract
tests/direct/                     direct-mode tests + real API fixtures (228 tests)
CONSENSUS.md                      quote + settlement equivalence checks
CHANGELOG.md                      milestone v1 delta
frontend/                         Vite web app (vanilla JS + genlayer-js)
  src/lib/padiguard.js            client library shared by UI and scripts
  src/main.js                     UI
  scripts/                        deploy, e2e, read, verify-code (Node)
docs/LIVE_RUN.md                  live Studionet run with tx hashes
docs/screenshots/                 screenshots from the headless-browser run
tools/browser_check.py            headless Chromium check of the built app
.github/workflows/                CI (lint + tests + build) and GitHub Pages deploy
```

License: MIT
