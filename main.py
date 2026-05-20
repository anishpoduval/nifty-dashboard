import os, time, pyotp, requests
from flask import Flask, jsonify, render_template_string
from datetime import datetime, date, timedelta
from SmartApi import SmartConnect

app = Flask(__name__)

# ── Credentials ───────────────────────────────────────────────
API_KEY     = "PRv269tC"
CLIENT_CODE = "A61831553"
ANGEL_PIN   = "8547"
TOTP_SECRET = "XA5CSSZRIMAHEQRJAGJFCJ5MLE"
# ─────────────────────────────────────────────────────────────

_cache = {"data":None,"ts":0,"obj":None,"obj_ts":0,"jwt":None}
CACHE_TTL = 120

# ── Auth ──────────────────────────────────────────────────────
def get_smart():
    if _cache["obj"] and time.time()-_cache["obj_ts"] < 3000:
        return _cache["obj"], _cache["jwt"]
    totp_code = pyotp.TOTP(TOTP_SECRET).now()
    obj  = SmartConnect(api_key=API_KEY)
    data = obj.generateSession(CLIENT_CODE, ANGEL_PIN, totp_code)
    if not data.get("status"):
        raise Exception(f"Login failed: {data.get('message')} | {data}")
    _cache["obj"]    = obj
    _cache["jwt"]    = data["data"]["jwtToken"]
    _cache["obj_ts"] = time.time()
    return obj, _cache["jwt"]

# ── Spot ──────────────────────────────────────────────────────
def get_spot(obj):
    for exch, token in [("NSE","99926000"),("NSE","26000")]:
        try:
            r = obj.ltpData(exch, "Nifty 50", token)
            if r.get("status") and r.get("data",{}).get("ltp"):
                return float(r["data"]["ltp"])
        except Exception:
            continue
    raise Exception("Could not fetch NIFTY spot")

# ── Expiry ────────────────────────────────────────────────────
def next_thursday():
    today = date.today()
    days  = (3-today.weekday())%7
    if days==0 and datetime.now().hour>=15:
        days=7
    return (today+timedelta(days=days)).strftime("%d%b%Y").upper()

# ── Option Chain — tries 3 methods ────────────────────────────
def get_chain(obj, jwt, spot, expiry):
    atm = round(spot/50)*50

    # Method 1: SmartAPI optionChain with correct strike
    try:
        d = obj.optionChain("NIFTY", expiry, str(atm), "OPTIDX")
        if d.get("status") and d.get("data") and len(d["data"]) > 5:
            return d["data"]
    except Exception:
        pass

    # Method 2: SmartAPI with "0" strike
    try:
        d = obj.optionChain("NIFTY", expiry, "0", "OPTIDX")
        if d.get("status") and d.get("data") and len(d["data"]) > 5:
            return d["data"]
    except Exception:
        pass

    # Method 3: Direct REST call with JWT
    for strike in [str(atm), "0", ""]:
        try:
            headers = {
                "Authorization": f"Bearer {jwt}",
                "Content-Type":  "application/json",
                "Accept":        "application/json",
                "X-UserType":    "USER",
                "X-SourceID":    "WEB",
                "X-ClientLocalIP": "127.0.0.1",
                "X-ClientPublicIP":"106.193.147.98",
                "X-MACAddress":  "fe80::216e:6507:4b90:3719",
                "X-PrivateKey":  API_KEY,
            }
            body = {"name":"NIFTY","expirydate":expiry}
            if strike:
                body["strike"] = strike
            r = requests.post(
                "https://apiconnect.angelbroking.com/rest/secure/angelbroking/marketData/v1/optionChain",
                json=body, headers=headers, timeout=15)
            d = r.json()
            if d.get("data") and len(d["data"]) > 5:
                return d["data"]
        except Exception:
            continue

    return None

# ── Parse chain ───────────────────────────────────────────────
def parse_chain(chain, spot):
    co, po = {}, {}
    for row in chain:
        sp = row.get("strikePrice") or row.get("strike", 0)
        try: sp = int(float(sp))
        except: continue
        ce = row.get("CE") or {}
        pe = row.get("PE") or {}
        # Handle both nested dict and flat formats
        if isinstance(ce, dict):
            coi = ce.get("openInterest",0) or ce.get("oi",0)
        else:
            coi = row.get("CE_openInterest",0) or row.get("callOI",0) or row.get("ce_oi",0)
        if isinstance(pe, dict):
            poi = pe.get("openInterest",0) or pe.get("oi",0)
        else:
            poi = row.get("PE_openInterest",0) or row.get("putOI",0) or row.get("pe_oi",0)
        try: co[sp]=int(coi)
        except: pass
        try: po[sp]=int(poi)
        except: pass

    tc = sum(co.values()); tp = sum(po.values())
    pcr = round(tp/tc, 2) if tc > 0 else 1.0
    calls = sorted([(s,o) for s,o in co.items() if s >  spot], key=lambda x:-x[1])[:5]
    puts  = sorted([(s,o) for s,o in po.items() if s <= spot], key=lambda x:-x[1])[:5]
    cw1 = calls[0][0] if calls else int(round(spot/500+.5)*500)
    pw1 = puts[0][0]  if puts  else int(round(spot/500-.5)*500)
    call_list = [{"s":s,"oi":f"{round(o/100000,1)}L","oi_raw":o} for s,o in calls[:4]]
    put_list  = [{"s":s,"oi":f"{round(o/100000,1)}L","oi_raw":o} for s,o in puts[:4]]
    c_cr = round(tc/10000000, 2); p_cr = round(tp/10000000, 2)
    mp = atm = round(spot/50)*50
    try:
        sks = sorted(set(list(co)+list(po))); best = float("inf")
        for s in sks:
            loss = sum(max(0,s-k)*v for k,v in co.items()) + sum(max(0,k-s)*v for k,v in po.items())
            if loss < best: best, mp = loss, s
    except: pass
    return pcr, co, po, tc, tp, calls, puts, cw1, pw1, call_list, put_list, c_cr, p_cr, mp

# ── PCR → Signal ──────────────────────────────────────────────
def pcr_signal(p):
    if p>=1.4: return "STRONGLY BULLISH","BUY CALL","green"
    if p>=1.2: return "BULLISH",         "BUY CALL","green"
    if p>=1.0: return "MILDLY BULLISH",  "BUY CALL","green"
    if p>=0.9: return "NEUTRAL",          "WAIT",    "gold"
    if p>=0.75:return "MILDLY BEARISH",  "BUY PUT", "red"
    if p>=0.6: return "BEARISH",          "BUY PUT", "red"
    return              "STRONGLY BEARISH","BUY PUT", "red"

# ── Main analysis ─────────────────────────────────────────────
def analyse():
    obj, jwt = get_smart()
    spot     = get_spot(obj)
    atm      = round(spot/50)*50
    expiry   = next_thursday()
    chain    = get_chain(obj, jwt, spot, expiry)
    live     = False

    if chain:
        live = True
        pcr,co,po,tc,tp,calls,puts,cw1,pw1,call_list,put_list,c_cr,p_cr,mp = parse_chain(chain,spot)
    else:
        pcr=1.0; cw1=int(round(spot/500+.5)*500); pw1=int(round(spot/500-.5)*500)
        mp=atm; c_cr=p_cr=0.0
        call_list=[{"s":cw1,"oi":"--"},{"s":cw1+500,"oi":"--"}]
        put_list =[{"s":pw1,"oi":"--"},{"s":pw1-500,"oi":"--"}]

    bias,sig,sc = pcr_signal(pcr)
    straddle = int(round(spot*0.20*(5/252)**0.5/50)*50)

    if "CALL" in sig:
        buy=f"{atm+50} CE  or  {atm+100} CE"; hedge=f"{atm-200} PE"; t1,t2=cw1,cw1+100
        note=f"Bullish (PCR {pcr}). Put floor {pw1}. Call wall {cw1} = target."
    elif "PUT" in sig:
        buy=f"{atm-50} PE  or  {atm-100} PE"; hedge=f"{atm+200} CE"; t1,t2=pw1,pw1-100
        note=f"Bearish (PCR {pcr}). Call wall {cw1} capping. Put wall {pw1} = target."
    else:
        buy="Wait — confirm 9:30 AM candle"; hedge="—"; t1,t2=cw1,pw1
        note=f"Neutral (PCR {pcr}). Wait 9:30 candle — Bull:{atm+50}CE | Bear:{atm-50}PE"

    return {"ok":True,"spot":spot,"atm":atm,"expiry":expiry,"pcr_all":pcr,
            "c_oi_cr":c_cr,"p_oi_cr":p_cr,"max_pain":mp,"straddle":straddle,
            "range_low":round(spot-straddle),"range_high":round(spot+straddle),
            "bias":bias,"signal":sig,"sig_color":sc,"buy":buy,"hedge":hedge,
            "t1":t1,"t2":t2,"calls":call_list,"puts":put_list,"note":note,
            "chain_live":live,"ts":datetime.now().strftime("%d %b %Y  %I:%M:%S %p")}

# ── Routes ────────────────────────────────────────────────────
@app.route("/api/data")
def api_data():
    if _cache["data"] and time.time()-_cache["ts"]<CACHE_TTL:
        return jsonify(_cache["data"])
    try:
        d=analyse(); _cache["data"]=d; _cache["ts"]=time.time()
        return jsonify(d)
    except Exception as e:
        if _cache["data"]:
            s=dict(_cache["data"]); s["warning"]=f"Stale — {e}"; return jsonify(s)
        return jsonify({"ok":False,"error":str(e),
                        "ts":datetime.now().strftime("%I:%M:%S %p")})

@app.route("/api/test")
def api_test():
    result={}
    try:
        code=pyotp.TOTP(TOTP_SECRET).now()
        result["totp_generated"]=code; result["totp_ok"]=True
    except Exception as e:
        result["totp_ok"]=False; result["totp_error"]=str(e)
        return jsonify(result)
    try:
        obj=SmartConnect(api_key=API_KEY)
        data=obj.generateSession(CLIENT_CODE,ANGEL_PIN,code)
        result["login_ok"]=data.get("status",False)
        result["login_msg"]=data.get("message","")
        if data.get("status"):
            result["jwt_preview"]=data["data"]["jwtToken"][:20]+"..."
    except Exception as e:
        result["login_ok"]=False; result["login_error"]=str(e)
    return jsonify(result)

@app.route("/")
def index():
    return render_template_string(HTML)

# ── Dashboard HTML ────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>NIFTY Dashboard</title>
<style>
:root{--bg:#04090f;--card:#070e19;--c2:#091221;--ln:#0d1e30;
  --acc:#00d4ff;--gr:#00ff88;--rd:#ff3355;--gd:#ffcc00;--mt:#2a4060;--tx:#7aaac8;--wh:#e8f8ff}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--tx);font-family:'Courier New',monospace;
  min-height:100vh;padding-bottom:60px;-webkit-text-size-adjust:100%}
.hdr{background:linear-gradient(180deg,#0a1828,#04090f);
  border-bottom:1px solid var(--ln);padding:12px 14px;position:sticky;top:0;z-index:20}
.spot{font-size:28px;font-weight:900;color:var(--wh);letter-spacing:-0.5px}
.btn{background:rgba(0,212,255,.12);border:1px solid rgba(0,212,255,.3);
  border-radius:8px;padding:8px 14px;color:var(--acc);font-size:11px;
  font-family:inherit;cursor:pointer;letter-spacing:1px;touch-action:manipulation}
.wrap{padding:12px 14px;display:flex;flex-direction:column;gap:12px}
.card{background:var(--card);border-radius:14px;padding:14px;border:1px solid var(--ln)}
.lbl{font-size:9px;color:var(--mt);letter-spacing:3px;margin-bottom:10px}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.g3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
.g4{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:8px}
.cell{background:var(--c2);border-radius:8px;padding:10px 12px}
.cl{font-size:8px;color:var(--mt);letter-spacing:1px;margin-bottom:4px}
.cv{font-size:13px;font-weight:700}
.bw{height:10px;background:var(--ln);border-radius:5px;overflow:hidden;margin:8px 0 5px}
.bf{height:100%;background:linear-gradient(90deg,var(--rd),var(--gd),var(--gr));
  border-radius:5px;transition:width 1.2s ease}
.bl{display:flex;justify-content:space-between;font-size:9px;color:var(--mt)}
.struct{background:var(--c2);border-radius:10px;padding:14px;font-size:13px;line-height:2.5}
.oib{margin-bottom:10px}
.orow{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px}
.obg{height:6px;background:var(--ln);border-radius:3px;overflow:hidden}
.ofl{height:100%;border-radius:3px;transition:width .8s}
.sig{border-radius:14px;padding:14px;border-width:2px;border-style:solid}
.sa{font-size:24px;font-weight:900;margin-bottom:14px;letter-spacing:0.5px}
.bb{background:var(--c2);border-radius:8px;padding:13px 14px;margin-bottom:10px}
.nt{background:var(--c2);border-radius:8px;padding:11px 13px;
  font-size:12px;font-style:italic;line-height:1.7;margin-top:8px}
.rl{background:rgba(255,204,0,.04);border:1px solid rgba(255,204,0,.15);
  border-radius:12px;padding:13px;font-size:11px;color:var(--gd);line-height:2.2}
.er{background:rgba(255,51,85,.06);border:1px solid rgba(255,51,85,.2);
  border-radius:12px;padding:14px;flex-direction:column;gap:8px}
.ld{background:var(--card);border-radius:14px;padding:40px 20px;
  text-align:center;border:1px solid var(--ln)}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:5px}
.live{animation:blink 2s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.15}}
.gr{color:var(--gr)}.rd{color:var(--rd)}.gd{color:var(--gd)}.ac{color:var(--acc)}
</style></head><body>

<div class="hdr">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <div>
      <div style="font-size:9px;color:var(--mt);letter-spacing:3px;margin-bottom:3px">
        NIFTY 50 &bull; ANGEL ONE LIVE
      </div>
      <span class="spot" id="sp">&#8212;</span>
    </div>
    <div style="text-align:right">
      <div style="font-size:10px;color:var(--mt);margin-bottom:5px">
        <span class="dot" id="dot" style="background:var(--mt)"></span>
        <span id="stxt">Loading</span>
      </div>
      <button class="btn" id="rb" onclick="load()">&#8635; REFRESH</button>
    </div>
  </div>
  <div style="display:flex;gap:14px;margin-top:6px;font-size:10px;color:var(--mt)" id="hm"></div>
  <div style="font-size:9px;color:var(--mt);margin-top:3px" id="ts"></div>
</div>

<div class="wrap">

  <!-- Error -->
  <div class="er" id="er" style="display:none">
    <div class="rd" id="em" style="font-size:13px;word-break:break-word"></div>
    <div style="color:var(--mt);font-size:11px">
      Open <a href="/api/test" target="_blank" style="color:var(--acc)">/api/test</a> for diagnostics
    </div>
    <button class="btn" style="width:fit-content" onclick="load()">&#8635; Retry</button>
  </div>

  <!-- Loading -->
  <div class="ld" id="ld">
    <div class="ac" style="font-size:14px;letter-spacing:2px">&#9203; CONNECTING TO ANGEL ONE...</div>
    <div style="color:var(--mt);font-size:11px;margin-top:8px">Authenticating &amp; fetching live data</div>
  </div>

  <div id="mn" style="display:none;flex-direction:column;gap:12px">

    <!-- Warning banner -->
    <div id="wb" style="display:none;background:rgba(255,204,0,.06);
      border:1px solid rgba(255,204,0,.2);border-radius:8px;padding:9px 13px;
      font-size:10px;color:var(--gd)"></div>

    <!-- PCR Card -->
    <div class="card">
      <div class="lbl">PCR ANALYSIS &bull; ANGEL ONE LIVE</div>
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <span style="font-size:24px;font-weight:900" id="pv">&#8212;</span>
        <span style="font-size:14px;font-weight:700" id="pb">&#8212;</span>
      </div>
      <div class="bw"><div class="bf" id="pbar" style="width:50%"></div></div>
      <div class="bl"><span>0.5 BEAR</span><span>1.0 NEUTRAL</span><span>1.5+ BULL</span></div>
      <div class="g4" style="margin-top:11px">
        <div class="cell"><div class="cl">CALL OI</div><div class="cv rd" id="co">&#8212;</div></div>
        <div class="cell"><div class="cl">PUT OI</div><div class="cv gr" id="po">&#8212;</div></div>
        <div class="cell"><div class="cl">MAX PAIN</div><div class="cv gd" id="mp">&#8212;</div></div>
        <div class="cell"><div class="cl">EXPIRY</div><div class="cv ac" id="ex">&#8212;</div></div>
      </div>
    </div>

    <!-- Straddle & Range -->
    <div class="card">
      <div class="lbl">STRADDLE &amp; EXPECTED RANGE</div>
      <div class="g3">
        <div class="cell"><div class="cl">STRADDLE</div><div class="cv ac" id="str">&#8212;</div></div>
        <div class="cell"><div class="cl">RANGE LOW</div><div class="cv rd" id="rl">&#8212;</div></div>
        <div class="cell"><div class="cl">RANGE HIGH</div><div class="cv gr" id="rh">&#8212;</div></div>
      </div>
    </div>

    <!-- Key Levels -->
    <div class="card">
      <div class="lbl">&#128205; KEY LEVELS</div>
      <div class="struct" id="struct">&#8212;</div>
    </div>

    <!-- Call Walls -->
    <div class="card">
      <div class="lbl rd">&#128308; CALL WALLS &bull; Resistance</div>
      <div id="cw">&#8212;</div>
    </div>

    <!-- Put Walls -->
    <div class="card">
      <div class="lbl gr">&#128994; PUT WALLS &bull; Support</div>
      <div id="pw">&#8212;</div>
    </div>

    <!-- Signal -->
    <div class="sig" id="sc">
      <div class="lbl">&#127919; TRADE SIGNAL</div>
      <div class="sa" id="sa">&#8212;</div>
      <div class="bb">
        <div style="color:var(--mt);font-size:8px;letter-spacing:1px;margin-bottom:5px">STRIKE TO BUY</div>
        <div style="font-size:16px;font-weight:700" id="sb">&#8212;</div>
      </div>
      <div class="g2">
        <div class="cell"><div class="cl">HEDGE</div><div class="cv gd" id="sh">&#8212;</div></div>
        <div class="cell"><div class="cl">TARGET 1</div><div class="cv ac" id="t1">&#8212;</div></div>
        <div class="cell"><div class="cl">TARGET 2</div><div class="cv ac" id="t2">&#8212;</div></div>
        <div class="cell"><div class="cl">STOP LOSS</div><div class="cv rd">40% premium</div></div>
      </div>
      <div class="nt" id="nt"></div>
    </div>

    <!-- Rules -->
    <div class="rl">
      &#9888;&#65039; Wait for 9:30 AM candle confirmation<br>
      &#9888;&#65039; 1-2 lots MAX &bull; SL = 40% of premium<br>
      &#9888;&#65039; Always buy hedge on every trade<br>
      &#9888;&#65039; Lot size = 65 &bull; This is analysis, not advice
    </div>

  </div>
</div>

<script>
let cd = 120;
const INR = n => typeof n==='number'
  ? '\u20B9' + n.toLocaleString('en-IN',{minimumFractionDigits:0,maximumFractionDigits:0})
  : String(n||'—');
const fi  = n => typeof n==='number'
  ? n.toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2})
  : String(n||'—');
const fii = n => typeof n==='number' ? n.toLocaleString('en-IN') : String(n||'—');

function oiBar(walls, isCall) {
  if (!walls || !walls.length)
    return '<div style="color:var(--mt);font-size:12px">No OI data</div>';
  const col = isCall ? 'var(--rd)' : 'var(--gr)';
  const mx  = Math.max(...walls.map(w => parseFloat(w.oi)||1), 1);
  return walls.map(w => {
    const pct = Math.min((parseFloat(w.oi)||0)/mx*100, 100);
    return `<div class="oib">
      <div class="orow">
        <span style="color:var(--wh);font-weight:700;font-size:13px">
          ${fii(w.s)} ${isCall?'CE':'PE'}
        </span>
        <span style="color:${col};font-size:12px">${w.oi}</span>
      </div>
      <div class="obg">
        <div class="ofl" style="width:${pct}%;background:${col}"></div>
      </div>
    </div>`;
  }).join('');
}

function render(d) {
  if (!d.ok) {
    document.getElementById('ld').style.display = 'none';
    document.getElementById('er').style.display = 'flex';
    document.getElementById('em').textContent   = 'Error: ' + (d.error||'Unknown');
    return;
  }

  // Header
  document.getElementById('sp').textContent = fi(d.spot);
  document.getElementById('hm').innerHTML   =
    `ATM:${fii(d.atm)} &nbsp; Straddle:${INR(d.straddle)} &nbsp; Expiry:${d.expiry}`;
  document.getElementById('ts').textContent = 'Updated: ' + d.ts;

  // Warning
  const wb = document.getElementById('wb');
  if (d.warning || !d.chain_live) {
    wb.style.display = 'block';
    wb.textContent   = d.warning || '\u26A0\uFE0F OI chain unavailable \u2014 showing estimated levels';
  } else {
    wb.style.display = 'none';
  }

  // PCR gauge
  const sc  = d.sig_color;
  const col = sc==='green' ? 'var(--gr)' : sc==='red' ? 'var(--rd)' : 'var(--gd)';
  const pv  = document.getElementById('pv');
  pv.textContent = d.pcr_all; pv.style.color = col;
  const pb  = document.getElementById('pb');
  pb.textContent = d.bias; pb.style.color = col;
  document.getElementById('pbar').style.width = Math.min(d.pcr_all/2*100, 100) + '%';

  document.getElementById('co').textContent = d.c_oi_cr ? d.c_oi_cr+'Cr' : '\u2014';
  document.getElementById('po').textContent = d.p_oi_cr ? d.p_oi_cr+'Cr' : '\u2014';
  document.getElementById('mp').textContent = fii(d.max_pain);
  document.getElementById('ex').textContent = d.expiry || '\u2014';

  // Straddle & Range
  document.getElementById('str').textContent = INR(d.straddle);
  document.getElementById('rl').textContent  = fii(d.range_low);
  document.getElementById('rh').textContent  = fii(d.range_high);

  // Key Levels
  const c1 = d.calls[0]?.s, p1 = d.puts[0]?.s;
  document.getElementById('struct').innerHTML = `
    <div class="rd">
      \u{1F534} ${fii((c1||0)+500)} CE \u2190 Upper wall<br>
      <strong>\u{1F534} ${fii(c1)} CE \u2190 CEILING</strong>
    </div>
    <div style="color:var(--ln)">\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500</div>
    <div class="ac" style="font-size:16px;font-weight:900">
      \u{1F4CD} ${fi(d.spot)} &nbsp; ATM ${fii(d.atm)}
    </div>
    <div style="color:var(--ln)">\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500</div>
    <div class="gr">
      <strong>\u{1F7E2} ${fii(p1)} PE \u2190 FLOOR</strong><br>
      \u{1F7E2} ${fii((p1||0)-500)} PE \u2190 Deep floor
    </div>
    <div class="gd">\u{1F3AF} Max Pain: ${fii(d.max_pain)}</div>
    <div style="color:var(--mt)">\u{1F4D0} Range: ${fii(d.range_low)} \u2013 ${fii(d.range_high)}</div>`;

  // OI walls
  document.getElementById('cw').innerHTML = oiBar(d.calls, true);
  document.getElementById('pw').innerHTML = oiBar(d.puts,  false);

  // Signal card
  const brd = sc==='green'?'rgba(0,255,136,.35)':sc==='red'?'rgba(255,51,85,.35)':'rgba(255,204,0,.35)';
  const bg  = sc==='green'?'rgba(0,255,136,.06)':sc==='red'?'rgba(255,51,85,.06)':'rgba(255,204,0,.06)';
  const sigC = document.getElementById('sc');
  sigC.style.borderColor = brd; sigC.style.background = bg;
  const icon = sc==='green' ? '\u{1F7E2}' : sc==='red' ? '\u{1F534}' : '\u{1F7E1}';
  document.getElementById('sa').innerHTML =
    `<span style="color:${col}">${icon} ${d.signal}</span>`;
  const sb = document.getElementById('sb');
  sb.textContent = d.buy; sb.style.color = col;
  document.getElementById('sh').textContent = d.hedge;
  document.getElementById('t1').textContent = fii(d.t1);
  document.getElementById('t2').textContent = fii(d.t2);
  document.getElementById('nt').textContent = d.note;

  // Status
  document.getElementById('dot').style.background = 'var(--gr)';
  document.getElementById('dot').className = 'dot live';
  document.getElementById('stxt').textContent = d.chain_live ? 'LIVE' : 'LIVE (est. OI)';
  document.getElementById('ld').style.display  = 'none';
  document.getElementById('er').style.display  = 'none';
  document.getElementById('mn').style.display  = 'flex';
  cd = 120;
}

async function load() {
  document.getElementById('rb').disabled = true;
  document.getElementById('stxt').textContent = 'Fetching...';
  try {
    const r = await fetch('/api/data');
    render(await r.json());
  } catch(e) {
    document.getElementById('er').style.display = 'flex';
    document.getElementById('em').textContent   = 'Network error: ' + e.message;
  } finally {
    document.getElementById('rb').disabled = false;
  }
}

// Countdown + auto-refresh
setInterval(() => { cd--; if (cd <= 0) load(); }, 1000);
load();
</script>
</body></html>"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
