# Fixtures: real API responses

Captured with `curl` on 2026-10-23 for Tasikmalaya, West Java (lat -7.327400, lon 108.220700).
They are used unmodified as web mocks (some tests derive modified copies in code).

| File | URL |
|---|---|
| `om_2023-08.json` | `https://archive-api.open-meteo.com/v1/archive?latitude=-7.327400&longitude=108.220700&start_date=2023-08-01&end_date=2023-08-31&daily=precipitation_sum&timezone=auto` |
| `om_2025-01.json` | same, `start_date=2025-01-01&end_date=2025-01-31` |
| `om_recent.json` | same, `start_date=2026-10-10&end_date=2026-10-22` |
| `nasa_2023-08.json` | `https://power.larc.nasa.gov/api/temporal/daily/point?parameters=PRECTOTCORR&community=AG&longitude=108.220700&latitude=-7.327400&start=20230801&end=20230831&format=JSON` |
| `nasa_2025-01.json` | same, `start=20250101&end=20250131` |
| `nasa_recent.json` | same, `start=20261010&end=20261022` (last 2 days are the `-999` fill value) |

Totals in hundredths of mm (what the contract computes):

| Window | Open-Meteo total / max | NASA POWER total / max |
|---|---|---|
| Aug 2023 (El Niño dry season) | 4840 / 880 | 1399 / 508 |
| Jan 2025 (wet season) | 33430 / 3150 | 41246 / 8055 |
| 10–22 Oct 2026 | 2870 / 620 (13 days) | 43272 / 8208 (11 of 13 days) |

## Quote history fixtures (added for PadiGuard)

Captured with `curl` on 2026-10-25, same location, one request each (exactly the
request `request_quote()` makes for these windows):

| File | URL | Used for |
|---|---|---|
| `om_hist_aug_2018-2022.json` | `…/v1/archive?latitude=-7.327400&longitude=108.220700&start_date=2018-08-01&end_date=2022-08-31&daily=precipitation_sum&timezone=auto` | quote for 2023-08-01..31 requested on 2023-07-01 |
| `om_hist_jan_2020-2024.json` | same, `start_date=2020-01-01&end_date=2024-01-31` | quote for 2025-01-01..31 requested on 2024-12-15 |

Per-window totals / wettest day (hundredths of mm):

| Year | Aug total / max | Jan total / max |
|---|---|---|
| 2018 | 4930 / 510 | – |
| 2019 | 4490 / 630 | – |
| 2020 | 13470 / 2170 | 50930 / 10000 |
| 2021 | 34460 / 6340 | 40500 / 6070 |
| 2022 | 23520 / 2470 | 28860 / 3130 |
| 2023 | – | 20760 / 2150 |
| 2024 | – | 40380 / 4860 |

Tests that only exercise settlement or pool accounting use a **synthetic**
constant-rainfall history built in `conftest.py` (`synthetic_history`), chosen so
no history year hits the trigger; that keeps their premium at the pool minimum.

## NOAA CPC and cross-year fixtures (added for milestone v1)

Captured with `curl` on 2026-10-26, same location. CPC = NOAA CPC Unified
Gauge-Based Analysis of Global Daily Precipitation V1.0 RT on NOAA PSL THREDDS
(OPeNDAP ASCII), grid cell lat index 194 (-7.25), lon index 216 (108.25):
`https://psl.noaa.gov/thredds/dodsC/Datasets/cpc_global_precip/precip.<YEAR>.nc.ascii?precip%5B<t0>:1:<t1>%5D%5B194:1:194%5D%5B216:1:216%5D`

| File | Year / day-of-year indices | Notes |
|---|---|---|
| `cpc_2023-08.txt` | 2023, 212..242 | Aug 2023 |
| `cpc_2025-01.txt` | 2025, 0..30 | Jan 2025 |
| `cpc_recent.txt` | 2026, 282..294 | 10–22 Oct 2026 |
| `cpc_2024-12-20_2024-12-31.txt` | 2024, 354..365 | first file of a window crossing New Year |
| `cpc_2025-01-01_2025-01-10.txt` | 2025, 0..9 | second file of that window |
| `cpc_out_of_range.txt` | 2026, 292..302 | real **HTTP 400** body (`stop >= size: 302:297`): days not published yet |
| `om_2024-12-20_2025-01-10.json` | Open-Meteo, same URL pattern as above | cross-year window |
| `nasa_2024-12-20_2025-01-10.json` | NASA POWER, same URL pattern as above | cross-year window |

Totals in hundredths of mm (what the contract computes):

| Window | NOAA CPC total / max |
|---|---|
| Aug 2023 | 744 / 369 |
| Jan 2025 | 47083 / 5752 |
| 10–22 Oct 2026 | 231 / 231 (13 days) |
| 2024-12-20..31 + 2025-01-01..10 | 9026 + 16033 = 25059 / 5752 |
| 2024-12-20..2025-01-10, Open-Meteo / NASA POWER | 25600 / 3130 and 21901 / 2723 |

The live Studionet dry runs on 2026-10-26 (docs/LIVE_RUN.md) agreed on exactly these
numbers.
