import os, time, pyotp, requests
from flask import Flask, jsonify, render_template_string
from datetime import datetime, date, timedelta

app = Flask(__name__)

API_KEY     = os.environ.get("ANGEL_API_KEY", "")
CLIENT_CODE = os.environ.get("ANGEL_CLIENT_CODE", "")
ANGEL_PIN   = os.environ.get("ANGEL_PIN", "")
TOTP_SECRET = os.environ.get("ANGEL_TOTP_SECRET", "")
BASE        = "https://apiconnect.angelbroking.com"
_cache      = {"data": None, "ts": 0, "jwt": None, "jwt_ts": 0}
CACHE_TTL   = 120

# ── Angel One Auth ─────────────────────────────────────────────
def base_headers(jwt=None):
    h = {
        "Content-Type":     "application/json",
        "Accept":           "application/json",
        "X-UserType":       "USER",
        "X-SourceID":       "WEB",
        "X-ClientLocalIP":  "127.0.0.1",
        "X-ClientPublicIP": "106.193.147.98",
        "X-MACAddress":     "fe80::216e:6507:4b90:3719",
        "X-PrivateKey":     API_KEY,
    }
    if jwt:
        h["Authorization"] = f"Bearer {jwt}"
    return h

def login():
    """Try multiple param-name variations to handle API version differences"""
    if _cache["jwt"] and time.time() - _cache["jwt_ts"] < 3000:
        return _cache["jwt"]

    totp = pyotp.TOTP(TOTP_SECRET).now()
    url  = f"{BASE}/rest/auth/angelbroking/user/v1/loginByPassword"

    # Try different param names - Angel One changed these across versions
    payloads = [
        {"clientcode": CLIENT_CODE, "password": ANGEL_PIN, "totp": totp},
        {"clientCode": CLIENT_CODE, "password": ANGEL_PIN, "totp": totp},
        {"userId":     CLIENT_CODE, "password": ANGEL_PIN, "totp": totp},
        {"userName":   CLIENT_CODE, "password": ANGEL_PIN, "totp": totp},
    ]

    last_err = ""
    for payload in payloads:
        try:
            r = requests.post(url, json=payload,
                              headers=base_headers(), timeout=15)
            d = r.json()
            if d.get("status") and d.get("data", {}).get("jwtToken"):
                _cache["jwt"]    = d["data"]["jwtToken"]
                _cache["jwt_ts"] = time.time()
                return _cache["jwt"]
            last_err = d.get("message", str(d))
        except Exception as e:
            last_err = str(e)

    raise Exception(f"All login attempts failed. Last error: {last_err}")

# ── Market Data ────────────────────────────────────────────────
def get_spot(jwt):
    """Get NIFTY 50 spot — tries multiple tokens"""
    tokens = [
        ("NSE", "99926000", "Nifty 50"),
        ("NSE", "26000",    "NIFTY"),
        ("NSE", "26074",    "NIFTY"),
    ]
    for exch, token, sym in tokens:
        try:
            r = requests.post(
                f"{BASE}/rest/secure/angelbroking/market/v1/quote/",
                json={"mode": "LTP", "exchangeTokens": {exch: [token]}},
                headers=base_headers(jwt), timeout=10)
            d = r.json()
            fetched = (d.get("data") or {}).get("fetched", [])
            if fetched and fetched[0].get("ltp"):
                return float(fetched[0]["ltp"])
        except Exception:
            continue
    raise Exception("Could not get NIFTY spot from any token")

def next_thursday():
    today = date.today()
    days  = (3 - today.weekday()) % 7
    if days == 0 and datetime.now().hour >= 15:
        days = 7
    return (today + timedelta(days=days)).strftime("%d%b%Y").upper()

def get_chain(jwt, expiry):
    """Try Angel One option chain endpoints"""
    endpoints = [
        (f"{BASE}/rest/secure/angelbroking/marketData/v1/optionChain",
         {"name": "NIFTY", "expirydate": expiry}),
        (f"{BASE}/rest/secure/angelbroking/marketData/v1/optionChain",
         {"name": "NIFTY", "expirydate": expiry, "exchange": "NFO"}),
    ]
    for url, body in endpoints:
        try:
            r = requests.post(url, json=body,
                              headers=base_headers(jwt), timeout=15)
            d = r.json()
            if d.get("data"):
                return d["data"]
        except Exception:
            continue
    return None

# ── Analysis ───────────────────────────────────────────────────
def pcr_signal(p):
    if p >= 1.4: return "STRONGLY BULLISH", "BUY CALL", "green"
    if p >= 1.2: return "BULLISH",          "BUY CALL", "green"
    if p >= 1.0: return "MILDLY BULLISH",   "BUY CALL", "green"
    if p >= 0.9: return "NEUTRAL",           "WAIT",     "gold"
    if p >= 0.75:return "MILDLY BEARISH",   "BUY PUT",  "red"
    if p >= 0.6: return "BEARISH",           "BUY PUT",  "red"
    return               "STRONGLY BEARISH", "BUY PUT",  "red"

def analyse():
    jwt    = login()
    spot   = get_spot(jwt)
    atm    = round(spot / 50) * 50
    expiry = next_thursday()
    chain  = get_chain(jwt, expiry)

    if chain:
        co, po = {}, {}
        for row in chain:
            sp = row.get("strikePrice") or row.get("strike", 0)
            try: sp = int(float(sp))
            except: continue
            ce = row.get("CE") or {}
            pe = row.get("PE") or {}
            coi = ce.get("openInterest", 0) if isinstance(ce, dict) else row.get("CE_openInterest", row.get("callOI", 0))
            poi = pe.get("openInterest", 0) if isinstance(pe, dict) else row.get("PE_openInterest", row.get("putOI", 0))
            try: co[sp] = int(coi)
            except: pass
            try: po[sp] = int(poi)
            except: pass

        tc = sum(co.values()); tp = sum(po.values())
        pcr = round(tp/tc, 2) if tc else 1.0
        calls = sorted([(s,o) for s,o in co.items() if s >  spot], key=lambda x:-x[1])[:5]
        puts  = sorted([(s,o) for s,o in po.items() if s <= spot], key=lambda x:-x[1])[:5]
        cw1 = calls[0][0] if calls else int(round(spot/500+.5)*500)
        pw1 = puts[0][0]  if puts  else int(round(spot/500-.5)*500)
        call_list = [{"s":s,"oi":round(o/100000,1)} for s,o in calls[:4]]
        put_list  = [{"s":s,"oi":round(o/100000,1)} for s,o in puts[:4]]
        c_cr = round(tc/10000000, 2); p_cr = round(tp/10000000, 2)
        mp = atm
        try:
            sks = sorted(set(list(co)+list(po)))
            best = float("inf")
            for s in sks:
                loss = sum(max(0,s-k)*v for k,v in co.items()) + sum(max(0,k-s)*v for k,v in po.items())
                if loss < best: best, mp = loss, s
        except: pass
        live = True
    else:
        pcr  = 1.0; cw1 = int(round(spot/500+.5)*500); pw1 = int(round(spot/500-.5)*500)
        mp   = atm;  c_cr = p_cr = 0.0; live = False
        call_list = [{"s":cw1,"oi":"--"},{"s":cw1+500,"oi":"--"}]
        put_list  = [{"s":pw1,"oi":"--"},{"s":pw1-500,"oi":"--"}]

    bias, sig, sc = pcr_signal(pcr)
    straddle = int(round(spot * 0.20 * (5/252)**0.5 / 50) * 50)

    if "CALL" in sig:
        buy=f"{atm+50} CE  or  {atm+100} CE"; hedge=f"{atm-200} PE"; t1,t2=cw1,cw1+100
        note=f"Bullish (PCR {pcr}). Floor at {pw1}. Call wall {cw1} = target."
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

# ── Routes ─────────────────────────────────────────────────────
@app.route("/api/data")
def api_data():
    if _cache["data"] and time.time()-_cache["ts"] < CACHE_TTL:
        return jsonify(_cache["data"])
    try:
        d = analyse()
        _cache["data"]=d; _cache["ts"]=time.time()
        return jsonify(d)
    except Exception as e:
        if _cache["data"]:
            s=dict(_cache["data"]); s["warning"]=f"Stale — {e}"; return jsonify(s)
        return jsonify({"ok":False,"error":str(e),"ts":datetime.now().strftime("%I:%M:%S %p")})

@app.route("/")
def index():
    return render_template_string(HTML)

HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NIFTY Dashboard</title>
<style>
:root{--bg:#04090f;--card:#070e19;--c2:#091221;--ln:#0d1e30;
  --acc:#00d4ff;--gr:#00ff88;--rd:#ff3355;--gd:#ffcc00;--mt:#2a4060;--tx:#7aaac8;--wh:#e8f8ff}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--tx);font-family:'Courier New',monospace;min-height:100vh;padding-bottom:50px}
.hdr{background:linear-gradient(180deg,#0a1828,#04090f);border-bottom:1px solid var(--ln);padding:12px 14px;position:sticky;top:0;z-index:20}
.spot{font-size:26px;font-weight:900;color:var(--wh)}
.btn{background:rgba(0,212,255,.12);border:1px solid rgba(0,212,255,.3);border-radius:8px;padding:7px 13px;color:var(--acc);font-size:11px;font-family:inherit;cursor:pointer;letter-spacing:1px}
.wrap{padding:12px 14px;display:flex;flex-direction:column;gap:11px}
.card{background:var(--card);border-radius:14px;padding:14px;border:1px solid var(--ln)}
.lbl{font-size:9px;color:var(--mt);letter-spacing:3px;margin-bottom:10px}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.g3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
.g4{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:8px}
.cell{background:var(--c2);border-radius:8px;padding:10px 12px}
.cl{font-size:8px;color:var(--mt);letter-spacing:1px;margin-bottom:4px}
.cv{font-size:13px;font-weight:700}
.bw{height:8px;background:var(--ln);border-radius:4px;overflow:hidden;margin:8px 0 4px}
.bf{height:100%;background:linear-gradient(90deg,var(--rd),var(--gd),var(--gr));border-radius:4px;transition:width 1.2s}
.bl{display:flex;justify-content:space-between;font-size:9px;color:var(--mt)}
.struct{background:var(--c2);border-radius:10px;padding:12px;font-size:12px;line-height:2.4}
.oib{margin-bottom:9px}.or{display:flex;justify-content:space-between;margin-bottom:3px;font-size:12px}
.obg{height:5px;background:var(--ln);border-radius:3px;overflow:hidden}
.ofl{height:100%;border-radius:3px;transition:width .8s}
.sig{border-radius:14px;padding:14px;border-width:2px;border-style:solid}
.sa{font-size:22px;font-weight:900;margin-bottom:12px}
.bb{background:var(--c2);border-radius:8px;padding:12px 14px;margin-bottom:10px}
.nt{background:var(--c2);border-radius:8px;padding:10px 12px;font-size:12px;font-style:italic;line-height:1.6;margin-top:8px}
.rl{background:rgba(255,204,0,.04);border:1px solid rgba(255,204,0,.15);border-radius:12px;padding:12px;font-size:11px;color:var(--gd);line-height:2.1}
.er{background:rgba(255,51,85,.06);border:1px solid rgba(255,51,85,.2);border-radius:12px;padding:14px;flex-direction:column;gap:8px}
.ld{background:var(--card);border-radius:14px;padding:32px 20px;text-align:center;border:1px solid var(--ln)}
.dot{width:7px;height:7px;border-radius:50%;display:inline-block;margin-right:4px}
.live{animation:blink 2s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.15}}
.gr{color:var(--gr)}.rd{color:var(--rd)}.gd{color:var(--gd)}.ac{color:var(--acc)}
</style></head><body>
<div class="hdr">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <div>
      <div style="font-size:9px;color:var(--mt);letter-spacing:3px;margin-bottom:2px">NIFTY 50 · ANGEL ONE LIVE</div>
      <span class="spot" id="sp">—</span>
    </div>
    <div style="text-align:right">
      <div style="font-size:10px;color:var(--mt);margin-bottom:4px">
        <span class="dot" id="dot" style="background:var(--mt)"></span><span id="stxt">Loading</span>
      </div>
      <button class="btn" id="rb" onclick="load()">&#8635; REFRESH</button>
    </div>
  </div>
  <div style="display:flex;gap:14px;margin-top:5px;font-size:10px;color:var(--mt)" id="hm"></div>
  <div style="font-size:9px;color:var(--mt);margin-top:3px" id="ts"></div>
</div>
<div class="wrap">
  <div class="er" id="er" style="display:none">
    <div class="rd" id="em"></div>
    <button class="btn" onclick="load()">&#8635; Retry</button>
  </div>
  <div class="ld" id="ld">
    <div class="ac" style="font-size:13px;letter-spacing:2px">&#9203; CONNECTING TO ANGEL ONE...</div>
    <div style="color:var(--mt);font-size:11px;margin-top:6px">Authenticating — ~10 seconds first load</div>
  </div>
  <div id="mn" style="display:none;flex-direction:column;gap:11px">
    <div id="wb" style="display:none;background:rgba(255,204,0,.06);border:1px solid rgba(255,204,0,.2);border-radius:8px;padding:8px 12px;font-size:10px;color:var(--gd)"></div>
    <div class="card">
      <div class="lbl">PCR ANALYSIS &#183; ANGEL ONE LIVE</div>
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
        <span style="font-size:22px;font-weight:900" id="pv">—</span>
        <span style="font-size:13px;font-weight:700" id="pb">—</span>
      </div>
      <div class="bw"><div class="bf" id="pbar" style="width:50%"></div></div>
      <div class="bl"><span>0.5 BEAR</span><span>1.0 NEUTRAL</span><span>1.5+ BULL</span></div>
      <div class="g4" style="margin-top:10px">
        <div class="cell"><div class="cl">CALL OI</div><div class="cv rd" id="co">—</div></div>
        <div class="cell"><div class="cl">PUT OI</div><div class="cv gr" id="po">—</div></div>
        <div class="cell"><div class="cl">MAX PAIN</div><div class="cv gd" id="mp">—</div></div>
        <div class="cell"><div class="cl">EXPIRY</div><div class="cv ac" id="ex">—</div></div>
      </div>
    </div>
    <div class="card">
      <div class="lbl">STRADDLE &amp; RANGE</div>
      <div class="g3">
        <div class="cell"><div class="cl">STRADDLE</div><div class="cv ac" id="str">—</div></div>
        <div class="cell"><div class="cl">RANGE LOW</div><div class="cv rd" id="rl">—</div></div>
        <div class="cell"><div class="cl">RANGE HIGH</div><div class="cv gr" id="rh">—</div></div>
      </div>
    </div>
    <div class="card">
      <div class="lbl">&#128205; KEY LEVELS</div>
      <div class="struct" id="struct">—</div>
    </div>
    <div class="card"><div class="lbl rd">&#128308; CALL WALLS &#183; Resistance</div><div id="cw">—</div></div>
    <div class="card"><div class="lbl gr">&#128994; PUT WALLS &#183; Support</div><div id="pw">—</div></div>
    <div class="sig" id="sc">
      <div class="lbl">&#127919; TRADE SIGNAL</div>
      <div class="sa" id="sa">—</div>
      <div class="bb">
        <div style="color:var(--mt);font-size:8px;letter-spacing:1px;margin-bottom:4px">STRIKE TO BUY</div>
        <div style="font-size:15px;font-weight:700" id="sb">—</div>
      </div>
      <div class="g2">
        <div class="cell"><div class="cl">HEDGE</div><div class="cv gd" id="sh">—</div></div>
        <div class="cell"><div class="cl">TARGET 1</div><div class="cv ac" id="t1">—</div></div>
        <div class="cell"><div class="cl">TARGET 2</div><div class="cv ac" id="t2">—</div></div>
        <div class="cell"><div class="cl">STOP LOSS</div><div class="cv rd">40% premium</div></div>
      </div>
      <div class="nt" id="nt"></div>
    </div>
    <div class="rl">Wait 9:30 candle &#183; 1-2 lots MAX &#183; SL=40% &#183; Always hedge &#183; Lot=65</div>
  </div>
</div>
<script>
let cd=120;
const fi=(n,d=2)=>typeof n==='number'?n.toLocaleString('en-IN',{minimumFractionDigits:d,maximumFractionDigits:d}):String(n||'—');
const fii=n=>typeof n==='number'?n.toLocaleString('en-IN'):String(n||'—');
function oiBar(walls,isCall){
  if(!walls||!walls.length)return'<div style="color:var(--mt);font-size:12px">No OI data</div>';
  const col=isCall?'var(--rd)':'var(--gr)',mx=Math.max(...walls.map(w=>parseFloat(w.oi)||1),1);
  return walls.map(w=>`<div class="oib"><div class="or">
    <span style="color:var(--wh);font-weight:700">${fii(w.s)} ${isCall?'CE':'PE'}</span>
    <span style="color:${col}">${w.oi}Cr</span>
  </div><div class="obg"><div class="ofl" style="width:${Math.min((parseFloat(w.oi)||0)/mx*100,100)}%;background:${col}"></div></div></div>`).join('');
}
function render(d){
  if(!d.ok){
    document.getElementById('ld').style.display='none';
    document.getElementById('er').style.display='flex';
    document.getElementById('em').textContent='Error: '+(d.error||'Unknown');return;
  }
  document.getElementById('sp').textContent=fi(d.spot);
  document.getElementById('hm').innerHTML=`ATM:${fii(d.atm)} &nbsp; Straddle:&#8377;${d.straddle} &nbsp; Expiry:${d.expiry}`;
  document.getElementById('ts').textContent='Updated: '+d.ts;
  const wb=document.getElementById('wb');
  if(d.warning||!d.chain_live){wb.style.display='block';wb.textContent=d.warning||'&#9888; OI chain unavailable — estimated levels';}
  else wb.style.display='none';
  const sc=d.sig_color,col=sc==='green'?'var(--gr)':sc==='red'?'var(--rd)':'var(--gd)';
  document.getElementById('pv').textContent=d.pcr_all;document.getElementById('pv').style.color=col;
  document.getElementById('pb').textContent=d.bias;document.getElementById('pb').style.color=col;
  document.getElementById('pbar').style.width=Math.min(d.pcr_all/2*100,100)+'%';
  document.getElementById('co').textContent=d.c_oi_cr?d.c_oi_cr+'Cr':'—';
  document.getElementById('po').textContent=d.p_oi_cr?d.p_oi_cr+'Cr':'—';
  document.getElementById('mp').textContent=fii(d.max_pain);
  document.getElementById('ex').textContent=d.expiry||'—';
  document.getElementById('str').textContent='&#8377;'+d.straddle;
  document.getElementById('rl').textContent=fii(d.range_low);
  document.getElementById('rh').textContent=fii(d.range_high);
  const c1=d.calls[0]?.s,p1=d.puts[0]?.s;
  document.getElementById('struct').innerHTML=`
    <div class="rd">&#128308; ${fii((c1||0)+500)} CE ← Upper wall<br><strong>&#128308; ${fii(c1)} CE ← CEILING</strong></div>
    <div style="color:var(--ln)">──────────────────────────────</div>
    <div class="ac" style="font-size:15px;font-weight:900">&#128205; ${fi(d.spot)} &nbsp; ATM ${fii(d.atm)}</div>
    <div style="color:var(--ln)">──────────────────────────────</div>
    <div class="gr"><strong>&#128994; ${fii(p1)} PE ← FLOOR</strong><br>&#128994; ${fii((p1||0)-500)} PE ← Deep floor</div>
    <div class="gd">&#127919; Max Pain: ${fii(d.max_pain)}</div>
    <div style="color:var(--mt)">&#128208; Range: ${fii(d.range_low)} – ${fii(d.range_high)}</div>`;
  document.getElementById('cw').innerHTML=oiBar(d.calls,true);
  document.getElementById('pw').innerHTML=oiBar(d.puts,false);
  const brd=sc==='green'?'rgba(0,255,136,.3)':sc==='red'?'rgba(255,51,85,.3)':'rgba(255,204,0,.3)';
  const bg=sc==='green'?'rgba(0,255,136,.05)':sc==='red'?'rgba(255,51,85,.05)':'rgba(255,204,0,.05)';
  document.getElementById('sc').style.borderColor=brd;document.getElementById('sc').style.background=bg;
  const icon=sc==='green'?'&#128994;':sc==='red'?'&#128308;':'&#129001;';
  document.getElementById('sa').innerHTML=`<span style="color:${col}">${icon} ${d.signal}</span>`;
  document.getElementById('sb').textContent=d.buy;document.getElementById('sb').style.color=col;
  document.getElementById('sh').textContent=d.hedge;
  document.getElementById('t1').textContent=fii(d.t1);document.getElementById('t2').textContent=fii(d.t2);
  document.getElementById('nt').textContent=d.note;
  document.getElementById('dot').style.background='var(--gr)';
  document.getElementById('dot').className='dot live';
  document.getElementById('stxt').textContent='LIVE';
  document.getElementById('ld').style.display='none';
  document.getElementById('er').style.display='none';
  document.getElementById('mn').style.display='flex';cd=120;
}
async function load(){
  document.getElementById('rb').disabled=true;
  document.getElementById('stxt').textContent='Fetching...';
  try{const r=await fetch('/api/data');render(await r.json());}
  catch(e){document.getElementById('er').style.display='flex';document.getElementById('em').textContent='Network: '+e.message;}
  finally{document.getElementById('rb').disabled=false;}
}
setInterval(()=>{cd--;if(cd<=0)load();},1000);
load();
</script></body></html>"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
