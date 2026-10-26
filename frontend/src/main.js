import "./style.css";
import { CONTRACT_ADDRESS } from "./config.js";
import {
  api,
  balanceOf,
  bpsToPct,
  degToMicro,
  describeTerms,
  genToWei,
  keyClient,
  microToDeg,
  mm100ToMm,
  mmToMm100,
  newBurnerKey,
  pctToBps,
  readClient,
  SOURCE_LABELS,
  settlementPhase,
  studioFaucet,
  tierLabel,
  walletClient,
  weiToGen,
} from "./lib/padiguard.js";

// ------------------------------------------------------------------ state

const LS = {
  addr: "padiguard.contract",
  burner: "padiguard.burnerKey",
  mode: "padiguard.accountMode",
};

const state = {
  address: localStorage.getItem(LS.addr) || import.meta.env.VITE_PADIGUARD_ADDRESS || CONTRACT_ADDRESS,
  rc: readClient(),
  wc: null, // write client
  me: null, // connected address
  mode: null, // "burner" | "wallet"
  pools: [],
  stats: null,
  lastQuote: null,
  busy: false,
};

// West Java rice districts (approximate town-centre coordinates, for convenience only)
const PRESETS = [
  { name: "Tasikmalaya", lat: "-7.327400", lon: "108.220700" },
  { name: "Indramayu", lat: "-6.327500", lon: "108.324900" },
  { name: "Karawang", lat: "-6.322700", lon: "107.337600" },
  { name: "Subang", lat: "-6.571500", lon: "107.758700" },
  { name: "Cirebon", lat: "-6.732000", lon: "108.552300" },
];

const REGIONS = {
  "West Java": [-7900000, -5900000, 106300000, 108900000],
  Java: [-8900000, -5800000, 105000000, 114700000],
  Worldwide: [-90000000, 90000000, -180000000, 180000000],
};

// ------------------------------------------------------------------ helpers

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const short = (a) => (a ? `${a.slice(0, 6)}…${a.slice(-4)}` : "");
const gen = (w) => `${weiToGen(w)} GEN`;
const sameAddr = (a, b) => a && b && a.toLowerCase() === b.toLowerCase();
const todayUtc = () => new Date().toISOString().slice(0, 10);

function isoAddDays(iso, days) {
  const d = new Date(iso + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

function nextMonthWindow() {
  const now = new Date();
  const s = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 1));
  const e = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 2, 0));
  return [s.toISOString().slice(0, 10), e.toISOString().slice(0, 10)];
}

function regionLabel(p) {
  for (const [name, box] of Object.entries(REGIONS)) {
    if (box[0] === Number(p.lat_min) && box[1] === Number(p.lat_max) && box[2] === Number(p.lon_min) && box[3] === Number(p.lon_max))
      return name;
  }
  return `lat ${microToDeg(p.lat_min)}..${microToDeg(p.lat_max)}, lon ${microToDeg(p.lon_min)}..${microToDeg(p.lon_max)}`;
}

function statusBadge(s) {
  const cls = { ACTIVE: "b-blue", PAID_OUT: "b-green", SETTLED_NO_PAYOUT: "b-grey", CANCELLED: "b-grey", EXPIRED_REFUNDED: "b-amber" }[s] || "b-grey";
  return `<span class="badge ${cls}">${esc(s)}</span>`;
}

// ------------------------------------------------------------------ tx log

function txEntry(label) {
  const box = $("#txlog");
  const el = document.createElement("div");
  el.className = "tx";
  el.innerHTML = `<div><b>${esc(label)}</b></div><div class="muted">signing…</div>`;
  box.prepend(el);
  box.classList.add("show");
  return {
    update(stage, info) {
      if (stage === "sent")
        el.querySelector(".muted").innerHTML = `sent <code title="${esc(info.hash)}">${short(info.hash)}</code> · waiting for GenLayer validators…`;
      if (stage === "accepted") {
        const ok = info.ok;
        el.classList.add(ok ? "ok" : "err");
        el.querySelector(".muted").innerHTML =
          `<code title="${esc(info.hash)}">${short(info.hash)}</code> · ${esc(info.status)} · ${esc(info.consensus || "")}` +
          (ok ? " · success" : ` · <b>reverted:</b> ${esc(info.error)}`);
      }
    },
    fail(e) {
      el.classList.add("err");
      el.querySelector(".muted").textContent = `failed: ${e?.shortMessage || e?.message || e}`;
    },
  };
}

/** Run a write through the connected account with a tx-log entry. Returns the summary. */
async function send(label, fn) {
  if (!state.wc) {
    alert("Connect an account first (top right).");
    return null;
  }
  const t = txEntry(label);
  state.busy = true;
  document.body.classList.add("busy");
  try {
    return await fn(state.wc, state.address, (stage, info) => t.update(stage, info));
  } catch (e) {
    console.error(e);
    t.fail(e);
    return null;
  } finally {
    state.busy = false;
    document.body.classList.remove("busy");
    refreshBalance();
  }
}

// ------------------------------------------------------------------ account

async function connectBurner() {
  let key = localStorage.getItem(LS.burner);
  if (!key) {
    key = newBurnerKey();
    localStorage.setItem(LS.burner, key);
  }
  const { client, address } = keyClient(key);
  state.wc = client;
  state.me = address;
  state.mode = "burner";
  localStorage.setItem(LS.mode, "burner");
  renderAccount();
  renderAll();
}

async function connectWallet() {
  if (!window.ethereum) {
    alert("No browser wallet found. Use the burner account instead, or install MetaMask.");
    return;
  }
  try {
    const { client, address } = await walletClient(window.ethereum);
    state.wc = client;
    state.me = address;
    state.mode = "wallet";
    localStorage.setItem(LS.mode, "wallet");
    renderAccount();
    renderAll();
  } catch (e) {
    const msg = e?.shortMessage || e?.message || String(e);
    alert(
      "Wallet connection failed: " + msg +
      (e?.code === 4001 || /reject/i.test(msg)
        ? "\n\nApprove the wallet's \"Add / switch network: GenLayer Studionet\" prompt, or click \"Use burner account\" to try PadiGuard without a wallet."
        : ""),
    );
  }
}

function disconnect() {
  state.wc = null;
  state.me = null;
  state.mode = null;
  localStorage.removeItem(LS.mode);
  renderAccount();
  renderAll();
}

async function refreshBalance() {
  const el = $("#bal");
  if (!el || !state.me) return;
  try {
    el.textContent = gen(await balanceOf(state.rc, state.me));
  } catch {
    el.textContent = "?";
  }
}

function renderAccount() {
  const box = $("#account");
  if (!state.me) {
    box.innerHTML = `
      <button id="btn-burner" class="primary">Use burner account</button>
      <button id="btn-wallet">Connect MetaMask</button>`;
    $("#btn-burner").onclick = connectBurner;
    $("#btn-wallet").onclick = connectWallet;
    return;
  }
  box.innerHTML = `
    <div class="acct">
      <span class="badge ${state.mode === "burner" ? "b-amber" : "b-blue"}">${state.mode === "burner" ? "burner" : "wallet"}</span>
      <code title="${esc(state.me)}">${short(state.me)}</code>
      <span id="bal" class="muted">…</span>
      <button id="btn-faucet" title="Studio faucet (test GEN, Studionet only)">+10 test GEN</button>
      <button id="btn-out" class="ghost">Disconnect</button>
    </div>`;
  $("#btn-faucet").onclick = async () => {
    try {
      await studioFaucet(state.rc, state.me, 10);
      setTimeout(refreshBalance, 1500);
    } catch (e) {
      alert("Faucet failed: " + (e?.shortMessage || e?.message || e));
    }
  };
  $("#btn-out").onclick = disconnect;
  refreshBalance();
}

// ------------------------------------------------------------------ data

async function loadCore() {
  const [stats, pools] = await Promise.all([api.stats(state.rc, state.address), api.pools(state.rc, state.address, 0, 50)]);
  state.stats = stats;
  state.pools = pools;
}

// ------------------------------------------------------------------ overview

function renderOverview() {
  const s = state.stats;
  const el = $("#tab-overview");
  if (!s) {
    el.innerHTML = `<p class="muted">Loading contract state…</p>`;
    return;
  }
  el.innerHTML = `
    <div class="hero">
      <h1>Rainfall cover settled from public weather data.</h1>
      <p>Insurers open pools. Farmers get a premium priced from the last ${s.quote_years} years of rainfall at their field,
      then buy cover against a dry or a flooded season. After the season, anyone can trigger settlement:
      GenLayer validators each read three public rainfall datasets (Open-Meteo ERA5, NASA POWER and the NOAA CPC gauge analysis),
      agree on the result, and the contract pays the farmer when at least two of the three datasets show the trigger was hit.
      Terms can be tiered: a moderate shortfall pays part of the cover, a severe one pays all of it. No claim forms, no adjuster.</p>
      <ol class="steps">
        <li><b>Quote</b> · validators fetch ${s.quote_years} years of daily rainfall for your location and window and agree what each year would have paid; the average is your expected payout.</li>
        <li><b>Buy</b> · pay the quoted premium; the pool reserves your payout.</li>
        <li><b>Settle</b> · from ${s.settlement_delay_days} days after the window ends, validators read ${(s.settlement_sources || []).length || 3} rainfall sources; ${s.min_agreeing_sources || 2} must agree on the tier, otherwise the decision is deferred.</li>
      </ol>
    </div>
    <div class="tiles">
      <div class="tile"><span>Pools</span><b>${s.pool_count}</b></div>
      <div class="tile"><span>Quotes</span><b>${s.quote_count}</b></div>
      <div class="tile"><span>Policies</span><b>${s.policy_count}</b></div>
      <div class="tile"><span>Pool capital</span><b>${gen(s.total_balance)}</b></div>
      <div class="tile"><span>Reserved for payouts</span><b>${gen(s.total_reserved)}</b></div>
      <div class="tile"><span>Paid to farmers</span><b>${gen(s.total_paid_out)}</b></div>
      <div class="tile"><span>Settlement dry runs</span><b>${s.dry_run_count ?? 0}</b></div>
    </div>
    <h2>Insurer pools</h2>
    ${poolsTable(state.pools)}
    <p class="muted small">Values are read live from the contract with <code>genlayer-js</code> (<code>get_stats</code>, <code>list_pools</code>).</p>`;
}

function poolsTable(pools, withActions = false) {
  if (!pools.length) return `<p class="muted">No pools yet.</p>`;
  return `<div class="tablewrap"><table>
    <thead><tr><th>#</th><th>Name</th><th>Insurer</th><th>Region</th><th>Min rate</th><th>Loading</th>
    <th>Free liquidity</th><th>Reserved</th><th>Policies</th><th>Status</th>${withActions ? "<th></th>" : ""}</tr></thead>
    <tbody>${pools
      .map(
        (p) => `<tr>
        <td>${esc(p.id)}</td><td>${esc(p.name)}</td><td><code title="${esc(p.insurer)}">${short(p.insurer)}</code></td>
        <td>${esc(regionLabel(p))}</td><td>${bpsToPct(p.min_rate_bps)}</td><td>${bpsToPct(p.loading_bps)}</td>
        <td>${gen(p.free_liquidity)}</td><td>${gen(p.reserved)}</td><td>${p.active_policies} active / ${p.policy_count}</td>
        <td>${p.accepting ? '<span class="badge b-green">open</span>' : '<span class="badge b-grey">closed</span>'}</td>
        ${withActions ? `<td>${poolActions(p)}</td>` : ""}
      </tr>`,
      )
      .join("")}</tbody></table></div>`;
}

// ------------------------------------------------------------------ farmer

function renderFarmer() {
  const el = $("#tab-farmer");
  const open = state.pools.filter((p) => p.accepting);
  const [ds, de] = nextMonthWindow();
  const fv = state.form || { preset: "0", lat: PRESETS[0].lat, lon: PRESETS[0].lon, start: ds, end: de, drought: "300", droughtSevere: "150", flood: "", floodSevere: "", partial: "50", payout: "2" };
  el.innerHTML = `
    <div class="grid2">
      <form id="quote-form" class="card">
        <h2>1 · Get a risk-priced quote</h2>
        <label>Pool
          <select name="pool">${open.map((p) => `<option value="${esc(p.id)}">#${esc(p.id)} ${esc(p.name)} · min ${bpsToPct(p.min_rate_bps)} + ${bpsToPct(p.loading_bps)} · free ${gen(p.free_liquidity)}</option>`).join("") || '<option value="">no open pools</option>'}</select>
        </label>
        <label>Location preset
          <select name="preset"><option value="">custom…</option>${PRESETS.map((p, i) => `<option value="${i}" ${String(i) === fv.preset ? "selected" : ""}>${p.name} (approx. town centre)</option>`).join("")}</select>
        </label>
        <div class="row">
          <label>Latitude (°)<input name="lat" value="${esc(fv.lat)}" required /></label>
          <label>Longitude (°)<input name="lon" value="${esc(fv.lon)}" required /></label>
          <button type="button" id="btn-geo" class="ghost" title="Use this device's location">My location</button>
        </div>
        <div class="row">
          <label>Coverage start<input type="date" name="start" value="${esc(fv.start)}" required /></label>
          <label>Coverage end<input type="date" name="end" value="${esc(fv.end)}" required /></label>
        </div>
        <div class="row">
          <label>Drought trigger: total rain below (mm)<input name="drought" value="${esc(fv.drought)}" placeholder="empty = off" /></label>
          <label>Flood trigger: wettest day above (mm)<input name="flood" value="${esc(fv.flood)}" placeholder="empty = off" /></label>
        </div>
        <div class="row">
          <label>Severe drought: below (mm) pays 100 %<input name="droughtSevere" value="${esc(fv.droughtSevere ?? "")}" placeholder="empty = no tier" /></label>
          <label>Severe flood: a day above (mm) pays 100 %<input name="floodSevere" value="${esc(fv.floodSevere ?? "")}" placeholder="empty = no tier" /></label>
        </div>
        <label>Partial payout when only the first trigger is crossed (%)<input name="partial" value="${esc(fv.partial ?? "")}" placeholder="only with a severe level" /></label>
        <label>Payout (GEN, 100 % tier)<input name="payout" value="${esc(fv.payout)}" required /></label>
        <p class="muted small">Tiers are optional. Without a severe level, crossing a trigger pays 100 %.</p>
        <p class="muted small">Coverage must start at least 1 day from today (UTC) and last at most 184 days.
        The quote is a GenLayer transaction: validators each fetch the rainfall history themselves, so it takes a minute or two.</p>
        <button class="primary" type="submit">Request quote</button>
        <div id="quote-err" class="err-text"></div>
      </form>
      <div id="quote-card" class="card">${state.lastQuote ? quoteCard(state.lastQuote) : '<h2>2 · Review & buy</h2><p class="muted">Your quote will appear here.</p>'}</div>
    </div>
    <div class="card">
      <h2>My quotes &amp; policies</h2>
      <div id="my-stuff">${state.me ? '<p class="muted">Loading…</p>' : '<p class="muted">Connect an account to see your quotes and policies.</p>'}</div>
    </div>`;

  const f = $("#quote-form");
  f.preset.onchange = () => {
    const p = PRESETS[Number(f.preset.value)];
    if (p) {
      f.lat.value = p.lat;
      f.lon.value = p.lon;
    }
  };
  $("#btn-geo").onclick = () =>
    navigator.geolocation?.getCurrentPosition(
      (pos) => {
        f.lat.value = pos.coords.latitude.toFixed(6);
        f.lon.value = pos.coords.longitude.toFixed(6);
        f.preset.value = "";
      },
      () => alert("Location not available"),
    );
  f.onsubmit = async (ev) => {
    ev.preventDefault();
    $("#quote-err").textContent = "";
    state.form = { preset: f.preset.value, lat: f.lat.value, lon: f.lon.value, start: f.start.value, end: f.end.value, drought: f.drought.value, droughtSevere: f.droughtSevere.value, flood: f.flood.value, floodSevere: f.floodSevere.value, partial: f.partial.value, payout: f.payout.value };
    let terms;
    try {
      terms = {
        poolId: f.pool.value,
        latMicro: degToMicro(f.lat.value),
        lonMicro: degToMicro(f.lon.value),
        startDate: f.start.value,
        endDate: f.end.value,
        droughtMm100: mmToMm100(f.drought.value),
        floodMm100: mmToMm100(f.flood.value),
        droughtSevereMm100: mmToMm100(f.droughtSevere.value),
        floodSevereMm100: mmToMm100(f.floodSevere.value),
        partialBps: 0,
        payoutWei: genToWei(f.payout.value),
      };
      if (terms.droughtSevereMm100 && !terms.droughtMm100) throw new Error("A severe drought level needs a drought trigger");
      if (terms.floodSevereMm100 && !terms.floodMm100) throw new Error("A severe flood level needs a flood trigger");
      if (terms.droughtSevereMm100 && terms.droughtSevereMm100 >= terms.droughtMm100) throw new Error("Severe drought level must be below the drought trigger");
      if (terms.floodSevereMm100 && terms.floodSevereMm100 <= terms.floodMm100) throw new Error("Severe flood level must be above the flood trigger");
      if (terms.droughtSevereMm100 || terms.floodSevereMm100) {
        terms.partialBps = pctToBps(f.partial.value);
        if (terms.partialBps <= 0 || terms.partialBps >= 10000) throw new Error("Partial payout must be between 0 and 100 % (exclusive)");
      }
      if (!terms.poolId) throw new Error("No open pool selected");
      if (!terms.droughtMm100 && !terms.floodMm100) throw new Error("Set at least one trigger");
    } catch (e) {
      $("#quote-err").textContent = e.message;
      return;
    }
    const r = await send("request_quote (validators fetch 5 years of rainfall)", (c, a, cb) => api.requestQuote(c, a, terms, cb));
    if (r?.ok) {
      await new Promise((res) => setTimeout(res, 1500));
      const qs = await api.farmerQuotes(state.rc, state.address, state.me);
      state.lastQuote = qs[qs.length - 1];
      await loadCore();
      renderAll();
    }
  };
  if (state.me) renderMyStuff();
}

function quoteCard(q) {
  const h = JSON.parse(q.history || "{}");
  const expired = todayUtc() > q.valid_until;
  const used = q.policy_id !== "";
  const rows = (h.years || [])
    .map(
      (y, i) => `<tr><td>${esc(y.start_date)} → ${esc(y.end_date)}</td><td>${mm100ToMm(y.total_mm100)}</td><td>${mm100ToMm(y.max_mm100)}</td>
      <td>${y.days}/${y.expected_days}</td><td>${h.year_payout_bps?.[i] ? `<span class="badge b-amber">would pay ${Number(h.year_payout_bps[i]) / 100}%</span>` : "–"}</td></tr>`,
    )
    .join("");
  return `
    <h2>2 · Quote #${esc(q.id)}</h2>
    <div class="kv">
      <span>Pool</span><b>#${esc(q.pool_id)}</b>
      <span>Location</span><b>${microToDeg(q.lat_micro)}, ${microToDeg(q.lon_micro)}</b>
      <span>Window</span><b>${esc(q.start_date)} → ${esc(q.end_date)}</b>
      <span>Terms</span><b>${esc(describeTerms(q))}</b>
      <span>Historical hits</span><b>${q.hits} of ${q.years} years paid something</b>
      <span>Expected payout</span><b>${bpsToPct(q.expected_payout_bps ?? 0)} of the payout (average over ${q.years} years) · ${gen((BigInt(q.payout) * BigInt(q.expected_payout_bps ?? 0)) / 10000n)}</b>
      <span>Premium rate</span><b>${bpsToPct(q.rate_bps)}</b>
      <span>Payout</span><b>${gen(q.payout)}</b>
      <span>Premium</span><b class="big">${gen(q.premium)}</b>
      <span>Valid until</span><b>${esc(q.valid_until)} (UTC)</b>
    </div>
    <details open><summary>Rainfall history the validators agreed on (${esc(h.source)})</summary>
      <div class="tablewrap"><table><thead><tr><th>Window</th><th>Total mm</th><th>Wettest day mm</th><th>Days</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
      <p class="small"><a id="hist-src" target="_blank" rel="noopener">Open the source data</a></p>
    </details>
    ${
      used
        ? `<p>Bought as policy <b>#${esc(q.policy_id)}</b>.</p>`
        : expired
          ? `<p class="muted">Quote expired. Request a new one.</p>`
          : sameAddr(q.requester, state.me)
            ? `<button id="btn-buy" class="primary">Buy policy · pay ${gen(q.premium)}</button>`
            : `<p class="muted">Only the account that requested this quote can buy it.</p>`
    }`;
}

function wireQuoteCard(q) {
  api
    .quoteSourceUrl(state.rc, state.address, q.id)
    .then((u) => {
      const a = $("#hist-src");
      if (a) a.href = u;
    })
    .catch(() => {});
  const b = $("#btn-buy");
  if (b)
    b.onclick = async () => {
      const r = await send(`buy_policy (quote #${q.id})`, (c, a, cb) => api.buyPolicy(c, a, q.id, BigInt(q.premium), cb));
      if (r?.ok) {
        state.lastQuote = await api.quote(state.rc, state.address, q.id);
        await loadCore();
        renderAll();
      }
    };
}

async function renderMyStuff() {
  const box = $("#my-stuff");
  try {
    const [qs, ps] = await Promise.all([
      api.farmerQuotes(state.rc, state.address, state.me),
      api.farmerPolicies(state.rc, state.address, state.me),
    ]);
    box.innerHTML = `
      <h3>Quotes</h3>
      ${
        qs.length
          ? `<div class="tablewrap"><table><thead><tr><th>#</th><th>Pool</th><th>Window</th><th>Hits</th><th>Rate</th><th>Premium</th><th>Valid until</th><th>Policy</th><th></th></tr></thead><tbody>
        ${qs
          .slice()
          .reverse()
          .map(
            (q) => `<tr><td>${esc(q.id)}</td><td>#${esc(q.pool_id)}</td><td>${esc(q.start_date)} → ${esc(q.end_date)}</td><td>${q.hits}/${q.years}</td>
          <td>${bpsToPct(q.rate_bps)} (exp. ${bpsToPct(q.expected_payout_bps ?? 0)})</td><td>${gen(q.premium)}</td><td>${esc(q.valid_until)}</td><td>${q.policy_id ? "#" + esc(q.policy_id) : "–"}</td>
          <td><button class="ghost" data-showq="${esc(q.id)}">view</button></td></tr>`,
          )
          .join("")}</tbody></table></div>`
          : '<p class="muted">No quotes yet.</p>'
      }
      <h3>Policies</h3>
      ${policiesTable(ps)}`;
    box.querySelectorAll("[data-showq]").forEach(
      (b) =>
        (b.onclick = async () => {
          state.lastQuote = await api.quote(state.rc, state.address, b.dataset.showq);
          $("#quote-card").innerHTML = quoteCard(state.lastQuote);
          wireQuoteCard(state.lastQuote);
          $("#quote-card").scrollIntoView({ behavior: "smooth" });
        }),
    );
    wirePolicyLinks(box);
  } catch (e) {
    box.innerHTML = `<p class="err-text">Could not load: ${esc(e?.shortMessage || e?.message || e)}</p>`;
  }
}

// ------------------------------------------------------------------ insurer

function poolActions(p) {
  if (!sameAddr(p.insurer, state.me)) return "";
  return `<div class="actions">
    <button class="ghost" data-fund="${esc(p.id)}">Fund</button>
    <button class="ghost" data-withdraw="${esc(p.id)}">Withdraw</button>
    <button class="ghost" data-toggle="${esc(p.id)}" data-on="${p.accepting ? "0" : "1"}">${p.accepting ? "Close" : "Open"}</button>
    <button class="ghost" data-pricing="${esc(p.id)}">Pricing</button></div>`;
}

function myPoolsList(pools) {
  if (!pools.length) return '<p class="muted">You have not opened a pool with this account.</p>';
  return pools
    .map(
      (p) => `<div class="poolcard">
      <div class="row between"><b>#${esc(p.id)} ${esc(p.name)}</b>${p.accepting ? '<span class="badge b-green">open</span>' : '<span class="badge b-grey">closed</span>'}</div>
      <div class="kv small">
        <span>Region</span><b>${esc(regionLabel(p))}</b>
        <span>Pricing</span><b>min ${bpsToPct(p.min_rate_bps)} · loading ${bpsToPct(p.loading_bps)}</b>
        <span>Balance</span><b>${gen(p.balance)} (reserved ${gen(p.reserved)}, free ${gen(p.free_liquidity)})</b>
        <span>Premiums / paid out</span><b>${gen(p.total_premiums)} / ${gen(p.total_paid_out)}</b>
        <span>Policies</span><b>${p.active_policies} active / ${p.policy_count}</b>
      </div>
      ${poolActions(p)}
    </div>`,
    )
    .join("");
}

function renderInsurer() {
  const el = $("#tab-insurer");
  const mine = state.me ? state.pools.filter((p) => sameAddr(p.insurer, state.me)) : [];
  el.innerHTML = `
    <div class="grid2">
      <form id="pool-form" class="card">
        <h2>Open a pool</h2>
        <p class="muted small">Any account can open a pool. Each pool has its own capital, reserve and pricing.
        Premium rate = max(minimum rate, historical hit rate + loading), capped at 100 %.</p>
        <label>Pool name<input name="name" maxlength="64" value="My Padi Pool" required /></label>
        <div class="row">
          <label>Minimum rate (%)<input name="min" value="10" required /></label>
          <label>Loading (%)<input name="loading" value="5" required /></label>
        </div>
        <label>Coverage region
          <select name="region">${Object.keys(REGIONS).map((r) => `<option>${r}</option>`).join("")}</select>
        </label>
        <label>Initial deposit (GEN)<input name="deposit" value="10" /></label>
        <button class="primary" type="submit">Create pool</button>
        <div id="pool-err" class="err-text"></div>
      </form>
      <div class="card">
        <h2>My pools</h2>
        ${state.me ? myPoolsList(mine) : '<p class="muted">Connect an account to manage your pools.</p>'}
      </div>
    </div>
    <div class="card"><h2>All pools</h2>${poolsTable(state.pools)}</div>`;

  const pct = (s) => {
    const v = Number(String(s).trim());
    if (!Number.isFinite(v) || v < 0 || v > 100) throw new Error("Rates must be 0–100 %");
    return Math.round(v * 100);
  };
  const f = $("#pool-form");
  f.onsubmit = async (ev) => {
    ev.preventDefault();
    $("#pool-err").textContent = "";
    let p, deposit;
    try {
      const box = REGIONS[f.region.value];
      p = { name: f.name.value.trim(), minRateBps: pct(f.min.value), loadingBps: pct(f.loading.value), latMin: box[0], latMax: box[1], lonMin: box[2], lonMax: box[3] };
      if (p.minRateBps <= 0) throw new Error("Minimum rate must be above 0 %");
      deposit = f.deposit.value.trim() ? genToWei(f.deposit.value) : 0n;
    } catch (e) {
      $("#pool-err").textContent = e.message;
      return;
    }
    const r = await send(`create_pool "${p.name}"`, (c, a, cb) => api.createPool(c, a, p, deposit, cb));
    if (r?.ok) {
      await loadCore();
      renderAll();
    }
  };

  el.querySelectorAll("[data-fund]").forEach(
    (b) =>
      (b.onclick = async () => {
        const v = prompt("Deposit how many GEN?", "5");
        if (!v) return;
        const r = await send(`fund_pool #${b.dataset.fund}`, (c, a, cb) => api.fundPool(c, a, b.dataset.fund, genToWei(v), cb));
        if (r?.ok) await loadCore().then(renderAll);
      }),
  );
  el.querySelectorAll("[data-withdraw]").forEach(
    (b) =>
      (b.onclick = async () => {
        const v = prompt("Withdraw how many GEN (unreserved funds only)?", "1");
        if (!v) return;
        const r = await send(`withdraw #${b.dataset.withdraw}`, (c, a, cb) => api.withdraw(c, a, b.dataset.withdraw, genToWei(v), cb));
        if (r?.ok) await loadCore().then(renderAll);
      }),
  );
  el.querySelectorAll("[data-toggle]").forEach(
    (b) =>
      (b.onclick = async () => {
        const on = b.dataset.on === "1";
        const r = await send(`set_pool_accepting #${b.dataset.toggle} ${on}`, (c, a, cb) => api.setPoolAccepting(c, a, b.dataset.toggle, on, cb));
        if (r?.ok) await loadCore().then(renderAll);
      }),
  );
  el.querySelectorAll("[data-pricing]").forEach(
    (b) =>
      (b.onclick = async () => {
        const v = prompt("New minimum rate % and loading %, e.g. 10,5", "10,5");
        if (!v) return;
        const [m, l] = v.split(",").map((x) => pct(x));
        const r = await send(`set_pool_pricing #${b.dataset.pricing}`, (c, a, cb) => api.setPoolPricing(c, a, b.dataset.pricing, m, l, cb));
        if (r?.ok) await loadCore().then(renderAll);
      }),
  );
}

// ------------------------------------------------------------------ policies

function policiesTable(ps) {
  if (!ps.length) return '<p class="muted">No policies.</p>';
  return `<div class="tablewrap"><table><thead><tr><th>#</th><th>Pool</th><th>Farmer</th><th>Location</th><th>Window</th><th>Payout</th><th>Premium</th><th>Status</th><th>Settles</th><th></th></tr></thead><tbody>
  ${ps
    .slice()
    .reverse()
    .map(
      (p) => `<tr><td>${esc(p.id)}</td><td>#${esc(p.pool_id)}</td><td><code title="${esc(p.farmer)}">${short(p.farmer)}</code></td>
      <td>${microToDeg(p.lat_micro)}, ${microToDeg(p.lon_micro)}</td><td>${esc(p.start_date)} → ${esc(p.end_date)}</td>
      <td>${gen(p.payout)}</td><td>${gen(p.premium)}</td><td>${statusBadge(p.status)}</td><td>${esc(p.settle_opens)} – ${esc(p.settle_deadline)}</td>
      <td><button class="ghost" data-policy="${esc(p.id)}">open</button></td></tr>`,
    )
    .join("")}</tbody></table></div>`;
}

function wirePolicyLinks(root) {
  root.querySelectorAll("[data-policy]").forEach(
    (b) =>
      (b.onclick = () => {
        showTab("policies");
        showPolicy(b.dataset.policy);
      }),
  );
}

async function renderPolicies() {
  const el = $("#tab-policies");
  el.innerHTML = `
    <div class="card">
      <div class="row between"><h2>Policies</h2>
        <form id="lookup" class="row"><input name="id" placeholder="policy #" size="6" /><button class="ghost">Open</button></form></div>
      <div id="all-policies"><p class="muted">Loading…</p></div>
    </div>
    <div id="policy-detail"></div>
    <div class="card">
      <h2>Settlement dry runs</h2>
      <p class="muted small"><code>dry_run_settlement</code> runs the exact settlement consensus (3 sources, 2-of-3, tiers) on a past window,
      with no policy and no money. Records are public.</p>
      <div id="dry-runs"><p class="muted">Loading…</p></div>
    </div>`;
  renderDryRuns();
  $("#lookup").onsubmit = (ev) => {
    ev.preventDefault();
    const id = ev.target.id.value.trim();
    if (id) showPolicy(id);
  };
  try {
    const n = Number(state.stats?.policy_count || 0);
    const offset = Math.max(0, n - 50);
    const ps = n ? await api.policies(state.rc, state.address, offset, 50) : [];
    $("#all-policies").innerHTML = policiesTable(ps) + (n > 50 ? `<p class="muted small">Showing the latest 50 of ${n}.</p>` : "");
    wirePolicyLinks($("#all-policies"));
  } catch (e) {
    $("#all-policies").innerHTML = `<p class="err-text">${esc(e?.message || e)}</p>`;
  }
}

function evidenceView(evJson, dryRun = false) {
  if (!evJson) return '<p class="muted">Not settled yet: no evidence recorded.</p>';
  const ev = JSON.parse(evJson);
  const tiers = Object.fromEntries((ev.source_tiers || []).map((s) => [s.name, s]));
  const agree = (list) => (list && list.length ? ` · agreed by ${list.map((n) => SOURCE_LABELS[n] || n).join(" + ")}` : "");
  return `
    <div class="kv">
      <span>Decision</span><b>${esc(ev.decision)}${ev.defer_reason ? ` (${esc(ev.defer_reason)})` : ""}</b>
      <span>Drought component</span><b>${esc(ev.drought)}${esc(agree(ev.drought_agree))}</b>
      <span>Flood component</span><b>${esc(ev.flood)}${esc(agree(ev.flood_agree))}</b>
      <span>Sources complete</span><b>${ev.sources_ok ?? "?"} of ${ev.sources.length} (2 must agree)</b>
      <span>${dryRun ? "Would pay (dry run, no money moved)" : "Payout"}</span><b>${bpsToPct(ev.payout_bps ?? 0)} · ${gen(ev.payout_wei)}</b>
    </div>
    <div class="tablewrap"><table><thead><tr><th>Source</th><th>Status</th><th>Days</th><th>Total mm</th><th>Wettest day mm</th><th>Drought tier</th><th>Flood tier</th></tr></thead><tbody>
    ${ev.sources
      .map(
        (s) => `<tr><td>${esc(SOURCE_LABELS[s.name] || s.name)}</td><td>${esc(s.status)}</td><td>${s.days}/${ev.expected_days}</td><td>${mm100ToMm(s.total_mm100)}</td><td>${mm100ToMm(s.max_mm100)}</td>
        <td>${esc(tierLabel(tiers[s.name]?.drought_tier ?? "-2"))}</td><td>${esc(tierLabel(tiers[s.name]?.flood_tier ?? "-2"))}</td></tr>`,
      )
      .join("")}
    </tbody></table></div>`;
}

function sourceLinks(urls) {
  const names = ["Open-Meteo", "NASA POWER"];
  return urls.map((u, i) => `<a href="${esc(u)}" target="_blank" rel="noopener">${names[i] || `NOAA CPC${urls.length > 3 ? ` (${i - 1})` : ""}`}</a>`).join(" · ");
}

async function renderDryRuns() {
  const box = $("#dry-runs");
  if (!box) return;
  const n = Number(state.stats?.dry_run_count || 0);
  if (!n) {
    box.innerHTML = '<p class="muted">No dry runs yet.</p>';
    return;
  }
  const ids = [];
  for (let i = n; i >= Math.max(1, n - 9); i--) ids.push(i);
  try {
    const rs = await Promise.all(ids.map((i) => api.dryRunRecord(state.rc, state.address, i)));
    box.innerHTML = rs
      .map((r) => {
        const ev = JSON.parse(r.evidence);
        return `<details><summary>Dry run #${esc(r.id)} · ${microToDeg(r.lat_micro)}, ${microToDeg(r.lon_micro)} · ${esc(r.start_date)} → ${esc(r.end_date)} · <b>${esc(ev.decision)}</b> ${bpsToPct(ev.payout_bps ?? 0)}</summary>
          <p class="small">${esc(describeTerms({ drought_trigger_mm100: ev.terms.drought_mm100, drought_severe_mm100: ev.terms.drought_severe_mm100, flood_trigger_mm100: ev.terms.flood_mm100, flood_severe_mm100: ev.terms.flood_severe_mm100, partial_bps: ev.terms.partial_bps }))}</p>
          ${evidenceView(r.evidence, true)}
          <p class="small">Data: ${sourceLinks(r.source_urls)}</p></details>`;
      })
      .join("");
  } catch (e) {
    box.innerHTML = `<p class="err-text">${esc(e?.message || e)}</p>`;
  }
}

async function showPolicy(id) {
  const box = $("#policy-detail");
  box.innerHTML = `<div class="card"><p class="muted">Loading policy #${esc(id)}…</p></div>`;
  let p, urls;
  try {
    [p, urls] = await Promise.all([api.policy(state.rc, state.address, id), api.sourceUrls(state.rc, state.address, id)]);
  } catch {
    box.innerHTML = `<div class="card"><p class="err-text">Policy #${esc(id)} not found.</p></div>`;
    return;
  }
  const phase = settlementPhase(p);
  const phaseText = {
    CLOSED: "Closed.",
    BEFORE_COVERAGE: "Coverage has not started. The farmer can still cancel for a full premium refund.",
    COVERAGE_OR_WAITING_DATA: `In coverage or waiting for weather data. Settlement opens ${p.settle_opens} (UTC).`,
    SETTLE_OPEN: "Settlement is open. Anyone can trigger it.",
    EXPIRABLE: "The claim window has passed without a decision. Anyone can expire the policy to refund the premium.",
  }[phase];
  box.innerHTML = `
    <div class="card">
      <h2>Policy #${esc(p.id)} ${statusBadge(p.status)}</h2>
      <div class="kv">
        <span>Pool / quote</span><b>#${esc(p.pool_id)} / #${esc(p.quote_id)}</b>
        <span>Farmer</span><b><code>${esc(p.farmer)}</code></b>
        <span>Location</span><b>${microToDeg(p.lat_micro)}, ${microToDeg(p.lon_micro)}</b>
        <span>Coverage window</span><b>${esc(p.start_date)} → ${esc(p.end_date)}</b>
        <span>Terms</span><b>${esc(describeTerms(p))}</b>
        <span>Payout / premium</span><b>${gen(p.payout)} / ${gen(p.premium)} (${bpsToPct(p.rate_bps)})</b>
        <span>Settlement window</span><b>${esc(p.settle_opens)} → ${esc(p.settle_deadline)} (UTC)</b>
        <span>Settle attempts</span><b>${p.settle_attempts}</b>
        <span>Paid</span><b>${gen(p.paid_amount)}${Number(p.paid_bps) ? ` (${bpsToPct(p.paid_bps)})` : ""}</b>
      </div>
      <p>${esc(phaseText)}</p>
      <div class="actions">
        ${phase === "SETTLE_OPEN" ? '<button id="btn-settle" class="primary">Settle now (validators read 3 rainfall sources)</button>' : ""}
        ${phase === "EXPIRABLE" ? '<button id="btn-expire">Expire &amp; refund premium</button>' : ""}
        ${phase === "BEFORE_COVERAGE" && sameAddr(p.farmer, state.me) ? '<button id="btn-cancel">Cancel &amp; refund premium</button>' : ""}
        ${phase !== "SETTLE_OPEN" && p.status === "ACTIVE" ? '<button id="btn-settle-anyway" class="ghost" title="The contract will reject this until settlement opens">Try settle anyway</button>' : ""}
      </div>
      <h3>Settlement evidence</h3>
      ${evidenceView(p.evidence)}
      <p class="small">Data used at settlement: ${sourceLinks(urls)}</p>
    </div>`;
  const after = async (r) => {
    if (r) {
      await loadCore();
      renderOverview();
      await renderPolicies();
      showPolicy(id);
    }
  };
  const settle = async () => after(await send(`settle #${id}`, (c, a, cb) => api.settle(c, a, id, cb)));
  $("#btn-settle")?.addEventListener("click", settle);
  $("#btn-settle-anyway")?.addEventListener("click", settle);
  $("#btn-expire")?.addEventListener("click", async () => after(await send(`expire #${id}`, (c, a, cb) => api.expire(c, a, id, cb))));
  $("#btn-cancel")?.addEventListener("click", async () => after(await send(`cancel_policy #${id}`, (c, a, cb) => api.cancelPolicy(c, a, id, cb))));
  box.scrollIntoView({ behavior: "smooth" });
}

// ------------------------------------------------------------------ settings

function renderSettings() {
  const el = $("#tab-settings");
  el.innerHTML = `
    <form id="settings" class="card">
      <h2>Settings</h2>
      <label>Contract address (GenLayer Studionet)<input name="addr" value="${esc(state.address)}" size="46" /></label>
      <div class="actions"><button class="primary">Save</button><button type="button" id="btn-reset" class="ghost">Reset to default</button></div>
      <h3>Burner account</h3>
      <p class="muted small">The burner account is a private key generated in this browser and stored in localStorage.
      Use it only for Studionet test GEN. Clearing it makes its funds and policies inaccessible from this browser.</p>
      <div class="actions"><button type="button" id="btn-clear-burner" class="ghost">Forget burner key</button></div>
    </form>`;
  $("#settings").onsubmit = (ev) => {
    ev.preventDefault();
    const a = ev.target.addr.value.trim();
    if (!/^0x[0-9a-fA-F]{40}$/.test(a)) return alert("Not an address");
    localStorage.setItem(LS.addr, a);
    location.reload();
  };
  $("#btn-reset").onclick = () => {
    localStorage.removeItem(LS.addr);
    location.reload();
  };
  $("#btn-clear-burner").onclick = () => {
    if (confirm("Forget the burner key stored in this browser?")) {
      localStorage.removeItem(LS.burner);
      if (state.mode === "burner") disconnect();
    }
  };
}

// ------------------------------------------------------------------ shell

function showTab(name) {
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${name}`));
  if (name === "policies") renderPolicies();
}

function renderAll() {
  renderOverview();
  renderFarmer();
  if (state.lastQuote) wireQuoteCard(state.lastQuote);
  renderInsurer();
  renderSettings();
  if ($("#tab-policies").classList.contains("active")) renderPolicies();
}

async function boot() {
  $("#footer-addr").textContent = state.address;
  document.querySelectorAll("#tabs button").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));
  renderAccount();
  if (localStorage.getItem(LS.mode) === "burner" && localStorage.getItem(LS.burner)) await connectBurner();
  try {
    await loadCore();
  } catch (e) {
    $("#tab-overview").innerHTML = `<p class="err-text">Could not read the contract at ${esc(state.address)} on Studionet: ${esc(e?.shortMessage || e?.message || e)}</p>`;
    return;
  }
  renderAll();
}

boot();

window.addEventListener("unhandledrejection", (ev) => {
  console.error(ev.reason);
  const t = txEntry("RPC error");
  t.fail(ev.reason);
  ev.preventDefault();
});
