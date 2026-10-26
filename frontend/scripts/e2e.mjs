// Live end-to-end run against the deployed contract on GenLayer Studionet,
// using the same library as the web app (src/lib/padiguard.js).
// Each step is a separate invocation so long consensus rounds don't block:
//
//   node scripts/e2e.mjs pool            # insurer: create a West Java pool (20 GEN) + fund 5 GEN
//   node scripts/e2e.mjs faucet          # farmer: Studio faucet
//   node scripts/e2e.mjs quote           # farmer: tiered request_quote (validators fetch 5 years of rainfall)
//   node scripts/e2e.mjs dry-run <case>  # anyone: dry_run_settlement on a past window (3 sources, 2-of-3)
//                                        #   cases: aug2023-tiered, jan2025-flood, jan2025-split, xmas2024-cross-year
//   node scripts/e2e.mjs wait <txhash>   # wait for a tx to be ACCEPTED and print the outcome
//   node scripts/e2e.mjs buy             # farmer: buy_policy with the latest quote's premium
//   node scripts/e2e.mjs settle-early    # anyone: settle() before the window -> expected revert
//   node scripts/e2e.mjs read            # print stats, pools, policies, farmer lookups
//
// Results are appended to ../padiguard-e2e-log.json (outside the repo; override with PADIGUARD_E2E_LOG).
import fs from "node:fs";
import path from "node:path";
import { TransactionStatus } from "genlayer-js/types";
import { REPO_ROOT, loadKeyHex, contractAddress, json } from "./_env.mjs";
import { GEN, api, dryRunArgs, keyClient, quoteArgs, readClient, studioFaucet, summarizeReceipt, weiToGen } from "../src/lib/padiguard.js";

const ADDRESS = contractAddress();
const LOG = process.env.PADIGUARD_E2E_LOG || path.join(REPO_ROOT, "..", "padiguard-e2e-log.json");
const ins = keyClient(loadKeyHex("insurer"));
const far = keyClient(loadKeyHex("farmer"));
const rc = readClient();

// Demo terms: Tasikmalaya, West Java; coverage 2026-11-01..2026-11-30, tiered drought:
// month total < 300 mm pays 50 %, < 150 mm pays 100 %.
const WEST_JAVA = { latMin: -7900000, latMax: -5900000, lonMin: 106300000, lonMax: 108900000 };
const TERMS = {
  latMicro: -7327400,
  lonMicro: 108220700,
  startDate: "2026-11-01",
  endDate: "2026-11-30",
  droughtMm100: 30000,
  droughtSevereMm100: 15000,
  floodMm100: 0,
  floodSevereMm100: 0,
  partialBps: 5000,
  payoutWei: 2n * GEN,
};

// Past windows for dry_run_settlement (same place)
const DRY_RUNS = {
  "aug2023-tiered": { ...TERMS, startDate: "2023-08-01", endDate: "2023-08-31", droughtMm100: 6000, droughtSevereMm100: 1000, partialBps: 5000, payoutWei: 10n * GEN },
  "jan2025-flood": { ...TERMS, startDate: "2025-01-01", endDate: "2025-01-31", droughtMm100: 0, droughtSevereMm100: 0, floodMm100: 5000, floodSevereMm100: 0, partialBps: 0, payoutWei: 10n * GEN },
  "jan2025-split": { ...TERMS, startDate: "2025-01-01", endDate: "2025-01-31", droughtMm100: 45000, droughtSevereMm100: 38000, partialBps: 5000, payoutWei: 10n * GEN },
  "xmas2024-cross-year": { ...TERMS, startDate: "2024-12-20", endDate: "2025-01-10", droughtMm100: 30000, droughtSevereMm100: 0, partialBps: 0, payoutWei: 10n * GEN },
};

function log(step, data) {
  const all = fs.existsSync(LOG) ? JSON.parse(fs.readFileSync(LOG, "utf8")) : [];
  all.push({ at: new Date().toISOString(), contract: ADDRESS, step, data: JSON.parse(json(data)) });
  fs.writeFileSync(LOG, JSON.stringify(all, null, 1));
  console.log(step, json(data));
}

const cb = (stage, info) => console.log(" ", stage, info.hash ?? "", info.status ?? "");
const step = process.argv[2];

if (step === "pool") {
  const created = await api.createPool(ins.client, ADDRESS, { name: "West Java Padi Pool (demo)", minRateBps: 1000, loadingBps: 500, ...WEST_JAVA }, 20n * GEN, cb);
  log("create_pool", created);
  const funded = await api.fundPool(ins.client, ADDRESS, created.returned?.replaceAll('"', "") || "1", 5n * GEN, cb);
  log("fund_pool", funded);
} else if (step === "faucet") {
  const r = await studioFaucet(rc, far.address, 20);
  log("faucet_farmer", { tx: r, farmer: far.address, balance: (await rc.getBalance({ address: far.address })).toString() });
} else if (step === "quote") {
  const pools = await api.insurerPools(rc, ADDRESS, ins.address);
  const poolId = pools[pools.length - 1].id;
  const hash = await far.client.writeContract({
    address: ADDRESS,
    functionName: "request_quote",
    args: quoteArgs({ ...TERMS, poolId }),
    value: 0n,
  });
  log("request_quote_sent", { hash, poolId, terms: TERMS });
} else if (step === "dry-run") {
  const name = process.argv[3];
  const t = DRY_RUNS[name];
  if (!t) throw new Error("unknown dry-run case: " + Object.keys(DRY_RUNS).join(", "));
  const hash = await far.client.writeContract({ address: ADDRESS, functionName: "dry_run_settlement", args: dryRunArgs(t), value: 0n });
  log("dry_run_sent", { hash, case: name, terms: t });
} else if (step === "wait") {
  const hash = process.argv[3];
  const want = process.argv[4] === "finalized" ? TransactionStatus.FINALIZED : TransactionStatus.ACCEPTED;
  const receipt = await rc.waitForTransactionReceipt({ hash, status: want, interval: 5000, retries: 20 });
  log("tx_outcome", summarizeReceipt(receipt));
} else if (step === "buy") {
  const quotes = await api.farmerQuotes(rc, ADDRESS, far.address);
  const q = quotes[quotes.length - 1];
  console.log("using quote", q.id, "premium", weiToGen(q.premium), "GEN", "rate", q.rate_bps, "hits", q.hits, "expected payout bps", q.expected_payout_bps);
  const r = await api.buyPolicy(far.client, ADDRESS, q.id, BigInt(q.premium), cb);
  log("buy_policy", { quote_id: q.id, premium: q.premium, ...r });
} else if (step === "settle-early") {
  const ps = await api.farmerPolicies(rc, ADDRESS, far.address);
  const r = await api.settle(far.client, ADDRESS, ps[ps.length - 1].id, cb);
  log("settle_before_window", r);
} else if (step === "read") {
  const out = {
    stats: await api.stats(rc, ADDRESS),
    pools: await api.pools(rc, ADDRESS),
    policies: await api.policies(rc, ADDRESS),
    farmer: far.address,
    farmer_policies: (await api.farmerPolicies(rc, ADDRESS, far.address)).map((p) => p.id),
    farmer_quotes: (await api.farmerQuotes(rc, ADDRESS, far.address)).map((q) => ({ id: q.id, rate_bps: q.rate_bps, hits: q.hits, expected_payout_bps: q.expected_payout_bps, premium: q.premium, policy_id: q.policy_id, history: JSON.parse(q.history) })),
    insurer: ins.address,
    insurer_pools: (await api.insurerPools(rc, ADDRESS, ins.address)).map((p) => p.id),
  };
  const ps = out.farmer_policies;
  if (ps.length) out.source_urls = await api.sourceUrls(rc, ADDRESS, ps[ps.length - 1]);
  out.dry_runs = [];
  for (let i = 1; i <= Number(out.stats.dry_run_count || 0); i++) {
    const r = await api.dryRunRecord(rc, ADDRESS, i);
    out.dry_runs.push({ ...r, evidence: JSON.parse(r.evidence) });
  }
  log("read", out);
} else {
  console.log("unknown step; see header");
}
