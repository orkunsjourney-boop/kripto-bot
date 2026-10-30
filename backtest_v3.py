#!/usr/bin/env python3
"""
BACKTEST v3 - dürüst karşılaştırma
- Son 150 günü ikiye böler: ilk 100 gün EĞİTİM (ayar seçilir), son 50 gün TEST (hiç görülmemiş dönem).
- Risk sınırları dahil: aynı anda en fazla 3 açık sinyal, aynı saatte en fazla 2 yeni sinyal.
- Maliyet: komisyon + kayma toplam %0.3 düşülür.
- Sonuç Telegram'a gelir; otomatik bir "işe yarıyor mu?" yorumu içerir.
Gerekli: aynı klasörde bot.py ve backtest.py
Çalıştırma: python backtest_v3.py
"""
import time
from itertools import product

from bot import api, send, STABLES, MAX_24H_CHANGE, COOLDOWN_H, EXPIRE_H
from backtest import fetch, prep1, prep4, align, btc_weak_map

DAYS = 150          # toplam test süresi (gün)
TRAIN_DAYS = 100    # ilk bu kadar gün eğitim, kalanı test
TOP_N = 80          # hacme göre en büyük kaç coin
COST = 0.3          # alım+satım komisyon + kayma (%)
MIN_POT = 5.0       # 2 x ATR(4s) en az % kaç olmalı (canlı bottaki şart)
MIN_TARGET = 0.05   # hedef en az %5
MAX_OPEN = 3        # aynı anda en fazla açık sinyal
MAX_PER_SCAN = 2    # aynı saatte en fazla yeni sinyal
H = 3_600_000


def make_candidates(sym, rank, p1, p4, eval_start):
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
        atr_pct = p4["atr"][j] / price
        if score < 4 or atr_pct * 200 < MIN_POT:
            continue
        out.append((p1["t"][i], sym, i, score, atr_pct, rank))
    return out


def resolve(p1, i, tp, sp):
    h, l, c = p1["h"], p1["l"], p1["c"]
    n, entry = len(c), c[i]
    target, stop = entry * (1 + tp), entry * (1 - sp)
    last = min(i + EXPIRE_H, n - 1)
    for j in range(i + 1, last + 1):
        if l[j] <= stop:                      # aynı mumda ikisi de olursa stop sayılır
            return "stop", -sp * 100, j
        if h[j] >= target:
            return "hit", tp * 100, j
    if i + EXPIRE_H <= n - 1:
        return "exp", (c[i + EXPIRE_H] / entry - 1) * 100, i + EXPIRE_H
    return None, 0.0, n - 1


def simulate(cands, data, weak, params, t_lo, t_hi):
    min_score, use_btc, topn, ks, kt = params
    res, open_end, free, per_hour = [], [], {}, {}
    for (t, sym, i, score, atr_pct, rank) in cands:
        if t < t_lo or t >= t_hi or score < min_score or rank >= topn:
            continue
        if use_btc and weak.get(t, False):
            continue
        open_end = [e for e in open_end if e > t]
        if len(open_end) >= MAX_OPEN or per_hour.get(t, 0) >= MAX_PER_SCAN:
            continue
        if i < free.get(sym, 0):
            continue
        p1 = data[sym]
        tp = max(kt * atr_pct, MIN_TARGET)
        sp = ks * atr_pct
        r, pnl, end = resolve(p1, i, tp, sp)
        free[sym] = max(end + 1, i + COOLDOWN_H)
        if r is None:
            continue
        open_end.append(p1["t"][end])
        per_hour[t] = per_hour.get(t, 0) + 1
        res.append((r, pnl - COST))
    return res


def stats(res):
    n = len(res)
    if n == 0:
        return {"n": 0, "avg": 0.0, "text": "sinyal yok"}
    hit = sum(1 for r, _ in res if r == "hit")
    stp = sum(1 for r, _ in res if r == "stop")
    exp = n - hit - stp
    avg = sum(p for _, p in res) / n
    return {"n": n, "avg": avg,
            "text": f"{n} sinyal | ✅{hit} 🛑{stp} ⏳{exp} | ort. net {avg:+.2f}%"}


def label(p):
    ms, ub, tn, ks, kt = p
    return (f"Skor≥{ms}, BTC filtresi {'AÇIK' if ub else 'KAPALI'}, ilk {tn} coin, "
            f"stop {ks}xATR, hedef {kt}xATR")


def main():
    now = int(time.time() * 1000)
    eval_start = now - DAYS * 24 * H
    split = eval_start + TRAIN_DAYS * 24 * H
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

    def load(sym):
        return (fetch(sym, "1h", eval_start - 250 * H, now),
                fetch(sym, "4h", eval_start - 140 * 4 * H, now))

    r1, r4 = load("BTCUSDT")
    weak = btc_weak_map(prep1(r1), prep4(r4))

    data, cands = {}, []
    for k, sym in enumerate(syms):
        try:
            r1, r4 = load(sym)
            if len(r1) < 400 or len(r4) < 100:
                continue
            p1, p4 = prep1(r1), prep4(r4)
            data[sym] = p1
            cands += make_candidates(sym, k, p1, p4, eval_start)
        except Exception as e:
            print(sym, "atlandı:", e)
        if (k + 1) % 10 == 0:
            print(f"{k + 1}/{len(syms)} coin işlendi")
    cands.sort(key=lambda x: (x[0], -x[3], x[5]))
    print(f"{len(data)} coin, {len(cands)} aday sinyal")

    grid = list(product((4, 5), (False, True), (30, 80), (1.0, 1.5, 2.5), (1.5, 2.0, 3.0)))
    rows = []
    for p in grid:
        tr = stats(simulate(cands, data, weak, p, eval_start, split))
        te = stats(simulate(cands, data, weak, p, split, now + 1))
        rows.append((p, tr, te))

    base = (5, True, 80, 1.5, 2.0)   # mevcut canlı ayar (risk sınırlarıyla)
    base_row = next(r for r in rows if r[0] == base)
    ranked = sorted([r for r in rows if r[1]["n"] >= 30], key=lambda r: -r[1]["avg"])[:3]

    lines = [f"📊 BACKTEST v3: {len(data)} coin, son {DAYS} gün "
             f"(ilk {TRAIN_DAYS} gün EĞİTİM, son {DAYS - TRAIN_DAYS} gün TEST)",
             f"Risk sınırı: en fazla {MAX_OPEN} açık sinyal, saatte en fazla {MAX_PER_SCAN} yeni. "
             f"Maliyet %{COST} düşüldü.", "",
             "Şu anki ayar (risk sınırlı):",
             f"  Eğitim: {base_row[1]['text']}",
             f"  TEST  : {base_row[2]['text']}", "",
             "Eğitimde en iyi çıkan 3 ayar ve onların TEST sonucu:"]
    for k, (p, tr, te) in enumerate(ranked, 1):
        lines += [f"{k}) {label(p)}", f"  Eğitim: {tr['text']}", f"  TEST  : {te['text']}"]
    if not ranked:
        lines.append("  (eğitimde 30 sinyali geçen ayar bulunamadı)")

    good = [r for r in ranked if r[2]["n"] >= 30 and r[2]["avg"] > 0.3]
    lines.append("")
    if good:
        lines.append("YORUM: Bazı ayarlar görülmemiş TEST döneminde de artı verdi. Bu bir umut, kanıt "
                     "değil: sadece 50 günlük test ve 72 ayar denendi (şans payı var). Gerçek para yerine "
                     "en az 2-3 hafta Telegram skor tablosuyla canlı izle.")
    else:
        lines.append("YORUM: Hiçbir ayar, eğitimde iyi görünse bile, görülmemiş TEST döneminde güvenilir "
                     "kazanç göstermedi. Bu kurallarla gerçek parayla işlem yapma ve sinyalleri başkalarına "
                     "kazandırır gibi paylaşma.")
    lines += ["", "⚠️ Sadece bugün hacmi yüksek coinler test edildi (batmışlar yok, sonuç iyimser olabilir). "
                  "1 saatlik mumlar; aynı mumda hedef ve stop olduysa stop sayıldı. "
                  "30'dan az sinyalli sonuçlar güvenilir değil. Geçmiş geleceği garanti etmez."]
    msg = "\n".join(lines)
    print("\n" + msg)
    send(msg)


if __name__ == "__main__":
    main()
