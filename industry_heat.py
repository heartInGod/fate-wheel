#!/usr/bin/env python3
"""行业景气分（长线仓选股参考热门行业，参数在 rules_params.json 的 sleeves.core.industry）。

口径：东财二级行业（与个股行情 f100、业绩报表 PUBLISHNAME 一致，如「半导体」「元件」「电子化学品Ⅱ」）。
每个行业算 5 项指标，在全部行业里各取百分位（0~100），按 weights 加权得到景气分：
  rev_cagr3   近年营收复合增速：最近年报 vs 基期年报（同口径样本：两期都有数据的公司加总）
  np_cagr3    近年归母净利复合增速（同上；基期或当期合计 ≤ 0 记缺失，排名垫底）
              基期 = max(base_year_min, 最近年报 − 3)：2022 年受疫情影响不可比，统一从 2023 年报起算
              （如 2026-11 用 2023→2025、2 年复合；2027 年报出来后 2023→2026；之后滚动 3 年）
  ytd_np_yoy  今年最新报告期累计归母净利同比（同口径样本加总；去年同期合计 ≤ 0 记缺失）
  ret_1y      行业指数近 1 年涨跌幅（东财行业板块日 K）
  flow20      近 20 个交易日主力净流入合计 / 成交额合计（%）
政策项（policy_signal.py，参数在 industry.policy）：景气分 = 上面 5 项加权的基础分 + 行业政策分（政策事件按类型半衰期
  快速衰减，默认一场 +3 的新发布会 = +10 分、10 天减半），排名与分档按最终分；盈利门槛不变，政策分不能替代业绩。
分档：
  hot  高景气：分数 ≥ hot_min_score，且 np_cagr3 > 0（基期以来净利增长）、ytd_np_yoy ≥ 0（盈利确实在增长，防止只因涨得多、资金热就放宽估值）
  cold 低景气：分数 ≤ cold_max_score
  neutral 其余；行业成员少于 min_members 或没匹配到的也按 neutral
长线仓里的用法（core_value.py）：hot 行业放宽 PE 上限 / 合理 PE 上限（hot 段），cold 行业收紧买入线（cold 段），
长线仓空位按 hot → neutral → cold、再按空间排序分配。

取数：财报按报告期整页拉全市场（东财 RPT_LICO_FN_CPD，每页 500 条），年报缓存永久、季报缓存 cache_days 天；
行业指数 K 线与资金流按行业逐个拉，结果整体缓存 cache_days 天（cache/industry/heat_yyyyMMdd.json）。

用法：python3 industry_heat.py [--refresh] [行业名 ...]   # 打印景气排名（默认前 30 + 指定行业）
"""
import datetime
import json
import os
import sys

import policy_signal as ps
from datasources._http import DataSourceError, http_json

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, "cache", "industry")
DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"
DEFAULTS = {"enabled": True, "min_members": 5, "hot_min_score": 70, "cold_max_score": 30, "cache_days": 1,
            "base_year_min": 2023, "span_years": 3,
            "weights": {"rev_cagr3": 0.15, "np_cagr3": 0.25, "ytd_np_yoy": 0.2, "ret_1y": 0.2, "flow20": 0.2},
            "hot": {"pe_max": 80, "pe_fair_max": 60}, "cold": {"buy_upside_pct": 40}}
TIER_NAME = {"hot": "高景气", "neutral": "中性", "cold": "低景气"}
METRIC_NAME = {"rev_cagr3": "营收CAGR", "np_cagr3": "净利CAGR", "ytd_np_yoy": "今年净利同比",
               "ret_1y": "1年指数涨幅", "flow20": "20日主力净流入占比"}


def cfg_of(params_core):
    c = dict(DEFAULTS, **{k: v for k, v in ((params_core or {}).get("industry") or {}).items() if not k.startswith("_")})
    c["weights"] = dict(c.get("weights") or DEFAULTS["weights"])
    for k in ("hot", "cold"):
        c[k] = dict(DEFAULTS[k], **(c.get(k) or {}))
    return c


def _f(v):
    try:
        x = float(v)
        return x if x == x and abs(x) != float("inf") else None
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------- 报告期
def periods(today=None, base_year_min=2023, span_years=3):
    """(最近年报, 基期年报, 最新季报, 去年同期)。年报 4 月底前出齐，季报按披露截止日（4/30、8/31、10/31）。
    基期 = max(base_year_min, 最近年报 − span_years)，且至少比最近年报早 1 年。"""
    t = today or datetime.date.today()
    y = t.year
    last_ann = y - 1 if t >= datetime.date(y, 5, 1) else y - 2
    if t >= datetime.date(y, 11, 1):
        q = f"{y}-09-30"
    elif t >= datetime.date(y, 9, 1):
        q = f"{y}-06-30"
    elif t >= datetime.date(y, 5, 1):
        q = f"{y}-03-31"
    else:
        q = f"{y - 1}-09-30"
    q_prev = f"{int(q[:4]) - 1}{q[4:]}"
    base = min(max(base_year_min, last_ann - span_years), last_ann - 1)
    return f"{last_ann}-12-31", f"{base}-12-31", q, q_prev


def span_label(p):
    """「23→25」这样的基期标签。p: periods() 结果或 build() 的 periods dict。"""
    a, b = (p["ann"], p["ann_base"]) if isinstance(p, dict) else (p[0], p[1])
    return f"{b[2:4]}→{a[2:4]}"


def _cache_ok(path, days):
    try:
        age = datetime.datetime.now() - datetime.datetime.fromtimestamp(os.path.getmtime(path))
        return days is None or age.days < days
    except OSError:
        return False


def fetch_period(date, cache_days=1, refresh=False):
    """全市场某报告期：code → [营收, 归母净利, 行业]。年报永久缓存，其余 cache_days 天。"""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"fin_{date}.json")
    keep = None if date.endswith("12-31") and date[:4] < str(datetime.date.today().year) else cache_days
    if not refresh and _cache_ok(path, keep):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    out, page, pages = {}, 1, 1
    while page <= pages:
        j = http_json(DC, params={"reportName": "RPT_LICO_FN_CPD", "filter": f"(REPORTDATE='{date}')",
                                  "columns": "SECURITY_CODE,TOTAL_OPERATE_INCOME,PARENT_NETPROFIT,PUBLISHNAME",
                                  "pageSize": "500", "pageNumber": str(page), "source": "WEB", "client": "WEB"},
                      timeout=30, retries=2)
        r = j.get("result") or {}
        pages = r.get("pages") or 0
        for d in r.get("data") or []:
            out[d["SECURITY_CODE"]] = [_f(d.get("TOTAL_OPERATE_INCOME")), _f(d.get("PARENT_NETPROFIT")), d.get("PUBLISHNAME")]
        page += 1
    if not out:
        raise DataSourceError(f"{date} 业绩报表为空")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    return out


def fetch_boards():
    """东财行业板块：名称 → BK 代码（含一/二/三级，按名称匹配二级行业）。"""
    out = {}
    for pn in range(1, 11):
        j = http_json("https://push2.eastmoney.com/api/qt/clist/get", params={
            "pn": pn, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f3", "fs": "m:90+t:2",
            "fields": "f12,f14"}, timeout=20, retries=2)
        d = (j.get("data") or {}).get("diff") or []
        d = list(d.values()) if isinstance(d, dict) else d
        for x in d:
            out[x["f14"]] = x["f12"]
        if len(d) < 100:
            break
    return out


def fetch_board_market(bk):
    """行业指数近 1 年涨跌幅、近 20 日主力净流入占成交额（%）。"""
    j = http_json("https://push2his.eastmoney.com/api/qt/stock/kline/get", params={
        "secid": f"90.{bk}", "klt": 101, "fqt": 1, "lmt": 260, "end": "20500101",
        "fields1": "f1,f2,f3", "fields2": "f51,f53,f57"}, timeout=20, retries=2)
    k = [x.split(",") for x in ((j.get("data") or {}).get("klines") or [])]
    if len(k) < 200:
        return {"ret_1y": None, "flow20": None}
    ret = (float(k[-1][1]) / float(k[0][1]) - 1) * 100
    amt = {x[0]: float(x[2]) for x in k[-20:]}
    j = http_json("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get", params={
        "secid": f"90.{bk}", "lmt": 20, "klt": 101, "fields1": "f1,f2,f3", "fields2": "f51,f52"}, timeout=20, retries=2)
    fl = [x.split(",") for x in ((j.get("data") or {}).get("klines") or [])]
    net = sum(float(x[1]) for x in fl if x[0] in amt)
    tot = sum(amt[x[0]] for x in fl if x[0] in amt)
    return {"ret_1y": round(ret, 1), "flow20": round(net / tot * 100, 2) if tot else None}


# ----------------------------------------------------------------- 计算
def _cagr(a, b, years):
    if not a or not b or a <= 0 or b <= 0:
        return None
    return ((b / a) ** (1 / years) - 1) * 100


def fundamentals(ann, ann0, q, q0, min_members=5, years=3):
    """按行业（以最新季报的行业归属为准）加总同口径样本，算基期以来的 CAGR 与今年净利同比。years=复合年数。"""
    ind_of = {c: v[2] for c, v in q.items() if v[2]}
    for src in (ann, ann0):
        for c, v in src.items():
            ind_of.setdefault(c, v[2])
    agg = {}
    for c, ind in ind_of.items():
        if not ind:
            continue
        a = agg.setdefault(ind, {"n": 0, "r0": 0, "r1": 0, "p0": 0, "p1": 0, "n3": 0, "q1": 0, "q0": 0, "nq": 0})
        a["n"] += 1
        x0, x1 = ann0.get(c), ann.get(c)
        if x0 and x1 and None not in (x0[0], x1[0], x0[1], x1[1]):
            a["r0"] += x0[0]; a["r1"] += x1[0]; a["p0"] += x0[1]; a["p1"] += x1[1]; a["n3"] += 1
        y0, y1 = q0.get(c), q.get(c)
        if y0 and y1 and y0[1] is not None and y1[1] is not None:
            a["q0"] += y0[1]; a["q1"] += y1[1]; a["nq"] += 1
    out = {}
    for ind, a in agg.items():
        yoy = (a["q1"] / a["q0"] - 1) * 100 if a["nq"] and a["q0"] > 0 else None   # 去年同期合计亏损（扭亏）记缺失
        out[ind] = {"members": a["n"], "small": a["n"] < min_members,
                    "rev_cagr3": _r(_cagr(a["r0"], a["r1"], years)), "np_cagr3": _r(_cagr(a["p0"], a["p1"], years)),
                    "ytd_np_yoy": _r(yoy), "n_sample3": a["n3"], "n_sample_q": a["nq"]}
    return out


def _r(x, nd=1):
    return None if x is None else round(x, nd)


def pct_rank(vals):
    """name → value（None 记 0 分）的百分位：最高 100，最低 0。"""
    xs = sorted(v for v in vals.values() if v is not None)
    n = len(xs)
    out = {}
    for k, v in vals.items():
        if v is None or n == 0:
            out[k] = 0.0
        elif n == 1:
            out[k] = 100.0
        else:
            lo = sum(1 for x in xs if x < v)
            eq = sum(1 for x in xs if x == v)
            out[k] = (lo + (eq - 1) / 2) / (n - 1) * 100
    return out


def _tier(v, cfg):
    """按最终景气分 v['score'] 分档（就地写 tier / tier_why / tier_base）。盈利门槛只看财务指标，政策分不能替代。"""
    grow = (v.get("np_cagr3") or 0) > 0 and v.get("ytd_np_yoy") is not None and v["ytd_np_yoy"] >= 0

    def t(sc):
        if sc >= cfg["hot_min_score"] and grow:
            return "hot", f"景气分 ≥ {cfg['hot_min_score']} 且基期以来、今年净利都在增长"
        if sc >= cfg["hot_min_score"]:
            return "neutral", "景气分够高，但净利未持续增长（基期以来 CAGR ≤ 0 或今年同比 < 0），不放宽估值"
        if sc <= cfg["cold_max_score"]:
            return "cold", f"景气分 ≤ {cfg['cold_max_score']}"
        return "neutral", ""
    v["tier"], v["tier_why"] = t(v["score"])
    v["tier_base"] = t(v["score_base"] if v.get("score_base") is not None else v["score"])[0]
    return v


def score(rows, cfg, policy=None):
    """rows: 行业 → 指标 dict。就地写入 score_base / policy_pts / score / rank / tier，返回 rows。
    score_base = 5 项指标百分位加权；policy（policy_signal.compute 的结果）给的行业政策分加在上面得 score（截到 0~100），
    排名与分档都按 score。成员太少的行业不参与排名、记 neutral。"""
    cfg = dict(DEFAULTS, **(cfg or {}))
    w = cfg["weights"]
    pind = (policy or {}).get("industries") or {}
    pool = {k: v for k, v in rows.items() if not v.get("small")}
    ranks = {m: pct_rank({k: v.get(m) for k, v in pool.items()}) for m in w}
    tw = sum(w.values()) or 1
    for k, v in rows.items():
        pe = pind.get(k)
        v["policy_pts"] = (pe or {}).get("pts", 0.0)
        v["policy_brief"] = ps.brief(pe) if pe else ""
        if k not in pool:
            v.update(score=None, score_base=None, rank=None, tier="neutral", tier_base="neutral",
                     tier_why=f"成员 {v.get('members')} 家 < {cfg['min_members']} 家，不评分")
            continue
        v["pct"] = {m: round(ranks[m][k]) for m in w}
        v["score_base"] = round(sum(ranks[m][k] * w[m] for m in w) / tw, 1)
        v["score"] = round(max(0.0, min(100.0, v["score_base"] + v["policy_pts"])), 1)
    order = sorted(pool, key=lambda k: -rows[k]["score"])
    border = sorted(pool, key=lambda k: -rows[k]["score_base"])
    brank = {k: i for i, k in enumerate(border, 1)}
    for i, k in enumerate(order, 1):
        v = rows[k]
        v["rank"], v["of"], v["rank_base"] = i, len(order), brank[k]
        _tier(v, cfg)
    return rows


def stock_view(hv, sp, cfg=None):
    """个股口径：行业景气 + 个股政策分（policy_signal 的 stocks 项）。返回新 dict（不改行业行），排名沿用行业的。"""
    if not hv or hv.get("score") is None or not sp or not sp.get("pts"):
        return hv
    cfg = dict(DEFAULTS, **(cfg or {}))
    v = dict(hv, stock_policy_pts=sp["pts"], stock_policy_brief=ps.brief(sp),
             score=round(max(0.0, min(100.0, hv["score"] + sp["pts"])), 1))
    v["score_base"] = hv.get("score_base", hv["score"])
    v["policy_pts"] = round((hv.get("policy_pts") or 0) + sp["pts"], 1)
    return _tier(v, cfg)


def policy_of(cfg, today=None, errors=None):
    """按当天日期算政策分（不缓存：衰减按天变）。policy.enabled=false 或文件缺失 → None。"""
    pc = ps.cfg_of(cfg)
    if not pc.get("enabled"):
        return None
    try:
        evs = ps.load_events(pc)
    except (OSError, ValueError) as e:
        if errors is not None:
            errors.append(f"政策事件文件：{e}（政策分按 0）")
        return None
    bad = ps.validate(evs)
    if bad and errors is not None:
        errors.append("政策事件校验：" + "；".join(bad[:5]))
    return ps.compute(evs, today, pc)


def build(cfg=None, refresh=False, today=None, errors=None):
    """拉数 + 打分，返回 {asof, periods, industries: 名称 → 指标}；当日缓存。"""
    cfg = dict(DEFAULTS, **(cfg or {}))
    errors = errors if errors is not None else []
    os.makedirs(CACHE, exist_ok=True)
    t = today or datetime.date.today()
    path = os.path.join(CACHE, f"heat_{t.strftime('%Y%m%d')}.json")
    pol = policy_of(cfg, t, errors)
    if not refresh and _cache_ok(path, cfg["cache_days"]):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("periods") == _pdict(periods(t, cfg["base_year_min"], cfg["span_years"])):
            score(data["industries"], cfg, pol)   # 参数/政策事件可能改过：重算分数与分档（指标不变）
            data["policy"] = pol
            return data
    p = periods(t, cfg["base_year_min"], cfg["span_years"])
    ann, ann0, q, q0 = (fetch_period(d, cfg["cache_days"], refresh) for d in p)
    rows = fundamentals(ann, ann0, q, q0, cfg["min_members"], years=int(p[0][:4]) - int(p[1][:4]))
    try:
        boards = fetch_boards()
    except DataSourceError as e:
        errors.append(f"行业板块列表：{e}")
        boards = {}
    miss = []
    for ind, v in rows.items():
        bk = boards.get(ind)
        v["bk"] = bk
        if v.get("small"):
            continue
        if not bk:
            miss.append(ind)
            v.update(ret_1y=None, flow20=None)
            continue
        try:
            v.update(fetch_board_market(bk))
        except DataSourceError as e:
            errors.append(f"行业 {ind}({bk}) 行情：{e}")
            v.update(ret_1y=None, flow20=None)
    if miss:
        errors.append(f"{len(miss)} 个行业没匹配到东财板块（指数涨幅/资金流记缺失）：{'、'.join(miss[:10])}")
    score(rows, cfg, pol)
    data = {"asof": t.strftime("%Y%m%d"), "periods": _pdict(p),
            "industries": rows}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    data["policy"] = pol   # 政策分按天衰减，不进缓存（读缓存时重算）
    return data


def _pdict(p):
    return {"ann": p[0], "ann_base": p[1], "q": p[2], "q_prev": p[3]}


def note(ind, v, cfg=None, span=None):
    """分类理由里用的一句话，如「半导体｜景气 82.5 分（3/120）｜3年净利CAGR 25%…｜高景气：PE 上限 80、合理 PE 上限 60」。"""
    cfg = dict(DEFAULTS, **(cfg or {}))
    if not v:
        return f"{ind or '未知行业'}｜无行业景气数据（按中性）"
    if v.get("score") is None:
        return f"{ind}｜{v.get('tier_why') or '未评分'}（按中性）"
    pp = v.get("policy_pts") or 0
    parts = [f"{ind}｜景气 {v['score']:.0f} 分（第 {v['rank']}/{v['of']}）"
             + (f"＝基础 {v['score_base']:.0f} + 政策 {pp:+.1f}" if pp and v.get("score_base") is not None else "")]
    for m, unit in (("np_cagr3", "%"), ("rev_cagr3", "%"), ("ytd_np_yoy", "%"), ("ret_1y", "%"), ("flow20", "%")):
        x = v.get(m)
        lab = METRIC_NAME[m] + (f"({span})" if span and m in ("np_cagr3", "rev_cagr3") else "")
        parts.append(f"{lab} {'-' if x is None else f'{x:+.1f}{unit}'}")
    for b in (v.get("policy_brief"), v.get("stock_policy_brief") and "个股" + v["stock_policy_brief"]):
        if b:
            parts.append(b)
    if v.get("tier_base") and v["tier_base"] != v.get("tier"):
        parts.append(f"档位因政策由{TIER_NAME[v['tier_base']]}变为{TIER_NAME[v['tier']]}（政策分衰减后会回落，需业绩验证）")
    t = v.get("tier")
    if t == "hot":
        h = cfg["hot"]
        parts.append(f"高景气：PE 上限放宽到 {h.get('pe_max')}、合理 PE 上限 {h.get('pe_fair_max')}，长线仓空位优先")
    elif t == "cold":
        parts.append(f"低景气：买入线收紧到空间 ≥ {cfg['cold'].get('buy_upside_pct')}%，长线仓空位排后")
    else:
        parts.append("中性" + (f"（{v['tier_why']}）" if v.get("tier_why") else ""))
    return "｜".join(parts)


def main(argv):
    import signal_store as st
    cfg = cfg_of((st.load_params().get("sleeves") or {}).get("core"))
    errors = []
    data = build(cfg, refresh="--refresh" in argv, errors=errors)
    want = [a for a in argv if not a.startswith("--")]
    rows = data["industries"]
    order = sorted([k for k in rows if rows[k].get("score") is not None], key=lambda k: -rows[k]["score"])
    print(f"行业景气 {data['asof']}（年报 {data['periods']['ann']} vs {data['periods']['ann_base']}；"
          f"今年 {data['periods']['q']} vs {data['periods']['q_prev']}）")
    for k in order[:30] + [k for k in want if k in rows and k not in order[:30]]:
        print(" ", note(k, rows[k], cfg, span_label(data["periods"])))
    for e in errors:
        print("  !", e)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
