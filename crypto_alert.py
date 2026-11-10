#!/usr/bin/env python3
"""
Spot crypto ALERT bot (alerts only: no trading, no exchange API keys needed).
Coins: BTC, ETH, SOL, SUI, PAXG, XAUT
Alerts: BUY idea (score /10), PUMP/DUMP, RISK warning.
Delivery: WhatsApp via CallMeBot (free). Run once per hour (see alerts.yml).

Usage:
  python crypto_alert.py          # normal run
  python crypto_alert.py --dry    # print only, don't send WhatsApp
  python crypto_alert.py --test   # send a test WhatsApp message
"""
import os
import sys
import requests

# name: (data sources in order of preference, symbol, pump/dump threshold % on 1H)
COINS = {
    "BTC":  (["binance"], "BTCUSDT", 3.0),
    "ETH":  (["binance"], "ETHUSDT", 3.5),
    "SOL":  (["binance"], "SOLUSDT", 5.0),
    "SUI":  (["binance"], "SUIUSDT", 6.0),
    "PAXG": (["binance"], "PAXGUSDT", 1.2),
    "XAUT": (["bybit", "okx"], "XAUTUSDT", 1.2),
}
MIN_SCORE = 7          # alert only if score >= this (out of 10)
FUNDING_HOT = 0.0003   # 0.03% per 8h = crowded longs
HDR = {"User-Agent": "alert-bot/1.0"}


def get(url, params=None):
    r = requests.get(url, params=params, timeout=15, headers=HDR)
    r.raise_for_status()
    return r.json()


# ---------- data ----------
def klines(src, sym, tf):
    """Return closed candles as list of (o,h,l,c,v). tf in {'1h','4h'}."""
    if src == "binance":
        d = None
        for host in ("https://data-api.binance.vision", "https://api.binance.com"):
            try:
                d = get(f"{host}/api/v3/klines", {"symbol": sym, "interval": tf, "limit": 300})
                break
            except Exception:
                continue
        if d is None:
            raise RuntimeError("binance unreachable")
        rows = [tuple(float(x[i]) for i in (1, 2, 3, 4, 5)) for x in d]
        return rows[:-1]  # last candle is still open
    if src == "bybit":
        iv = {"1h": "60", "4h": "240"}[tf]
        d = get("https://api.bybit.com/v5/market/kline",
                {"category": "spot", "symbol": sym, "interval": iv, "limit": 300})["result"]["list"]
        rows = [tuple(float(x[i]) for i in (1, 2, 3, 4, 5)) for x in reversed(d)]
        return rows[:-1]
    if src == "okx":
        inst = sym.replace("USDT", "-USDT")
        bar = {"1h": "1H", "4h": "4H"}[tf]
        d = get("https://www.okx.com/api/v5/market/candles",
                {"instId": inst, "bar": bar, "limit": "300"})["data"]
        d = [x for x in reversed(d) if x[-1] == "1"]  # closed only
        return [tuple(float(x[i]) for i in (1, 2, 3, 4, 5)) for x in d]
    raise ValueError(src)


def load(srcs, sym, tf):
    last = None
    for s in srcs:
        try:
            return klines(s, sym, tf), s
        except Exception as e:
            last = e
    raise RuntimeError(f"{sym} {tf}: {last}")


def funding(src, sym):
    """Funding rate from perp market; None if unavailable."""
    try:
        if src == "binance":
            return float(get("https://fapi.binance.com/fapi/v1/premiumIndex",
                             {"symbol": sym})["lastFundingRate"])
        if src == "bybit":
            d = get("https://api.bybit.com/v5/market/tickers",
                    {"category": "linear", "symbol": sym})["result"]["list"][0]
            return float(d["fundingRate"])
    except Exception:
        return None
    return None


# ---------- indicators ----------
def ema(vals, n):
    k = 2 / (n + 1)
    e = vals[0]
    out = [e]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes, n=14):
    out = [50.0] * len(closes)
    if len(closes) <= n:
        return out
    g = l = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        g += max(d, 0)
        l += max(-d, 0)
    g /= n
    l /= n
    out[n] = 100 - 100 / (1 + g / l) if l else 100
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        g = (g * (n - 1) + max(d, 0)) / n
        l = (l * (n - 1) + max(-d, 0)) / n
        out[i] = 100 - 100 / (1 + g / l) if l else 100
    return out


def atr_at(H, L, C, j, n=14):
    trs = [max(H[i] - L[i], abs(H[i] - C[i - 1]), abs(L[i] - C[i - 1]))
           for i in range(max(1, j - n + 1), j + 1)]
    return sum(trs) / len(trs)


def pack(rows):
    O, H, L, C, V = (list(x) for x in zip(*rows))
    return {"O": O, "H": H, "L": L, "C": C, "V": V,
            "e20": ema(C, 20), "e50": ema(C, 50), "e200": ema(C, 200), "rsi": rsi(C)}


def trend4h(t):
    c, e50, e200 = t["C"][-1], t["e50"], t["e200"]
    if c > e200[-1] and e50[-1] > e200[-1] and e50[-1] > e50[-6]:
        return "UP"
    if c < e200[-1] and e50[-1] < e200[-1]:
        return "DOWN"
    return "SIDEWAYS"


# ---------- buy-idea score ----------
def buy_score(h, tr, fund, j):
    """Return (score, levels dict, reasons list) for 1H candle index j."""
    O, H, L, C, V = h["O"], h["H"], h["L"], h["C"], h["V"]
    reasons, s = [], 0.0
    if tr == "DOWN" or h["rsi"][j] > 75:
        return 0, None, []
    # 1) trend (3)
    s += 3 if tr == "UP" else 1
    reasons.append(f"4H trend: {tr}")
    # 2) pullback to EMA50 (2)
    e50 = h["e50"][j]
    touched = min(L[j - 2:j + 1]) <= e50 * 1.005 and C[j] >= e50 * 0.99
    if touched and h["rsi"][j] <= 58:
        s += 2
        reasons.append("pullback EMA50 par")
    elif C[j] <= e50 * 1.03 and h["rsi"][j] <= 58:
        s += 1
        reasons.append("EMA50 ke qareeb")
    # 3) bounce confirmation (2)
    green, above20 = C[j] > O[j], C[j] > h["e20"][j]
    if green and above20:
        s += 2
        reasons.append("bounce candle confirm")
    elif green:
        s += 1
    # 4) volume (1)
    avg = sum(V[j - 20:j]) / 20
    if V[j] >= 1.3 * avg:
        s += 1
        reasons.append("volume barha")
    # 5) funding (1)
    if fund is None:
        s += 0.5
    elif fund <= FUNDING_HOT:
        s += 1
        reasons.append(f"funding theek ({fund * 100:.3f}%)")
    else:
        reasons.append(f"funding garam ({fund * 100:.3f}%) - ehtiyat")
    # 6) risk/reward (1)
    entry = C[j]
    stop = min(L[j - 9:j + 1]) - 0.2 * atr_at(H, L, C, j)
    risk = entry - stop
    levels = None
    if risk > 0:
        rh = max(H[j - 72:j + 1])
        rr = (rh - entry) / risk
        levels = {"entry": entry, "stop": stop, "t1": entry + 1.5 * risk,
                  "t2": entry + 3 * risk, "risk_pct": risk / entry * 100, "rr": rr}
        if rr >= 1.5 and levels["risk_pct"] <= 6:
            s += 1
            reasons.append(f"R:R ~1:{rr:.1f}")
    return round(s, 1), levels, reasons


def px(x):
    return f"{x:,.4f}" if x < 10 else f"{x:,.2f}"


# ---------- per-coin check ----------
def check(name, cfg):
    srcs, sym, thr = cfg
    h1, src = load(srcs, sym, "1h")
    t4, _ = load([src] + [s for s in srcs if s != src], sym, "4h")
    h, t = pack(h1), pack(t4)
    tr = trend4h(t)
    fund = funding(src, sym)
    j = len(h["C"]) - 1
    msgs = []

    # BUY idea: fresh crossing above MIN_SCORE
    now, lv, why = buy_score(h, tr, fund, j)
    prev, _, _ = buy_score(h, tr, fund, j - 1)
    if now >= MIN_SCORE and prev < MIN_SCORE and lv:
        msgs.append(
            f"🟢 {name} BUY IDEA  {now}/10\n"
            f"Entry ~{px(lv['entry'])} | Stop {px(lv['stop'])} (-{lv['risk_pct']:.1f}%)\n"
            f"T1 {px(lv['t1'])} | T2 {px(lv['t2'])}\n"
            + "\n".join("• " + r for r in why))

    # PUMP / DUMP on last closed 1H candle
    chg = (h["C"][j] / h["O"][j] - 1) * 100
    avg = sum(h["V"][j - 20:j]) / 20
    if abs(chg) >= thr and h["V"][j] >= 2 * avg:
        if chg > 0:
            msgs.append(f"🚀 {name} PUMP {chg:+.1f}% (1H, volume 2x+). Chase mat karo, pullback ka intezar.")
        else:
            msgs.append(f"🔻 {name} DUMP {chg:+.1f}% (1H, volume 2x+). Zabardasti buy mat karo, stabilize hone do.")

    # RISK warnings (for holders)
    c4, e200 = t["C"], t["e200"]
    if c4[-2] >= e200[-2] and c4[-1] < e200[-1]:
        msgs.append(f"⚠️ {name}: 4H close EMA200 ke neeche. Trend kamzor, spot hold review karein.")
    hot = lambda k: h["rsi"][k] > 78 and fund is not None and fund > FUNDING_HOT
    if hot(j) and not hot(j - 1):
        msgs.append(f"⚠️ {name}: overheated (RSI {h['rsi'][j]:.0f}, funding high). Thora profit book karne ka sochein.")

    status = f"{name} {px(h['C'][j])} | {tr} | score {now}"
    return msgs, status


# ---------- delivery ----------
def send(text):
    phone, key = os.getenv("WA_PHONE"), os.getenv("WA_APIKEY")
    if "--dry" in sys.argv or not (phone and key):
        print(text)
        return
    requests.get("https://api.callmebot.com/whatsapp.php",
                 params={"phone": phone, "text": text, "apikey": key}, timeout=20)


def main():
    if "--test" in sys.argv:
        send("✅ Crypto alert bot test message")
        return
    out, status = [], []
    for name, cfg in COINS.items():
        try:
            m, s = check(name, cfg)
            out += m
            status.append(s)
        except Exception as e:
            status.append(f"{name}: data error ({e})")
    print("\n".join(status))
    if out:
        send("\n\n".join(out) + "\n\n(Idea hai, hukm nahi. Apna stop zaroor lagayein.)")


if __name__ == "__main__":
    main()
