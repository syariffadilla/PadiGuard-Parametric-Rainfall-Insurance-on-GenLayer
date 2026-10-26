# Live runs on GenLayer Studionet

* [v1 milestone (2026-10-26)](#v1-milestone-live-run-2026-10-26): new contract with 3 rainfall sources, 2-of-3 settlement and tiered payouts.
* [v0 accepted version (2026-10-25)](#v0-accepted-version-live-run-on-genlayer-studionet-2026-10-25): kept below unchanged.

# v1 milestone: live run (2026-10-26)

Everything below ran against the **v1** contract on GenLayer Studionet
(`https://studio.genlayer.com/api`, chain id `0xf22f`). All keys were throwaway
Studionet keys (the same roles as in v0); balances are Studio test GEN with no real value.

| | |
|---|---|
| Contract | `0x4dA4E888B9Eb59227bC948767a0a424A0a8310Bd` |
| Source | `contracts/padi_guard.py`, sha256 `5db6a2cd3c98c1219d922f80b886e15cf7ad0b59e8618272fe3eea32f54e8818` |
| On-chain code check | `npm run verify-code` → `MATCH` (code returned by `getContractCode` is byte-identical to the file) |
| Deploy tx | `0x269c47f2c3a5427a51bcb515ecbcee4b8292eaa1b125164577bb3350c2330167` · FINALIZED · MAJORITY_AGREE (5/5 agree) |

Accounts: insurer `0xd3E7eBD19e1501ee94B8aBE66933Ed5316f47B83`, farmer
`0x4a2ED8EF12c17B9Ea7D1d6eC4511bbea25213c68` (Node script), browser burner
`0xF8caAb3Dc756a83c234342E0f116CFCDeD4AD625` (headless Chromium driving the built frontend).

## A. Settlement consensus with three sources: `dry_run_settlement` on past windows

`dry_run_settlement` runs the same code path as `settle()` (`_run_settlement_consensus`:
leader and validators each fetch Open-Meteo ERA5, NASA POWER and NOAA CPC, validator
check `_evidence_matches`, deterministic re-check `_check_evidence`) for a past window,
**without a policy and without moving any GEN**. Location: Tasikmalaya, West Java
(-7.327400, 108.220700), payout parameter 10 GEN. All four rounds: **FINALIZED ·
MAJORITY_AGREE (3 agree, 2 idle)**. The rainfall numbers the validators agreed on are
identical to the fixtures captured with curl for the direct-mode tests.

| # | Tx | Window · terms | Open-Meteo total / max (mm) | NASA POWER | NOAA CPC | Agreed outcome |
|---|---|---|---|---|---|---|
| 1 | `0xabccf535f27f0cfaeb6ca5edbec8ed8fa7b361451065f3b0c112d9ac64134fd2` | Aug 2023 · drought < 60 mm pays 50 %, < 10 mm pays 100 % | 48.40 / 8.80 → partial | 13.99 / 5.08 → partial | 7.44 / 3.69 → full | drought **PARTIAL** (Open-Meteo + NASA) → would pay **50 %** = 5 GEN |
| 2 | `0xbfffef37aa91a03df75d41815b0688e9152a1cc199096468692545b54761ee32` | Jan 2025 · flood: a day > 50 mm | 334.30 / 31.50 → none | 412.46 / 80.55 → full | 470.83 / 57.52 → full | flood **FULL** (NASA + CPC) → would pay 100 %. *The accepted 2-source version deferred this case (sources diverged).* |
| 3 | `0xdc887feb79f0d1c7676733fc2616ea7227816c996347539f2242ae449bfd8ee2` | Jan 2025 · drought < 450 mm pays 50 %, < 380 mm pays 100 % | → full | → partial | → none | no two sources in the same tier → **DEFER (SOURCES_DIVERGE)**, would pay 0 |
| 4 | `0x4f6dd20cb8724786989b208aaf6e5b20bcc4e374e799f666351bdee14568ddd5` | 2024-12-20 → 2025-01-10 (crosses New Year: CPC read from the 2024 and 2025 files) · drought < 300 mm | 256.00 / 31.30 → full | 219.01 / 27.23 → full | 250.59 / 57.52 → full | drought **FULL** (all three) → would pay 100 % |

The stored records are readable with `get_dry_run("1")` … `get_dry_run("4")` and are
shown in the app under **Policies & settlement → Settlement dry runs**
(`docs/screenshots/dry-runs.png`).

## B. Pool, tiered quote and purchase (Node script `frontend/scripts/e2e.mjs`)

| Step | Tx | Result |
|---|---|---|
| `create_pool("West Java Padi Pool (demo)", 1000, 500, West Java box)` + 20 GEN | `0x33a9b552d61514d6f78e52dcab5f14029c3c432ff5889c3319c66acfa7d4fc3d` | FINALIZED · MAJORITY_AGREE · pool `"1"` |
| `fund_pool("1")` + 5 GEN | `0x6ae148d5bf3ec437c308d4692ff21c196d6657152a37a2d50af62a2faef7f398` | FINALIZED · pool balance 25 GEN |
| `request_quote("1", -7327400, 108220700, "2026-11-01", "2026-11-30", 30000, 15000, 0, 0, 5000, 2 GEN)` (drought < 300 mm pays 50 %, < 150 mm pays 100 %) | `0xb535333328f67047a356761dff289e0d8117b2c0116b45ed44c9a699c6afe2b3` | **FINALIZED · MAJORITY_AGREE (3 agree, 2 idle)**. History Nov 2021–2025; only Nov 2023 (246.50 mm) is below 300 mm and above 150 mm → would have paid 50 %; per-year `[0, 0, 5000, 0, 0]` bps → expected payout **10 %** → rate 10 % + 5 % loading = **15 %** → premium **0.3 GEN** |
| `buy_policy("1")` + 0.3 GEN | `0x657e8615b4c58523f0abc152836094d443c63c7a1c437593c031a51d5440673e` | FINALIZED · policy `"1"` (tier terms stored on the policy) |
| `settle("1")` before the window | `0xbfe9c2c3e05168210fde5d6748a75edbb84114ba50af4f0a2e2d9397b3e25dae` | FINALIZED, execution ERROR **"Settlement not open yet"** (expected: opens 2026-12-07) |

For comparison, the same window with an *untiered* 300 mm trigger was priced at 25 %
(0.5 GEN) in the v0 live run; the tiered version costs 15 % because the one dry
year would only have paid half.

## C. Browser (built `frontend/dist`, headless Chromium, `tools/browser_check.py`)

| UI action | Tx | Result |
|---|---|---|
| "+10 test GEN" (Studio faucet, `sim_fundAccount`) | `0xd8ef7057fa7a5a9611717f3762786df8603f172530781f4c004db858f2fc18f8` | balance shown |
| Farmer tab → Request quote: drought < 350 mm pays 40 %, < 250 mm pays 100 %, payout 2 GEN | `0xe56c7da6d48562308fc27e9602ca12e7223e677db36ef2888584fc14897606a5` | **FINALIZED · MAJORITY_AGREE (3 agree, 2 idle)**. Quote #2: Nov 2023 (246.50 mm) would have paid 100 %, other years 0 → expected payout 20 % → rate 25 % → premium 0.5 GEN. The card shows the expected payout (20 % · 0.4 GEN) and "would have paid 100.00 %" for 2023 (`docs/screenshots/quote.png`). |
| Buy policy · pay 0.5 GEN | `0x144c1b0939b55bf25dd4d4baf492c08a2d8a338e51e2360774adb87d3b92ef90` | FINALIZED · policy `"2"` (`docs/screenshots/bought.png`) |
| Overview, policy #1 page, dry-run list | (reads) | `docs/screenshots/overview.png`, `policy.png`, `dry-runs.png` |

## Not done live in v1 (and why)

* **No live `settle()` decision on a policy.** A policy can only cover a future
  window and settlement opens 7 days after it ends. Policies #1 and #2 on the v1
  contract cover 2026-11-01..30, so `settle()` can first run on **2026-12-07**
  (deadline 2027-02-05). The three-source settlement consensus has been run live only
  through `dry_run_settlement` (section A), which uses the same consensus code but
  moves no money. Partial payouts are covered by direct-mode tests on real data.
* **A live round with one source actually down.** All three sources answered during
  this run. "One source unavailable → the other two decide" is covered by
  direct-mode tests with real captured responses (e.g. the real CPC HTTP 400 for
  unpublished days and NASA POWER's real `-999` fill values).
* **MetaMask.** As in v0, the wallet path is implemented but a headless browser has no wallet.

## State after the v1 run

`node scripts/e2e.mjs read` on 2026-10-26: 1 pool (balance 25.8 GEN, 4 GEN reserved),
2 quotes, 2 policies (both ACTIVE, tiered), 4 dry runs, 0 GEN paid out.

---

# v0 (accepted version): live run on GenLayer Studionet (2026-10-25)

Everything below ran against the deployed contract on GenLayer Studionet
(`https://studio.genlayer.com/api`, chain id `0xf22f`). All keys were throwaway
Studionet keys; balances are Studio test GEN with no real value.

| | |
|---|---|
| Contract | `0xE33aaE2B162663688E0FcB21F030C5BB87B5656F` |
| Source | `contracts/padi_guard.py`, sha256 `45208d51bb412f49458dcbf13fe286102f07115f88a6849196bf91f9901b8803` |
| On-chain code check | `npm run verify-code` → `MATCH` (code returned by `getContractCode` is byte-identical to the file) |
| Deploy tx | `0x693b613c27ba019dcb8bc1f0f5cad860c9adc2720b9c9b815a8a8c9e72486641` · FINALIZED · MAJORITY_AGREE (5/5 agree) |

Accounts:

* Insurer of pool #1 (Node script): `0xd3E7eBD19e1501ee94B8aBE66933Ed5316f47B83`
* Farmer (Node script): `0x4a2ED8EF12c17B9Ea7D1d6eC4511bbea25213c68`
* Browser burner account (headless Chromium driving the built frontend): `0xF8caAb3Dc756a83c234342E0f116CFCDeD4AD625`, acting as farmer and as the insurer of pool #2

## A. Node script (`frontend/scripts/e2e.mjs`, same client library as the UI)

| Step | Tx | Result |
|---|---|---|
| `create_pool("West Java Padi Pool (demo)", 1000, 500, West Java box)` + 20 GEN | `0xbea9fa6d37a267cf904d1b12e966150d6e94fe673157019462fb7c70c6110c32` | ACCEPTED · MAJORITY_AGREE · returned pool `"1"` |
| `fund_pool("1")` + 5 GEN | `0x8680d477561a91780e37e897f97e469b0386417bdea14d41b0d31742c5a23fde` | ACCEPTED · pool balance 25 GEN |
| `request_quote("1", -7327400, 108220700, "2026-11-01", "2026-11-30", 15000, 0, 2 GEN)` | `0xfd5edd8a4470622d11418ddeccc98734d962bc30fff25e8625f3977179793b6c` | **FINALIZED · MAJORITY_AGREE (3 agree, 2 idle)**. Validators fetched November 2021–2025 for Tasikmalaya. 0 of 5 years below 150 mm → rate 10 % (pool minimum) → premium 0.2 GEN |
| `buy_policy("1")` + 0.2 GEN | `0xa89121068081018524ac34265f5b678522397ab033e23699e4eda8762b984cea` | FINALIZED · policy `"1"` |
| `settle("1")` before the window | `0xcfde910e17d80160b78ec9ef19ba944a171dab60f66e5e552d2cf2b3f2f9490e` | ACCEPTED, execution ERROR **"Settlement not open yet"** (expected: settlement opens 2026-12-07) |
| `withdraw("1", 1 GEN)` | `0x852d64fb554fc93c03dcfe3993c1eebd1a9526a786287a766d8b1a126885cbe9` | FINALIZED; triggered transfer tx `0x737e63f28ba93737e152371ffcced8cf133c34dbb55a4009d675001922d86364`; insurer balance 175 → 176 GEN, contract balance 30.7 → 29.7 GEN after finalization |

## B. Browser (built `frontend/dist`, headless Chromium, `tools/browser_check.py`)

| UI action | Tx | Result |
|---|---|---|
| "+10 test GEN" (Studio faucet) | `0x3cbd787bc724a3ef0bae266d7ce2d6ff6138560f9245cbfb907ee222d44ad1a8` | balance 10 GEN shown |
| Farmer tab → Request quote (drought < 300 mm, payout 2 GEN, pool #1) | `0x728434d97a01e52318fe74706744de39e8becce4eb2673b1fc86838a108d70da` | **FINALIZED · MAJORITY_AGREE (3 agree, 2 idle)**. Quote #2: 1 of 5 years would have paid (Nov 2023: 246.50 mm) → 20 % + 5 % loading = 25 % → premium 0.5 GEN |
| Buy policy · pay 0.5 GEN | `0xf03dcd3eb9b16f4ee31a58107cb67154fd01e78fcaefe68c698d70f54145b8ec` | FINALIZED · policy `"2"` |
| Insurer tab → Create pool "Java Mutual (demo, 2nd insurer)", 8 % / 3 %, Java, 5 GEN | `0x98c813c3c2c4154f5709cbf0effabe487c589dd3c4a23f1c19f51674aeeb6047` | FINALIZED · pool `"2"` (second insurer) |
| Insurer tab → Fund pool #2 with 1 GEN | `0x0dc3f723fb358619dac8932e94fc68090a137a5cdecc34fbc7cade402e7bf279` | ACCEPTED · pool #2 balance 6 GEN |
| Policies tab → policy #2 → "Try settle anyway" | `0x52192bbe058664a5f1f5e6b561df6d178d72cad389a59525d9aa1c3495bcce8c` | execution ERROR "Settlement not open yet" shown in the UI (expected) |

Rainfall history agreed on-chain in quote #2 (Open-Meteo ERA5, Tasikmalaya):

| Window | Total | Wettest day | Below 300 mm? |
|---|---|---|---|
| 2025-11-01 → 30 | 384.40 mm | 33.70 mm | no |
| 2024-11-01 → 30 | 377.90 mm | 44.90 mm | no |
| 2023-11-01 → 30 | 246.50 mm | 28.60 mm | **yes** |
| 2022-11-01 → 30 | 457.80 mm | 49.60 mm | no |
| 2021-11-01 → 30 | 460.70 mm | 39.10 mm | no |

Screenshots of these screens were in `docs/screenshots/`. `overview.png`, `policy.png` and `quote.png` were re-taken against the v1 contract on 2026-10-26; the v0 images are in git history at commit `2b11724`.

## Not done live (and why)

* **A live `settle()` with a payout decision.** A policy can only be bought for a
  window that starts in the future, and settlement opens 7 days after the window
  ends. Policies #1 and #2 cover 2026-11-01..30, so `settle("1")` / `settle("2")`
  can first run on **2026-12-07** (deadline 2027-02-05). The settlement path is
  covered by the direct-mode tests on real captured data, and the same settlement
  consensus code was run live in the earlier RainfallInsurance project through a
  probe contract.
* **MetaMask.** The wallet path (`provider: window.ethereum` + `client.connect("studionet")`)
  is implemented but was not exercised here: a headless browser has no wallet. The
  burner-account path is the one tested end to end.

## State after the run

`npm run read` on 2026-10-25: 2 pools, 2 quotes, 2 policies (both ACTIVE),
total pool balance 29.7 GEN before the pool #2 top-up (30.7 GEN after), 4 GEN reserved for payouts,
0.7 GEN premiums collected, 0 GEN paid out.
