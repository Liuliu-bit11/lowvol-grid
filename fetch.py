#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
低波网格选股 · 全市场数据管线
数据源：东方财富公开接口（行情快照 / 前复权日K / 分红送配明细）
输出：docs/data/full.json

股息率口径（对齐教程"消灭假红利"）：
  按财年归集 -> 中期 + 年报合并为当年 DPS，不做 12 个月滚动叠加
  -> 取最近一个完整财年（有年报）的 DPS ÷ 现价
  这样避免"中报+年报两笔钱挤进同一 12 个月窗口"造成的假尖峰。
"""
import json, os, math, time, sys, datetime, pickle
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
HDR = {"User-Agent": UA, "Referer": "https://quote.eastmoney.com/", "Accept": "*/*"}

CLIST = "https://push2.eastmoney.com/api/qt/clist/get"
KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
DATA = "https://datacenter-web.eastmoney.com/api/data/v1/get"
FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
FIELDS = "f12,f14,f2,f3,f9,f23,f20,f21,f100"

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "docs", "data")
CACHE = os.path.join(BASE, "cache")
os.makedirs(OUT, exist_ok=True)
os.makedirs(CACHE, exist_ok=True)
TODAY = datetime.date.today().isoformat()

S = requests.Session()
S.headers.update(HDR)


def get(url, params=None, tries=3, timeout=25):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        time.sleep(0.4 * (i + 1))
    return None


def pk(name):
    return os.path.join(CACHE, name)


def cache_get(name, max_age_h=20):
    p = pk(name)
    if not os.path.exists(p):
        return None
    if time.time() - os.path.getmtime(p) > max_age_h * 3600:
        return None
    try:
        with open(p, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def cache_put(name, obj):
    try:
        with open(pk(name), "wb") as f:
            pickle.dump(obj, f)
    except Exception:
        pass


# ---------------- 1. 全市场快照 ----------------
def fetch_list():
    hit = cache_get("list.pkl", 12)
    if hit:
        print(f"[list] cached {len(hit)}", flush=True)
        return hit
    first = get(CLIST, {"pn": 1, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                        "fid": "f12", "fs": FS, "fields": FIELDS})
    total = first["data"]["total"]
    pages = math.ceil(total / 100)
    print(f"[list] total={total} pages={pages}", flush=True)

    def one(pn):
        d = get(CLIST, {"pn": pn, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                        "fid": "f12", "fs": FS, "fields": FIELDS})
        return (d or {}).get("data", {}).get("diff") or []

    rows = list(first["data"]["diff"])
    with ThreadPoolExecutor(max_workers=8) as ex:
        for r in ex.map(one, range(2, pages + 1)):
            rows.extend(r)
    seen, out = set(), []
    for r in rows:
        c = r.get("f12")
        if c and c not in seen:
            seen.add(c)
            out.append(r)
    print(f"[list] got {len(out)}", flush=True)
    cache_put("list.pkl", out)
    return out


# ---------------- 2. 分红明细 ----------------
REPORT_DATES = ["2026-06-30", "2025-12-31", "2025-06-30", "2024-12-31",
                "2023-12-31", "2022-12-31", "2021-12-31", "2020-12-31"]
DIV_COLS = ("SECURITY_CODE,PRETAX_BONUS_RMB,BONUS_RATIO,IT_RATIO,"
            "REPORT_DATE,EX_DIVIDEND_DATE,ASSIGN_PROGRESS,BASIC_EPS")


def fetch_dividends():
    hit = cache_get("div.pkl", 12)
    if hit:
        print(f"[div] cached {len(hit)}", flush=True)
        return hit
    recs = []
    for rd in REPORT_DATES:
        page, pages = 1, 1
        while page <= pages:
            d = get(DATA, {"reportName": "RPT_SHAREBONUS_DET", "columns": DIV_COLS,
                           "pageNumber": page, "pageSize": 500,
                           "filter": f"(REPORT_DATE='{rd}')",
                           "sortColumns": "SECURITY_CODE", "sortTypes": 1})
            if not d or not d.get("result"):
                break
            res = d["result"]
            pages = res.get("pages") or 1
            recs.extend(res.get("data") or [])
            page += 1
        print(f"[div] {rd} -> {len(recs)}", flush=True)
    cache_put("div.pkl", recs)
    return recs


def by_fiscal_year(divs):
    """{code: {fy: {'dps':总每股派息, 'eps':年报EPS, 'done':bool}}}"""
    m = {}
    for x in divs:
        code, rep = x.get("SECURITY_CODE"), (x.get("REPORT_DATE") or "")[:10]
        if not code or not rep:
            continue
        d10 = x.get("PRETAX_BONUS_RMB") or 0
        if not d10:
            continue
        # 每10股派息 -> 每股；送转按比例前复权折算
        mult = 1 + ((x.get("BONUS_RATIO") or 0) + (x.get("IT_RATIO") or 0)) / 10.0
        dps = d10 / 10.0 / mult
        e = m.setdefault(code, {}).setdefault(rep[:4], {"dps": 0.0, "eps": None, "done": False})
        e["dps"] += dps
        if rep.endswith("12-31"):
            e["eps"] = x.get("BASIC_EPS")
            e["done"] = (x.get("ASSIGN_PROGRESS") or "").startswith("实施")
    return m


# ---------------- 3. 技术指标 ----------------
def ema(v, n):
    k = 2.0 / (n + 1)
    o, e = [], None
    for x in v:
        e = x if e is None else x * k + e * (1 - k)
        o.append(e)
    return o


def macd_bull(c):
    if len(c) < 35:
        return False
    dif = [a - b for a, b in zip(ema(c, 12), ema(c, 26))]
    dea = ema(dif, 9)
    return dif[-1] > dea[-1]


def boll_pos(c):
    """布林带 %B：收盘价在带内的相对位置，0=下轨，1=上轨。返回 None 表示数据不足"""
    if len(c) < 20:
        return None
    w = c[-20:]
    m = sum(w) / 20
    sd = math.sqrt(sum((x - m) ** 2 for x in w) / 20)
    if sd <= 0:
        return None
    return (c[-1] - (m - 2 * sd)) / (4 * sd)


# 下轨区定义：%B <= 0.15，即收盘落在布林带下方 15% 区域内
LOW_B = 0.15


def boll_low(c):
    v = boll_pos(c)
    return v is not None and v <= LOW_B


def agg(rows, key):
    out, cur, k0 = [], None, None
    for r in rows:
        k = key(r[0])
        if k != k0:
            if cur:
                out.append(cur)
            cur = [k, r[1], r[2], r[3]]
            k0 = k
        else:
            cur[1] = r[1]; cur[2] = max(cur[2], r[2]); cur[3] = min(cur[3], r[3])
    if cur:
        out.append(cur)
    return out


def iso_week(s):
    y, m, d = map(int, s.split("-"))
    return datetime.date(y, m, d).isocalendar()[:2]


def ym(s):
    return s[:7]


def calc_factors(bars, idx_ret):
    if len(bars) < 70:
        return None
    c = [b[1] for b in bars]
    hi = [b[2] for b in bars]
    lo = [b[3] for b in bars]

    n52 = min(len(c), 250)
    h52, l52 = max(hi[-n52:]), min(lo[-n52:])
    pct = (c[-1] - l52) / (h52 - l52) * 100 if h52 > l52 else 50.0

    rets = []
    for i in range(max(1, len(c) - 60), len(c)):
        if c[i - 1] > 0:
            rets.append(c[i] / c[i - 1] - 1)
    vol = 0.0
    if len(rets) > 5:
        m = sum(rets) / len(rets)
        vol = math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(252) * 100

    beta = None
    if idx_ret and len(rets) >= 20:
        n = min(len(rets), len(idx_ret))
        a, b = rets[-n:], idx_ret[-n:]
        ma, mb = sum(a) / n, sum(b) / n
        cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
        var = sum((b[i] - mb) ** 2 for i in range(n))
        if var > 0:
            beta = cov / var

    dd, dm = boll_low(c), macd_bull(c)
    wk = [x[1] for x in agg(bars, lambda s: iso_week(s))]
    mo = [x[1] for x in agg(bars, lambda s: ym(s))]
    wd, wm = boll_low(wk), macd_bull(wk)
    md, mm = boll_low(mo), macd_bull(mo)

    nb = sum([dd, wd, md])
    nm = sum([dm, wm, mm])
    if nb == 3 and nm >= 1:
        sig = 3          # 反转：三周期全线超跌 + 动能转强
    elif nb >= 2 and nm >= 1:
        sig = 2          # 筑底：多周期低位 + 动能回暖
    elif nb >= 1:
        sig = 1          # 探底：进入低位区，动能未确认
    else:
        sig = 0          # 无机会

    return {"pct": pct, "vol": vol, "beta": beta, "sig": sig,
            "lamps": [int(dd), int(wd), int(md), int(dm), int(wm), int(mm)],
            "closes": c, "bars": bars}


def fetch_kline(sid, lmt=800):
    d = get(KLINE, {"secid": sid, "fields1": "f1,f2,f3,f4,f5,f6",
                    "fields2": "f51,f52,f53,f54,f55,f56", "klt": 101,
                    "fqt": 1, "end": "20500101", "lmt": lmt}, tries=2, timeout=20)
    kl = ((d or {}).get("data") or {}).get("klines")
    if not kl:
        return None
    out = []
    for line in kl:
        p = line.split(",")
        try:
            out.append([p[0], float(p[2]), float(p[3]), float(p[4])])
        except Exception:
            pass
    return out


def secid(code):
    return ("1." if code.startswith(("60", "68", "90", "91")) else "0.") + code


def fresh(bars, days=12):
    if not bars:
        return False
    cut = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    return bars[-1][0] >= cut


# ---------------- 4. 组装 ----------------
def build_rows(lst, divs, klines, idx_ret):
    fym = by_fiscal_year(divs)
    rows = []
    skipped = 0

    for r in lst:
        code = r.get("f12")
        price = r.get("f2")
        if not code or not isinstance(price, (int, float)) or price <= 0:
            skipped += 1
            continue
        bars = klines.get(code)
        if not fresh(bars):
            skipped += 1
            continue
        fac = calc_factors(bars, idx_ret)
        if not fac:
            skipped += 1
            continue

        fys = fym.get(code, {})
        # 完整财年 = 有年报(12-31)记录
        complete = sorted([int(k) for k, v in fys.items() if k and v.get("eps") is not None], reverse=True)
        dps_latest, eps_latest, latest_fy = 0.0, None, None
        for fy in complete:
            v = fys[str(fy)]
            if v["dps"] > 0:
                dps_latest, eps_latest, latest_fy = v["dps"], v["eps"], fy
                break

        y = dps_latest / price * 100 if dps_latest > 0 else 0.0
        payout = None
        if eps_latest and eps_latest > 0 and dps_latest > 0:
            payout = dps_latest / eps_latest * 100

        # 分红中断判定：最近分红财年早于"今年-2"，视为已停发，股息率不可信
        cur_year = int(TODAY[:4])
        stale = 1 if (latest_fy and latest_fy < cur_year - 2) else 0
        if stale:
            y = 0.0
            payout = None

        # 连续分红年数
        dc = 0
        if complete:
            cur = max(complete)
            ys = set([int(k) for k in fys.keys()])
            while cur in ys and fys[str(cur)]["dps"] > 0:
                dc += 1
                cur -= 1

        # 股息率历史分位：锚点用"当时已披露的最近完整财年 DPS"
        anchors = []
        ds = [b[0] for b in fac["bars"]]
        cs = fac["closes"]
        for i in range(len(ds) - 2, -1, -1):
            d = ds[i]
            if d < "2023-11-08":
                break
            if (len(ds) - i) % 21 != 1:
                continue
            c0 = cs[i]
            if c0 <= 0:
                continue
            fy_avail = None
            for fy in complete:
                if f"{fy + 1}-06-30" <= d:   # 年报 4/30 截止，留缓冲
                    fy_avail = fy
                    break
            if fy_avail is None:
                continue
            dps0 = fys[str(fy_avail)]["dps"]
            if dps0 > 0:
                anchors.append(dps0 / c0 * 100)

        if len(anchors) >= 8 and y > 0:
            yp = sum(1 for a in anchors if a < y) / len(anchors) * 100
        else:
            yp = fac["pct"]

        zone = 3 if yp >= 75 else 2 if yp >= 50 else 1 if yp >= 25 else 0

        rows.append([
            code, r.get("f14") or "", r.get("f100") or "", round(price, 2),
            round(r.get("f3") or 0, 2), round(y, 2), round(payout, 1) if payout else 0,
            round(fac["vol"], 1), round(fac["beta"], 2) if fac["beta"] is not None else 1.0,
            round(fac["pct"], 1), round(yp, 1),
            round((r.get("f20") or 0) / 1e8, 0), round((r.get("f21") or 0) / 1e8, 0),
            dc, fac["sig"], fac["lamps"],
            round(r.get("f9"), 2) if isinstance(r.get("f9"), (int, float)) else 0,
            round(r.get("f23"), 2) if isinstance(r.get("f23"), (int, float)) else 0,
            latest_fy or 0, round(dps_latest, 4), stale,
        ])
    print(f"[build] rows={len(rows)} skipped={skipped}", flush=True)
    return rows


def main():
    lst = fetch_list()
    divs = fetch_dividends()

    idx = fetch_kline("1.000300", 400)
    ic = [b[1] for b in idx] if idx else []
    idx_ret = [ic[i] / ic[i - 1] - 1 for i in range(1, len(ic))] if ic else []

    kcache = cache_get("klines.pkl", 20) or {}
    todo = [r for r in lst if isinstance(r.get("f2"), (int, float)) and r.get("f2", 0) > 0]
    need = [r["f12"] for r in todo if r["f12"] not in kcache]
    print(f"[kline] total={len(todo)} cached={len(todo) - len(need)} need={len(need)}", flush=True)

    if need:
        done = [0]

        def work(code):
            return code, fetch_kline(secid(code))

        with ThreadPoolExecutor(max_workers=16) as ex:
            for code, bars in ex.map(work, need):
                kcache[code] = bars
                done[0] += 1
                if done[0] % 800 == 0:
                    print(f"[kline] {done[0]}/{len(need)}", flush=True)
        cache_put("klines.pkl", kcache)

    rows = build_rows(todo, divs, kcache, idx_ret)
    rows.sort(key=lambda x: -x[11])

    meta = {"date": TODAY, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "count": len(rows), "universe": len(todo),
            "source": "东方财富公开行情接口",
            "yield_basis": "最近完整财年 DPS ÷ 现价"}
    fn = os.path.join(OUT, "full.json")
    with open(fn, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "rows": rows}, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[done] {len(rows)} -> {fn}", flush=True)

    # 导出 K 线快照（前端本地读取，规避跨域）：{代码: [收盘价*100, ...]} 近 120 根
    ksnap = {}
    for r in rows:
        bars = kcache.get(r[0])
        if not bars:
            continue
        seg = bars[-120:]
        # 每根: [close, open, high, low]，价格×100 取整。
        # 接口未返回开盘价，用前一根收盘价近似（OHLC 缺失时的通用做法）。
        arr = []
        for i, b in enumerate(seg):
            o = seg[i - 1][1] if i else b[1]
            arr.append([int(round(b[1] * 100)), int(round(o * 100)),
                        int(round(b[2] * 100)), int(round(b[3] * 100))])
        if len(arr) >= 25:
            ksnap[r[0]] = arr
    kf = os.path.join(OUT, "klines.json")
    with open(kf, "w", encoding="utf-8") as f:
        json.dump(ksnap, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[done] klines {len(ksnap)} -> {kf}", flush=True)

    top = sorted([r for r in rows if r[5] > 0], key=lambda x: -x[5])[:8]
    print("股息率 TOP8:", flush=True)
    for r in top:
        print(f"  {r[1]}({r[0]}) 价{r[3]} 息{r[5]}% 分位{r[10]}% 信号{r[14]}", flush=True)


if __name__ == "__main__":
    main()
