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

def ah(jwt=None):
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
    if _cache["jwt"] and time.time() - _cache["jwt_ts"] < 3600:
        return _cache["jwt"]
    totp = pyotp.TOTP(TOTP_SECRET).now()
    r = requests.post(
        f"{BASE}/rest/auth/angelbroking/user/v1/loginByPassword",
        json={"clientcode": CLIENT_CODE, "password": ANGEL_PIN, "totp": totp},
        headers=ah(), timeout=15)
    d = r.json()
    if d.get("status") and d.get("data", {}).get("jwtToken"):
        _cache["jwt"] = d["data"]["jwtToken"]
        _cache["jwt_ts"] = time.time()
        return _cache["jwt"]
    raise Exception(f"Login failed: {d.get('message','Unknown')} | {d}")

def nifty_spot(jwt):
    r = requests.post(
        f"{BASE}/rest/secure/angelbroking/market/v1/quote/",
        json={"mode": "LTP", "exchangeTokens": {"NSE": ["26000"]}},
        headers=ah(jwt), timeout=10)
    d = r.json()
    fetched = d.get("data", {}).get("fetched", [])
    if fetched:
        return float(fetched[0]["ltp"])
    raise Exception(f"Spot failed: {d}")

def next_thursday():
    today = date.today()
    days  = (3 - today.weekday()) % 7
    if days == 0:
        now = datetime.now()
        if now.hour >= 15 and now.minute >= 30:
            days = 7
    exp = today + timedelta(days=days)
    return exp.strftime("%d%b%Y").upper()

def get_option_chain(jwt, expiry):
    try:
        r = requests.post(
            f"{BASE}/rest/secure/angelbroking/marketData/v1/optionChain",
            json={"name": "NIFTY", "expirydate": expiry},
            headers=ah(jwt), timeout=15)
        d = r.json()
        if d.get("data"):
            return d["data"]
    except Exception:
        pass
    return None

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
    spot   = nifty_spot(jwt)
    atm    = round(spot / 50) * 50
    expiry = next_thursday()
    chain  = get_option_chain(jwt, expiry)
    chain_live = False

    if chain:
        chain_live = True
        call_oi, put_oi = {}, {}
        for row in chain:
            sp = row.get("strikePrice", row.get("strike", 0))
            try:
                sp = int(float(sp))
            except:
                continue
            # Handle different Angel One response formats
            ce = row.get("CE") or {}
            pe = row.get("PE") or {}
            if isinstance(ce, dict):
                coi = ce.get("openInterest", 0)
            else:
                coi = row.get("CE_openInterest", 0) or row.get("callOI", 0)
            if isinstance(pe, dict):
                poi = pe.get("openInterest", 0)
            else:
                poi = row.get("PE_openInterest", 0) or row.get("putOI", 0)
            try: call_oi[sp] = int(coi)
            except: pass
            try: put_oi[sp]  = int(poi)
            except: pass

        tot_c = sum(call_oi.values())
        tot_p = sum(put_oi.values())
        pcr   = round(tot_p / tot_c, 2) if tot_c > 0 else 1.0
        calls = sorted([(s,o) for s,o in call_oi.items() if s > spot],  key=lambda x:-x[1])[:5]
        puts  = sorted([(s,o) for s,o in put_oi.items()  if s <= spot], key=lambda x:-x[1])[:5]
        cw1   = calls[0][0] if calls else atm+500
        pw1   = puts[0][0]  if puts  else atm-500
        call_list = [{"s":s,"oi":round(o/100000,1)} for s,o in calls[:4]]
        put_list  = [{"s":s,"oi":round(o/100000,1)} for s,o in puts[:4]]
        c_cr  = round(tot_c/10000000, 2)
        p_cr  = round(tot_p/10000000, 2)

        # Max pain
        mp = atm
        try:
            strikes = sorted(set(list(call_oi.keys()) + list(put_oi.keys())))
            best = float("inf")
            for s in strikes:
                cl = sum(max(0,s-k)*v for k,v in call_oi.items())
                pl = sum(max(0,k-s)*v for k,v in put_oi.items())
                if cl+pl < best:
                    best, mp = cl+pl, s
        except:
            mp = atm
    else:
        # Estimated from spot
        pcr  = 1.0
        cw1  = int(round(spot/500 + 0.5) * 500)
        pw1  = int(round(spot/500 - 0.5) * 500)
        mp   = atm
        c_cr = p_cr = 0.0
        call_list = [{"s":cw1,"oi":"--"},{"s":cw1+500,"oi":"--"}]
        put_list  = [{"s":pw1,"oi":"--"},{"s":pw1-500,"oi":"--"}]

    bias, sig, sc = pcr_signal(pcr)
    straddle = round(spot * 0.20 * (5/252)**0.5 / 50) * 50

    if "CALL" in sig:
        buy   = f"{atm+50} CE  or  {atm+100} CE"
        hedge = f"{atm-200} PE"
        t1, t2 = cw1, cw1+100
        note  = f"Bullish bias (PCR {pcr}). Put floor at {pw1}. Call ceiling at {cw1} is target."
    elif "PUT" in sig:
        buy   = f"{atm-50} PE  or  {atm-100} PE"
        hedge = f"{atm+200} CE"
        t1, t2 = pw1, pw1-100
        note  = f"Bearish bias (PCR {pcr}). Call wall {cw1} is capping. Put wall {pw1} is target."
    else:
        buy   = "Wait — confirm 9:30 AM candle direction"
        hedge = "—"
        t1, t2 = cw1, pw1
        note  = f"Neutral PCR {pcr}. Wait for 9:30 candle — Bull: {atm+50}CE | Bear: {atm-50}PE"

    return {
        "ok": True, "spot": spot, "atm": atm, "expiry": expiry,
        "pcr_all": pcr, "c_oi_cr": c_cr, "p_oi_cr": p_cr,
        "max_pain": mp, "straddle": straddle,
        "range_low": round(spot - straddle), "range_high": round(spot + straddle),
        "bias": bias, "signal": sig, "sig_color": sc,
        "buy": buy, "hedge": hedge, "t1": t1, "t2": t2,
        "calls": call_list, "puts": put_list, "note": note,
        "chain_live": chain_live,
        "ts": datetime.now().strftime("%d %b %Y  %I:%M:%S %p"),
    }

@app.route("/api/data")
def api_data():
    if _cache["data"] and time.time() - _cache["ts"] < CACHE_TTL:
        return jsonify(_cache["data"])
    try:
        d = analyse()
        _cache["data"] = d
        _cache["ts"]   = time.time()
        return jsonify(d)
    except Exception as e:
        if _cache["data"]:
            stale = dict(_cache["data"])
            stale["warning"] = f"Showing cached — {e}"
            return jsonify(stale)
        return jsonify({"ok": False, "error": str(e),
                        "ts": datetime.now().strftime("%I:%M:%S %p")})

@app.route("/")
def index():
    return render_template_string(HTML)

HTML = open_html = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NIFTY Dashboard</title>
<style>
:root{--bg:#04090f;--card:#070e19;--c2:#091221;--ln:#0d1e30;
  --acc:#00d4ff;--gr:#00ff88;--rd:#ff3355;--gd:#ffcc00;--mt:#2a4060;--tx:#7aaac8;--wh:#e8f8ff}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--tx);font-family:'Courier New',monospace;min-height:100vh;padding-bottom:50px}
.hdr{background:linear-gradient(180deg,#0a1828,#04090f);border-bottom:1px solid var(--ln);
  padding:12px 14px;position:sticky;top:0;z-index:20}
.spot{font-size:26px;font-weight:900;color:var(--wh)}
.btn{background:rgba(0,212,255,.12);border:1px solid rgba(0,212,255,.3);border-radius:8px;
  padding:7px 13px;color:var(--acc);font-size:11px;font-family:inherit;cursor:pointer;letter-spacing:1px}
.wrap{padding:12px 14px;display:flex;flex-direction:column;gap:11px}
.card{background:var(--card);border-radius:14px;padding:14px;border:1px solid var(--ln)}
.lbl{font-size:9px;color:var(--mt);letter-spacing:3px;margin-bottom:10px}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.g4{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:8px}
.g3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
.cell{background:var(--c2);border-radius:8px;padding:10px 12px}
.cl{font-size:8px;color:var(--mt);letter-spacing:1px;margin-bottom:4px}
.cv{font-size:13px;font-weight:700}
.bw{height:8px;background:var(--ln);border-radius:4px;overflow:hidden;margin:8px 0 4px}
.bf{height:100%;background:linear-gradient(90deg,var(--rd),var(--gd),var(--gr));border-radius:4px;transition:width 1.2s}
.bl{display:flex;justify-content:space-between;font-size:9px;color:var(--mt)}
.struct{background:var(--c2);border-radius:10px;padding:12px;font-size:12px;line-height:2.4}
.oib{margin-bottom:9px}
.or{display:flex;justify-content:space-between;margin-bottom:3px;font-size:12px}
.obg{height:5px;background:var(--ln);border-radius:3px;overflow:hidden}
.ofl{height:100%;border-radius:3px;transition:width .8s}
.sig{border-radius:14px;padding:14px;border-width:2px;border-style:solid}
.sa{font-size:22px;font-weight:900;margin-bottom:12px}
.bb{background:var(--c2);border-radius:8px;padding:12px 14px;margin-bottom:10px}
.nt{background:var(--c2);border-radius:8px;padding:10px 12px;font-size:12px;font-style:italic;line-height:1.6;margin-top:8px}
.rl{background:rgba(255,204,0,.04);border:1px solid rgba(255,204,0,.15);border-radius:12px;padding:12px;font-size:11px;color:var(--gd);line-height:2.1}
.er{background:rgba(255,51,85,.06);border:1px solid rgba(255,51,85,.2);border-radius:12px;padding:14px;display:flex;flex-direction:column;gap:8px}
.ld{background:var(--card);border-radius:14px;padding:32px 20px;text-align:center;border:1px solid var(--ln)}
.dot{width:7px;height:7px;border-radius:50%;display:inline-block;margin-right:4px}
.live{animation:blink 2s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.15}}
.gr{color:var(--gr)}.rd{color:var(--rd)}.gd{color:var(--gd)}.ac{color:var(--acc)}
</style></head><body>
<div class="hdr">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <div>
      <div style="font-size:9px;color:var(--mt);letter-spacing:3px;margin-bottom:2px">NIFTY 50 · ANGEL ONE LIVE API</div>
      <div><span class="spot" id="sp">—</span></div>
    </div>
    <div style="text-align:right">
      <div style="font-size:10px;color:var(--mt);margin-bottom:4px">
        <span class="dot" id="dot" style="background:var(--mt)"></span><span id="stxt">Loading</span>
      </div>
      <button class="btn" id="rb" onclick="load()">↻ REFRESH</button>
    </div>
  </div>
  <div style="display:flex;gap:14px;margin-top:5px;font-size:10px;color:var(--mt)" id="hm"></div>
  <div style="font-size:9px;color:var(--mt);margin-top:3px" id="ts"></div>
</div>
<div class="wrap">
  <div class="er" id="er" style="display:none">
    <div class="rd" id="em"></div>
    <button class="btn" onclick="load()">↻ Retry</button>
  </div>
  <div class="ld" id="ld">
    <div class="ac" style="font-size:13px;letter-spacing:2px">⏳ CONNECTING TO ANGEL ONE...</div>
    <div style="color:var(--mt);font-size:11px;margin-top:6px">Authenticating — takes ~10 seconds</div>
  </div>
  <div id="main" style="display:none;flex-direction:column;gap:11px">
    <div id="wb" style="display:none;background:rgba(255,204,0,.06);border:1px solid rgba(255,204,0,.2);border-radius:8px;padding:8px 12px;font-size:10px;color:var(--gd)"></div>
    <div class="card">
      <div class="lbl">PCR ANALYSIS — LIVE</div>
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
      <div class="lbl">STRADDLE & RANGE</div>
      <div class="g3">
        <div class="cell"><div class="cl">STRADDLE</div><div class="cv ac" id="str">—</div></div>
        <div class="cell"><div class="cl">RANGE LOW</div><div class="cv rd" id="rl">—</div></div>
        <div class="cell"><div class="cl">RANGE HIGH</div><div class="cv gr" id="rh">—</div></div>
      </div>
    </div>
    <div class="card">
      <div class="lbl">KEY LEVELS</div>
      <div class="struct" id="struct">—</div>
    </div>
    <div class="card">
      <div class="lbl rd">CALL WALLS — Resistance</div>
      <div id="cw">—</div>
    </div>
    <div class="card">
      <div class="lbl gr">PUT WALLS — Support</div>
      <div id="pw">—</div>
    </div>
    <div class="sig" id="sc">
      <div class="lbl">TRADE SIGNAL</div>
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
    <div class="rl">
      Wait for 9:30 AM candle · 1-2 lots MAX<br>
      SL = 40% of premium · Always buy hedge<br>
      Lot size = 65 · This is analysis, not advice
    </div>
  </div>
</div>
<script>
let cd=120;
const fi=(n,d=2)=>typeof n==='number'?n.toLocaleString('en-IN',{minimumFractionDigits:d,maximumFractionDigits:d}):String(n||'—');
const fii=n=>typeof n==='number'?n.toLocaleString('en-IN'):String(n||'—');
function oiBar(walls,col){
  if(!walls||!walls.length)return'<div style="color:var(--mt);font-size:12px">No OI data</div>';
  const mx=Math.max(...walls.map(w=>parseFloat(w.oi)||1),1);
  return walls.map(w=>{
    const pct=Math.min((parseFloat(w.oi)||0)/mx*100,100);
    return`<div class="oib"><div class="or">
      <span style="color:var(--wh);font-weight:700">${fii(w.s)} ${col.includes('rd')||col.includes('ff3')?'CE':'PE'}</span>
      <span style="color:${col}">${w.oi}Cr</span>
    </div><div class="obg"><div class="ofl" style="width:${pct}%;background:${col}"></div></div></div>`;
  }).join('');
}
function render(d){
  if(!d.ok){
    document.getElementById('ld').style.display='none';
    document.getElementById('er').style.display='flex';
    document.getElementById('em').textContent='Error: '+(d.error||'Unknown');
    return;
  }
  document.getElementById('sp').textContent=fi(d.spot);
  document.getElementById('hm').innerHTML=`ATM:${fii(d.atm)} &nbsp; Straddle:₹${d.straddle} &nbsp; Expiry:${d.expiry}`;
  document.getElementById('ts').textContent='Updated: '+d.ts;
  if(d.warning||!d.chain_live){
    const wb=document.getElementById('wb');
    wb.style.display='block';
    wb.textContent=d.warning||(d.chain_live?'':'⚠️ OI chain unavailable — using estimated levels from spot');
  }
  const sc=d.sig_color,col=sc==='green'?'var(--gr)':sc==='red'?'var(--rd)':'var(--gd)';
  const pv=document.getElementById('pv');pv.textContent=d.pcr_all;pv.style.color=col;
  const pb=document.getElementById('pb');pb.textContent=d.bias;pb.style.color=col;
  document.getElementById('pbar').style.width=Math.min(d.pcr_all/2*100,100)+'%';
  document.getElementById('co').textContent=d.c_oi_cr?d.c_oi_cr+'Cr':'—';
  document.getElementById('po').textContent=d.p_oi_cr?d.p_oi_cr+'Cr':'—';
  document.getElementById('mp').textContent=fii(d.max_pain);
  document.getElementById('ex').textContent=d.expiry||'—';
  document.getElementById('str').textContent='₹'+d.straddle;
  document.getElementById('rl').textContent=fii(d.range_low);
  document.getElementById('rh').textContent=fii(d.range_high);
  const c1=d.calls[0]?.s,p1=d.puts[0]?.s;
  document.getElementById('struct').innerHTML=`
    <div class="rd">🔴 ${fii((c1||0)+500)} CE ← Upper wall<br><strong>🔴 ${fii(c1)} CE ← CEILING</strong></div>
    <div style="color:var(--ln)">${'─'.repeat(28)}</div>
    <div class="ac" style="font-size:15px;font-weight:900">📍 ${fi(d.spot)} &nbsp; ATM ${fii(d.atm)}</div>
    <div style="color:var(--ln)">${'─'.repeat(28)}</div>
    <div class="gr"><strong>🟢 ${fii(p1)} PE ← FLOOR</strong><br>🟢 ${fii((p1||0)-500)} PE ← Deep floor</div>
    <div class="gd">🎯 Max Pain: ${fii(d.max_pain)}</div>
    <div style="color:var(--mt)">📐 Range: ${fii(d.range_low)} – ${fii(d.range_high)}</div>`;
  const crCol='var(--rd)',prCol='var(--gr)';
  document.getElementById('cw').innerHTML=oiBar(d.calls,crCol);
  document.getElementById('pw').innerHTML=oiBar(d.puts,prCol);
  const brd=sc==='green'?'rgba(0,255,136,.3)':sc==='red'?'rgba(255,51,85,.3)':'rgba(255,204,0,.3)';
  const bg=sc==='green'?'rgba(0,255,136,.05)':sc==='red'?'rgba(255,51,85,.05)':'rgba(255,204,0,.05)';
  const sigC=document.getElementById('sc');sigC.style.borderColor=brd;sigC.style.background=bg;
  const icon=sc==='green'?'🟢':sc==='red'?'🔴':'🟡';
  document.getElementById('sa').innerHTML=`<span style="color:${col}">${icon} ${d.signal}</span>`;
  const sb=document.getElementById('sb');sb.textContent=d.buy;sb.style.color=col;
  document.getElementById('sh').textContent=d.hedge;
  document.getElementById('t1').textContent=fii(d.t1);
  document.getElementById('t2').textContent=fii(d.t2);
  document.getElementById('nt').textContent=d.note;
  document.getElementById('dot').style.background='var(--gr)';
  document.getElementById('dot').className='dot live';
  document.getElementById('stxt').textContent='LIVE';
  document.getElementById('ld').style.display='none';
  document.getElementById('er').style.display='none';
  document.getElementById('main').style.display='flex';
  cd=120;
}
async function load(){
  document.getElementById('rb').disabled=true;
  document.getElementById('stxt').textContent='Fetching...';
  try{
    const r=await fetch('/api/data');
    render(await r.json());
  }catch(e){
    document.getElementById('er').style.display='flex';
    document.getElementById('em').textContent='Network error: '+e.message;
  }finally{document.getElementById('rb').disabled=false;}
}
setInterval(()=>{cd--;if(cd<=0)load();},1000);
load();
</script></body></html>"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
