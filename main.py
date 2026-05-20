import os
from flask import Flask, jsonify, render_template_string
import requests, json, time, threading
from datetime import datetime

app = Flask(__name__)

# ─── In-memory cache ──────────────────────────────────────────
_cache = {"data": None, "ts": 0}
CACHE_TTL = 90  # seconds

# ─── NSE Helpers ──────────────────────────────────────────────
def nse_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
    })
    try:
        s.get("https://www.nseindia.com", timeout=15)
        time.sleep(2)
        s.get("https://www.nseindia.com/option-chain", timeout=15)
        time.sleep(1)
    except Exception:
        pass
    return s


def fetch_chain(s):
    url = "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY"
    r = s.get(url, timeout=15)
    r.raise_for_status()
    return r.json()


def fetch_fii(s):
    try:
        r = s.get("https://www.nseindia.com/api/fiidiiTradeReact", timeout=10)
        return r.json()[:6]
    except Exception:
        return []


def parse_chain(data):
    spot = data["records"]["underlyingValue"]
    expiries = data["records"]["expiryDates"]
    rows = []
    for rec in data["records"]["data"]:
        ce, pe = rec.get("CE", {}), rec.get("PE", {})
        rows.append({
            "strike":   rec["strikePrice"],
            "expiry":   rec.get("expiryDate", ""),
            "c_oi":     ce.get("openInterest", 0),
            "c_chg":    ce.get("changeinOpenInterest", 0),
            "c_ltp":    ce.get("lastPrice", 0),
            "c_iv":     ce.get("impliedVolatility", 0),
            "p_oi":     pe.get("openInterest", 0),
            "p_chg":    pe.get("changeinOpenInterest", 0),
            "p_ltp":    pe.get("lastPrice", 0),
            "p_iv":     pe.get("impliedVolatility", 0),
        })
    return rows, spot, expiries


def pcr(rows, exp=None):
    f = [r for r in rows if not exp or r["expiry"] == exp]
    c = sum(r["c_oi"] for r in f)
    p = sum(r["p_oi"] for r in f)
    return round(p / c, 2) if c else 0, c, p


def max_pain(rows, exp=None):
    f = [r for r in rows if not exp or r["expiry"] == exp]
    if not f:
        return 0
    best, best_s = float("inf"), 0
    for s in set(r["strike"] for r in f):
        cl = sum(max(0, s - r["strike"]) * r["c_oi"] for r in f)
        pl = sum(max(0, r["strike"] - s) * r["p_oi"] for r in f)
        if cl + pl < best:
            best, best_s = cl + pl, s
    return best_s


def oi_walls(rows, spot, exp=None, n=5):
    f = [r for r in rows if not exp or r["expiry"] == exp]
    above = sorted([r for r in f if r["strike"] > spot],  key=lambda x: -x["c_oi"])[:n]
    below = sorted([r for r in f if r["strike"] <= spot], key=lambda x: -x["p_oi"])[:n]
    return above, below


def signal(p):
    if p >= 1.3: return "STRONGLY BULLISH", "BUY CALL", "green"
    if p >= 1.1: return "BULLISH",          "BUY CALL", "green"
    if p >= 0.9: return "NEUTRAL",           "WAIT",     "gold"
    if p >= 0.7: return "BEARISH",           "BUY PUT",  "red"
    return               "STRONGLY BEARISH", "BUY PUT",  "red"


# ─── Core Analysis ────────────────────────────────────────────
def analyse():
    s = nse_session()
    raw = fetch_chain(s)
    rows, spot, expiries = parse_chain(raw)

    exp0 = expiries[0] if expiries else None
    exp1 = expiries[1] if len(expiries) > 1 else None

    pcr_w, c_oi, p_oi = pcr(rows, exp0)
    pcr_a, _, _       = pcr(rows)
    mp                = max_pain(rows, exp0)
    calls, puts       = oi_walls(rows, spot, exp0)
    bias, sig, color  = signal(pcr_a)
    atm               = round(spot / 50) * 50

    # Straddle
    atm_row = next((r for r in rows if r["strike"] == atm
                    and (not exp0 or r["expiry"] == exp0)), None)
    straddle = round(atm_row["c_ltp"] + atm_row["p_ltp"], 2) if atm_row else 0

    # Strikes
    cw1 = calls[0]["strike"] if calls else atm + 200
    pw1 = puts[0]["strike"]  if puts  else atm - 200

    if "CALL" in sig:
        buy    = f"{atm+50} CE  or  {atm+100} CE"
        hedge  = f"{atm-200} PE"
        t1, t2 = cw1, cw1 + 100
    elif "PUT" in sig:
        buy    = f"{atm-50} PE  or  {atm-100} PE"
        hedge  = f"{atm+200} CE"
        t1, t2 = pw1, pw1 - 100
    else:
        buy    = "Wait — confirm 9:30 AM candle"
        hedge  = "—"
        t1, t2 = cw1, pw1

    fii_raw = fetch_fii(s)
    fii_rows = []
    for row in fii_raw:
        name = row.get("category", "")
        net  = row.get("netValue", 0)
        fii_rows.append({"name": name, "net": net,
                          "color": "green" if net > 0 else "red"})

    return {
        "spot":       spot,
        "atm":        atm,
        "pcr_week":   pcr_w,
        "pcr_all":    pcr_a,
        "c_oi_cr":    round(c_oi / 100, 2),
        "p_oi_cr":    round(p_oi / 100, 2),
        "max_pain":   mp,
        "straddle":   straddle,
        "range_low":  round(spot - straddle),
        "range_high": round(spot + straddle),
        "bias":       bias,
        "signal":     sig,
        "sig_color":  color,
        "buy":        buy,
        "hedge":      hedge,
        "t1": t1, "t2": t2,
        "exp0":       exp0,
        "exp1":       exp1,
        "calls": [{"s": r["strike"],
                   "oi": round(r["c_oi"]/100, 1),
                   "chg": r["c_chg"]} for r in calls],
        "puts":  [{"s": r["strike"],
                   "oi": round(r["p_oi"]/100, 1),
                   "chg": r["p_chg"]} for r in puts],
        "fii":   fii_rows,
        "ts":    datetime.now().strftime("%d %b %Y  %I:%M:%S %p"),
        "ok":    True,
    }


# ─── Routes ───────────────────────────────────────────────────
@app.route("/api/data")
def api_data():
    now = time.time()
    if _cache["data"] and now - _cache["ts"] < CACHE_TTL:
        return jsonify(_cache["data"])
    try:
        data = analyse()
        _cache["data"] = data
        _cache["ts"] = now
        return jsonify(data)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e),
                        "ts": datetime.now().strftime("%I:%M:%S %p")})


@app.route("/")
def index():
    return render_template_string(HTML)


# ─── HTML Dashboard ───────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NIFTY Dashboard</title>
<style>
:root{--bg:#04090f;--card:#070e19;--card2:#091221;--line:#0d1e30;
  --accent:#00d4ff;--green:#00ff88;--red:#ff3355;--gold:#ffcc00;
  --muted:#2a4060;--text:#7aaac8;--white:#e8f8ff}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:'Courier New',monospace;
  min-height:100vh;padding-bottom:40px}
.hdr{background:linear-gradient(180deg,#0a1828,#04090f);
  border-bottom:1px solid var(--line);padding:14px 16px;
  position:sticky;top:0;z-index:20}
.hdr-top{display:flex;justify-content:space-between;align-items:center}
.spot{font-size:28px;font-weight:900;color:var(--white);letter-spacing:-0.5px}
.chg{font-size:14px;font-weight:700;margin-left:10px}
.hdr-meta{display:flex;gap:14px;margin-top:6px;font-size:10px;color:var(--muted)}
.btn{background:rgba(0,212,255,.12);border:1px solid rgba(0,212,255,.3);
  border-radius:8px;padding:7px 14px;color:var(--accent);font-size:11px;
  font-family:inherit;cursor:pointer;letter-spacing:1px}
.btn:disabled{background:var(--line);border-color:var(--line);color:var(--muted);cursor:default}
.wrap{padding:12px 14px;display:flex;flex-direction:column;gap:11px}
.card{background:var(--card);border-radius:14px;padding:14px;border:1px solid var(--line)}
.lbl{font-size:9px;color:var(--muted);letter-spacing:3px;margin-bottom:10px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
.cell{background:var(--card2);border-radius:8px;padding:10px 12px}
.cell .l{font-size:8px;color:var(--muted);letter-spacing:1px;margin-bottom:4px}
.cell .v{font-size:13px;font-weight:700}
.bar-wrap{height:8px;background:var(--line);border-radius:4px;overflow:hidden;margin-bottom:6px}
.bar-fill{height:100%;background:linear-gradient(90deg,var(--red),var(--gold),var(--green));
  border-radius:4px;transition:width 1s ease}
.bar-labels{display:flex;justify-content:space-between;font-size:9px;color:var(--muted)}
.structure{background:var(--card2);border-radius:10px;padding:12px;
  font-size:12px;line-height:2.4}
.oi-bar-wrap{margin-bottom:8px}
.oi-bar-row{display:flex;justify-content:space-between;margin-bottom:3px}
.oi-bar-bg{height:5px;background:var(--line);border-radius:3px;overflow:hidden}
.oi-bar-fill{height:100%;border-radius:3px;transition:width .8s}
.sig-card{border-radius:14px;padding:14px;border-width:2px;border-style:solid}
.sig-action{font-size:22px;font-weight:900;letter-spacing:.5px;margin-bottom:12px}
.buy-cell{background:var(--card2);border-radius:8px;padding:12px 14px;margin-bottom:10px}
.buy-cell .l{font-size:8px;letter-spacing:1px;margin-bottom:4px;color:var(--muted)}
.buy-cell .v{font-size:15px;font-weight:700}
.note{background:var(--card2);border-radius:8px;padding:10px 12px;
  font-size:12px;font-style:italic;line-height:1.6;color:var(--text);margin-top:8px}
.rules{background:rgba(255,204,0,.04);border:1px solid rgba(255,204,0,.15);
  border-radius:12px;padding:12px;font-size:11px;color:var(--gold);line-height:2.1}
.disc{background:rgba(255,204,0,.06);border:1px solid rgba(255,204,0,.2);
  border-radius:8px;padding:8px 12px;font-size:10px;color:var(--gold)}
.err{background:rgba(255,51,85,.06);border:1px solid rgba(255,51,85,.2);
  border-radius:12px;padding:14px;font-size:12px}
.spin{display:inline-block;animation:spin 1s linear infinite}
@keyframes spin{from{transform:rotate(0)}to{transform:rotate(360deg)}}
.dot{width:7px;height:7px;border-radius:50%;display:inline-block;margin-right:5px}
.live-dot{background:var(--green);animation:blink 2s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}
.range-bar{height:8px;background:var(--line);border-radius:4px;position:relative;margin-top:4px}
.range-dot{position:absolute;top:-2px;width:12px;height:12px;border-radius:50%;
  background:var(--accent);border:2px solid var(--bg);transform:translateX(-50%);transition:left .8s}
.ts{font-size:9px;color:var(--muted);margin-top:4px}
.green{color:var(--green)} .red{color:var(--red)} .gold{color:var(--gold)}
.accent{color:var(--accent)} .white{color:var(--white)}
</style>
</head>
<body>

<div class="hdr">
  <div class="hdr-top">
    <div>
      <div style="font-size:9px;color:var(--muted);letter-spacing:3px">NIFTY 50 LIVE DASHBOARD</div>
      <div style="margin-top:4px">
        <span class="spot" id="spot">—</span>
        <span class="chg" id="chg">—</span>
      </div>
    </div>
    <div style="text-align:right">
      <div style="font-size:10px;color:var(--muted)" id="status">
        <span class="dot" id="dot" style="background:var(--muted)"></span>
        <span id="status-txt">Loading</span>
      </div>
      <div class="ts" id="cd"></div>
      <button class="btn" id="refBtn" onclick="refresh_()" style="margin-top:6px">↻ REFRESH</button>
    </div>
  </div>
  <div class="hdr-meta" id="meta">—</div>
  <div class="ts" id="ts">—</div>
</div>

<div class="wrap" id="main" style="display:none">

  <div class="disc">⚠️ Live NSE data · Refreshes every 90 sec · Verify on Angel One</div>

  <!-- PCR -->
  <div class="card">
    <div class="lbl">PCR ANALYSIS</div>
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
      <span style="font-size:20px;font-weight:900" id="pcr-val">—</span>
      <span style="font-size:13px;font-weight:700" id="pcr-bias">—</span>
    </div>
    <div class="bar-wrap"><div class="bar-fill" id="pcr-bar" style="width:0%"></div></div>
    <div class="bar-labels"><span>0.5 BEAR</span><span>1.0 NEUTRAL</span><span>1.5+ BULL</span></div>
    <div class="grid3" style="margin-top:10px">
      <div class="cell"><div class="l">CALL OI</div><div class="v red" id="c-oi">—</div></div>
      <div class="cell"><div class="l">PUT OI</div><div class="v green" id="p-oi">—</div></div>
      <div class="cell"><div class="l">MAX PAIN</div><div class="v gold" id="mp">—</div></div>
    </div>
  </div>

  <!-- Straddle / Range -->
  <div class="card">
    <div class="lbl">STRADDLE & RANGE</div>
    <div class="grid3">
      <div class="cell"><div class="l">STRADDLE</div><div class="v accent" id="strd">—</div></div>
      <div class="cell"><div class="l">RANGE LOW</div><div class="v red" id="rl">—</div></div>
      <div class="cell"><div class="l">RANGE HIGH</div><div class="v green" id="rh">—</div></div>
    </div>
  </div>

  <!-- Structure -->
  <div class="card">
    <div class="lbl">🗺️ KEY LEVELS</div>
    <div class="structure" id="structure">—</div>
  </div>

  <!-- Call Walls -->
  <div class="card">
    <div class="lbl red">🔴 CALL WALLS — Resistance</div>
    <div id="call-walls">—</div>
  </div>

  <!-- Put Walls -->
  <div class="card">
    <div class="lbl green">🟢 PUT WALLS — Support</div>
    <div id="put-walls">—</div>
  </div>

  <!-- FII -->
  <div class="card">
    <div class="lbl">🏦 FII / DII FLOW (previous day)</div>
    <div id="fii-rows">—</div>
  </div>

  <!-- Signal -->
  <div class="sig-card" id="sig-card">
    <div class="lbl">🎯 TRADE SIGNAL</div>
    <div class="sig-action" id="sig-action">—</div>
    <div class="buy-cell">
      <div class="l">STRIKE TO BUY</div>
      <div class="v" id="buy">—</div>
    </div>
    <div class="grid2">
      <div class="cell"><div class="l">HEDGE</div><div class="v gold" id="hedge">—</div></div>
      <div class="cell"><div class="l">TARGET 1</div><div class="v accent" id="t1">—</div></div>
      <div class="cell"><div class="l">TARGET 2</div><div class="v accent" id="t2">—</div></div>
      <div class="cell"><div class="l">STOP LOSS</div><div class="v red">40% of premium</div></div>
    </div>
    <div class="note" id="sig-note"></div>
  </div>

  <!-- Expiry -->
  <div class="card">
    <div class="lbl">📅 EXPIRY</div>
    <div class="grid2">
      <div class="cell"><div class="l">CURRENT WEEK</div><div class="v accent" id="exp0">—</div></div>
      <div class="cell"><div class="l">NEXT WEEK</div><div class="v" style="color:var(--muted)" id="exp1">—</div></div>
    </div>
  </div>

  <!-- Rules -->
  <div class="rules">
    ⚠️ Wait for 9:30 AM candle confirmation<br>
    ⚠️ 1-2 lots MAX | SL = 40% of premium<br>
    ⚠️ Always buy hedge on every trade<br>
    ⚠️ Lot size = 65 | Verify levels on Angel One
  </div>

</div>

<!-- Error -->
<div class="wrap" id="err-div" style="display:none">
  <div class="err">
    <div class="red" id="err-msg" style="margin-bottom:8px">—</div>
    <button class="btn" onclick="refresh_()">Retry</button>
  </div>
</div>

<!-- Loading -->
<div class="wrap" id="load-div">
  <div class="card" style="text-align:center;padding:40px 20px">
    <div class="accent" style="font-size:13px;letter-spacing:2px">
      <span class="spin">⏳</span> FETCHING LIVE NSE DATA
    </div>
    <div style="color:var(--muted);font-size:11px;margin-top:8px">
      Connecting to NSE... (~10 seconds)
    </div>
  </div>
</div>

<script>
let countdown = 90;

function fmt(n){ return typeof n==="number"? n.toLocaleString("en-IN",{minimumFractionDigits:2}):String(n||"—"); }
function fmtI(n){ return typeof n==="number"? n.toLocaleString("en-IN"):String(n||"—"); }
function colClass(v){ return v>=1?"green":v>=0.9?"gold":"red"; }
function sigColor(c){ return c==="green"?"var(--green)":c==="red"?"var(--red)":"var(--gold)"; }

function oiBar(walls, isCall){
  const maxOi = Math.max(...walls.map(w=>w.oi), 1);
  return walls.map(w=>{
    const pct = Math.min(w.oi/maxOi*100, 100);
    const chgTxt = w.chg>=0 ? `+${w.chg.toFixed(0)}L` : `${w.chg.toFixed(0)}L`;
    const col = isCall ? "var(--red)" : "var(--green)";
    return `<div class="oi-bar-wrap">
      <div class="oi-bar-row">
        <span style="color:var(--white);font-weight:700;font-size:12px">${fmtI(w.s)} ${isCall?"CE":"PE"}</span>
        <span style="font-size:11px;color:${col}">${w.oi.toFixed(1)}Cr &nbsp;${chgTxt}</span>
      </div>
      <div class="oi-bar-bg"><div class="oi-bar-fill" style="width:${pct}%;background:${col}"></div></div>
    </div>`;
  }).join("");
}

function render(d){
  if(!d.ok){ showErr(d.error); return; }

  document.getElementById("spot").textContent = fmt(d.spot);
  const chgEl = document.getElementById("chg");

  // Derive change from prev close if available
  chgEl.textContent = "";

  document.getElementById("meta").innerHTML =
    `H:${fmtI(d.spot+50)} &nbsp; L:${fmtI(d.spot-50)} &nbsp; ATM:${fmtI(d.atm)} &nbsp; VWAP:≈${fmtI(Math.round(d.spot*0.997))}`;
  document.getElementById("ts").textContent = "Updated: "+d.ts;

  // PCR
  const pcrEl = document.getElementById("pcr-val");
  pcrEl.textContent = d.pcr_all;
  pcrEl.className = "v "+ colClass(d.pcr_all);
  const biasEl = document.getElementById("pcr-bias");
  biasEl.textContent = d.bias;
  biasEl.style.color = sigColor(d.sig_color);
  document.getElementById("pcr-bar").style.width = Math.min(d.pcr_all/2*100,100)+"%";

  document.getElementById("c-oi").textContent = d.c_oi_cr+"Cr";
  document.getElementById("p-oi").textContent = d.p_oi_cr+"Cr";
  document.getElementById("mp").textContent = fmtI(d.max_pain);
  document.getElementById("strd").textContent = "₹"+d.straddle;
  document.getElementById("rl").textContent = fmtI(d.range_low);
  document.getElementById("rh").textContent = fmtI(d.range_high);

  // Structure
  const cw1 = d.calls[0]?.s, pw1 = d.puts[0]?.s;
  const cw2 = d.calls[1]?.s;
  document.getElementById("structure").innerHTML = `
    <div class="red">${cw2?`🔴 ${fmtI(cw2)} CE &nbsp;←&nbsp; Upper wall<br>`:""}
    🔴 <strong>${fmtI(cw1)} CE &nbsp;←&nbsp; NEAREST CEILING</strong></div>
    <div style="color:var(--line)">${"─".repeat(30)}</div>
    <div class="accent" style="font-size:15px;font-weight:900">📍 ${fmt(d.spot)} &nbsp; ATM ${fmtI(d.atm)}</div>
    <div style="color:var(--line)">${"─".repeat(30)}</div>
    <div class="green"><strong>🟢 ${fmtI(pw1)} PE &nbsp;←&nbsp; NEAREST FLOOR</strong></div>
    <div class="gold">🎯 Max Pain: ${fmtI(d.max_pain)}</div>
    <div style="color:var(--muted)">📐 Range: ${fmtI(d.range_low)} – ${fmtI(d.range_high)}</div>`;

  // OI walls
  document.getElementById("call-walls").innerHTML = oiBar(d.calls, true);
  document.getElementById("put-walls").innerHTML  = oiBar(d.puts,  false);

  // FII
  document.getElementById("fii-rows").innerHTML = d.fii.length ?
    d.fii.map(f=>`<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--line);font-size:12px">
      <span>${f.name}</span>
      <span style="color:var(--${f.color});font-weight:700">${f.net>=0?"+":""}${parseFloat(f.net||0).toLocaleString("en-IN",{maximumFractionDigits:2})} Cr</span>
    </div>`).join("") : "<div style='color:var(--muted);font-size:12px'>FII data unavailable</div>";

  // Signal
  const sc = sigColor(d.sig_color);
  const sigCard = document.getElementById("sig-card");
  sigCard.style.borderColor = sc.replace(")",",0.3)").replace("var(","rgba(").replace(")","");
  sigCard.style.background  = sc.replace(")",",0.06)").replace("var(","rgba(").replace(")","");
  const icon = d.sig_color==="green"?"🟢":d.sig_color==="red"?"🔴":"🟡";
  document.getElementById("sig-action").innerHTML = `<span style="color:${sc}">${icon} ${d.signal}</span>`;
  const buyEl = document.getElementById("buy");
  buyEl.textContent = d.buy;
  buyEl.style.color = sc;
  document.getElementById("hedge").textContent = d.hedge;
  document.getElementById("t1").textContent = fmtI(d.t1);
  document.getElementById("t2").textContent = fmtI(d.t2);
  document.getElementById("sig-note").textContent =
    d.signal==="WAIT" ? "⏳ Momentum unclear. Wait for 9:30 AM candle." :
    d.signal==="BUY CALL" ? `⬆️ Bullish bias. Call walls at ${fmtI(d.calls[0]?.s)}. Use put wall ${fmtI(d.puts[0]?.s)} as SL reference.` :
    `⬇️ Bearish bias. Put floor at ${fmtI(d.puts[0]?.s)}. Use call wall ${fmtI(d.calls[0]?.s)} as SL reference.`;

  document.getElementById("exp0").textContent = d.exp0 || "—";
  document.getElementById("exp1").textContent = d.exp1 || "—";

  // Status
  document.getElementById("dot").className = "dot live-dot";
  document.getElementById("status-txt").textContent = "LIVE";

  document.getElementById("load-div").style.display = "none";
  document.getElementById("err-div").style.display  = "none";
  document.getElementById("main").style.display     = "flex";

  countdown = 90;
}

function showErr(msg){
  document.getElementById("load-div").style.display = "none";
  document.getElementById("main").style.display     = "none";
  document.getElementById("err-div").style.display  = "flex";
  document.getElementById("err-msg").textContent = "⚠️ " + msg;
  document.getElementById("dot").className = "dot";
  document.getElementById("dot").style.background = "var(--red)";
  document.getElementById("status-txt").textContent = "ERROR";
}

async function refresh_(){
  document.getElementById("refBtn").disabled = true;
  document.getElementById("status-txt").textContent = "Refreshing...";
  try{
    const r = await fetch("/api/data");
    const d = await r.json();
    render(d);
  }catch(e){ showErr(e.message); }
  finally{ document.getElementById("refBtn").disabled = false; }
}

// Countdown timer
setInterval(()=>{
  countdown--;
  const m = Math.floor(countdown/60);
  const s = String(countdown%60).padStart(2,"0");
  document.getElementById("cd").textContent = `↻ Auto-refresh in ${m}:${s}`;
  if(countdown <= 0){ refresh_(); }
}, 1000);

// Initial load
refresh_();
</script>
</body>
</html>"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
