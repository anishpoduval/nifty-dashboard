import os, time, pyotp, requests, math
from flask import Flask, jsonify, render_template_string
from datetime import datetime, date, timedelta
from SmartApi import SmartConnect

app = Flask(__name__)

API_KEY     = "PRv269tC"
CLIENT_CODE = "A61831553"
ANGEL_PIN   = "8547"
TOTP_SECRET = "XA5CSSZRIMAHEQRJAGJFCJ5MLE"

_cache = {"data":None,"ts":0,"mom":None,"mom_ts":0,"obj":None,"obj_ts":0,"jwt":None}
CACHE_TTL = 120

# ── Auth ──────────────────────────────────────────────────────
def get_smart():
    if _cache["obj"] and time.time()-_cache["obj_ts"] < 3000:
        return _cache["obj"], _cache["jwt"]
    totp_code = pyotp.TOTP(TOTP_SECRET).now()
    obj  = SmartConnect(api_key=API_KEY)
    data = obj.generateSession(CLIENT_CODE, ANGEL_PIN, totp_code)
    if not data.get("status"):
        raise Exception("Login failed: " + str(data.get("message")) + " | " + str(data))
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

# ── Option Chain ──────────────────────────────────────────────
def get_chain(obj, jwt, spot, expiry):
    atm = round(spot/50)*50
    for strike in [str(atm), "0"]:
        try:
            d = obj.optionChain("NIFTY", expiry, strike, "OPTIDX")
            if d.get("status") and d.get("data") and len(d["data"])>5:
                return d["data"]
        except Exception:
            pass
    try:
        hdrs = {
            "Authorization":"Bearer "+jwt,"Content-Type":"application/json",
            "Accept":"application/json","X-UserType":"USER","X-SourceID":"WEB",
            "X-ClientLocalIP":"127.0.0.1","X-ClientPublicIP":"106.193.147.98",
            "X-MACAddress":"fe80::216e:6507:4b90:3719","X-PrivateKey":API_KEY,
        }
        r = requests.post(
            "https://apiconnect.angelbroking.com/rest/secure/angelbroking/marketData/v1/optionChain",
            json={"name":"NIFTY","expirydate":expiry}, headers=hdrs, timeout=15)
        d = r.json()
        if d.get("data") and len(d["data"])>5:
            return d["data"]
    except Exception:
        pass
    return None

def parse_chain(chain, spot):
    co,po = {},{}
    for row in chain:
        sp = row.get("strikePrice") or row.get("strike",0)
        try: sp=int(float(sp))
        except: continue
        ce=row.get("CE") or {}; pe=row.get("PE") or {}
        coi=ce.get("openInterest",0) if isinstance(ce,dict) else row.get("callOI",0)
        poi=pe.get("openInterest",0) if isinstance(pe,dict) else row.get("putOI",0)
        try: co[sp]=int(coi)
        except: pass
        try: po[sp]=int(poi)
        except: pass
    tc=sum(co.values()); tp=sum(po.values())
    pcr=round(tp/tc,2) if tc>0 else 1.0
    calls=sorted([(s,o) for s,o in co.items() if s>spot],  key=lambda x:-x[1])[:5]
    puts =sorted([(s,o) for s,o in po.items() if s<=spot], key=lambda x:-x[1])[:5]
    cw1=calls[0][0] if calls else int(round(spot/500+.5)*500)
    pw1=puts[0][0]  if puts  else int(round(spot/500-.5)*500)
    call_list=[{"s":s,"oi":str(round(o/100000,1))+"L"} for s,o in calls[:4]]
    put_list =[{"s":s,"oi":str(round(o/100000,1))+"L"} for s,o in puts[:4]]
    mp=round(spot/50)*50
    try:
        sks=sorted(set(list(co)+list(po))); best=float("inf")
        for s in sks:
            loss=sum(max(0,s-k)*v for k,v in co.items())+sum(max(0,k-s)*v for k,v in po.items())
            if loss<best: best,mp=loss,s
    except: pass
    return pcr,cw1,pw1,call_list,put_list,round(tc/10000000,2),round(tp/10000000,2),mp

def pcr_signal(p):
    if p>=1.4: return "STRONGLY BULLISH","BUY CALL","green"
    if p>=1.2: return "BULLISH",         "BUY CALL","green"
    if p>=1.0: return "MILDLY BULLISH",  "BUY CALL","green"
    if p>=0.9: return "NEUTRAL",          "WAIT",    "gold"
    if p>=0.75:return "MILDLY BEARISH",  "BUY PUT", "red"
    if p>=0.6: return "BEARISH",          "BUY PUT", "red"
    return              "STRONGLY BEARISH","BUY PUT", "red"

# ── Candles + Indicators ──────────────────────────────────────
def get_candles(obj):
    today   = date.today()
    from_dt = today.strftime("%Y-%m-%d") + " 09:15"
    to_dt   = today.strftime("%Y-%m-%d") + " 15:30"
    for token in ["99926000","26000"]:
        try:
            d = obj.getCandleData({"exchange":"NSE","symboltoken":token,
                "interval":"FIVE_MINUTE","fromdate":from_dt,"todate":to_dt})
            if d.get("status") and d.get("data"): return d["data"]
        except Exception: continue
    return []

def calc_vwap(candles):
    cumtpv=cumv=0; tps=[]
    for c in candles:
        h,l,cl = c[2],c[3],c[4]
        v = c[5] if len(c)>5 else 1
        tp=(h+l+cl)/3; cumtpv+=tp*v; cumv+=v; tps.append(tp)
    if cumv==0: return 0,0,0
    vwap=cumtpv/cumv
    sd=math.sqrt(sum((t-vwap)**2 for t in tps)/len(tps)) if tps else 0
    return round(vwap,2), round(vwap+sd,2), round(vwap-sd,2)

def calc_rsi(closes, period=14):
    if len(closes)<period+1: return 50
    diffs=[closes[i]-closes[i-1] for i in range(1,len(closes))]
    ag=sum(max(d,0) for d in diffs[-period:])/period
    al=sum(max(-d,0) for d in diffs[-period:])/period
    if al==0: return 100
    return round(100-(100/(1+ag/al)),1)

def calc_macd(closes):
    def ema(data,n):
        k=2/(n+1); e=[data[0]]
        for p in data[1:]: e.append(p*k+e[-1]*(1-k))
        return e
    if len(closes)<26: return 0,0,0
    e12=ema(closes,12); e26=ema(closes,26)
    macd=[a-b for a,b in zip(e12,e26)]
    sig=ema(macd[-26:],9) if len(macd)>=9 else [macd[-1]]
    return round(macd[-1],2), round(sig[-1],2), round(macd[-1]-sig[-1],2)

def detect_patterns(candles):
    pats=[]
    for i in range(max(1,len(candles)-4),len(candles)):
        c=candles[i]; o,h,l,cl=c[1],c[2],c[3],c[4]
        body=abs(cl-o); rng=h-l or 0.001
        ush=h-max(cl,o); lsh=min(cl,o)-l; bull=cl>=o
        if body<0.1*rng:
            pats.append({"name":"Doji","type":"neutral","desc":"Indecision - wait for next candle"})
        elif lsh>2*body and ush<body and bull:
            pats.append({"name":"Hammer","type":"bullish","desc":"Reversal up at support - watch CE entry"})
        elif ush>2*body and lsh<body and bull:
            pats.append({"name":"Shooting Star","type":"bearish","desc":"Rejection at resistance - watch PE entry"})
        elif ush>2*body and lsh<body and not bull:
            pats.append({"name":"Inv Hammer","type":"bullish","desc":"Potential reversal forming up"})
        if i>0:
            p=candles[i-1]; po,pcl=p[1],p[4]
            if bull and pcl<po and cl>po and o<pcl:
                pats.append({"name":"Bull Engulfing","type":"bullish","desc":"Strong reversal - buy CE at put wall"})
            elif not bull and pcl>po and cl<po and o>pcl:
                pats.append({"name":"Bear Engulfing","type":"bearish","desc":"Strong reversal - buy PE at call wall"})
    return pats[-3:]

def get_momentum(obj, spot):
    candles=get_candles(obj)
    if not candles:
        return {"ok":False,"error":"No candle data - market closed or pre-market"}
    closes=[c[4] for c in candles]
    vwap,upper,lower=calc_vwap(candles)
    rsi=calc_rsi(closes)
    _,_,hist=calc_macd(closes)
    pats=detect_patterns(candles)
    day_h=max(c[2] for c in candles); day_l=min(c[3] for c in candles)
    rpos=round((spot-day_l)/(day_h-day_l)*100) if day_h>day_l else 50
    above=spot>vwap
    sb=(above and rsi>55 and hist>0); sr=(not above and rsi<45 and hist<0)
    nu=vwap>0 and abs(spot-upper)<25; nl=vwap>0 and abs(spot-lower)<25
    atm=round(spot/50)*50
    if sb and not nu:   vsig,vcol,vnote="SCALP CE","green","Price "+str(round(spot-vwap,0))[:4]+" pts above VWAP. RSI "+str(rsi)+" bullish. MACD confirms. Buy "+str(atm+50)+" CE"
    elif sr and not nl: vsig,vcol,vnote="SCALP PE","red","Price "+str(round(vwap-spot,0))[:4]+" pts below VWAP. RSI "+str(rsi)+" bearish. MACD confirms. Buy "+str(atm-50)+" PE"
    elif nu:            vsig,vcol,vnote="NEAR UPPER BAND","gold","Near VWAP upper band ("+str(upper)+"). Avoid fresh CE. Potential reversal zone."
    elif nl:            vsig,vcol,vnote="NEAR LOWER BAND","gold","Near VWAP lower band ("+str(lower)+"). Avoid fresh PE. Potential reversal zone."
    elif above:         vsig,vcol,vnote="MILD BULL - WAIT","gold","Above VWAP but RSI "+str(rsi)+". Wait for RSI>55 + MACD cross to confirm."
    else:               vsig,vcol,vnote="MILD BEAR - WAIT","gold","Below VWAP but RSI "+str(rsi)+". Wait for RSI<45 + MACD cross to confirm."
    if rsi>=70:   rl,rc="OVERBOUGHT","red"
    elif rsi>=60: rl,rc="BULLISH","green"
    elif rsi>=45: rl,rc="NEUTRAL","gold"
    elif rsi>=30: rl,rc="BEARISH","red"
    else:         rl,rc="OVERSOLD","green"
    return {"ok":True,"candles":len(candles),"vwap":vwap,"upper":upper,"lower":lower,
            "rsi":rsi,"rsi_label":rl,"rsi_color":rc,"macd_hist":hist,"macd_bull":hist>0,
            "day_high":day_h,"day_low":day_l,"range_pos":rpos,"patterns":pats,
            "scalp_signal":vsig,"scalp_color":vcol,"scalp_note":vnote,"spot":spot,
            "ts":datetime.now().strftime("%d %b %Y  %I:%M:%S %p")}

# ── OI Analysis ───────────────────────────────────────────────
def analyse():
    obj,jwt=get_smart(); spot=get_spot(obj)
    atm=round(spot/50)*50; expiry=next_thursday()
    chain=get_chain(obj,jwt,spot,expiry); live=False
    if chain:
        live=True
        pcr,cw1,pw1,call_list,put_list,c_cr,p_cr,mp=parse_chain(chain,spot)
    else:
        pcr=1.0; cw1=int(round(spot/500+.5)*500); pw1=int(round(spot/500-.5)*500)
        mp=atm; c_cr=p_cr=0.0
        call_list=[{"s":cw1,"oi":"--"},{"s":cw1+500,"oi":"--"}]
        put_list =[{"s":pw1,"oi":"--"},{"s":pw1-500,"oi":"--"}]
    bias,sig,sc=pcr_signal(pcr)
    straddle=int(round(spot*0.20*(5/252)**0.5/50)*50)
    if "CALL" in sig:
        buy=str(atm+50)+" CE  or  "+str(atm+100)+" CE"; hedge=str(atm-200)+" PE"; t1,t2=cw1,cw1+100
        note="Bullish (PCR "+str(pcr)+"). Floor "+str(pw1)+". Target call wall "+str(cw1)+"."
    elif "PUT" in sig:
        buy=str(atm-50)+" PE  or  "+str(atm-100)+" PE"; hedge=str(atm+200)+" CE"; t1,t2=pw1,pw1-100
        note="Bearish (PCR "+str(pcr)+"). Call wall "+str(cw1)+" capping. Target "+str(pw1)+"."
    else:
        buy="Wait - confirm 9:30 AM candle"; hedge="--"; t1,t2=cw1,pw1
        note="Neutral (PCR "+str(pcr)+"). Wait 9:30 candle - Bull:"+str(atm+50)+"CE | Bear:"+str(atm-50)+"PE"
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
        d=analyse(); _cache["data"]=d; _cache["ts"]=time.time(); return jsonify(d)
    except Exception as e:
        if _cache["data"]:
            s=dict(_cache["data"]); s["warning"]="Stale - "+str(e); return jsonify(s)
        return jsonify({"ok":False,"error":str(e),"ts":datetime.now().strftime("%I:%M:%S %p")})

@app.route("/api/momentum")
def api_momentum():
    if _cache["mom"] and time.time()-_cache.get("mom_ts",0)<60:
        return jsonify(_cache["mom"])
    try:
        obj,_=get_smart()
        spot=_cache["data"]["spot"] if _cache.get("data") else get_spot(obj)
        d=get_momentum(obj,spot); _cache["mom"]=d; _cache["mom_ts"]=time.time()
        return jsonify(d)
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

@app.route("/api/test")
def api_test():
    result={}
    try:
        code=pyotp.TOTP(TOTP_SECRET).now(); result["totp_generated"]=code; result["totp_ok"]=True
    except Exception as e:
        result["totp_ok"]=False; result["totp_error"]=str(e); return jsonify(result)
    try:
        obj=SmartConnect(api_key=API_KEY)
        data=obj.generateSession(CLIENT_CODE,ANGEL_PIN,code)
        result["login_ok"]=data.get("status",False); result["login_msg"]=data.get("message","")
    except Exception as e:
        result["login_ok"]=False; result["login_error"]=str(e)
    return jsonify(result)

@app.route("/")
def index():
    return render_template_string(HTML)

HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>NIFTY Dashboard</title>
<style>
:root{--bg:#04090f;--card:#070e19;--c2:#091221;--ln:#0d1e30;
  --acc:#00d4ff;--gr:#00ff88;--rd:#ff3355;--gd:#ffcc00;--mt:#2a4060;--tx:#7aaac8;--wh:#e8f8ff}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--tx);font-family:'Courier New',monospace;min-height:100vh;padding-bottom:60px}
.hdr{background:linear-gradient(180deg,#0a1828,#04090f);border-bottom:1px solid var(--ln);padding:12px 14px;position:sticky;top:0;z-index:20}
.spot{font-size:28px;font-weight:900;color:var(--wh)}
.tabs{display:flex;background:var(--card);border-bottom:1px solid var(--ln)}
.tab{flex:1;padding:12px 8px;background:transparent;border:none;border-bottom:3px solid transparent;
  color:var(--mt);font-family:inherit;font-size:10px;letter-spacing:2px;cursor:pointer;transition:all .2s}
.tab.active{border-bottom-color:var(--acc);color:var(--acc);background:rgba(0,212,255,.08)}
.tab.active.mom{border-bottom-color:var(--gr);color:var(--gr);background:rgba(0,255,136,.08)}
.btn{background:rgba(0,212,255,.12);border:1px solid rgba(0,212,255,.3);border-radius:8px;
  padding:8px 14px;color:var(--acc);font-size:11px;font-family:inherit;cursor:pointer;letter-spacing:1px}
.wrap{padding:12px 14px;display:flex;flex-direction:column;gap:12px}
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
.struct{background:var(--c2);border-radius:10px;padding:14px;font-size:13px;line-height:2.5}
.oib{margin-bottom:10px}.orow{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px}
.obg{height:6px;background:var(--ln);border-radius:3px;overflow:hidden}
.ofl{height:100%;border-radius:3px;transition:width .8s}
.sig{border-radius:14px;padding:14px;border-width:2px;border-style:solid}
.sa{font-size:22px;font-weight:900;margin-bottom:12px}
.bb{background:var(--c2);border-radius:8px;padding:12px 14px;margin-bottom:10px}
.nt{background:var(--c2);border-radius:8px;padding:11px 13px;font-size:12px;font-style:italic;line-height:1.7;margin-top:8px}
.rl{background:rgba(255,204,0,.04);border:1px solid rgba(255,204,0,.15);border-radius:12px;padding:12px;font-size:11px;color:var(--gd);line-height:2.2}
.sl{background:rgba(0,212,255,.04);border:1px solid rgba(0,212,255,.15);border-radius:12px;padding:12px;font-size:11px;color:var(--acc);line-height:2.2}
.er{background:rgba(255,51,85,.06);border:1px solid rgba(255,51,85,.2);border-radius:12px;padding:14px;flex-direction:column;gap:8px}
.ld{background:var(--card);border-radius:14px;padding:36px 20px;text-align:center;border:1px solid var(--ln)}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:5px}
.live{animation:blink 2s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.15}}
.gr{color:var(--gr)}.rd{color:var(--rd)}.gd{color:var(--gd)}.ac{color:var(--acc)}
</style></head><body>

<div class="hdr">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <div>
      <div style="font-size:9px;color:var(--mt);letter-spacing:3px;margin-bottom:3px">NIFTY 50 &bull; ANGEL ONE LIVE</div>
      <span class="spot" id="sp">--</span>
    </div>
    <div style="text-align:right">
      <div style="font-size:10px;color:var(--mt);margin-bottom:5px">
        <span class="dot" id="dot" style="background:var(--mt)"></span><span id="stxt">Loading</span>
      </div>
      <button class="btn" id="rb" onclick="loadOI()">&#8635; REFRESH</button>
    </div>
  </div>
  <div style="display:flex;gap:14px;margin-top:6px;font-size:10px;color:var(--mt)" id="hm"></div>
  <div style="font-size:9px;color:var(--mt);margin-top:3px" id="ts"></div>
</div>

<div class="tabs">
  <button class="tab active" id="tab1btn" onclick="switchTab(1)">&#128200; OI ANALYSIS</button>
  <button class="tab mom" id="tab2btn" onclick="switchTab(2)">&#9889; MOMENTUM &amp; VWAP</button>
</div>

<!-- TAB 1: OI Analysis -->
<div id="tab1" class="wrap">
  <div class="er" id="er" style="display:none">
    <div class="rd" id="em" style="font-size:13px;word-break:break-word"></div>
    <div style="color:var(--mt);font-size:11px">Open <a href="/api/test" target="_blank" style="color:var(--acc)">/api/test</a> for diagnostics</div>
    <button class="btn" style="width:fit-content" onclick="loadOI()">&#8635; Retry</button>
  </div>
  <div class="ld" id="ld">
    <div class="ac" style="font-size:14px;letter-spacing:2px">&#9203; CONNECTING TO ANGEL ONE...</div>
    <div style="color:var(--mt);font-size:11px;margin-top:8px">First load ~10 seconds</div>
  </div>
  <div id="oi-main" style="display:none;flex-direction:column;gap:12px">
    <div id="wb" style="display:none;background:rgba(255,204,0,.06);border:1px solid rgba(255,204,0,.2);border-radius:8px;padding:9px 13px;font-size:10px;color:var(--gd)"></div>
    <div class="card">
      <div class="lbl">PCR ANALYSIS &bull; LIVE</div>
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <span style="font-size:24px;font-weight:900" id="pv">--</span>
        <span style="font-size:14px;font-weight:700" id="pb">--</span>
      </div>
      <div class="bw"><div class="bf" id="pbar" style="width:50%"></div></div>
      <div class="bl"><span>0.5 BEAR</span><span>1.0 NEUTRAL</span><span>1.5+ BULL</span></div>
      <div class="g4" style="margin-top:11px">
        <div class="cell"><div class="cl">CALL OI</div><div class="cv rd" id="co">--</div></div>
        <div class="cell"><div class="cl">PUT OI</div><div class="cv gr" id="po">--</div></div>
        <div class="cell"><div class="cl">MAX PAIN</div><div class="cv gd" id="mp">--</div></div>
        <div class="cell"><div class="cl">EXPIRY</div><div class="cv ac" id="ex">--</div></div>
      </div>
    </div>
    <div class="card">
      <div class="lbl">STRADDLE &amp; RANGE</div>
      <div class="g3">
        <div class="cell"><div class="cl">STRADDLE</div><div class="cv ac" id="str">--</div></div>
        <div class="cell"><div class="cl">RANGE LOW</div><div class="cv rd" id="rl">--</div></div>
        <div class="cell"><div class="cl">RANGE HIGH</div><div class="cv gr" id="rh">--</div></div>
      </div>
    </div>
    <div class="card"><div class="lbl">&#128205; KEY LEVELS</div><div class="struct" id="struct">--</div></div>
    <div class="card"><div class="lbl rd">&#128308; CALL WALLS &bull; Resistance</div><div id="cw">--</div></div>
    <div class="card"><div class="lbl gr">&#128994; PUT WALLS &bull; Support</div><div id="pw">--</div></div>
    <div class="sig" id="sc">
      <div class="lbl">&#127919; TRADE SIGNAL</div>
      <div class="sa" id="sa">--</div>
      <div class="bb"><div style="color:var(--mt);font-size:8px;letter-spacing:1px;margin-bottom:5px">STRIKE TO BUY</div>
        <div style="font-size:16px;font-weight:700" id="sb">--</div></div>
      <div class="g2">
        <div class="cell"><div class="cl">HEDGE</div><div class="cv gd" id="sh">--</div></div>
        <div class="cell"><div class="cl">TARGET 1</div><div class="cv ac" id="t1">--</div></div>
        <div class="cell"><div class="cl">TARGET 2</div><div class="cv ac" id="t2">--</div></div>
        <div class="cell"><div class="cl">STOP LOSS</div><div class="cv rd">40% premium</div></div>
      </div>
      <div class="nt" id="nt"></div>
    </div>
    <div class="rl">&#9888;&#65039; Wait 9:30 candle &bull; 1-2 lots MAX &bull; SL=40% &bull; Always hedge &bull; Lot=65</div>
  </div>
</div>

<!-- TAB 2: Momentum & VWAP -->
<div id="tab2" style="display:none">
  <div class="wrap">
    <div class="er" id="mom-er" style="display:none">
      <div class="rd" id="mom-em"></div>
      <div style="color:var(--mt);font-size:11px">Market may be closed. Candle data loads 9:20 AM onwards.</div>
      <button class="btn" style="width:fit-content" onclick="loadMom()">&#8635; Retry</button>
    </div>
    <div class="ld" id="mom-ld" style="display:none">
      <div class="ac" style="font-size:13px;letter-spacing:2px">&#9203; LOADING CANDLES...</div>
      <div style="color:var(--mt);font-size:11px;margin-top:6px">Fetching 5-min data from Angel One</div>
    </div>
    <div id="mom-main" style="display:none;flex-direction:column;gap:12px">

      <!-- Scalp Signal -->
      <div class="sig" id="mom-sig" style="border-color:rgba(255,204,0,.3);background:rgba(255,204,0,.05)">
        <div class="lbl">&#9889; VWAP SCALP SIGNAL</div>
        <div style="font-size:22px;font-weight:900;margin-bottom:10px" id="mom-sig-txt">--</div>
        <div style="background:var(--c2);border-radius:8px;padding:11px 13px;font-size:12px;font-style:italic;line-height:1.7" id="mom-sig-note"></div>
      </div>

      <!-- VWAP levels -->
      <div class="card">
        <div class="lbl">VWAP LEVELS &bull; <span id="mom-candle-count" style="color:var(--acc)"></span></div>
        <div class="g3">
          <div class="cell"><div class="cl">UPPER BAND</div><div class="cv gr" id="mom-upper">--</div></div>
          <div class="cell"><div class="cl">VWAP</div><div class="cv ac" id="mom-vwap">--</div></div>
          <div class="cell"><div class="cl">LOWER BAND</div><div class="cv rd" id="mom-lower">--</div></div>
        </div>
        <div style="margin-top:12px">
          <div style="display:flex;justify-content:space-between;font-size:9px;color:var(--mt);margin-bottom:5px">
            <span id="mom-dl">Low --</span>
            <span class="ac" id="mom-rpos">Range pos --%</span>
            <span id="mom-dh">High --</span>
          </div>
          <div style="height:8px;background:var(--ln);border-radius:4px;position:relative;margin-bottom:8px">
            <div id="mom-pos-dot" style="position:absolute;top:-2px;width:12px;height:12px;border-radius:50%;background:var(--acc);border:2px solid var(--bg);transform:translateX(-50%);transition:left .8s;left:50%"></div>
          </div>
          <div style="height:8px;background:var(--ln);border-radius:4px;overflow:hidden">
            <div id="mom-vwap-bar" style="height:100%;background:linear-gradient(90deg,var(--rd),var(--gd),var(--gr));border-radius:4px;width:50%;transition:width .8s"></div>
          </div>
          <div style="display:flex;justify-content:space-between;font-size:9px;color:var(--mt);margin-top:4px">
            <span>Below VWAP (Bearish)</span><span>Above VWAP (Bullish)</span>
          </div>
        </div>
      </div>

      <!-- Indicators -->
      <div class="card">
        <div class="lbl">MOMENTUM INDICATORS</div>
        <div class="g2">
          <div style="background:var(--c2);border-radius:8px;padding:12px">
            <div class="cl">RSI (14) &bull; 5-MIN</div>
            <div style="font-size:26px;font-weight:900" id="mom-rsi">--</div>
            <div style="font-size:11px;margin-top:3px;font-weight:700" id="mom-rsi-lbl">--</div>
            <div style="height:6px;background:var(--ln);border-radius:3px;margin-top:8px;overflow:hidden">
              <div id="mom-rsi-bar" style="height:100%;background:var(--acc);border-radius:3px;transition:width .8s;width:50%"></div>
            </div>
            <div style="display:flex;justify-content:space-between;font-size:9px;color:var(--mt);margin-top:3px">
              <span>0 BEAR</span><span>50</span><span>100 BULL</span>
            </div>
          </div>
          <div style="background:var(--c2);border-radius:8px;padding:12px">
            <div class="cl">MACD HISTOGRAM</div>
            <div style="font-size:26px;font-weight:900" id="mom-macd">--</div>
            <div style="font-size:11px;margin-top:3px;font-weight:700" id="mom-macd-lbl">--</div>
            <div style="margin-top:8px;font-size:10px;color:var(--mt)">
              Positive = Bull cross<br>Negative = Bear cross
            </div>
          </div>
        </div>
      </div>

      <!-- Patterns -->
      <div class="card">
        <div class="lbl">&#127343; CANDLESTICK PATTERNS &bull; Last 4 candles</div>
        <div id="mom-pats">Scanning...</div>
      </div>

      <!-- Scalping guide -->
      <div class="sl">
        &#128161; VWAP SCALP RULES (5-15 min trades):<br>
        &#128994; Price &gt; VWAP + RSI&gt;55 + MACD+ &rarr; Buy CE (ATM+50)<br>
        &#128308; Price &lt; VWAP + RSI&lt;45 + MACD- &rarr; Buy PE (ATM-50)<br>
        &#128308; Exit when price hits opposite VWAP band<br>
        &#9888;&#65039; SL: 40% premium. Never hold past 2:30 PM
      </div>

      <button class="btn" style="width:100%" onclick="loadMom()">&#8635; Refresh Momentum Data</button>
    </div>
  </div>
</div>

<script>
var cd=120;
function fi(n,d){d=d||2;return typeof n==='number'?n.toLocaleString('en-IN',{minimumFractionDigits:d,maximumFractionDigits:d}):String(n||'--');}
function fii(n){return typeof n==='number'?n.toLocaleString('en-IN'):String(n||'--');}
function INR(n){return typeof n==='number'?'\u20B9'+n.toLocaleString('en-IN'):String(n||'--');}
function gc(c){return c==='green'?'var(--gr)':c==='red'?'var(--rd)':'var(--gd)';}

function switchTab(n){
  document.getElementById('tab1').style.display=n===1?'flex':'none';
  document.getElementById('tab2').style.display=n===2?'block':'none';
  document.getElementById('tab1btn').className='tab'+(n===1?' active':'');
  document.getElementById('tab2btn').className='tab mom'+(n===2?' active':'');
  if(n===2 && document.getElementById('mom-main').style.display!=='flex') loadMom();
}

function oiBar(walls,isCall){
  if(!walls||!walls.length) return '<div style="color:var(--mt);font-size:12px">No OI data</div>';
  var col=isCall?'var(--rd)':'var(--gr)';
  var mx=Math.max.apply(null,walls.map(function(w){return parseFloat(w.oi)||1;}));
  return walls.map(function(w){
    var pct=Math.min((parseFloat(w.oi)||0)/mx*100,100);
    return '<div class="oib"><div class="orow">'
      +'<span style="color:var(--wh);font-weight:700;font-size:13px">'+fii(w.s)+' '+(isCall?'CE':'PE')+'</span>'
      +'<span style="color:'+col+';font-size:12px">'+w.oi+'</span>'
      +'</div><div class="obg"><div class="ofl" style="width:'+pct+'%;background:'+col+'"></div></div></div>';
  }).join('');}

function renderOI(d){
  if(!d.ok){
    document.getElementById('ld').style.display='none';
    document.getElementById('er').style.display='flex';
    document.getElementById('em').textContent='Error: '+(d.error||'Unknown');return;
  }
  document.getElementById('sp').textContent=fi(d.spot);
  document.getElementById('hm').innerHTML='ATM:'+fii(d.atm)+'&nbsp;&nbsp;Straddle:'+INR(d.straddle)+'&nbsp;&nbsp;'+d.expiry;
  document.getElementById('ts').textContent='Updated: '+d.ts;
  var wb=document.getElementById('wb');
  if(d.warning||!d.chain_live){wb.style.display='block';wb.textContent=d.warning||'\u26A0\uFE0F OI chain unavailable \u2014 estimated levels';}
  else wb.style.display='none';
  var sc=d.sig_color,col=gc(sc);
  document.getElementById('pv').textContent=d.pcr_all; document.getElementById('pv').style.color=col;
  document.getElementById('pb').textContent=d.bias;    document.getElementById('pb').style.color=col;
  document.getElementById('pbar').style.width=Math.min(d.pcr_all/2*100,100)+'%';
  document.getElementById('co').textContent=d.c_oi_cr?d.c_oi_cr+'Cr':'--';
  document.getElementById('po').textContent=d.p_oi_cr?d.p_oi_cr+'Cr':'--';
  document.getElementById('mp').textContent=fii(d.max_pain);
  document.getElementById('ex').textContent=d.expiry||'--';
  document.getElementById('str').innerHTML=INR(d.straddle);
  document.getElementById('rl').textContent=fii(d.range_low);
  document.getElementById('rh').textContent=fii(d.range_high);
  var c1=d.calls[0]&&d.calls[0].s, p1=d.puts[0]&&d.puts[0].s;
  document.getElementById('struct').innerHTML=
    '<div class="rd">&#128308; '+fii((c1||0)+500)+' CE &larr; Upper wall<br>'
    +'<strong>&#128308; '+fii(c1)+' CE &larr; CEILING</strong></div>'
    +'<div style="color:var(--ln)">\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500</div>'
    +'<div class="ac" style="font-size:16px;font-weight:900">&#128205; '+fi(d.spot)+'&nbsp;&nbsp;ATM '+fii(d.atm)+'</div>'
    +'<div style="color:var(--ln)">\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500</div>'
    +'<div class="gr"><strong>&#128994; '+fii(p1)+' PE &larr; FLOOR</strong><br>&#128994; '+fii((p1||0)-500)+' PE &larr; Deep floor</div>'
    +'<div class="gd">&#127919; Max Pain: '+fii(d.max_pain)+'</div>'
    +'<div style="color:var(--mt)">&#128208; Range: '+fii(d.range_low)+' &ndash; '+fii(d.range_high)+'</div>';
  document.getElementById('cw').innerHTML=oiBar(d.calls,true);
  document.getElementById('pw').innerHTML=oiBar(d.puts,false);
  var brd=sc==='green'?'rgba(0,255,136,.35)':sc==='red'?'rgba(255,51,85,.35)':'rgba(255,204,0,.35)';
  var bg=sc==='green'?'rgba(0,255,136,.06)':sc==='red'?'rgba(255,51,85,.06)':'rgba(255,204,0,.06)';
  document.getElementById('sc').style.borderColor=brd; document.getElementById('sc').style.background=bg;
  var icon=sc==='green'?'&#128994;':sc==='red'?'&#128308;':'&#129001;';
  document.getElementById('sa').innerHTML='<span style="color:'+col+'">'+icon+' '+d.signal+'</span>';
  document.getElementById('sb').textContent=d.buy; document.getElementById('sb').style.color=col;
  document.getElementById('sh').textContent=d.hedge;
  document.getElementById('t1').textContent=fii(d.t1); document.getElementById('t2').textContent=fii(d.t2);
  document.getElementById('nt').textContent=d.note;
  document.getElementById('dot').style.background='var(--gr)'; document.getElementById('dot').className='dot live';
  document.getElementById('stxt').textContent=d.chain_live?'LIVE':'LIVE (est OI)';
  document.getElementById('ld').style.display='none'; document.getElementById('er').style.display='none';
  document.getElementById('oi-main').style.display='flex'; cd=120;
}

async function loadOI(){
  document.getElementById('rb').disabled=true; document.getElementById('stxt').textContent='Fetching...';
  try{var r=await fetch('/api/data'); renderOI(await r.json());}
  catch(e){document.getElementById('er').style.display='flex'; document.getElementById('em').textContent='Network: '+e.message;}
  finally{document.getElementById('rb').disabled=false;}
}

async function loadMom(){
  document.getElementById('mom-ld').style.display='block';
  document.getElementById('mom-er').style.display='none';
  document.getElementById('mom-main').style.display='none';
  try{
    var r=await fetch('/api/momentum'); var d=await r.json();
    document.getElementById('mom-ld').style.display='none';
    if(!d.ok){
      document.getElementById('mom-er').style.display='flex';
      document.getElementById('mom-em').textContent=d.error||'Error'; return;
    }
    var sc=d.scalp_color, scol=gc(sc);
    var ms=document.getElementById('mom-sig');
    ms.style.borderColor=sc==='green'?'rgba(0,255,136,.35)':sc==='red'?'rgba(255,51,85,.35)':'rgba(255,204,0,.35)';
    ms.style.background=sc==='green'?'rgba(0,255,136,.06)':sc==='red'?'rgba(255,51,85,.06)':'rgba(255,204,0,.06)';
    var icon2=sc==='green'?'&#128994;':sc==='red'?'&#128308;':'&#129001;';
    document.getElementById('mom-sig-txt').innerHTML='<span style="color:'+scol+'">'+icon2+' '+d.scalp_signal+'</span>';
    document.getElementById('mom-sig-note').textContent=d.scalp_note;
    document.getElementById('mom-candle-count').textContent=d.candles+' candles';
    document.getElementById('mom-vwap').textContent=fii(d.vwap);
    document.getElementById('mom-upper').textContent=fii(d.upper);
    document.getElementById('mom-lower').textContent=fii(d.lower);
    document.getElementById('mom-dl').textContent='Low '+fii(d.day_low);
    document.getElementById('mom-dh').textContent='High '+fii(d.day_high);
    document.getElementById('mom-rpos').textContent='Range pos: '+d.range_pos+'%';
    document.getElementById('mom-pos-dot').style.left=d.range_pos+'%';
    var vpos=d.upper>d.lower?Math.min(Math.max((d.spot-d.lower)/(d.upper-d.lower)*100,0),100):50;
    document.getElementById('mom-vwap-bar').style.width=vpos+'%';
    var rv=document.getElementById('mom-rsi');
    rv.textContent=d.rsi; rv.style.color=gc(d.rsi_color);
    document.getElementById('mom-rsi-lbl').textContent=d.rsi_label;
    document.getElementById('mom-rsi-lbl').style.color=gc(d.rsi_color);
    document.getElementById('mom-rsi-bar').style.width=d.rsi+'%';
    document.getElementById('mom-rsi-bar').style.background=gc(d.rsi_color);
    var mh=document.getElementById('mom-macd');
    mh.textContent=d.macd_hist; mh.style.color=d.macd_bull?'var(--gr)':'var(--rd)';
    document.getElementById('mom-macd-lbl').textContent=d.macd_bull?'BULL CROSS \u25B2':'BEAR CROSS \u25BC';
    document.getElementById('mom-macd-lbl').style.color=d.macd_bull?'var(--gr)':'var(--rd)';
    var pats=d.patterns||[];
    var patDiv=document.getElementById('mom-pats');
    if(!pats.length){
      patDiv.innerHTML='<div style="color:var(--mt);font-size:12px;padding:8px 0">No strong patterns on last 4 candles \u2014 market moving without clear signal</div>';
    } else {
      patDiv.innerHTML=pats.map(function(p){
        var c=p.type==='bullish'?'var(--gr)':p.type==='bearish'?'var(--rd)':'var(--gd)';
        var ic=p.type==='bullish'?'&#128994;':p.type==='bearish'?'&#128308;':'&#129001;';
        return '<div style="background:var(--c2);border-radius:8px;padding:11px 13px;margin-bottom:8px">'
          +'<div style="color:'+c+';font-weight:700;font-size:14px">'+ic+' '+p.name+'</div>'
          +'<div style="color:var(--tx);font-size:12px;margin-top:4px">'+p.desc+'</div></div>';
      }).join('');
    }
    document.getElementById('mom-main').style.display='flex';
  }catch(e){
    document.getElementById('mom-er').style.display='flex';
    document.getElementById('mom-em').textContent='Network error: '+e.message;
  }
}

setInterval(function(){cd--; if(cd<=0)loadOI();}, 1000);
loadOI();
</script>
</body></html>"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
