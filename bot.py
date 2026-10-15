#!/usr/bin/env python3
"""
Binance -> Telegram AL sinyal botu (sadece sinyal; emir vermez, API anahtarı gerekmez)

Kullanım:
  export TELEGRAM_TOKEN="123:ABC..."
  export TELEGRAM_CHAT_ID="123456789"
  python bot.py --test    # Telegram bağlantısını dener
  python bot.py --once    # tek tarama yapar
  python bot.py           # sürekli çalışır (her 15 dk tarar)
"""
import os
import sys
import json
import time
import requests
from datetime import datetime

TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ---------- AYARLAR ----------
SCAN_INTERVAL = 15 * 60      # tarama aralığı (sn)
MIN_QUOTE_VOL = 2_000_000    # 24s min hacim (USDT) - çöp/likidite yok coinleri eler
MIN_SCORE = 5                # 6 kriterden en az kaçı sağlanmalı
MIN_POTENTIAL = 5.0          # en az % kaç yükseliş potansiyeli
MAX_24H_CHANGE = 12.0        # 24s'de zaten bu kadar çıkmışsa geç kalınmış say
COOLDOWN_H = 8               # aynı coine tekrar sinyal için bekleme (saat)
STATE_FILE = "signals.json"
BASES = ["https://api.binance.com", "https://data-api.binance.vision"]

STABLES = {"USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "AEUR", "EUR",
           "USDD", "PYUSD", "UST", "USTC", "XUSD", "USD1"}


# ---------- BINANCE ----------
def api(path, params=None):
    last = None
    for base in BASES:
        try:
            r = requests.get(base + path, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
            last = f"{r.status_code} {r.text[:100]}"
        except Exception as e:
            last = str(e)
    raise RuntimeError(f"Binance hatası: {last}")


def klines(symbol, interval, limit):
    data = api("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    data = data[:-1]  # son (kapanmamış) mumu at
    return (
        [float(k[4]) for k in data],  # close
        [float(k[2]) for k in data],  # high
        [float(k[3]) for k in data],  # low
        [float(k[5]) for k in data],  # volume
    )


# ---------- İNDİKATÖRLER ----------
def ema(values, n):
    k = 2 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes, n=14):
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def macd_hist(closes):
    e12, e26 = ema(closes, 12), ema(closes, 26)
    line = [a - b for a, b in zip(e12, e26)]
    sig = ema(line, 9)
    return [l - s for l, s in zip(line, sig)]


def atr(highs, lows, closes, n=14):
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


# ---------- ANALİZ ----------
def analyze(symbol):
    c1, h1, l1, v1 = klines(symbol, "1h", 200)
    c4, h4, l4, _ = klines(symbol, "4h", 120)
    if len(c1) < 60 or len(c4) < 60:
        return None

    price = c1[-1]
    score, reasons = 0, []

    e20, e50 = ema(c1, 20)[-1], ema(c1, 50)[-1]
    if e20 > e50 and price > e20:
        score += 1
        reasons.append("1s trend yukarı (EMA20>EMA50)")

    if ema(c4, 20)[-1] > ema(c4, 50)[-1]:
        score += 1
        reasons.append("4s trend yukarı")

    r = rsi(c1)
    if 50 <= r <= 68:
        score += 1
        reasons.append(f"RSI güçlü ama şişmemiş ({r:.0f})")

    mh = macd_hist(c1)
    if mh[-1] > 0 and mh[-1] > mh[-2]:
        score += 1
        reasons.append("MACD pozitif ve güçleniyor")

    avg_vol = sum(v1[-21:-1]) / 20
    vol_ratio = v1[-1] / avg_vol if avg_vol else 0
    if vol_ratio >= 1.8:
        score += 1
        reasons.append(f"Hacim patlaması ({vol_ratio:.1f}x)")

    res = max(h1[-25:-1])
    if price >= res * 0.995:
        score += 1
        reasons.append("Son 24 saatlik direnç kırılıyor/test ediliyor")

    a4 = atr(h4, l4, c4)
    potential = 2 * a4 / price * 100      # beklenen hareket (4s ATR x2)
    stop = price - 1.5 * a4
    target = price + 2 * a4
    target = max(target, price * (1 + MIN_POTENTIAL / 100))

    return {
        "symbol": symbol, "price": price, "score": score, "reasons": reasons,
        "potential": potential, "target": target, "stop": stop, "rsi": r,
    }


# ---------- TELEGRAM ----------
def send(text):
    if not TOKEN or not CHAT_ID:
        print("HATA: TELEGRAM_TOKEN / TELEGRAM_CHAT_ID tanımlı değil")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text}, timeout=15)
        if r.status_code != 200:
            print("Telegram hata:", r.text[:200])
        return r.status_code == 200
    except Exception as e:
        print("Telegram hata:", e)
        return False


def fmt(p):
    return f"{p:.8g}"


# ---------- DURUM ----------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f)


# ---------- TARAMA ----------
def scan():
    print(f"[{datetime.now():%H:%M:%S}] Tarama başladı")
    info = api("/api/v3/exchangeInfo")
    valid = set()
    for s in info["symbols"]:
        base = s["baseAsset"]
        if (s["quoteAsset"] == "USDT" and s["status"] == "TRADING"
                and s.get("isSpotTradingAllowed")
                and base not in STABLES
                and not base.endswith(("UP", "DOWN", "BULL", "BEAR"))):
            valid.add(s["symbol"])

    tickers = api("/api/v3/ticker/24hr")
    cands = []
    for t in tickers:
        sym = t["symbol"]
        if sym not in valid:
            continue
        qv = float(t["quoteVolume"])
        ch = float(t["priceChangePercent"])
        if qv >= MIN_QUOTE_VOL and ch <= MAX_24H_CHANGE:
            cands.append((sym, qv, ch))
    cands.sort(key=lambda x: -x[1])
    print(f"Toplam {len(valid)} USDT paritesi, {len(cands)} tanesi detaylı incelenecek")

    state = load_state()
    now = time.time()
    found = []

    for sym, qv, ch in cands:
        if now - state.get(sym, 0) < COOLDOWN_H * 3600:
            continue
        try:
            res = analyze(sym)
        except Exception as e:
            print(sym, "atlandı:", e)
            time.sleep(1)
            continue
        time.sleep(0.2)  # rate limit koruması
        if not res:
            continue
        if res["score"] >= MIN_SCORE and res["potential"] >= MIN_POTENTIAL:
            res["change24"] = ch
            res["qv"] = qv
            found.append(res)

    found.sort(key=lambda x: (-x["score"], -x["potential"]))
    for r in found[:8]:  # tarama başına en fazla 8 sinyal
        gain = (r["target"] / r["price"] - 1) * 100
        loss = (1 - r["stop"] / r["price"]) * 100
        msg = (
            f"🟢 AL SİNYALİ: {r['symbol']}\n"
            f"Fiyat: {fmt(r['price'])}\n"
            f"Skor: {r['score']}/6 | Potansiyel: ~%{r['potential']:.1f}\n"
            f"🎯 Hedef: {fmt(r['target'])} (+%{gain:.1f})\n"
            f"🛑 Stop: {fmt(r['stop'])} (-%{loss:.1f})\n"
            f"24s değişim: %{r['change24']:.1f} | Hacim: {r['qv']/1e6:.1f}M USDT\n\n"
            + "\n".join("• " + x for x in r["reasons"])
            + "\n\n⚠️ Yatırım tavsiyesi değildir. Kendi araştırmanı yap."
        )
        if send(msg):
            state[r["symbol"]] = now
            print("Sinyal gönderildi:", r["symbol"])
    save_state(state)
    print(f"Tarama bitti. {len(found)} sinyal bulundu.")


def main():
    if "--test" in sys.argv:
        ok = send("✅ Bot bağlantısı çalışıyor!")
        print("Telegram testi:", "BAŞARILI" if ok else "BAŞARISIZ")
        return
    if "--once" in sys.argv:
        scan()
        return
    send("🤖 Sinyal botu başlatıldı. Taramaya başlıyorum.")
    while True:
        try:
            scan()
        except Exception as e:
            print("Tarama hatası:", e)
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
