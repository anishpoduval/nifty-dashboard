import os, time, pyotp, requests, math, threading
from flask import Flask, jsonify, render_template_string
from datetime import datetime, date, timedelta
from SmartApi import SmartConnect

app = Flask(__name__)

API_KEY     = "PRv269tC"
CLIENT_CODE = "A61831553"
ANGEL_PIN   = "8547"
TOTP_SECRET = "XA5CSSZRIMAHEQRJAGJFCJ5MLE"

_c = {
    "obj":None,"obj_ts":0,"jwt":None,
    "spot":0,"spot_ts":0,
    "oi":None,"oi_ts":0,
    "candles":[],"candles_ts":0,
    "sig":None,"errors":[]
}

# ── Single auth function ───────────────────────────────────────
def get_obj():
    if _c["obj"] and time.time()-_c["obj_ts"] < 2700:
        return _c["obj"], _c["jwt"]
    totp = pyotp.TOTP(TOTP_SECRET).now()
    obj  = SmartConnect(api_key=API_KEY)
    data = obj.generateSession(CLIENT_CODE, ANGEL_PIN, totp)
    if not data.get("status"):
        raise Exception("Login failed: " + str(data.get("message","")) + " | " + str(data))
    _c["obj"] = obj
    _c["jwt"] = data["data"]["jwtToken"]
    _c["obj_ts"] = time.time()
    return obj, _c["jwt"]

# ── REST headers ──────────────────────────────────────────────
def rest_headers(jwt):
    return {
        "Authorization":   "Bearer " + jwt,
        "Content-Type":    "application/json",
        "Accept":          "application/json",
        "X-UserType":      "USER",
        "X-SourceID":      "WEB",
        "X-ClientLocalIP": "127.0.0.1",
        "X-ClientPublicIP":"106.193.147.98",
        "X-MACAddress":    "fe80::216e:6507:4b90:3719",
        "X-PrivateKey":    API_KEY,
    }

# ── SPOT via REST (ltpData) ────────────────────────────────────
def fetch_spot_rest(obj, jwt):
    """Try multiple methods to get NIFTY spot"""
    # Method 1: ltpData via SmartConnect object
    for exch, sym, token in [("NSE","Nifty 50","99926000"),("NSE","NIFTY","26000"),("NSE","Nifty 50","26000")]:
        try:
            r = obj.ltpData(exch, sym, token)
            if r.get("status") and r.get("data",{}).get("ltp"):
                return float(r["data"]["ltp"])
        except: pass

    # Method 2: REST quote API
    for token in ["99926000","26000"]:
        try:
            r = requests.post(
                "https://apiconnect.angelbroking.com/rest/secure/angelbroking/market/v1/quote/",
                json={"mode":"LTP","exchangeTokens":{"NSE":[token]}},
                headers=rest_headers(jwt), timeout=8)
            d = r.json()
            fetched = d.get("data",{}).get("fetched",[])
            if fetched and fetched[0].get("ltp"):
                return float(fetched[0]["ltp"])
        except: pass

    # Method 3: Use last candle close as fallback
    if _c["candles"]:
        return float(_c["candles"][-1][4])

    return 0

# ── OPTION CHAIN via REST only (no obj.optionChain!) ──────────
def fetch_oi_rest(jwt, spot):
    expiry = next_expiry()
    atm = round(spot / 50) * 50
    hdrs = rest_headers(jwt)

    # Try multiple body formats
    bodies = [
        {"name":"NIFTY", "expirydate":expiry},
        {"name":"NIFTY", "expirydate":expiry, "strike":str(atm)},
        {"name":"NIFTY", "expirydate":expiry, "exchange":"NFO"},
    ]
    for body in bodies:
        try:
            r = requests.post(
                "https://apiconnect.angelbroking.com/rest/secure/angelbroking/marketData/v1/optionChain",
                json=body, headers=hdrs, timeout=5)
            d = r.json()
            if d.get("data") and isinstance(d["data"], list) and len(d["data"]) > 3:
                return d["data"], expiry
        except: pass

    return None, expiry

# ── CANDLES via SmartConnect ───────────────────────────────────
def fetch_candles(obj):
    td  = date.today()
    fd  = td.strftime("%Y-%m-%d") + " 09:15"
    tod = td.strftime("%Y-%m-%d") + " 15:30"
    for token in ["99926000", "26000"]:
        try:
            d = obj.getCandleData({
                "exchange":"NSE","symboltoken":token,
                "interval":"FIVE_MINUTE","fromdate":fd,"todate":tod
            })
            if d.get("status") and d.get("data") and len(d["data"]) > 0:
                return d["data"]
        except: pass
    return []

def next_expiry():
    today = date.today()
    days  = (3 - today.weekday()) % 7
    if days == 0 and datetime.now().hour >= 15:
        days = 7
    return (today + timedelta(days=days)).strftime("%d%b%Y").upper()

# ── Parse OI chain ────────────────────────────────────────────
def parse_oi(chain, spot, expiry):
    atm = round(spot / 50) * 50
    live = False; co = {}; po = {}
    if chain:
        live = True
        for row in chain:
            sp = row.get("strikePrice") or row.get("strike", 0)
            try: sp = int(float(sp))
            except: continue
            ce = row.get("CE") or {}
            pe = row.get("PE") or {}
            if isinstance(ce, dict):
                coi = ce.get("openInterest",0) or ce.get("oi",0)
            else:
                coi = row.get("CE_openInterest",0) or row.get("callOI",0)
            if isinstance(pe, dict):
                poi = pe.get("openInterest",0) or pe.get("oi",0)
            else:
                poi = row.get("PE_openInterest",0) or row.get("putOI",0)
            if coi: co[sp] = int(coi)
            if poi: po[sp] = int(poi)

    tc = sum(co.values()); tp = sum(po.values())
    pcr = round(tp/tc, 2) if tc > 0 else 0
    calls = sorted([(s,o) for s,o in co.items() if s > spot],  key=lambda x:-x[1])[:6]
    puts  = sorted([(s,o) for s,o in po.items() if s <= spot], key=lambda x:-x[1])[:6]
    cw1 = calls[0][0] if calls else int(round(spot/500+.5)*500)
    pw1 = puts[0][0]  if puts  else int(round(spot/500-.5)*500)
    cw2 = calls[1][0] if len(calls)>1 else cw1+500
    pw2 = puts[1][0]  if len(puts)>1  else pw1-500
    mp  = atm
    if co and po:
        try:
            sks = sorted(set(list(co)+list(po))); best = float("inf")
            for s in sks:
                loss = sum(max(0,s-k)*v for k,v in co.items()) + sum(max(0,k-s)*v for k,v in po.items())
                if loss < best: best, mp = loss, s
        except: pass
    straddle = int(round(spot*0.20*(5/252)**0.5/50)*50)
    return {
        "live":live,"expiry":expiry,"pcr":pcr,"atm":atm,"mp":mp,"straddle":straddle,
        "range_low":round(spot-straddle),"range_high":round(spot+straddle),
        "cw1":cw1,"cw2":cw2,"pw1":pw1,"pw2":pw2,
        "c_cr":round(tc/10000000,2),"p_cr":round(tp/10000000,2),
        "calls":[{"s":s,"oi":round(o/100000,1)} for s,o in calls[:5]],
        "puts" :[{"s":s,"oi":round(o/100000,1)} for s,o in puts[:5]],
        "total_call":tc,"total_put":tp,"chain_rows":len(chain) if chain else 0
    }

# ── Indicators ────────────────────────────────────────────────
def vwap_calc(candles):
    if not candles: return 0,0,0
    cumtpv=cumv=0; tps=[]
    for c in candles:
        h,l,cl=c[2],c[3],c[4]; v=c[5] if len(c)>5 and c[5] else 1
        tp=(h+l+cl)/3; cumtpv+=tp*v; cumv+=v; tps.append(tp)
    if cumv==0: return 0,0,0
    vwap=cumtpv/cumv; sd=math.sqrt(sum((t-vwap)**2 for t in tps)/len(tps))
    return round(vwap,2), round(vwap+sd,2), round(vwap-sd,2)

def rsi_calc(closes, p=14):
    if len(closes)<p+1: return 50
    diffs=[closes[i]-closes[i-1] for i in range(1,len(closes))]
    ag=sum(max(d,0) for d in diffs[-p:])/p
    al=sum(max(-d,0) for d in diffs[-p:])/p
    return 50 if al==0 else round(100-(100/(1+ag/al)),1)

def macd_calc(closes):
    def ema(data,n):
        k=2/(n+1); e=[data[0]]
        for p in data[1:]: e.append(p*k+e[-1]*(1-k))
        return e
    if len(closes)<26: return 0,0,0
    e12=ema(closes,12); e26=ema(closes,26)
    m=[a-b for a,b in zip(e12,e26)]
    sig=ema(m[-26:],9) if len(m)>=9 else [m[-1]]
    return round(m[-1],2), round(sig[-1],2), round(m[-1]-sig[-1],2)

def patterns_calc(candles):
    pats=[]
    for i in range(max(1,len(candles)-4),len(candles)):
        c=candles[i]; o,h,l,cl=c[1],c[2],c[3],c[4]
        body=abs(cl-o); rng=h-l or 0.001
        ush=h-max(cl,o); lsh=min(cl,o)-l; bull=cl>=o
        if body<0.1*rng:
            pats.append({"name":"Doji","type":"neutral","desc":"Indecision"})
        elif lsh>2*body and ush<body and bull:
            pats.append({"name":"Hammer","type":"bullish","desc":"Bullish reversal"})
        elif ush>2*body and lsh<body:
            pats.append({"name":"Shooting Star","type":"bearish","desc":"Bearish reversal"})
        if i>0:
            p=candles[i-1]; po2,pcl=p[1],p[4]
            if bull and pcl<po2 and cl>po2 and o<pcl:
                pats.append({"name":"Bull Engulfing","type":"bullish","desc":"Strong reversal up"})
            elif not bull and pcl>po2 and cl<po2 and o>pcl:
                pats.append({"name":"Bear Engulfing","type":"bearish","desc":"Strong reversal down"})
    return pats[-2:]

def compute_signal(spot, oi, candles):
    if not oi:
        oi={"pcr":0,"cw1":round(spot/500+.5)*500 if spot else 0,
            "pw1":round(spot/500-.5)*500 if spot else 0,"live":False,"expiry":"","atm":0,
            "mp":0,"straddle":0,"range_low":0,"range_high":0,"cw2":0,"pw2":0,
            "c_cr":0,"p_cr":0,"calls":[],"puts":[],"total_call":0,"total_put":0,"chain_rows":0}
    atm=round(spot/50)*50 if spot else 0
    score=0; reasons=[]; pcr=oi["pcr"]
    if pcr > 0:
        if pcr>=1.2:   score+=3; reasons.append("PCR "+str(pcr)+" bullish")
        elif pcr>=1.0: score+=1; reasons.append("PCR "+str(pcr)+" mild bull")
        elif pcr<=0.7: score-=3; reasons.append("PCR "+str(pcr)+" bearish")
        elif pcr<=0.9: score-=1; reasons.append("PCR "+str(pcr)+" mild bear")
        else:          reasons.append("PCR "+str(pcr)+" neutral")
    else:
        reasons.append("PCR unavailable")
    vwap=upper=lower=0; rsi=50; hist=0; pats=[]
    if candles and len(candles) >= 5:
        closes=[c[4] for c in candles]
        vwap,upper,lower=vwap_calc(candles)
        rsi=rsi_calc(closes); _,_,hist=macd_calc(closes)
        pats=patterns_calc(candles)
        if vwap > 0:
            if spot > vwap: score+=2; reasons.append("Above VWAP "+str(int(vwap)))
            else:            score-=2; reasons.append("Below VWAP "+str(int(vwap)))
        if rsi>=60:   score+=2; reasons.append("RSI "+str(rsi)+" bullish")
        elif rsi<=40: score-=2; reasons.append("RSI "+str(rsi)+" bearish")
        if hist>0: score+=1; reasons.append("MACD bull cross")
        elif hist<0: score-=1; reasons.append("MACD bear cross")
        for p in pats:
            if p["type"]=="bullish": score+=1
            elif p["type"]=="bearish": score-=1
    else:
        reasons.append("No candles yet")
    cw1=oi.get("cw1",0); pw1=oi.get("pw1",0)
    if cw1 and abs(spot-cw1)<50: score-=1
    if pw1 and abs(spot-pw1)<50: score+=1
    strength=min(abs(score)/9*100,100)
    if score>=4:    action="BUY CALL"; color="green"; strike=str(atm+50)+" CE"; hedge=str(atm-200)+" PE"; t1,t2=cw1,cw1+100; conf="HIGH" if score>=6 else "MEDIUM"
    elif score<=-4: action="BUY PUT";  color="red";   strike=str(atm-50)+" PE"; hedge=str(atm+200)+" CE"; t1,t2=pw1,pw1-100; conf="HIGH" if score<=-6 else "MEDIUM"
    elif score>=2:  action="LEAN CALL";color="green"; strike=str(atm+50)+" CE"; hedge=str(atm-200)+" PE"; t1,t2=cw1,cw1+50;  conf="LOW"
    elif score<=-2: action="LEAN PUT"; color="red";   strike=str(atm-50)+" PE"; hedge=str(atm+200)+" CE"; t1,t2=pw1,pw1-50;  conf="LOW"
    else:           action="WAIT";     color="gold";  strike="No setup yet";    hedge="--"; t1=cw1; t2=pw1; conf="--"
    return {"action":action,"color":color,"strike":strike,"hedge":hedge,"t1":t1,"t2":t2,
            "conf":conf,"score":score,"strength":round(strength),"reasons":reasons[:5],
            "vwap":vwap,"upper":upper,"lower":lower,"rsi":rsi,"hist":hist,
            "macd_bull":hist>0,"patterns":pats,"candles_count":len(candles)}

# ── Background thread: SPOT every 1s ─────────────────────────
def bg_spot():
    while True:
        try:
            obj, jwt = get_obj()
            s = fetch_spot_rest(obj, jwt)
            if s > 0:
                _c["spot"] = s
            if _c["spot"] > 0:
                _c["spot_ts"] = time.time()
        except Exception as e:
            _c["errors"].append("spot:"+str(e)[:60])
            _c["errors"] = _c["errors"][-5:]
        time.sleep(1)

# ── Background thread: OI + Candles every 60s ────────────────
def bg_heavy():
    time.sleep(8)   # let spot warm up first
    while True:
        try:
            obj, jwt = get_obj()
            spot = _c["spot"] or 23650

            # Candles (works!)
            candles = fetch_candles(obj)
            if candles:
                _c["candles"] = candles
                _c["candles_ts"] = time.time()
                # Use last candle close as spot fallback
                if _c["spot"] == 0:
                    _c["spot"] = float(candles[-1][4])
                    _c["spot_ts"] = time.time()

            # OI chain via REST (no obj.optionChain)
            chain, expiry = fetch_oi_rest(jwt, spot)
            _c["oi"] = parse_oi(chain, spot, expiry)
            _c["oi_ts"] = time.time()

            # Recompute signal
            _c["sig"] = compute_signal(_c["spot"], _c["oi"], _c["candles"])

        except Exception as e:
            _c["errors"].append("heavy:"+str(e)[:80])
            _c["errors"] = _c["errors"][-5:]
        time.sleep(60)

threading.Thread(target=bg_spot,  daemon=True).start()
threading.Thread(target=bg_heavy, daemon=True).start()

# ── Routes ────────────────────────────────────────────────────
@app.route("/api/spot")
def api_spot():
    return jsonify({"spot":_c["spot"],"ts":datetime.now().strftime("%H:%M:%S")})

@app.route("/api/live")
def api_live():
    spot = _c["spot"]
    if not spot:
        return jsonify({"ok":False,"error":"Starting up — spot loading (takes ~5s)",
                        "ts":datetime.now().strftime("%H:%M:%S")})
    oi  = _c["oi"]  or parse_oi(None, spot, next_expiry())
    sig = _c["sig"] or compute_signal(spot, oi, _c["candles"])
    return jsonify({
        "ok":True,"spot":spot,"oi":oi,"sig":sig,
        "ts":datetime.now().strftime("%H:%M:%S"),
        "market_open": 9*60+15 <= (datetime.utcnow().hour*60+datetime.utcnow().minute+330)%1440 < 15*60+31,
        "spot_age": round(time.time()-_c["spot_ts"],1) if _c["spot_ts"] else 99,
        "oi_age":   round(time.time()-_c["oi_ts"],0)  if _c["oi_ts"]   else -1,
        "candles":  len(_c["candles"])
    })

@app.route("/api/debug")
def api_debug():
    result = {"timestamp": datetime.now().strftime("%d %b %Y %H:%M:%S")}
    result["cache"] = {
        "spot": _c["spot"],
        "spot_age": round(time.time()-_c["spot_ts"],1) if _c["spot_ts"] else "never",
        "oi_live": _c["oi"]["live"] if _c["oi"] else False,
        "oi_pcr": _c["oi"]["pcr"] if _c["oi"] else 0,
        "oi_rows": _c["oi"]["chain_rows"] if _c["oi"] else 0,
        "candles": len(_c["candles"]),
        "errors": _c["errors"]
    }
    try:
        obj, jwt = get_obj()
        result["login"] = "OK"
        expiry = next_expiry()
        result["expiry"] = expiry

        # Test spot
        try:
            s = fetch_spot_rest(obj, jwt)
            result["spot_test"] = s
        except Exception as e:
            result["spot_test_error"] = str(e)

        # Test OI via REST
        try:
            chain, exp = fetch_oi_rest(jwt, _c["spot"] or 23650)
            result["oi_test"] = {
                "chain_length": len(chain) if chain else 0,
                "first_row": str(chain[0])[:200] if chain else "empty",
            }
        except Exception as e:
            result["oi_test_error"] = str(e)

        # Test candles
        try:
            c = fetch_candles(obj)
            result["candle_test"] = {
                "count": len(c),
                "last": str(c[-1])[:100] if c else "empty"
            }
        except Exception as e:
            result["candle_test_error"] = str(e)

    except Exception as e:
        result["login"] = "FAILED: " + str(e)
    return jsonify(result)

@app.route("/")
def index():
    return render_template_string(HTML)

HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>NIFTY Live</title>
<style>
:root{--bg:#070b12;--s1:#0c1220;--s2:#111a2a;--s3:#162235;--ln:#1d2f45;
  --acc:#00c8ff;--gr:#00e87a;--rd:#ff2d55;--gd:#ffd60a;
  --mt:#334d66;--tx:#8ab4cc;--wh:#eef6ff}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--tx);font-family:-apple-system,'SF Pro Text',sans-serif;min-height:100vh}
.hdr{background:var(--s1);border-bottom:1px solid var(--ln);padding:12px 16px;position:sticky;top:0;z-index:50}
.spot-num{font-size:30px;font-weight:800;color:var(--wh);letter-spacing:-1px;font-variant-numeric:tabular-nums;transition:color .15s}
.chg-badge{font-size:12px;font-weight:600;padding:3px 8px;border-radius:20px;margin-left:8px;transition:all .2s}
.live-pill{display:inline-flex;align-items:center;gap:4px;font-size:10px;font-weight:700;letter-spacing:1px;padding:3px 8px;border-radius:20px;background:rgba(0,232,122,.1);color:var(--gr);border:1px solid rgba(0,232,122,.25)}
.pulse{width:6px;height:6px;border-radius:50%;background:var(--gr);animation:pulse 1.5s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.tabs{display:flex;background:var(--s1);border-bottom:1px solid var(--ln)}
.tab{flex:1;padding:13px 0;border:none;background:transparent;color:var(--mt);font-size:10px;letter-spacing:1.5px;font-weight:700;cursor:pointer;border-bottom:2px solid transparent;transition:all .2s;text-transform:uppercase}
.tab.on{color:var(--acc);border-bottom-color:var(--acc)}
.wrap{padding:14px 14px 20px}
.card{background:var(--s1);border-radius:16px;padding:16px;border:1px solid var(--ln);margin-bottom:12px}
.ctitle{font-size:9px;color:var(--mt);letter-spacing:3px;font-weight:700;margin-bottom:14px;text-transform:uppercase}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.g3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
.g4{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.mt{background:var(--s2);border-radius:12px;padding:12px}
.ml{font-size:8px;color:var(--mt);letter-spacing:1px;margin-bottom:5px;text-transform:uppercase}
.mv{font-size:14px;font-weight:700;color:var(--wh)}
.mv.sm{font-size:12px}
.hero{border-radius:20px;padding:22px 18px;border:2px solid rgba(255,214,10,.3);background:rgba(255,214,10,.05);margin:14px 14px 0;transition:all .4s}
.hero.bull{border-color:rgba(0,232,122,.4);background:rgba(0,232,122,.07)}
.hero.bear{border-color:rgba(255,45,85,.4);background:rgba(255,45,85,.07)}
.hero-lbl{font-size:10px;letter-spacing:2px;font-weight:700;color:var(--mt);margin-bottom:8px}
.hero-act{font-size:38px;font-weight:900;letter-spacing:-1px;line-height:1}
.hero-str{font-size:21px;font-weight:700;margin-top:8px}
.conf-chip{display:inline-block;font-size:10px;font-weight:700;letter-spacing:1.5px;padding:4px 12px;border-radius:20px;margin-top:10px}
.ch-hi{background:rgba(0,232,122,.15);color:var(--gr);border:1px solid rgba(0,232,122,.3)}
.ch-md{background:rgba(255,214,10,.15);color:var(--gd);border:1px solid rgba(255,214,10,.3)}
.ch-lo{background:rgba(255,255,255,.05);color:var(--mt);border:1px solid var(--ln)}
.str-track{height:4px;background:var(--ln);border-radius:2px;margin-top:14px;overflow:hidden}
.str-fill{height:100%;border-radius:2px;transition:width .6s}
.reasons{margin-top:12px;display:flex;flex-wrap:wrap;gap:6px}
.reason-chip{font-size:10px;padding:4px 10px;border-radius:20px;background:rgba(255,255,255,.05);color:var(--tx);border:1px solid var(--ln)}
.pcr-num{font-size:48px;font-weight:900;line-height:1}
.pcr-track{height:8px;background:var(--ln);border-radius:4px;overflow:hidden;margin:10px 0 4px}
.pcr-fill{height:100%;background:linear-gradient(90deg,var(--rd),var(--gd) 50%,var(--gr));transition:width 1s}
.oi-item{margin-bottom:12px}
.oi-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:5px}
.oi-track{height:6px;background:var(--ln);border-radius:3px;overflow:hidden}
.oi-fill{height:100%;border-radius:3px;transition:width .8s}
.lvl{background:var(--s2);border-radius:12px;padding:14px;font-size:13px;line-height:2.8}
.vtrack{height:10px;background:var(--ln);border-radius:5px;position:relative;margin:12px 0}
.vdot{position:absolute;top:-3px;width:16px;height:16px;border-radius:50%;background:var(--acc);border:3px solid var(--bg);transform:translateX(-50%);transition:left .6s;box-shadow:0 0 8px var(--acc)}
.vmid{position:absolute;top:0;left:50%;width:2px;height:10px;background:var(--gd);opacity:.5}
.ind-big{font-size:42px;font-weight:900;line-height:1}
.ind-track{height:6px;background:var(--ln);border-radius:3px;overflow:hidden;margin:8px 0 3px}
.ind-fill{height:100%;border-radius:3px;transition:width .8s}
.pat{display:inline-flex;align-items:center;gap:6px;padding:8px 12px;border-radius:10px;font-size:12px;font-weight:600;margin:4px;border:1px solid var(--ln)}
.pat.bull{background:rgba(0,232,122,.08);color:var(--gr);border-color:rgba(0,232,122,.25)}
.pat.bear{background:rgba(255,45,85,.08);color:var(--rd);border-color:rgba(255,45,85,.25)}
.ld{text-align:center;padding:50px 20px;color:var(--mt)}
.ldot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--acc);margin:0 3px;animation:la .8s infinite}
.ldot:nth-child(2){animation-delay:.15s}.ldot:nth-child(3){animation-delay:.3s}
@keyframes la{0%,80%,100%{transform:scale(0);opacity:.3}40%{transform:scale(1);opacity:1}}
.err-box{margin:14px;padding:16px;background:rgba(255,45,85,.07);border:1px solid rgba(255,45,85,.25);border-radius:14px}
.btn{background:rgba(0,200,255,.1);border:1px solid rgba(0,200,255,.25);border-radius:10px;padding:9px 16px;color:var(--acc);font-size:12px;font-weight:700;cursor:pointer;margin-top:10px}
.warn-box{background:rgba(255,214,10,.05);border:1px solid rgba(255,214,10,.2);border-radius:10px;padding:10px 12px;font-size:11px;color:var(--gd);margin-bottom:12px}
.info-box{background:rgba(0,200,255,.04);border:1px solid rgba(0,200,255,.12);border-radius:14px;padding:14px;font-size:11px;color:var(--mt);line-height:2.1;margin-bottom:4px}
.gr{color:var(--gr)}.rd{color:var(--rd)}.gd{color:var(--gd)}.ac{color:var(--acc)}
.hide{display:none!important}
</style></head><body>
<div class="hdr">
  <div style="display:flex;justify-content:space-between;align-items:flex-start">
    <div>
      <div class="live-pill" style="margin-bottom:5px"><span class="pulse"></span><span id="live-txt">CONNECTING</span></div>
      <div><span class="spot-num" id="spot-num">--</span><span class="chg-badge" id="chg-badge" style="background:rgba(255,255,255,.05);color:var(--mt)">--</span></div>
      <div style="font-size:10px;color:var(--mt);margin-top:4px" id="hdr-sub">--</div>
    </div>
    <div style="text-align:right">
      <div style="font-size:9px;color:var(--mt)" id="upd-ts">--</div>
      <div style="font-size:9px;color:var(--mt)" id="data-age">--</div>
      <a href="/api/debug" target="_blank" style="font-size:9px;color:var(--mt);text-decoration:none">/api/debug</a>
    </div>
  </div>
</div>
<div class="tabs">
  <button class="tab on" id="tb1" onclick="goTab(1)">&#127919; SIGNAL</button>
  <button class="tab" id="tb2" onclick="goTab(2)">&#128200; OI DATA</button>
  <button class="tab" id="tb3" onclick="goTab(3)">&#9889; MOMENTUM</button>
</div>
<!-- TAB 1 -->
<div id="pg1">
  <div class="ld" id="p1ld"><span class="ldot"></span><span class="ldot"></span><span class="ldot"></span><div style="margin-top:14px;font-size:12px">Starting up ~5 seconds...</div></div>
  <div id="p1err" class="hide"><div class="err-box"><div class="rd" id="p1em"></div><div style="color:var(--mt);font-size:11px;margin-top:6px">Will retry automatically every 5s</div></div></div>
  <div id="p1main" class="hide">
    <div class="hero" id="sig-hero"><div class="hero-lbl">NIFTY SIGNAL &bull; <span id="s-exp">--</span></div><div class="hero-act" id="s-act">--</div><div class="hero-str" id="s-str">--</div><div><span class="conf-chip ch-lo" id="s-conf">--</span><span style="font-size:11px;color:var(--mt);margin-left:10px" id="s-score">--</span></div><div class="str-track"><div class="str-fill" id="s-sfill" style="width:0"></div></div><div class="reasons" id="s-reasons"></div></div>
    <div class="wrap">
      <div class="card"><div class="ctitle">TRADE DETAILS</div><div class="g2" style="margin-bottom:10px"><div class="mt"><div class="ml">BUY STRIKE</div><div class="mv sm" id="td-str" style="color:var(--gr)">--</div></div><div class="mt"><div class="ml">HEDGE</div><div class="mv sm gd" id="td-hdg">--</div></div></div><div class="g2"><div class="mt"><div class="ml">TARGET 1</div><div class="mv sm ac" id="td-t1">--</div></div><div class="mt"><div class="ml">TARGET 2</div><div class="mv sm ac" id="td-t2">--</div></div><div class="mt"><div class="ml">STOP LOSS</div><div class="mv sm rd">40% prem</div></div><div class="mt"><div class="ml">LOT SIZE</div><div class="mv sm" style="color:var(--wh)">75</div></div></div></div>
      <div class="card"><div class="ctitle">KEY LEVELS</div><div class="lvl" id="kl-struct">--</div></div>
      <div class="info-box"><span style="color:var(--gd);font-weight:700">RULES</span> &bull; Wait 9:30 candle &bull; 1-2 lots &bull; SL 40% &bull; Always hedge &bull; Exit before 3 PM</div>
    </div>
  </div>
</div>
<!-- TAB 2 -->
<div id="pg2" class="hide">
  <div class="ld" id="p2ld"><span class="ldot"></span><span class="ldot"></span><span class="ldot"></span></div>
  <div id="p2main" class="hide"><div class="wrap">
    <div id="oi-warn" class="warn-box hide"></div>
    <div class="card"><div class="ctitle">PUT / CALL RATIO</div><div style="display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:10px"><span class="pcr-num" id="pcr-num">--</span><span style="font-size:15px;font-weight:700" id="pcr-bias">--</span></div><div class="pcr-track"><div class="pcr-fill" id="pcr-bar" style="width:50%"></div></div><div style="display:flex;justify-content:space-between;font-size:9px;color:var(--mt)"><span>0.5 BEAR</span><span>1.0</span><span>1.5+ BULL</span></div><div class="g4" style="margin-top:12px"><div class="mt"><div class="ml">CALL OI</div><div class="mv sm rd" id="p2-co">--</div></div><div class="mt"><div class="ml">PUT OI</div><div class="mv sm gr" id="p2-po">--</div></div><div class="mt"><div class="ml">MAX PAIN</div><div class="mv sm gd" id="p2-mp">--</div></div><div class="mt"><div class="ml">EXPIRY</div><div class="mv sm ac" style="font-size:11px" id="p2-exp">--</div></div></div></div>
    <div class="card"><div class="ctitle">STRADDLE &amp; RANGE</div><div class="g3"><div class="mt"><div class="ml">STRADDLE</div><div class="mv ac" id="p2-str">--</div></div><div class="mt"><div class="ml">RANGE LOW</div><div class="mv rd" id="p2-rl">--</div></div><div class="mt"><div class="ml">RANGE HIGH</div><div class="mv gr" id="p2-rh">--</div></div></div></div>
    <div class="card"><div class="ctitle rd">CALL WALLS &bull; RESISTANCE</div><div id="p2-cw">--</div></div>
    <div class="card"><div class="ctitle gr">PUT WALLS &bull; SUPPORT</div><div id="p2-pw">--</div></div>
    <div style="text-align:center;font-size:10px;color:var(--mt);padding:4px 0 8px" id="oi-age-txt">OI refreshes every 60s in background</div>
  </div></div>
</div>
<!-- TAB 3 -->
<div id="pg3" class="hide">
  <div id="p3ld" class="ld"><span class="ldot"></span><span class="ldot"></span><span class="ldot"></span><div style="margin-top:14px;font-size:12px" id="p3ld-txt">Loading candle data...</div></div>
  <div id="p3main" class="hide">
    <div class="hero" id="vwap-hero" style="margin-top:14px"><div class="hero-lbl">VWAP SCALP SIGNAL &bull; 5-MIN</div><div style="font-size:28px;font-weight:900" id="vs-act">--</div><div style="font-size:13px;margin-top:8px;color:var(--tx);line-height:1.6" id="vs-note">--</div></div>
    <div class="wrap">
      <div class="card"><div class="ctitle">VWAP BANDS &bull; <span id="p3-cnt" style="color:var(--acc)"></span></div><div class="g3"><div class="mt"><div class="ml">UPPER</div><div class="mv gr" id="p3-upper">--</div></div><div class="mt"><div class="ml">VWAP</div><div class="mv ac" id="p3-vwap">--</div></div><div class="mt"><div class="ml">LOWER</div><div class="mv rd" id="p3-lower">--</div></div></div><div style="margin-top:14px"><div style="display:flex;justify-content:space-between;font-size:9px;color:var(--mt);margin-bottom:6px"><span id="p3-lo">Lower</span><span class="ac" id="p3-rpos">--%</span><span id="p3-hi">Upper</span></div><div class="vtrack"><div class="vmid"></div><div class="vdot" id="p3-dot" style="left:50%"></div></div><div style="display:flex;justify-content:space-between;font-size:9px;color:var(--mt);margin-top:3px"><span>Bear zone</span><span>Bull zone</span></div></div></div>
      <div class="card"><div class="ctitle">INDICATORS &bull; 5-MIN</div><div class="g2"><div style="background:var(--s2);border-radius:12px;padding:14px"><div class="ml">RSI (14)</div><div class="ind-big" id="p3-rsi">--</div><div style="font-size:12px;font-weight:700;margin-top:5px" id="p3-rlbl">--</div><div class="ind-track"><div class="ind-fill" id="p3-rbar" style="width:50%"></div></div></div><div style="background:var(--s2);border-radius:12px;padding:14px"><div class="ml">MACD HIST</div><div class="ind-big" id="p3-macd">--</div><div style="font-size:12px;font-weight:700;margin-top:5px" id="p3-mlbl">--</div><div style="margin-top:10px;font-size:10px;color:var(--mt);line-height:1.8">+ve = Bull<br>-ve = Bear</div></div></div></div>
      <div class="card"><div class="ctitle">CANDLESTICK PATTERNS</div><div id="p3-pats">Scanning...</div></div>
      <div class="info-box"><span style="color:var(--acc);font-weight:700">SCALP</span> &bull; Above VWAP+RSI&gt;55+MACD+ = CE &bull; Below VWAP+RSI&lt;45+MACD- = PE &bull; Exit at opposite band</div>
    </div>
  </div>
</div>
<script>
var DATA={spot:0,prev:0,oi:null,sig:null,ready:false};
function eid(id){return document.getElementById(id);}
function fi(n,d){d=d==null?2:d;return typeof n==="number"?n.toLocaleString("en-IN",{minimumFractionDigits:d,maximumFractionDigits:d}):String(n||"--");}
function fii(n){return typeof n==="number"?n.toLocaleString("en-IN"):String(n||"--");}
function inr(n){return typeof n==="number"?"\u20B9"+n.toLocaleString("en-IN"):String(n||"--");}
function gc(c){return c==="green"?"var(--gr)":c==="red"?"var(--rd)":"var(--gd)";}
function show(id){var e=eid(id);if(e)e.classList.remove("hide");}
function hide(id){var e=eid(id);if(e)e.classList.add("hide");}
function goTab(n){[1,2,3].forEach(function(i){var pg=eid("pg"+i);var tb=eid("tb"+i);if(pg)pg.className=i===n?"":"hide";if(tb)tb.className="tab"+(i===n?" on":"");});}

function renderAll(d){
  if(!d.ok){
    if(!DATA.ready){hide("p1ld");show("p1err");eid("p1em").textContent=d.error||"Error";}
    return;
  }
  var sig=d.sig;var oi=d.oi;var col=gc(sig.color);var spot=d.spot;
  var chg=spot-(DATA.prev||spot);if(DATA.prev===0)chg=0;
  // Header
  eid("spot-num").textContent=fi(spot);
  eid("spot-num").style.color=chg>0?"var(--gr)":chg<0?"var(--rd)":"var(--wh)";
  if(chg!==0){var pct=chg/(DATA.prev||spot)*100;var cb=eid("chg-badge");cb.textContent=(chg>=0?"+":"")+fi(chg,2)+" ("+(pct>=0?"+":"")+fi(pct,2)+"%)";cb.style.background=chg>0?"rgba(0,232,122,.12)":"rgba(255,45,85,.12)";cb.style.color=chg>0?"var(--gr)":"var(--rd)";}
  eid("hdr-sub").textContent="ATM "+fii(oi.atm)+" \u2022 PCR "+(oi.pcr>0?oi.pcr:"N/A")+(oi.live?" \u2022 "+oi.expiry:" \u2022 OI loading...");
  eid("upd-ts").textContent=d.ts;
  eid("data-age").textContent="Spot:"+d.spot_age+"s"+(d.oi_age>=0?" OI:"+d.oi_age+"s":"")+" Candles:"+d.candles;
  eid("live-txt").textContent=d.market_open?"LIVE":"CLOSED";
  // --- Tab 1: Signal ---
  var hero=eid("sig-hero");hero.className="hero "+(sig.color==="green"?"bull":sig.color==="red"?"bear":"");
  eid("s-exp").textContent=oi.expiry;
  var ae=eid("s-act");ae.textContent=sig.action;ae.style.color=col;
  var se=eid("s-str");se.textContent=sig.action==="WAIT"?"No setup \u2014 wait for alignment":sig.strike;se.style.color=sig.action==="WAIT"?"var(--mt)":col;
  var ce=eid("s-conf");ce.textContent=sig.conf==="HIGH"?"HIGH CONFIDENCE":sig.conf==="MEDIUM"?"MEDIUM":sig.conf==="LOW"?"LOW \u2014 wait more":"WAIT \u2014 NO SETUP";ce.className="conf-chip "+(sig.conf==="HIGH"?"ch-hi":sig.conf==="MEDIUM"?"ch-md":"ch-lo");
  eid("s-score").textContent="Score "+sig.score+"/9";
  eid("s-sfill").style.width=sig.strength+"%";eid("s-sfill").style.background=col;
  eid("s-reasons").innerHTML=sig.reasons.map(function(r){return "<span class='reason-chip'>"+r+"</span>";}).join("");
  eid("td-str").textContent=sig.strike;eid("td-str").style.color=col;
  eid("td-hdg").textContent=sig.hedge;eid("td-t1").textContent=fii(sig.t1);eid("td-t2").textContent=fii(sig.t2);
  eid("kl-struct").innerHTML="<div class='rd'>\u25CF "+fii(oi.cw1)+" CE \u2190 CEILING</div>"
    +"<div style='color:var(--ln)'>\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500</div>"
    +"<div class='ac' style='font-size:16px;font-weight:800'>\u25C6 "+fi(spot)+" ATM "+fii(oi.atm)+"</div>"
    +"<div style='color:var(--ln)'>\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500</div>"
    +"<div class='gr'>\u25CF "+fii(oi.pw1)+" PE \u2190 FLOOR</div>"
    +"<div class='gd' style='font-size:12px;margin-top:4px'>Max Pain: "+fii(oi.mp)+" \u2022 Straddle: "+inr(oi.straddle)+"</div>";
  hide("p1ld");hide("p1err");show("p1main");
  // --- Tab 2: OI ---
  hide("p2ld");
  if(!oi.live){show("oi-warn");eid("oi-warn").textContent="\u26A0 OI chain loading \u2014 levels estimated from spot. Live in ~60s after startup.";}else{hide("oi-warn");}
  var pcol=oi.pcr>=1?"var(--gr)":oi.pcr>=0.9?"var(--gd)":oi.pcr>0?"var(--rd)":"var(--mt)";
  var bias=oi.pcr===0?"LOADING...":oi.pcr>=1.4?"STRONGLY BULLISH":oi.pcr>=1.2?"BULLISH":oi.pcr>=1.0?"MILDLY BULLISH":oi.pcr>=0.9?"NEUTRAL":oi.pcr>=0.75?"MILDLY BEARISH":"BEARISH";
  eid("pcr-num").textContent=oi.pcr>0?oi.pcr:"--";eid("pcr-num").style.color=pcol;
  eid("pcr-bias").textContent=bias;eid("pcr-bias").style.color=pcol;
  eid("pcr-bar").style.width=oi.pcr>0?Math.min(oi.pcr/2*100,100)+"%":"0%";
  eid("p2-co").textContent=oi.c_cr>0?oi.c_cr+"Cr":"--";eid("p2-po").textContent=oi.p_cr>0?oi.p_cr+"Cr":"--";
  eid("p2-mp").textContent=fii(oi.mp);eid("p2-exp").textContent=oi.expiry;
  eid("p2-str").innerHTML=inr(oi.straddle);eid("p2-rl").textContent=fii(oi.range_low);eid("p2-rh").textContent=fii(oi.range_high);
  function oiW(walls,isCall){
    if(!walls||!walls.length||!oi.live)return"<div style='color:var(--mt);font-size:12px;padding:6px 0'>"+(oi.live?"No data":"Live OI loading \u2014 refreshes every 60s")+"</div>";
    var col2=isCall?"var(--rd)":"var(--gr)";var mx=Math.max.apply(null,walls.map(function(w){return parseFloat(w.oi)||1;}));
    return walls.map(function(w){var pct=Math.min((parseFloat(w.oi)||0)/mx*100,100);
      return"<div class='oi-item'><div class='oi-top'><span style='color:var(--wh);font-size:15px;font-weight:700'>"+fii(w.s)+" "+(isCall?"CE":"PE")+"</span><span style='color:"+col2+";font-size:13px;font-weight:600'>"+w.oi+"L</span></div><div class='oi-track'><div class='oi-fill' style='width:"+pct+"%;background:"+col2+"'></div></div></div>";
    }).join("");
  }
  eid("p2-cw").innerHTML=oiW(oi.calls,true);eid("p2-pw").innerHTML=oiW(oi.puts,false);
  eid("oi-age-txt").textContent=d.oi_age>=0?"OI last updated "+d.oi_age+"s ago \u2022 refreshes every 60s":"OI loading in background...";
  show("p2main");
  // --- Tab 3: Momentum ---
  if(!sig.candles_count||sig.candles_count<2){
    eid("p3ld-txt").textContent="No candle data \u2014 loading (market opens 9:15 AM)";show("p3ld");hide("p3main");
  } else if(sig.vwap===0){
    eid("p3ld-txt").textContent="Calculating VWAP \u2014 "+sig.candles_count+" candles loaded";show("p3ld");hide("p3main");
  } else {
    hide("p3ld");
    var vh=eid("vwap-hero");vh.className="hero "+(sig.color==="green"?"bull":sig.color==="red"?"bear":"");
    var icon=sig.color==="green"?"\u25B2 ":sig.color==="red"?"\u25BC ":"\u25A0 ";
    eid("vs-act").innerHTML="<span style='color:"+col+"'>"+icon+sig.action+"</span>";
    eid("vs-note").textContent=sig.reasons.slice(0,3).join(" \u2022 ");
    eid("p3-cnt").textContent=sig.candles_count+" candles";
    eid("p3-upper").textContent=fii(sig.upper);eid("p3-vwap").textContent=fii(sig.vwap);eid("p3-lower").textContent=fii(sig.lower);
    var vpos=sig.upper>sig.lower?Math.min(Math.max((spot-sig.lower)/(sig.upper-sig.lower)*100,0),100):50;
    eid("p3-dot").style.left=vpos+"%";eid("p3-rpos").textContent=Math.round(vpos)+"%";
    eid("p3-lo").textContent="Lower "+fii(sig.lower);eid("p3-hi").textContent="Upper "+fii(sig.upper);
    var rsi=sig.rsi;var rcol=rsi>=70?"var(--rd)":rsi>=60?"var(--gr)":rsi>=45?"var(--gd)":rsi>=30?"var(--rd)":"var(--gr)";
    var rlbl=rsi>=70?"OVERBOUGHT":rsi>=60?"BULLISH":rsi>=45?"NEUTRAL":rsi>=30?"BEARISH":"OVERSOLD";
    eid("p3-rsi").textContent=rsi;eid("p3-rsi").style.color=rcol;eid("p3-rlbl").textContent=rlbl;eid("p3-rlbl").style.color=rcol;
    eid("p3-rbar").style.width=rsi+"%";eid("p3-rbar").style.background=rcol;
    var h2=sig.hist;var mcol=h2>0?"var(--gr)":"var(--rd)";
    eid("p3-macd").textContent=h2>0?"+"+h2:String(h2);eid("p3-macd").style.color=mcol;
    eid("p3-mlbl").textContent=h2>0?"BULL CROSS \u25B2":"BEAR CROSS \u25BC";eid("p3-mlbl").style.color=mcol;
    var pats=sig.patterns||[];
    eid("p3-pats").innerHTML=!pats.length?"<div style='color:var(--mt);font-size:12px'>No strong patterns</div>":pats.map(function(p){return"<span class='pat "+(p.type==="bullish"?"bull":p.type==="bearish"?"bear":"")+"'>"+p.name+" \u2014 "+p.desc+"</span>";}).join("");
    show("p3main");
  }
  DATA.ready=true;
}

// ── Polling ───────────────────────────────────────────────────
// Spot: every 1s (fast, just updates the number)
async function pollSpot(){
  try{
    var r=await fetch("/api/spot");var d=await r.json();
    if(d.spot>0){
      DATA.prev=DATA.spot||d.spot;DATA.spot=d.spot;
      eid("spot-num").textContent=fi(d.spot);
      var chg=d.spot-DATA.prev;
      eid("spot-num").style.color=chg>0?"var(--gr)":chg<0?"var(--rd)":"var(--wh)";
      eid("upd-ts").textContent=d.ts;
    }
  }catch(e){}
}
// Full data: every 10s (updates everything)
async function pollFull(){
  try{
    var r=await fetch("/api/live?_="+Date.now());var d=await r.json();
    renderAll(d);
  }catch(e){if(!DATA.ready){hide("p1ld");show("p1err");eid("p1em").textContent="Network: "+e.message;}}
}

pollFull();
setInterval(pollSpot,1000);
setInterval(pollFull,10000);
</script>
</body></html>"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
