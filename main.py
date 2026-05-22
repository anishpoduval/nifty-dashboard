import os, time, pyotp, requests, math, threading
from flask import Flask, jsonify, render_template_string
from datetime import datetime, date, timedelta
from SmartApi import SmartConnect

app = Flask(__name__)

API_KEY     = "PRv269tC"
CLIENT_CODE = "A61831553"
ANGEL_PIN   = "8547"
TOTP_SECRET = "XA5CSSZRIMAHEQRJAGJFCJ5MLE"

# ── Cache store ───────────────────────────────────────────────
_c = {
    "obj":None,"obj_ts":0,"jwt":None,
    "spot":None,"spot_ts":0,
    "oi":None,"oi_ts":0,
    "mom":None,"mom_ts":0,
    "candles":[],"candles_ts":0,
}
SPOT_TTL   = 3
OI_TTL     = 90
MOM_TTL    = 5
CANDLE_TTL = 60

# ── Auth ──────────────────────────────────────────────────────
def get_obj():
    if _c["obj"] and time.time()-_c["obj_ts"]<3000:
        return _c["obj"],_c["jwt"]
    obj  = SmartConnect(api_key=API_KEY)
    data = obj.generateSession(CLIENT_CODE, ANGEL_PIN, pyotp.TOTP(TOTP_SECRET).now())
    if not data.get("status"):
        raise Exception("Login: "+str(data.get("message")))
    _c["obj"]=obj; _c["jwt"]=data["data"]["jwtToken"]; _c["obj_ts"]=time.time()
    return obj, _c["jwt"]

# ── Spot (fast, every 3s) ─────────────────────────────────────
def fetch_spot():
    obj,_ = get_obj()
    for exch,token in [("NSE","99926000"),("NSE","26000")]:
        try:
            r=obj.ltpData(exch,"Nifty 50",token)
            if r.get("status") and r.get("data",{}).get("ltp"):
                return float(r["data"]["ltp"])
        except: continue
    raise Exception("Spot unavailable")

def get_spot():
    if _c["spot"] and time.time()-_c["spot_ts"]<SPOT_TTL:
        return _c["spot"]
    s=fetch_spot(); _c["spot"]=s; _c["spot_ts"]=time.time()
    return s

# ── Candles ───────────────────────────────────────────────────
def fetch_candles():
    if _c["candles"] and time.time()-_c["candles_ts"]<CANDLE_TTL:
        return _c["candles"]
    obj,_ = get_obj()
    td=date.today()
    fd=td.strftime("%Y-%m-%d")+" 09:15"; td2=td.strftime("%Y-%m-%d")+" 15:30"
    for token in ["99926000","26000"]:
        try:
            d=obj.getCandleData({"exchange":"NSE","symboltoken":token,
                "interval":"FIVE_MINUTE","fromdate":fd,"todate":td2})
            if d.get("status") and d.get("data"):
                _c["candles"]=d["data"]; _c["candles_ts"]=time.time()
                return d["data"]
        except: continue
    return _c["candles"]  # return stale if available

# ── OI Chain ──────────────────────────────────────────────────
def next_expiry():
    today=date.today(); days=(3-today.weekday())%7
    if days==0 and datetime.now().hour>=15: days=7
    return (today+timedelta(days=days)).strftime("%d%b%Y").upper()

def fetch_oi(spot):
    if _c["oi"] and time.time()-_c["oi_ts"]<OI_TTL:
        return _c["oi"]
    obj,jwt=get_obj(); atm=round(spot/50)*50; expiry=next_expiry()
    chain=None
    for strike in [str(atm),"0"]:
        try:
            d=obj.optionChain("NIFTY",expiry,strike,"OPTIDX")
            if d.get("status") and d.get("data") and len(d["data"])>5:
                chain=d["data"]; break
        except: pass
    if not chain:
        try:
            hdrs={"Authorization":"Bearer "+jwt,"Content-Type":"application/json",
                  "Accept":"application/json","X-UserType":"USER","X-SourceID":"WEB",
                  "X-ClientLocalIP":"127.0.0.1","X-ClientPublicIP":"106.193.147.98",
                  "X-MACAddress":"fe80::216e:6507:4b90:3719","X-PrivateKey":API_KEY}
            r=requests.post("https://apiconnect.angelbroking.com/rest/secure/angelbroking/marketData/v1/optionChain",
                json={"name":"NIFTY","expirydate":expiry},headers=hdrs,timeout=15)
            d=r.json()
            if d.get("data") and len(d["data"])>5: chain=d["data"]
        except: pass
    result=parse_oi(chain,spot,expiry)
    _c["oi"]=result; _c["oi_ts"]=time.time()
    return result

def parse_oi(chain,spot,expiry):
    atm=round(spot/50)*50
    live=False; co={}; po={}
    if chain:
        live=True
        for row in chain:
            sp=row.get("strikePrice") or row.get("strike",0)
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
    calls=sorted([(s,o) for s,o in co.items() if s>spot],  key=lambda x:-x[1])[:6]
    puts =sorted([(s,o) for s,o in po.items() if s<=spot], key=lambda x:-x[1])[:6]
    cw1=calls[0][0] if calls else int(round(spot/500+.5)*500)
    pw1=puts[0][0]  if puts  else int(round(spot/500-.5)*500)
    mp=atm
    if co and po:
        try:
            sks=sorted(set(list(co)+list(po))); best=float("inf")
            for s in sks:
                loss=sum(max(0,s-k)*v for k,v in co.items())+sum(max(0,k-s)*v for k,v in po.items())
                if loss<best: best,mp=loss,s
        except: pass
    straddle=int(round(spot*0.20*(5/252)**0.5/50)*50)
    return {
        "live":live,"expiry":expiry,"pcr":pcr,"atm":atm,
        "c_cr":round(tc/10000000,2),"p_cr":round(tp/10000000,2),"mp":mp,
        "straddle":straddle,"range_low":round(spot-straddle),"range_high":round(spot+straddle),
        "cw1":cw1,"pw1":pw1,"cw2":calls[1][0] if len(calls)>1 else cw1+500,
        "pw2":puts[1][0] if len(puts)>1 else pw1-500,
        "calls":[{"s":s,"oi":round(o/100000,1)} for s,o in calls[:5]],
        "puts" :[{"s":s,"oi":round(o/100000,1)} for s,o in puts[:5]],
    }

# ── Indicators ────────────────────────────────────────────────
def calc_vwap(candles):
    if not candles: return 0,0,0
    cumtpv=cumv=0; tps=[]
    for c in candles:
        h,l,cl=c[2],c[3],c[4]; v=c[5] if len(c)>5 else 1
        tp=(h+l+cl)/3; cumtpv+=tp*v; cumv+=v; tps.append(tp)
    if cumv==0: return 0,0,0
    vwap=cumtpv/cumv
    sd=math.sqrt(sum((t-vwap)**2 for t in tps)/len(tps))
    return round(vwap,2),round(vwap+sd,2),round(vwap-sd,2)

def calc_rsi(closes,period=14):
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
    return round(macd[-1],2),round(sig[-1],2),round(macd[-1]-sig[-1],2)

def detect_patterns(candles):
    pats=[]
    for i in range(max(1,len(candles)-4),len(candles)):
        c=candles[i]; o,h,l,cl=c[1],c[2],c[3],c[4]
        body=abs(cl-o); rng=h-l or 0.001
        ush=h-max(cl,o); lsh=min(cl,o)-l; bull=cl>=o
        if body<0.1*rng:
            pats.append({"name":"Doji","type":"neutral","desc":"Indecision"})
        elif lsh>2*body and ush<body and bull:
            pats.append({"name":"Hammer","type":"bullish","desc":"Bullish reversal"})
        elif ush>2*body and lsh<body and bull:
            pats.append({"name":"Shooting Star","type":"bearish","desc":"Bearish reversal"})
        if i>0:
            p=candles[i-1]; po,pcl=p[1],p[4]
            if bull and pcl<po and cl>po and o<pcl:
                pats.append({"name":"Bullish Engulfing","type":"bullish","desc":"Strong reversal up"})
            elif not bull and pcl>po and cl<po and o>pcl:
                pats.append({"name":"Bearish Engulfing","type":"bearish","desc":"Strong reversal down"})
    return pats[-2:]

# ── Master Signal Engine ──────────────────────────────────────
def compute_signal(spot, oi, candles):
    """Combines PCR + VWAP + RSI + MACD + Patterns into ONE clear signal"""
    atm=round(spot/50)*50
    score=0; reasons=[]

    # 1. PCR signal (weight: 3)
    pcr=oi["pcr"]
    if pcr>=1.2:   score+=3; reasons.append("PCR "+str(pcr)+" bullish")
    elif pcr>=1.0: score+=1; reasons.append("PCR "+str(pcr)+" mild bull")
    elif pcr<=0.7: score-=3; reasons.append("PCR "+str(pcr)+" bearish")
    elif pcr<=0.9: score-=1; reasons.append("PCR "+str(pcr)+" mild bear")
    else:          reasons.append("PCR "+str(pcr)+" neutral")

    # 2. VWAP (weight: 2)
    vwap=upper=lower=0
    rsi=50; hist=0
    if candles:
        closes=[c[4] for c in candles]
        vwap,upper,lower=calc_vwap(candles)
        rsi=calc_rsi(closes)
        _,_,hist=calc_macd(closes)
        if vwap>0:
            if spot>vwap:   score+=2; reasons.append("Above VWAP ("+str(vwap)+")")
            else:           score-=2; reasons.append("Below VWAP ("+str(vwap)+")")

        # 3. RSI (weight: 2)
        if rsi>=60:   score+=2; reasons.append("RSI "+str(rsi)+" bullish")
        elif rsi>=50: score+=1
        elif rsi<=40: score-=2; reasons.append("RSI "+str(rsi)+" bearish")
        elif rsi<=50: score-=1

        # 4. MACD (weight: 1)
        if hist>0:   score+=1; reasons.append("MACD bull cross")
        elif hist<0: score-=1; reasons.append("MACD bear cross")

    # 5. OI wall proximity (weight: 1)
    cw1=oi["cw1"]; pw1=oi["pw1"]
    near_wall = abs(spot-cw1)<50
    near_floor= abs(spot-pw1)<50
    if near_wall:  score-=1; reasons.append("Near call wall "+str(cw1))
    if near_floor: score+=1; reasons.append("Near put floor "+str(pw1))

    # 6. Candlestick patterns (weight: 1)
    pats=detect_patterns(candles) if candles else []
    for p in pats:
        if p["type"]=="bullish":   score+=1
        elif p["type"]=="bearish": score-=1

    # Convert score to signal
    max_score=9
    strength=min(abs(score)/max_score*100,100)

    if score>=4:
        action="BUY CALL"; color="green"; emoji="UP"
        strike=str(atm+50)+" CE"
        hedge=str(atm-200)+" PE"
        t1,t2=cw1,cw1+100
        conf="HIGH" if score>=6 else "MEDIUM"
    elif score<=-4:
        action="BUY PUT"; color="red"; emoji="DOWN"
        strike=str(atm-50)+" PE"
        hedge=str(atm+200)+" CE"
        t1,t2=pw1,pw1-100
        conf="HIGH" if score<=-6 else "MEDIUM"
    elif score>=2:
        action="LEAN CALL"; color="green"; emoji="UP"
        strike=str(atm+50)+" CE"
        hedge=str(atm-200)+" PE"
        t1,t2=cw1,cw1+50
        conf="LOW"
    elif score<=-2:
        action="LEAN PUT"; color="red"; emoji="DOWN"
        strike=str(atm-50)+" PE"
        hedge=str(atm+200)+" CE"
        t1,t2=pw1,pw1-50
        conf="LOW"
    else:
        action="WAIT"; color="gold"; emoji="FLAT"
        strike="No trade yet"
        hedge="--"; t1=cw1; t2=pw1; conf="--"

    return {
        "action":action,"color":color,"emoji":emoji,"strike":strike,
        "hedge":hedge,"t1":t1,"t2":t2,"conf":conf,
        "score":score,"strength":round(strength),
        "reasons":reasons[:4],
        "vwap":vwap,"upper":upper,"lower":lower,
        "rsi":rsi,"hist":hist,"patterns":pats
    }

# ── Background refresher ──────────────────────────────────────
def background_refresh():
    """Continuously refresh spot every 3s in background"""
    while True:
        try:
            s=fetch_spot(); _c["spot"]=s; _c["spot_ts"]=time.time()
        except: pass
        time.sleep(3)

# Start background thread
t=threading.Thread(target=background_refresh,daemon=True)
t.start()

# ── API Endpoints ─────────────────────────────────────────────
@app.route("/api/live")
def api_live():
    """Fast endpoint — returns spot + signal every 3s"""
    try:
        spot=get_spot()
        oi=fetch_oi(spot)
        candles=fetch_candles()
        sig=compute_signal(spot,oi,candles)
        return jsonify({
            "ok":True,"spot":spot,"ts":datetime.now().strftime("%H:%M:%S"),
            "oi":oi,"sig":sig
        })
    except Exception as e:
        return jsonify({"ok":False,"error":str(e),"ts":datetime.now().strftime("%H:%M:%S")})

@app.route("/api/spot")
def api_spot():
    """Ultra-fast — spot only, every 3s"""
    try:
        return jsonify({"ok":True,"spot":get_spot(),"ts":datetime.now().strftime("%H:%M:%S")})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

@app.route("/api/test")
def api_test():
    try:
        code=pyotp.TOTP(TOTP_SECRET).now()
        obj=SmartConnect(api_key=API_KEY)
        data=obj.generateSession(CLIENT_CODE,ANGEL_PIN,code)
        return jsonify({"login_ok":data.get("status"),"msg":data.get("message"),"totp":code})
    except Exception as e:
        return jsonify({"error":str(e)})

@app.route("/")
def index():
    return render_template_string(HTML)

HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>NIFTY Live</title>
<style>
:root{
  --bg:#070b12;--s1:#0c1220;--s2:#111a2a;--s3:#162235;
  --acc:#00c8ff;--gr:#00e87a;--rd:#ff2d55;--gd:#ffd60a;
  --mt:#334d66;--tx:#8ab4cc;--wh:#eef6ff;
  --gr-bg:rgba(0,232,122,.07);--rd-bg:rgba(255,45,85,.07);--gd-bg:rgba(255,214,10,.07);
  --gr-br:rgba(0,232,122,.3);--rd-br:rgba(255,45,85,.3);--gd-br:rgba(255,214,10,.3)
}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{background:var(--bg);color:var(--tx);font-family:-apple-system,'SF Pro Display','Segoe UI',sans-serif;
  min-height:100vh;overflow-x:hidden}

/* Header */
.hdr{background:var(--s1);border-bottom:1px solid var(--s3);padding:14px 16px;
  position:sticky;top:0;z-index:50;backdrop-filter:blur(20px)}
.spot-val{font-size:32px;font-weight:800;color:var(--wh);letter-spacing:-1px;
  font-variant-numeric:tabular-nums;transition:color .3s}
.spot-chg{font-size:13px;font-weight:600;margin-left:10px;padding:3px 8px;
  border-radius:20px;display:inline-block}
.live-badge{display:flex;align-items:center;gap:5px;font-size:10px;color:var(--gr);
  letter-spacing:1px;font-weight:600}
.live-dot{width:6px;height:6px;border-radius:50%;background:var(--gr);
  animation:pulse 1.5s infinite}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.4;transform:scale(.8)}}

/* Tabs */
.tabs{display:flex;background:var(--s1);border-bottom:1px solid var(--s3)}
.tab{flex:1;padding:13px 0;border:none;background:transparent;
  color:var(--mt);font-size:11px;letter-spacing:1.5px;font-weight:700;
  cursor:pointer;border-bottom:2px solid transparent;transition:all .2s;text-transform:uppercase}
.tab.on{color:var(--acc);border-bottom-color:var(--acc)}

/* Signal card — the hero element */
.sig-hero{margin:16px;border-radius:20px;padding:24px 20px;
  border:1.5px solid var(--gd-br);background:var(--gd-bg);
  transition:all .4s ease}
.sig-hero.bull{border-color:var(--gr-br);background:var(--gr-bg)}
.sig-hero.bear{border-color:var(--rd-br);background:var(--rd-bg)}
.sig-label{font-size:11px;letter-spacing:2px;font-weight:700;color:var(--mt);margin-bottom:6px}
.sig-action{font-size:36px;font-weight:900;letter-spacing:-0.5px;margin-bottom:4px;line-height:1}
.sig-strike{font-size:22px;font-weight:700;margin:10px 0 0;opacity:.9}
.sig-conf{display:inline-block;font-size:10px;font-weight:700;letter-spacing:2px;
  padding:4px 12px;border-radius:20px;margin-top:10px}
.conf-high{background:rgba(0,232,122,.15);color:var(--gr);border:1px solid var(--gr-br)}
.conf-med{background:rgba(255,214,10,.15);color:var(--gd);border:1px solid var(--gd-br)}
.conf-low{background:rgba(255,255,255,.06);color:var(--mt);border:1px solid var(--s3)}

/* Signal strength bar */
.str-bar{height:4px;background:var(--s3);border-radius:2px;margin-top:16px;overflow:hidden}
.str-fill{height:100%;border-radius:2px;transition:width .5s ease}

/* Reasons */
.reasons{margin-top:14px;display:flex;flex-wrap:wrap;gap:6px}
.reason{font-size:10px;padding:4px 10px;border-radius:20px;
  background:rgba(255,255,255,.05);color:var(--tx);border:1px solid var(--s3)}

/* Cards */
.wrap{padding:0 16px 16px}
.card{background:var(--s1);border-radius:16px;padding:16px;
  border:1px solid var(--s3);margin-bottom:12px}
.card-title{font-size:9px;color:var(--mt);letter-spacing:3px;font-weight:700;
  margin-bottom:14px;text-transform:uppercase}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.g3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
.g4{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.metric{background:var(--s2);border-radius:12px;padding:12px}
.metric-l{font-size:8px;color:var(--mt);letter-spacing:1px;margin-bottom:5px;text-transform:uppercase}
.metric-v{font-size:15px;font-weight:700;color:var(--wh)}
.metric-v.sm{font-size:13px}

/* PCR gauge */
.pcr-big{font-size:44px;font-weight:900;line-height:1}
.pcr-bar{height:8px;background:var(--s3);border-radius:4px;overflow:hidden;margin:10px 0 4px}
.pcr-fill{height:100%;background:linear-gradient(90deg,var(--rd),var(--gd),var(--gr));transition:width 1s}
.pcr-labels{display:flex;justify-content:space-between;font-size:9px;color:var(--mt)}

/* OI walls */
.oi-row{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.oi-bar-bg{height:5px;background:var(--s3);border-radius:3px;overflow:hidden;margin-top:4px}
.oi-bar-fill{height:100%;border-radius:3px;transition:width .8s}

/* Level display */
.levels{background:var(--s2);border-radius:12px;padding:14px;font-size:13px;line-height:2.6}

/* RSI ring */
.rsi-big{font-size:44px;font-weight:900;line-height:1}
.ind-bar{height:6px;background:var(--s3);border-radius:3px;overflow:hidden;margin:8px 0 3px}
.ind-fill{height:100%;border-radius:3px;transition:width .8s}

/* Pattern chips */
.pat{display:inline-flex;align-items:center;gap:6px;padding:8px 12px;
  border-radius:10px;font-size:12px;font-weight:600;margin:4px}
.pat.bull{background:var(--gr-bg);color:var(--gr);border:1px solid var(--gr-br)}
.pat.bear{background:var(--rd-bg);color:var(--rd);border:1px solid var(--rd-br)}
.pat.neutral{background:rgba(255,255,255,.04);color:var(--mt);border:1px solid var(--s3)}

/* VWAP position */
.vwap-track{height:10px;background:var(--s3);border-radius:5px;
  position:relative;margin:10px 0}
.vwap-dot{position:absolute;top:-3px;width:16px;height:16px;border-radius:50%;
  background:var(--acc);border:3px solid var(--bg);transform:translateX(-50%);
  transition:left .8s;box-shadow:0 0 10px var(--acc)}
.vwap-line{position:absolute;top:-1px;left:50%;width:2px;height:12px;
  background:var(--gd);opacity:.6}

/* Error / Loading */
.loading{text-align:center;padding:50px 20px;color:var(--mt)}
.loading-dot{display:inline-block;width:8px;height:8px;border-radius:50%;
  background:var(--acc);margin:0 3px;animation:ld .8s infinite}
.loading-dot:nth-child(2){animation-delay:.15s}
.loading-dot:nth-child(3){animation-delay:.3s}
@keyframes ld{0%,80%,100%{transform:scale(0);opacity:.3}40%{transform:scale(1);opacity:1}}
.err-box{margin:16px;padding:16px;background:var(--rd-bg);border:1px solid var(--rd-br);
  border-radius:14px;font-size:13px}
.btn{background:rgba(0,200,255,.12);border:1px solid rgba(0,200,255,.3);
  border-radius:10px;padding:10px 18px;color:var(--acc);font-size:12px;
  font-weight:700;letter-spacing:1px;cursor:pointer;display:inline-block}

/* Refresh badge */
.refresh-info{text-align:center;padding:8px;font-size:10px;color:var(--mt)}

/* Hidden */
.hide{display:none!important}
</style></head><body>

<!-- HEADER -->
<div class="hdr">
  <div style="display:flex;justify-content:space-between;align-items:flex-start">
    <div>
      <div class="live-badge" id="live-badge">
        <span class="live-dot"></span><span id="live-txt">CONNECTING</span>
      </div>
      <div style="margin-top:4px">
        <span class="spot-val" id="spot-val">--</span>
        <span class="spot-chg" id="spot-chg" style="background:rgba(255,255,255,.06);color:var(--mt)">--</span>
      </div>
      <div style="font-size:10px;color:var(--mt);margin-top:4px" id="hdr-meta">--</div>
    </div>
    <div style="text-align:right;padding-top:2px">
      <div style="font-size:9px;color:var(--mt)" id="last-ts">--</div>
      <button class="btn" id="ref-btn" onclick="forceRefresh()" style="margin-top:8px;padding:7px 12px">&#8635;</button>
    </div>
  </div>
</div>

<!-- TABS -->
<div class="tabs">
  <button class="tab on" id="t1btn" onclick="showTab(1)">&#127919; SIGNAL</button>
  <button class="tab" id="t2btn" onclick="showTab(2)">&#128200; OI DATA</button>
  <button class="tab" id="t3btn" onclick="showTab(3)">&#9889; MOMENTUM</button>
</div>

<!-- ═══════════════════════════════════════════════════════
     TAB 1: SIGNAL (hero view)
═══════════════════════════════════════════════════════ -->
<div id="tab1">
  <div class="loading" id="t1-ld">
    <span class="loading-dot"></span><span class="loading-dot"></span><span class="loading-dot"></span>
    <div style="margin-top:16px;font-size:12px">Connecting to Angel One...</div>
  </div>
  <div id="t1-err" class="hide">
    <div class="err-box"><span class="rd" id="t1-em"></span>
      <div style="margin-top:10px"><button class="btn" onclick="forceRefresh()">Retry</button></div></div>
  </div>
  <div id="t1-main" class="hide">
    <!-- Hero Signal Card -->
    <div class="sig-hero" id="sig-hero">
      <div class="sig-label">NIFTY SIGNAL &bull; <span id="sig-exp">--</span></div>
      <div class="sig-action" id="sig-action">--</div>
      <div class="sig-strike" id="sig-strike">--</div>
      <div>
        <span class="sig-conf" id="sig-conf">--</span>
        <span style="font-size:11px;color:var(--mt);margin-left:10px" id="sig-score">--</span>
      </div>
      <div class="str-bar"><div class="str-fill" id="str-fill" style="width:0%"></div></div>
      <div class="reasons" id="sig-reasons"></div>
    </div>

    <div class="wrap">
      <!-- Trade Details -->
      <div class="card">
        <div class="card-title">TRADE DETAILS</div>
        <div class="g2" style="margin-bottom:10px">
          <div class="metric">
            <div class="metric-l">BUY STRIKE</div>
            <div class="metric-v sm" id="td-strike" style="color:var(--gr)">--</div>
          </div>
          <div class="metric">
            <div class="metric-l">HEDGE</div>
            <div class="metric-v sm gd" id="td-hedge">--</div>
          </div>
        </div>
        <div class="g2">
          <div class="metric">
            <div class="metric-l">TARGET 1</div>
            <div class="metric-v sm" style="color:var(--acc)" id="td-t1">--</div>
          </div>
          <div class="metric">
            <div class="metric-l">TARGET 2</div>
            <div class="metric-v sm" style="color:var(--acc)" id="td-t2">--</div>
          </div>
          <div class="metric">
            <div class="metric-l">STOP LOSS</div>
            <div class="metric-v sm rd">40% of prem</div>
          </div>
          <div class="metric">
            <div class="metric-l">LOT SIZE</div>
            <div class="metric-v sm" style="color:var(--wh)">75</div>
          </div>
        </div>
      </div>

      <!-- Key levels summary -->
      <div class="card">
        <div class="card-title">KEY LEVELS</div>
        <div class="levels" id="kl-struct">--</div>
      </div>

      <!-- Rules -->
      <div style="background:rgba(255,214,10,.04);border:1px solid rgba(255,214,10,.12);
        border-radius:14px;padding:14px;font-size:11px;color:var(--mt);line-height:2.1;margin-bottom:12px">
        <span style="color:var(--gd);font-weight:700">RULES &bull;</span>
        Wait 9:30 candle &bull; 1-2 lots only &bull; SL = 40% &bull; Always hedge &bull; Exit before 3 PM
      </div>
    </div>
  </div>
</div>

<!-- ═══════════════════════════════════════════════════════
     TAB 2: OI DATA
═══════════════════════════════════════════════════════ -->
<div id="tab2" class="hide">
  <div class="loading" id="t2-ld">
    <span class="loading-dot"></span><span class="loading-dot"></span><span class="loading-dot"></span>
  </div>
  <div id="t2-main" class="wrap hide">
    <!-- PCR Card -->
    <div class="card">
      <div class="card-title">PUT-CALL RATIO &bull; LIVE</div>
      <div style="display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:8px">
        <span class="pcr-big" id="pcr-val">--</span>
        <span style="font-size:15px;font-weight:700" id="pcr-bias">--</span>
      </div>
      <div class="pcr-bar"><div class="pcr-fill" id="pcr-bar" style="width:50%"></div></div>
      <div class="pcr-labels"><span>0.5 BEARISH</span><span>1.0 NEUTRAL</span><span>1.5+ BULLISH</span></div>
      <div class="g4" style="margin-top:12px">
        <div class="metric"><div class="metric-l">CALL OI</div><div class="metric-v sm rd" id="oi-c">--</div></div>
        <div class="metric"><div class="metric-l">PUT OI</div><div class="metric-v sm gr" id="oi-p">--</div></div>
        <div class="metric"><div class="metric-l">MAX PAIN</div><div class="metric-v sm gd" id="oi-mp">--</div></div>
        <div class="metric"><div class="metric-l">EXPIRY</div><div class="metric-v sm" style="color:var(--acc);font-size:11px" id="oi-exp">--</div></div>
      </div>
    </div>

    <!-- Straddle -->
    <div class="card">
      <div class="card-title">STRADDLE &amp; RANGE</div>
      <div class="g3">
        <div class="metric"><div class="metric-l">STRADDLE</div><div class="metric-v" style="color:var(--acc)" id="oi-str">--</div></div>
        <div class="metric"><div class="metric-l">RANGE LOW</div><div class="metric-v rd" id="oi-rl">--</div></div>
        <div class="metric"><div class="metric-l">RANGE HIGH</div><div class="metric-v gr" id="oi-rh">--</div></div>
      </div>
    </div>

    <!-- Call walls -->
    <div class="card">
      <div class="card-title" style="color:var(--rd)">&#128308; CALL WALLS &bull; Resistance (selling pressure above)</div>
      <div id="cw-list">--</div>
    </div>

    <!-- Put walls -->
    <div class="card">
      <div class="card-title" style="color:var(--gr)">&#128994; PUT WALLS &bull; Support (buying pressure below)</div>
      <div id="pw-list">--</div>
    </div>

    <div style="text-align:center;font-size:10px;color:var(--mt);padding:4px 0 16px">
      OI refreshes every 90s &bull; <span id="oi-age">--</span>
    </div>
  </div>
</div>

<!-- ═══════════════════════════════════════════════════════
     TAB 3: MOMENTUM
═══════════════════════════════════════════════════════ -->
<div id="tab3" class="hide">
  <div class="loading" id="t3-ld">
    <span class="loading-dot"></span><span class="loading-dot"></span><span class="loading-dot"></span>
    <div style="margin-top:16px;font-size:12px">Market open 9:20 AM onwards</div>
  </div>
  <div id="t3-main" class="wrap hide">

    <!-- VWAP signal -->
    <div class="sig-hero" id="vwap-hero">
      <div class="sig-label">VWAP SCALP SIGNAL &bull; 5-MIN</div>
      <div style="font-size:28px;font-weight:900" id="vwap-sig">--</div>
      <div style="font-size:13px;margin-top:8px;opacity:.8" id="vwap-note">--</div>
    </div>

    <!-- VWAP levels -->
    <div class="card">
      <div class="card-title">VWAP BANDS &bull; <span id="mom-candles" style="color:var(--acc)">--</span> candles</div>
      <div class="g3">
        <div class="metric"><div class="metric-l">UPPER BAND</div><div class="metric-v gr" id="vwap-upper">--</div></div>
        <div class="metric"><div class="metric-l">VWAP</div><div class="metric-v" style="color:var(--acc)" id="vwap-val">--</div></div>
        <div class="metric"><div class="metric-l">LOWER BAND</div><div class="metric-v rd" id="vwap-lower">--</div></div>
      </div>
      <div style="margin-top:14px">
        <div style="display:flex;justify-content:space-between;font-size:9px;color:var(--mt);margin-bottom:6px">
          <span id="mom-low">Low --</span>
          <span style="color:var(--acc)" id="mom-rpos">Position --%</span>
          <span id="mom-high">High --</span>
        </div>
        <div class="vwap-track">
          <div class="vwap-line"></div>
          <div class="vwap-dot" id="vwap-dot" style="left:50%"></div>
        </div>
        <div style="display:flex;justify-content:space-between;font-size:9px;color:var(--mt);margin-top:3px">
          <span>Below VWAP (Bear zone)</span><span>Above VWAP (Bull zone)</span>
        </div>
      </div>
    </div>

    <!-- RSI + MACD -->
    <div class="card">
      <div class="card-title">INDICATORS &bull; 5-MIN CANDLES</div>
      <div class="g2">
        <div style="background:var(--s2);border-radius:12px;padding:14px">
          <div class="metric-l">RSI (14)</div>
          <div class="rsi-big" id="rsi-val">--</div>
          <div style="font-size:12px;font-weight:700;margin-top:4px" id="rsi-lbl">--</div>
          <div class="ind-bar"><div class="ind-fill" id="rsi-bar" style="width:50%"></div></div>
          <div style="display:flex;justify-content:space-between;font-size:9px;color:var(--mt)">
            <span>0</span><span>OVERSOLD 30</span><span>70 OVERBOUGHT</span>
          </div>
        </div>
        <div style="background:var(--s2);border-radius:12px;padding:14px">
          <div class="metric-l">MACD HIST</div>
          <div class="rsi-big" id="macd-val">--</div>
          <div style="font-size:12px;font-weight:700;margin-top:4px" id="macd-lbl">--</div>
          <div style="margin-top:12px;font-size:10px;color:var(--mt);line-height:1.8">
            +ve = Bullish momentum<br>-ve = Bearish momentum
          </div>
        </div>
      </div>
    </div>

    <!-- Patterns -->
    <div class="card">
      <div class="card-title">CANDLESTICK PATTERNS</div>
      <div id="pat-list">Scanning...</div>
    </div>

    <!-- Guide -->
    <div style="background:rgba(0,200,255,.04);border:1px solid rgba(0,200,255,.12);
      border-radius:14px;padding:14px;font-size:11px;color:var(--mt);line-height:2.1;margin-bottom:12px">
      <span style="color:var(--acc);font-weight:700">SCALP RULES &bull;</span>
      Above VWAP + RSI>55 + MACD+ = Buy CE &bull;
      Below VWAP + RSI&lt;45 + MACD- = Buy PE &bull;
      Exit at opposite band &bull; SL 40% &bull; Max 15 min hold
    </div>
  </div>
</div>

<div class="refresh-info" id="refresh-info">Spot refreshes every 3s &bull; OI every 90s</div>

<script>
var D={spot:0,prev:0,oi:null,sig:null,candles:[]};
var lastFull=0, oiAge=0;
var activeTab=1, initialized=false;

function fi(n,d){
  d=d||2;
  return typeof n==='number'?n.toLocaleString('en-IN',{minimumFractionDigits:d,maximumFractionDigits:d}):String(n||'--');
}
function fii(n){return typeof n==='number'?n.toLocaleString('en-IN'):String(n||'--');}
function INR(n){return '\u20B9'+(typeof n==='number'?n.toLocaleString('en-IN'):String(n||'--'));}
function gc(c){return c==='green'?'var(--gr)':c==='red'?'var(--rd)':'var(--gd)';}

function showTab(n){
  activeTab=n;
  [1,2,3].forEach(function(i){
    document.getElementById('tab'+i).className=i===n?'':'hide';
    var b=document.getElementById('t'+i+'btn');
    b.className='tab'+(i===n?' on':'');
  });
}

// ── Render Signal Tab ─────────────────────────────────────────
function renderSignal(d){
  if(!d.ok){
    document.getElementById('t1-ld').className='hide';
    document.getElementById('t1-err').className='';
    document.getElementById('t1-em').textContent=d.error||'Connection failed';
    return;
  }
  var sig=d.sig, oi=d.oi;
  var col=gc(sig.color);

  // Spot header
  var prev=D.spot||d.spot;
  var chg=d.spot-prev; var chgPct=(chg/prev*100);
  document.getElementById('spot-val').textContent=fi(d.spot);
  document.getElementById('spot-val').style.color=chg>0?'var(--gr)':chg<0?'var(--rd)':'var(--wh)';
  var chgEl=document.getElementById('spot-chg');
  chgEl.textContent=(chg>=0?'+':'')+fi(chg,2)+' ('+(chgPct>=0?'+':'')+fi(chgPct,2)+'%)';
  chgEl.style.background=chg>0?'rgba(0,232,122,.15)':chg<0?'rgba(255,45,85,.15)':'rgba(255,255,255,.06)';
  chgEl.style.color=chg>0?'var(--gr)':chg<0?'var(--rd)':'var(--mt)';
  document.getElementById('hdr-meta').textContent='ATM:'+fii(oi.atm)+' \u2022 PCR:'+oi.pcr+' \u2022 '+oi.expiry;
  document.getElementById('last-ts').textContent=d.ts;
  document.getElementById('live-txt').textContent='LIVE';

  // Signal hero card
  var hero=document.getElementById('sig-hero');
  hero.className='sig-hero '+(sig.color==='green'?'bull':sig.color==='red'?'bear':'');
  document.getElementById('sig-exp').textContent=oi.expiry;
  var aEl=document.getElementById('sig-action');
  aEl.textContent=sig.action; aEl.style.color=col;
  var sEl=document.getElementById('sig-strike');
  sEl.textContent=sig.action==='WAIT'?'No trade — wait for setup':sig.strike;
  sEl.style.color=sig.action==='WAIT'?'var(--mt)':col;

  var confEl=document.getElementById('sig-conf');
  confEl.textContent=sig.conf==='HIGH'?'HIGH CONFIDENCE':sig.conf==='MEDIUM'?'MEDIUM':sig.conf==='LOW'?'LOW':'\u2014 WAIT';
  confEl.className='sig-conf '+(sig.conf==='HIGH'?'conf-high':sig.conf==='MEDIUM'?'conf-med':'conf-low');
  document.getElementById('sig-score').textContent='Score: '+sig.score+'/9';
  document.getElementById('str-fill').style.width=sig.strength+'%';
  document.getElementById('str-fill').style.background=col;

  var rDiv=document.getElementById('sig-reasons');
  rDiv.innerHTML=sig.reasons.map(function(r){
    return '<span class="reason">'+r+'</span>';
  }).join('');

  // Trade details
  document.getElementById('td-strike').textContent=sig.strike;
  document.getElementById('td-strike').style.color=col;
  document.getElementById('td-hedge').textContent=sig.hedge;
  document.getElementById('td-t1').textContent=fii(sig.t1);
  document.getElementById('td-t2').textContent=fii(sig.t2);

  // Key levels
  document.getElementById('kl-struct').innerHTML=
    '<div style="color:var(--rd)">\u{1F534} '+fii(oi.cw1)+' CE \u2190 CEILING</div>'
    +'<div style="color:var(--s3)">\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500</div>'
    +'<div style="color:var(--acc);font-size:16px;font-weight:800">\u{1F4CD} '+fi(d.spot)+' \u2022 ATM '+fii(oi.atm)+'</div>'
    +'<div style="color:var(--s3)">\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500</div>'
    +'<div style="color:var(--gr)">\u{1F7E2} '+fii(oi.pw1)+' PE \u2190 FLOOR</div>'
    +'<div style="color:var(--gd);font-size:12px">\u{1F3AF} Max Pain: '+fii(oi.mp)+' \u2022 Straddle: '+INR(oi.straddle)+'</div>';

  document.getElementById('t1-ld').className='hide';
  document.getElementById('t1-err').className='hide';
  document.getElementById('t1-main').className='';
}

// ── Render OI Tab ─────────────────────────────────────────────
function renderOI(oi){
  document.getElementById('t2-ld').className='hide';
  var col=oi.pcr>=1?'var(--gr)':oi.pcr>=0.9?'var(--gd)':'var(--rd)';
  var bias=oi.pcr>=1.4?'STRONGLY BULLISH':oi.pcr>=1.2?'BULLISH':oi.pcr>=1.0?'MILDLY BULLISH':
           oi.pcr>=0.9?'NEUTRAL':oi.pcr>=0.75?'MILDLY BEARISH':oi.pcr>=0.6?'BEARISH':'STRONGLY BEARISH';
  var pv=document.getElementById('pcr-val');
  pv.textContent=oi.pcr; pv.style.color=col;
  var pb=document.getElementById('pcr-bias');
  pb.textContent=bias; pb.style.color=col;
  document.getElementById('pcr-bar').style.width=Math.min(oi.pcr/2*100,100)+'%';
  document.getElementById('oi-c').textContent=oi.c_cr?oi.c_cr+'Cr':'--';
  document.getElementById('oi-p').textContent=oi.p_cr?oi.p_cr+'Cr':'--';
  document.getElementById('oi-mp').textContent=fii(oi.mp);
  document.getElementById('oi-exp').textContent=oi.expiry;
  document.getElementById('oi-str').innerHTML=INR(oi.straddle);
  document.getElementById('oi-rl').textContent=fii(oi.range_low);
  document.getElementById('oi-rh').textContent=fii(oi.range_high);
  oiAge=0;

  function oiWalls(walls,isCall){
    if(!walls||!walls.length) return '<div style="color:var(--mt);font-size:12px;padding:8px">No data</div>';
    var col=isCall?'var(--rd)':'var(--gr)';
    var mx=Math.max.apply(null,walls.map(function(w){return parseFloat(w.oi)||1;}));
    return walls.map(function(w){
      var pct=Math.min((parseFloat(w.oi)||0)/mx*100,100);
      return '<div class="oi-row" style="flex-direction:column;align-items:stretch;gap:0">'
        +'<div style="display:flex;justify-content:space-between;align-items:center">'
        +'<span style="color:var(--wh);font-size:14px;font-weight:700">'+fii(w.s)+' '+(isCall?'CE':'PE')+'</span>'
        +'<span style="color:'+col+';font-size:13px;font-weight:600">'+w.oi+'L</span>'
        +'</div>'
        +'<div class="oi-bar-bg"><div class="oi-bar-fill" style="width:'+pct+'%;background:'+col+'"></div></div>'
        +'</div>';
    }).join('');
  }
  document.getElementById('cw-list').innerHTML=oiWalls(oi.calls,true);
  document.getElementById('pw-list').innerHTML=oiWalls(oi.puts,false);
  document.getElementById('t2-main').className='wrap';
}

// ── Render Momentum Tab ───────────────────────────────────────
function renderMom(sig){
  if(!sig) return;
  document.getElementById('t3-ld').className='hide';
  var vwap=sig.vwap, upper=sig.upper, lower=sig.lower;
  var rsi=sig.rsi, hist=sig.hist;
  if(vwap===0){
    document.getElementById('t3-ld').className='loading';
    document.getElementById('t3-ld').querySelector('div').textContent='No candle data \u2014 opens at 9:20 AM';
    return;
  }
  var spot=D.spot||0;
  var vpos=upper>lower?Math.min(Math.max((spot-lower)/(upper-lower)*100,0),100):50;
  var rpos=D.oi?Math.min(Math.max((spot-(D.oi.pw1||spot-300))/((D.oi.cw1||spot+300)-(D.oi.pw1||spot-300))*100,0),100):50;

  // VWAP signal
  var vh=document.getElementById('vwap-hero');
  var sc=sig.color; var col=gc(sc);
  vh.className='sig-hero '+(sc==='green'?'bull':sc==='red'?'bear':'');
  var icon=sc==='green'?'\u{1F7E2}':sc==='red'?'\u{1F534}':'\u{1F7E1}';
  document.getElementById('vwap-sig').innerHTML='<span style="color:'+col+'">'+icon+' '+sig.action+'</span>';
  document.getElementById('vwap-note').textContent=sig.reasons.join(' \u2022 ');

  document.getElementById('vwap-upper').textContent=fii(upper);
  document.getElementById('vwap-val').textContent=fii(vwap);
  document.getElementById('vwap-lower').textContent=fii(lower);
  document.getElementById('mom-candles').textContent='--';
  document.getElementById('vwap-dot').style.left=vpos+'%';
  document.getElementById('mom-rpos').textContent='Position: '+Math.round(vpos)+'%';

  // RSI
  var rcol=rsi>=70?'var(--rd)':rsi>=60?'var(--gr)':rsi>=45?'var(--gd)':rsi>=30?'var(--rd)':'var(--gr)';
  var rlbl=rsi>=70?'OVERBOUGHT':rsi>=60?'BULLISH':rsi>=45?'NEUTRAL':rsi>=30?'BEARISH':'OVERSOLD';
  document.getElementById('rsi-val').textContent=rsi;
  document.getElementById('rsi-val').style.color=rcol;
  document.getElementById('rsi-lbl').textContent=rlbl;
  document.getElementById('rsi-lbl').style.color=rcol;
  document.getElementById('rsi-bar').style.width=rsi+'%';
  document.getElementById('rsi-bar').style.background=rcol;

  // MACD
  var mcol=hist>0?'var(--gr)':'var(--rd)';
  document.getElementById('macd-val').textContent=hist>0?'+'+hist:hist;
  document.getElementById('macd-val').style.color=mcol;
  document.getElementById('macd-lbl').textContent=hist>0?'BULL CROSS \u25B2':'BEAR CROSS \u25BC';
  document.getElementById('macd-lbl').style.color=mcol;

  // Patterns
  var pats=sig.patterns||[];
  var pd=document.getElementById('pat-list');
  if(!pats.length){
    pd.innerHTML='<div style="color:var(--mt);font-size:12px;padding:4px 0">No strong patterns \u2014 market in consolidation</div>';
  } else {
    pd.innerHTML=pats.map(function(p){
      return '<span class="pat '+(p.type==='bullish'?'bull':p.type==='bearish'?'bear':'neutral')+'">'+p.name+' \u2014 '+p.desc+'</span>';
    }).join('');
  }
  document.getElementById('t3-main').className='wrap';
}

// ── Fetch & Update ────────────────────────────────────────────
async function fetchLive(){
  try{
    var r=await fetch('/api/live?t='+Date.now());
    var d=await r.json();
    if(d.ok){
      var prev=D.spot; D.spot=d.spot; D.oi=d.oi; D.sig=d.sig;
      if(!initialized || prev!==d.spot){
        renderSignal(d);
        if(activeTab===2) renderOI(d.oi);
        if(activeTab===3) renderMom(d.sig);
        initialized=true;
      } else {
        // Just update spot & signal without full re-render
        document.getElementById('spot-val').textContent=fi(d.spot);
        document.getElementById('last-ts').textContent=d.ts;
        if(D.sig && D.sig.action!==d.sig.action){
          renderSignal(d); // Full render on signal change
          if(activeTab===2) renderOI(d.oi);
          if(activeTab===3) renderMom(d.sig);
        }
      }
    } else {
      if(!initialized){
        document.getElementById('t1-ld').className='hide';
        document.getElementById('t1-err').className='';
        document.getElementById('t1-em').textContent=d.error||'Connection error';
      }
    }
  }catch(e){
    if(!initialized){
      document.getElementById('t1-ld').className='hide';
      document.getElementById('t1-err').className='';
      document.getElementById('t1-em').textContent='Network error: '+e.message;
    }
  }
}

function forceRefresh(){
  initialized=false;
  document.getElementById('t1-main').className='hide';
  document.getElementById('t1-ld').className='loading';
  document.getElementById('t1-err').className='hide';
  fetchLive();
}

// Tab switch also triggers render with cached data
var origShowTab=showTab;
showTab=function(n){
  origShowTab(n);
  if(n===2 && D.oi) renderOI(D.oi);
  if(n===3 && D.sig) renderMom(D.sig);
};

// OI age counter
setInterval(function(){
  oiAge++;
  var el=document.getElementById('oi-age');
  if(el) el.textContent='Last OI update: '+oiAge+'s ago';
}, 1000);

// Main polling loop — every 3 seconds
fetchLive();
setInterval(fetchLive, 3000);
</script>
</body></html>"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
