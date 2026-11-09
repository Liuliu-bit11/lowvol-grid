#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
低波网格选股 · 全市场数据管线（基本面增强版）

数据源（全部为公开接口，仅依赖 requests + 标准库）：
  1) 东方财富 push2 / push2his  — 行情快照、前复权日K
  2) 东方财富 datacenter        — 分红送配、季报业绩、资产负债表、现金流量表
  3) 中债登 yield.chinabond.com.cn — 中债国债收益率曲线（10年期）

输出：docs/data/full.json、docs/data/klines.json

三个维度：
  A. 基本面质量   营收/净利同比、加权ROE、毛利率、资产负债率、经营现金流
  B. 股息可持续   股息支付率、经营现金流/分红覆盖倍数、连续分红年数、分红中断
  C. 相对债券性价比 (股息率 − 10Y国债) 的 spread，及其在个股自身历史上的 z-score

股息率口径（对齐教程"消灭假红利"）：
  按财年归集 -> 中期 + 年报合并为当年 DPS，不做 12 个月滚动叠加
  -> 取最近一个完整财年（有年报）的 DPS ÷ 现价
"""
import json, os, math, re, time, sys, datetime, pickle, bisect
from concurrent.futures import ThreadPoolExecutor
import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
HDR = {"User-Agent": UA, "Referer": "https://quote.eastmoney.com/", "Accept": "*/*"}
HDR_CB = {"User-Agent": UA, "Referer": "https://yield.chinabond.com.cn/"}

CLIST = "https://push2.eastmoney.com/api/qt/clist/get"
KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
DATA = "https://datacenter-web.eastmoney.com/api/data/v1/get"
CB_YIELD = "https://yield.chinabond.com.cn/cbweb-pbc-web/pbc/historyQuery"
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


def get(url, params=None, tries=3, timeout=25, hdr=None):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=timeout, headers=hdr)
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


def _rnd(v, d):
    if v is None:
        return None
    try:
        f = float(v)
        if f != f:
            return None
        return round(f, d)
    except Exception:
        return None


# ---------------- 0. 10年期国债收益率 ----------------
def _cb_rows(html):
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        tds = [re.sub(r"<[^>]+>", "", t).replace("&nbsp", "").strip()
               for t in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)]
        out.append(tds)
    return out


def _cb_chunk(s, e):
    """中债接口限制：查询区间需小于一年。返回 [(date, y10), ...]"""
    try:
        r = S.get(CB_YIELD, params={"startDate": s, "endDate": e, "gjqx": "0",
                                    "qxId": "ycqx", "locale": "cn_ZH"},
                  headers=HDR_CB, timeout=40)
        if r.status_code != 200:
            return []
    except Exception:
        return []
    out = []
    for row in _cb_rows(r.text):
        if len(row) < 10:
            continue
        if "国债收益率曲线" not in row[0]:
            continue
        try:
            y10 = float(row[8])
        except Exception:
            continue
        out.append((row[1], y10))
    return out


def fetch_y10():
    """近 5 个自然年 + 当年至今的中债国债 10 年期到期收益率日频序列"""
    hit = cache_get("y10.pkl", 20)
    if hit:
        print(f"[y10] cached {len(hit)}", flush=True)
        return hit
    y0 = int(TODAY[:4])
    spans = [(f"{y}-01-01", f"{y}-12-31") for y in range(y0 - 4, y0)]
    spans.append((f"{y0}-01-01", TODAY))
    acc = {}
    for s, e in spans:
        for d, v in _cb_chunk(s, e):
            acc[d] = v
        print(f"[y10] {s}~{e} -> {len(acc)}", flush=True)
    seq = sorted(acc.items())
    if seq:
        cache_put("y10.pkl", seq)
    return seq


def y10_on(seq, dates, date):
    """取 date 当日或之前最近一个交易日的 10Y 收益率"""
    i = bisect.bisect_right(dates, date) - 1
    return seq[i][1] if i >= 0 else None


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


# ---------------- 2. 财务数据 ----------------
def dc_all(report, columns, flt, page_size=500, max_pages=40):
    recs, page = [], 1
    while page <= max_pages:
        d = get(DATA, {"reportName": report, "columns": columns,
                       "pageNumber": page, "pageSize": page_size,
                       "filter": flt, "sortColumns": "SECURITY_CODE", "sortTypes": 1})
        if not d or not d.get("result"):
            break
        res = d["result"]
        recs.extend(res.get("data") or [])
        if page >= (res.get("pages") or 1):
            break
        page += 1
    return recs


def recent_report_dates(n=6):
    """从今天倒推最近的 n 个季末报告期（YYYY-MM-DD），最新在前"""
    d = datetime.date.today()
    y, m = d.year, d.month
    q_end_month = ((m - 1) // 3) * 3 + 3
    if q_end_month > 12:
        q_end_month = 12
    out = []
    for _ in range(n):
        day = 31 if q_end_month in (3, 12) else 30
        out.append(f"{y}-{q_end_month:02d}-{day}")
        q_end_month -= 3
        if q_end_month <= 0:
            q_end_month += 12
            y -= 1
    return out


LICO_COLS = ("SECURITY_CODE,REPORTDATE,YSTZ,SJLTZ,WEIGHTAVG_ROE,MGJYXJJE,XSMLL,"
             "PARENT_NETPROFIT,TOTAL_OPERATE_INCOME,BASIC_EPS")


def fetch_lico(rd):
    hit = cache_get(f"lico_{rd}.pkl", 20)
    if hit is not None:
        print(f"[lico] {rd} cached {len(hit)}", flush=True)
        return hit
    recs = dc_all("RPT_LICO_FN_CPD", LICO_COLS, f"(REPORTDATE='{rd}')")
    print(f"[lico] {rd} -> {len(recs)}", flush=True)
    cache_put(f"lico_{rd}.pkl", recs)
    return recs


def fetch_balance(rd):
    hit = cache_get(f"bal_{rd}.pkl", 20)
    if hit is not None:
        print(f"[bal] {rd} cached {len(hit)}", flush=True)
        return hit
    recs = dc_all("RPT_DMSK_FN_BALANCE",
                  "SECURITY_CODE,REPORT_DATE,DEBT_ASSET_RATIO,TOTAL_ASSETS,TOTAL_LIABILITIES",
                  f"(REPORT_DATE='{rd}')")
    print(f"[bal] {rd} -> {len(recs)}", flush=True)
    cache_put(f"bal_{rd}.pkl", recs)
    return recs


def fetch_cashflow(rd):
    hit = cache_get(f"cf_{rd}.pkl", 20)
    if hit is not None:
        print(f"[cf] {rd} cached {len(hit)}", flush=True)
        return hit
    recs = dc_all("RPT_DMSK_FN_CASHFLOW",
                  "SECURITY_CODE,REPORT_DATE,NETCASH_OPERATE",
                  f"(REPORT_DATE='{rd}')")
    print(f"[cf] {rd} -> {len(recs)}", flush=True)
    cache_put(f"cf_{rd}.pkl", recs)
    return recs


# ---------------- 3. 分红明细 ----------------
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
        recs.extend(dc_all("RPT_SHAREBONUS_DET", DIV_COLS, f"(REPORT_DATE='{rd}')"))
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
        mult = 1 + ((x.get("BONUS_RATIO") or 0) + (x.get("IT_RATIO") or 0)) / 10.0
        dps = d10 / 10.0 / mult
        e = m.setdefault(code, {}).setdefault(rep[:4], {"dps": 0.0, "eps": None, "done": False})
        e["dps"] += dps
        if rep.endswith("12-31"):
            e["eps"] = x.get("BASIC_EPS")
            e["done"] = (x.get("ASSIGN_PROGRESS") or "").startswith("实施")
    return m


# ---------------- 4. 技术指标 ----------------
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
    if len(c) < 20:
        return None
    w = c[-20:]
    m = sum(w) / 20
    sd = math.sqrt(sum((x - m) ** 2 for x in w) / 20)
    if sd <= 0:
        return None
    return (c[-1] - (m - 2 * sd)) / (4 * sd)


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
        sig = 3
    elif nb >= 2 and nm >= 1:
        sig = 2
    elif nb >= 1:
        sig = 1
    else:
        sig = 0

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


# ---------------- 5. 质量评分 ----------------
def clamp01(x):
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def seg(x, lo, hi):
    if x is None or hi == lo:
        return None
    return clamp01((x - lo) / (hi - lo))


def sseg(x, lo, hi):
    """带惩罚的区间映射：中心为中性，低于 lo 记负分（最多 -1），高于 hi 记满分"""
    if x is None or hi == lo:
        return None
    v = (x - lo) / (hi - lo)
    return -1.0 if v < -1 else (1.0 if v > 1 else v)


FIN_IND = ("银行", "保险", "证券", "多元金融", "信托")


def is_fin(ind):
    s = ind or ""
    return any(k in s for k in FIN_IND)


def quality_score(rev_yoy, pro_yoy, roe, debt, ocf_cov, payout, dc, fin):
    """
    0-100 透明加权分，四块各占 25：
      盈利 25：加权ROE
      成长 25：净利同比 60% + 营收同比 40%
      安全 25：资产负债率（金融业豁免）+ 现金流/分红覆盖
      分红 25：支付率合理区间 + 连续分红年数
    缺失项不计权，最后按已计权部分归一。
    """
    pts, w = 0.0, 0.0

    s = seg(roe, 0, 20)
    if s is not None:
        pts += 25 * s; w += 25

    # 成长：下滑必须真扣分（负分），否则"利润腰斩仍得高分"会违背风控初衷
    g, gw = 0.0, 0.0
    a = sseg(pro_yoy, -20, 40)
    if a is not None:
        g += a * 0.6; gw += 0.6
    b = sseg(rev_yoy, -10, 25)
    if b is not None:
        g += b * 0.4; gw += 0.4
    if gw > 0:
        pts += 25 * (g / gw); w += 25

    # 安全：金融业负债率口径不同、经营现金流与分红不可比，整块不参与
    sf, sw = 0.0, 0.0
    if not fin:
        if debt is not None:
            sf += (1 - seg(debt, 20, 80)) * 0.5; sw += 0.5
        cv = seg(ocf_cov, 0, 3)
        if cv is not None:
            sf += cv * 0.5; sw += 0.5
    if sw > 0:
        pts += 25 * (sf / sw); w += 25

    df, dw = 0.0, 0.0
    if payout is not None:
        if payout <= 0:
            p_sc = 0.3
        elif payout <= 70:
            p_sc = 1.0
        elif payout <= 100:
            p_sc = 1.0 - (payout - 70) / 30 * 0.6
        else:
            p_sc = max(0.0, 0.4 - (payout - 100) / 100)
        df += p_sc * 0.6; dw += 0.6
    if dc is not None:
        df += seg(dc, 0, 5) * 0.4; dw += 0.4
    if dw > 0:
        pts += 25 * (df / dw); w += 25

    if w <= 0:
        return None
    sc = pts / w * 100
    # 业绩硬刹车：利润大幅下滑的标的，无论其他项多好都不应给高分
    if pro_yoy is not None and pro_yoy < -30:
        sc = min(sc, 40.0)
    elif (pro_yoy is not None and pro_yoy < -10
          and rev_yoy is not None and rev_yoy < -10):
        sc = min(sc, 55.0)
    return round(max(sc, 0.0), 1)


# ---------------- 6. 组装 ----------------
QTR_FACTOR = {"03-31": 4.0, "06-30": 2.0, "09-30": 4.0 / 3.0, "12-31": 1.0}


def build_rows(lst, divs, klines, idx_ret, y10, lico_hist, bal, cf, latest_rd):
    fym = by_fiscal_year(divs)
    rows = []

    dates10 = [d for d, _ in y10]
    y10_cur = y10[-1][1] if y10 else None
    y10_date = y10[-1][0] if y10 else None
    w_mean = sum(v for _, v in y10) / len(y10) if y10 else None
    w_std = (math.sqrt(sum((v - w_mean) ** 2 for _, v in y10) / (len(y10) - 1))
             if y10 and len(y10) > 1 else None)

    qf = QTR_FACTOR.get(latest_rd[5:], 1.0)
    qdates = list(lico_hist.keys())

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
        complete = sorted([int(k) for k, v in fys.items() if k and v.get("eps") is not None],
                          reverse=True)
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

        cur_year = int(TODAY[:4])
        stale = 1 if (latest_fy and latest_fy < cur_year - 2) else 0
        if stale:
            y = 0.0
            payout = None

        dc = 0
        if complete:
            cur = max(complete)
            ys = set([int(k) for k in fys.keys()])
            while cur in ys and fys[str(cur)]["dps"] > 0:
                dc += 1
                cur -= 1

        # 股息率历史分位 + spread 历史
        anchors, spreads = [], []
        ds = [b[0] for b in fac["bars"]]
        cs = fac["closes"]
        start_anchor = (datetime.date.today() - datetime.timedelta(days=3 * 365)).isoformat()
        for i in range(len(ds) - 2, -1, -1):
            d = ds[i]
            if d < start_anchor:
                break
            if (len(ds) - i) % 21 != 1:
                continue
            c0 = cs[i]
            if c0 <= 0:
                continue
            fy_avail = None
            for fy in complete:
                if f"{fy + 1}-06-30" <= d:
                    fy_avail = fy
                    break
            if fy_avail is None:
                continue
            dps0 = fys[str(fy_avail)]["dps"]
            if dps0 > 0:
                a_y = dps0 / c0 * 100
                anchors.append(a_y)
                b10 = y10_on(y10, dates10, d) if y10 else None
                if b10 is not None:
                    spreads.append(a_y - b10)

        if len(anchors) >= 8 and y > 0:
            yp = sum(1 for a in anchors if a < y) / len(anchors) * 100
        else:
            yp = fac["pct"]

        spread = (y - y10_cur) if (y10_cur is not None and y > 0) else None
        spread_z = None
        if spread is not None and len(spreads) >= 10:
            sm = sum(spreads) / len(spreads)
            ss = math.sqrt(sum((v - sm) ** 2 for v in spreads) / (len(spreads) - 1))
            if ss > 0.01:
                spread_z = (spread - sm) / ss

        zone = 3 if yp >= 75 else 2 if yp >= 50 else 1 if yp >= 25 else 0

        # 基本面
        lico = lico_hist.get(qdates[0], {}).get(code) if qdates else None
        rev_yoy = lico.get("YSTZ") if lico else None
        pro_yoy = lico.get("SJLTZ") if lico else None
        roe = lico.get("WEIGHTAVG_ROE") if lico else None
        gm = lico.get("XSMLL") if lico else None
        ocfps = lico.get("MGJYXJJE") if lico else None

        b = bal.get(code)
        debt = b.get("DEBT_ASSET_RATIO") if b else None

        ind = r.get("f100") or ""
        fin = is_fin(ind)

        ocf_cov = None
        # 金融业经营现金流与分红不可比（保费/存贷现金流性质不同），不计算覆盖倍数
        if ocfps is not None and dps_latest > 0 and not fin:
            ocf_cov = (abs(ocfps) * qf) / dps_latest

        qser = []
        for qd in qdates:
            rec = lico_hist.get(qd, {}).get(code)
            if rec:
                qser.append([_rnd(rec.get("YSTZ"), 1), _rnd(rec.get("SJLTZ"), 1),
                             _rnd(rec.get("WEIGHTAVG_ROE"), 1)])
            else:
                qser.append([None, None, None])

        qs = quality_score(rev_yoy, pro_yoy, roe, debt, ocf_cov, payout, dc, fin)

        # 价值陷阱：高股息但基本面支撑不足
        trap = 0
        if y >= 6:
            bad = 0
            if pro_yoy is not None and pro_yoy < 0:
                bad += 1
            if payout is not None and payout > 100:
                bad += 1
            if ocf_cov is not None and ocf_cov < 1:
                bad += 1
            if debt is not None and not fin and debt > 70:
                bad += 1
            if bad >= 2:
                trap = 1

        rows.append([
            code, r.get("f14") or "", ind, round(price, 2),
            round(r.get("f3") or 0, 2), round(y, 2), round(payout, 1) if payout else 0,
            round(fac["vol"], 1), round(fac["beta"], 2) if fac["beta"] is not None else 1.0,
            round(fac["pct"], 1), round(yp, 1),
            round((r.get("f20") or 0) / 1e8, 0), round((r.get("f21") or 0) / 1e8, 0),
            dc, fac["sig"], fac["lamps"],
            round(r.get("f9"), 2) if isinstance(r.get("f9"), (int, float)) else 0,
            round(r.get("f23"), 2) if isinstance(r.get("f23"), (int, float)) else 0,
            latest_fy or 0, round(dps_latest, 4), stale,
            _rnd(rev_yoy, 1), _rnd(pro_yoy, 1), _rnd(roe, 2), _rnd(gm, 2),
            _rnd(debt, 2), _rnd(ocf_cov, 2),
            _rnd(spread, 2), _rnd(spread_z, 2), qs, trap,
            qser, 1 if fin else 0,
        ])
    print(f"[build] rows={len(rows)} skipped={skipped}", flush=True)

    macro = {"y10": y10_cur, "y10_date": y10_date,
             "y10_mean": _rnd(w_mean, 4), "y10_std": _rnd(w_std, 4),
             "y10_z": _rnd((y10_cur - w_mean) / w_std, 2) if (w_std and y10_cur is not None) else None,
             "y10_n": len(y10), "quarters": qdates, "latest_report": latest_rd}
    return rows, macro


def main():
    y10 = fetch_y10()
    print(f"[y10] cur={y10[-1] if y10 else None} n={len(y10)}", flush=True)

    lst = fetch_list()
    divs = fetch_dividends()

    latest_rd, lico0 = None, None
    for rd in recent_report_dates(6):
        recs = fetch_lico(rd)
        if len(recs) > 3000:
            latest_rd, lico0 = rd, recs
            break
    if not latest_rd:
        print("[fatal] 无可用季报数据", flush=True)
        sys.exit(1)
    print(f"[lico] latest={latest_rd} n={len(lico0)}", flush=True)

    qlist = [latest_rd]
    y, m = int(latest_rd[:4]), int(latest_rd[5:7])
    for _ in range(4):
        m -= 3
        if m <= 0:
            m += 12; y -= 1
        qlist.append(f"{y}-{m:02d}-{'31' if m in (3, 12) else '30'}")
    lico_hist = {}
    for rd in qlist:
        recs = lico0 if rd == latest_rd else fetch_lico(rd)
        lico_hist[rd] = {x["SECURITY_CODE"]: x for x in recs}
    print("[lico] " + ", ".join(f"{k}:{len(v)}" for k, v in lico_hist.items()), flush=True)

    bal = {x["SECURITY_CODE"]: x for x in fetch_balance(latest_rd)}
    cf = {x["SECURITY_CODE"]: x for x in fetch_cashflow(latest_rd)}
    print(f"[fin] balance={len(bal)} cashflow={len(cf)}", flush=True)

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

    rows, macro = build_rows(todo, divs, kcache, idx_ret, y10, lico_hist, bal, cf, latest_rd)
    rows.sort(key=lambda x: -x[11])

    meta = {"date": TODAY, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "count": len(rows), "universe": len(todo),
            "source": "东方财富公开行情/财务接口 + 中债登国债收益率曲线",
            "yield_basis": "最近完整财年 DPS ÷ 现价",
            "macro": macro}
    fn = os.path.join(OUT, "full.json")
    with open(fn, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "rows": rows}, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[done] {len(rows)} -> {fn}", flush=True)

    ksnap = {}
    for r in rows:
        bars = kcache.get(r[0])
        if not bars:
            continue
        seg_ = bars[-120:]
        arr = []
        for i, b in enumerate(seg_):
            o = seg_[i - 1][1] if i else b[1]
            arr.append([int(round(b[1] * 100)), int(round(o * 100)),
                        int(round(b[2] * 100)), int(round(b[3] * 100))])
        if len(arr) >= 25:
            ksnap[r[0]] = arr
    kf = os.path.join(OUT, "klines.json")
    with open(kf, "w", encoding="utf-8") as f:
        json.dump(ksnap, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[done] klines {len(ksnap)} -> {kf}", flush=True)
    print(f"[macro] 10Y={macro['y10']} ({macro['y10_date']}) z={macro['y10_z']}", flush=True)


if __name__ == "__main__":
    main()
