#!/usr/bin/env python3
"""
BACKTEST: bot.py'deki kuralları son 90 güne uygular.
Hangi sinyal hedefe, hangisi stopa gitti ölçer; sonucu Telegram'a yollar.
Çalıştırma: python backtest.py   (bot.py ile aynı klasörde olmalı)
"""
import time
from bot import (ema, macd_hist, api, send, STABLES, MAX_24H_CHANGE,
                 MIN_POTENTIAL, COOLDOWN_H, EXPIRE_H, BTC_MAX_DROP_24H)

DAYS = 90        # kaç günlük geçmiş test edilecek
TOP_N = 80       # hacme göre en büyük kaç coin
FEE = 0.2        # alım+satım toplam komisyon (%)
H = 3_600_000    # 1 saat (ms)


# ---------- VERİ ----------
def fetch(symbol, interval, start_ms, end_ms):
    out, cur = [], start_ms
    while cur < end_ms:
        data = api("/api/v3/klines", {
            "symbol": symbol, "interval": interval,
            "startTime": int(cur), "endTime": int(end_ms), "limit": 1000})
        time.sleep(0.25)
        if not data:
            break
        out += data
        nxt = data[-1][0] + 1
        if nxt <= cur or len(data) < 1000:
            break
        cur = nxt
    return [k for k in out if k[6] < end_ms]  # kapanmamış mumu at


def parse(ks):
    return ([k[0] for k in ks], [float(k[4]) for k in ks],
            [float(k[2]) for k in ks], [float(k[3]) for k in ks],
            [float(k[5]) for k in ks])


# ---------- İNDİKATÖR SERİLERİ ----------
def rsi_series(c, n=14):
    out = [None] * len(c)
    if len(c) <= n:
        return out
    g = [max(c[i] - c[i - 1], 0) for i in range(1, len(c))]
    lo = [max(c[i - 1] - c[i], 0) for i in range(1, len(c))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for k in range(n + 1, len(c)):
        ag = (ag * (n - 1) + g[k - 1]) / n
        al = (al * (n - 1) + lo[k - 1]) / n
        out[k] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def atr_series(h, l, c, n=14):
    out = [None] * len(c)
    if len(c) <= n:
        return out
    trs = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
           for i in range(1, len(c))]
    a = sum(trs[:n]) / n
    out[n] = a
    for k in range(n + 1, len(c)):
        a = (a * (n - 1) + trs[k - 1]) / n
        out[k] = a
    return out


def prep1(raw):
    t, c, h, l, v = parse(raw)
    return {"t": t, "c": c, "h": h, "l": l, "v": v,
            "e20": ema(c, 20), "e50": ema(c, 50),
            "rsi": rsi_series(c), "mh": macd_hist(c)}


def prep4(raw):
    t, c, h, l, v = parse(raw)
    return {"t": t, "c": c, "e20": ema(c, 20), "e50": ema(c, 50),
            "atr": atr_series(h, l, c)}


def align(t1, t4):
    """Her 1s mum için, o anda kapanmış son 4s mumun indeksi."""
    m, j = [], -1
    for i in range(len(t1)):
        while j + 1 < len(t4) and t4[j + 1] + 4 * H <= t1[i] + H:
            j += 1
        m.append(j)
    return m


# ---------- BITCOIN FILTRESI ----------
def btc_weak_map(b1, b4):
    al = align(b1["t"], b4["t"])
    res = {}
    for i in range(24, len(b1["c"])):
        j = al[i]
        if j < 0:
            continue
        ch = (b1["c"][i] / b1["c"][i - 24] - 1) * 100
        weak = ch <= BTC_MAX_DROP_24H or (
            b1["e20"][i] < b1["e50"][i] and b4["e20"][j] < b4["e50"][j])
        res[b1["t"][i]] = weak
    return res


# ---------- SİNYAL ADAYLARI (bot.py kurallarının aynısı) ----------
def candidates(p1, p4, eval_start):
    al = align(p1["t"], p4["t"])
    c, h, v = p1["c"], p1["h"], p1["v"]
    out = []
    for i in range(60, len(c)):
        if p1["t"][i] < eval_start:
            continue
        j = al[i]
        if j < 60 or p4["atr"][j] is None or p1["rsi"][i] is None:
            continue
        price = c[i]
        if (price / c[i - 24] - 1) * 100 > MAX_24H_CHANGE:
            continue

        score = 0
        if p1["e20"][i] > p1["e50"][i] and price > p1["e20"][i]:
            score += 1
        if p4["e20"][j] > p4["e50"][j]:
            score += 1
        if 50 <= p1["rsi"][i] <= 68:
            score += 1
        if p1["mh"][i] > 0 and p1["mh"][i] > p1["mh"][i - 1]:
            score += 1
        avg_v = sum(v[i - 20:i]) / 20
        if avg_v and v[i] / avg_v >= 1.8:
            score += 1
        if price >= max(h[i - 24:i]) * 0.995:
            score += 1

        a4 = p4["atr"][j]
        potential = 2 * a4 / price * 100
        if score < 4 or potential < MIN_POTENTIAL:
            continue
        target = max(price + 2 * a4, price * (1 + MIN_POTENTIAL / 100))
        stop = price - 1.5 * a4
        out.append({"i": i, "score": score, "entry": price,
                    "target": target, "stop": stop})
    return out


def resolve(p1, i, target, stop):
    h, l, c = p1["h"], p1["l"], p1["c"]
    n, entry = len(c), c[i]
    last = min(i + EXPIRE_H, n - 1)
    for j in range(i + 1, last + 1):
        if l[j] <= stop:                      # aynı mumda ikisi de olursa stop sayılır
            return "stop", (stop / entry - 1) * 100, j
        if h[j] >= target:
            return "hit", (target / entry - 1) * 100, j
    if i + EXPIRE_H <= n - 1:
        return "exp", (c[i + EXPIRE_H] / entry - 1) * 100, i + EXPIRE_H
    return None, 0.0, n - 1


def run(cands, data, weak, min_score, use_btc):
    res = []
    for sym, cl in cands.items():
        p1, free = data[sym], 0
        for cd in cl:
            i = cd["i"]
            if cd["score"] < min_score or i < free:
                continue
            if use_btc and weak.get(p1["t"][i], False):
                continue
            r, pnl, end = resolve(p1, i, cd["target"], cd["stop"])
            free = max(end + 1, i + COOLDOWN_H)
            if r is None:
                continue
            res.append((r, pnl - FEE))
    return res


def summarize(res):
    n = len(res)
    if n == 0:
        return "sinyal yok"
    hit = sum(1 for r, _ in res if r == "hit")
    stp = sum(1 for r, _ in res if r == "stop")
    exp = n - hit - stp
    avg = sum(p for _, p in res) / n
    return f"{n} sinyal | ✅{hit} 🛑{stp} ⏳{exp} | ort. net {avg:+.2f}%"


# ---------- ANA AKIŞ ----------
def main():
    now = int(time.time() * 1000)
    eval_start = now - DAYS * 24 * H
    print("Coin listesi alınıyor...")
    info = api("/api/v3/exchangeInfo")
    valid = set()
    for s in info["symbols"]:
        b = s["baseAsset"]
        if (s["quoteAsset"] == "USDT" and s["status"] == "TRADING"
                and s.get("isSpotTradingAllowed") and b not in STABLES
                and not b.endswith(("UP", "DOWN", "BULL", "BEAR"))):
            valid.add(s["symbol"])
    tk = [t for t in api("/api/v3/ticker/24hr") if t["symbol"] in valid]
    tk.sort(key=lambda t: -float(t["quoteVolume"]))
    syms = [t["symbol"] for t in tk[:TOP_N]]
    print(f"{len(syms)} coin test edilecek")

    def load(sym):
        r1 = fetch(sym, "1h", eval_start - 250 * H, now)
        r4 = fetch(sym, "4h", eval_start - 140 * 4 * H, now)
        return r1, r4

    r1, r4 = load("BTCUSDT")
    b1, b4 = prep1(r1), prep4(r4)
    weak = btc_weak_map(b1, b4)
    in_range = [w for t, w in weak.items() if t >= eval_start]
    weak_pct = 100 * sum(in_range) / max(len(in_range), 1)

    data, cands = {}, {}
    for k, sym in enumerate(syms, 1):
        try:
            r1, r4 = load(sym)
            if len(r1) < 300 or len(r4) < 100:
                continue
            p1, p4 = prep1(r1), prep4(r4)
            data[sym] = p1
            cands[sym] = candidates(p1, p4, eval_start)
        except Exception as e:
            print(sym, "atlandı:", e)
        if k % 10 == 0:
            print(f"{k}/{len(syms)} coin işlendi")

    lines = [f"📊 BACKTEST: son {DAYS} gün, {len(data)} coin, 1 saatlik mumlar",
             f"Bitcoin filtresi bu sürede zamanın ~%{weak_pct:.0f}'inde 'zayıf' dedi.",
             "(ort. net = komisyon %0.2 düşülmüş ortalama sonuç; + ise kazandırmış)", ""]
    for ms in (4, 5, 6):
        for use_btc in (False, True):
            tag = "BTC filtresi AÇIK " if use_btc else "BTC filtresi KAPALI"
            res = run(cands, data, weak, ms, use_btc)
            lines.append(f"Skor≥{ms}, {tag}: {summarize(res)}")
    lines += ["",
              "⚠️ Sınırlar: sadece bugün hacmi yüksek coinler test edildi (batmış/silinmiş coinler yok, "
              "sonuç olduğundan iyi görünebilir). Mumlar 1 saatlik, aynı mumda hem hedef hem stop "
              "olduysa stop sayıldı. 30'dan az sinyalli satırlar güvenilir sayılmaz. "
              "Geçmiş başarı geleceği garanti etmez."]
    msg = "\n".join(lines)
    print("\n" + msg)
    send(msg)


if __name__ == "__main__":
    main()
