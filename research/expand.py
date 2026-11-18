"""社保持仓补充候选：从社保基金持仓里挑「长线条件全过 + 估值在买入区」的股票，按 质量/空间/短期动能 打分，补进 watchlist。

口径与 core_value.py review 完全一致（同一套 screen / valuation / 行业景气分档），保证加进去的复查后就是候选区里的买入区股票。
用法：python3 research/expand.py [需要补充的只数，默认 27]   → 写 research/社保补充_2026Q3.{md,csv,json}，不改 watchlist.csv
     python3 research/expand.py --apply                      → 把上次结果追加到 watchlist.csv（只追加，不改已有行）
数据缓存在 /tmp/sb/（社保 5 期持仓），不进仓库。
"""
import collections
import csv
import datetime
import json
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "common"))
from datamul import data_path  # noqa: E402
import core_value as cv  # noqa: E402
import industry_heat as ih  # noqa: E402
from datasources import capital, quotes  # noqa: E402
from datasources._http import http_json  # noqa: E402

TMP = "/tmp/sb"
OUT = os.path.join(ROOT, "research", "社保补充_2026Q3")
Q = ["2026-09-30", "2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30"]
TODAY = datetime.date.today().isoformat()
IND_CAP = 3   # 同一细分行业（含已有自选）最多几只


def pull_holdings():
    os.makedirs(TMP, exist_ok=True)
    H = {}
    for d in Q:
        p = os.path.join(TMP, d + ".json")
        if not os.path.exists(p):
            rows, pg = [], 1
            while True:
                u = (f"https://data.eastmoney.com/dataapi/zlsj/list?date={d}&type=3&zjc=0&sortField=HOLD_VALUE"
                     f"&sortDirec=1&pageNum={pg}&pageSize=500")
                data = http_json(u, headers={"Referer": "https://data.eastmoney.com/zlsj/"}, timeout=30).get("data") or []   # 走 _http 限速
                rows += data
                if len(data) < 500:
                    break
                pg += 1
            json.dump(rows, open(p, "w"), ensure_ascii=False)
        H[d] = {r["SECURITY_CODE"]: r for r in json.load(open(p))}
    return H


def tech_flow(c):
    t, fl = {}, None
    try:
        k = quotes.kline(c, "day", 80)
        cl = [r["close"] for r in k]
        if len(cl) >= 65:
            ma = lambda n, off=0: sum(cl[len(cl) - n - off:len(cl) - off]) / n  # noqa: E731
            t = {"px": cl[-1], "ma20": ma(20), "ma60": ma(60), "ma20_5": ma(20, 5), "chg5": (cl[-1] / cl[-6] - 1) * 100,
                 "chg20": (cl[-1] / cl[-21] - 1) * 100, "high20": max(r["high"] for r in k[-20:]),
                 "low20": min(r["low"] for r in k[-20:])}
    except Exception:  # noqa: BLE001
        pass
    try:
        f = capital.fund_flow_daily(c, 10)
        fl = sum(x["main_net_pct"] or 0 for x in f) / len(f)
    except Exception:  # noqa: BLE001
        pass
    return c, t, fl


def short_score(t, fl, cha):
    """短期动能 0~100：趋势 30 + 位置 30 + 主力资金 25 + 社保动作 15。"""
    if not t:
        return 0, ["无 K 线"]
    s, why = 0, []
    up = t["ma20"] >= t["ma60"]
    rising = t["ma20"] > t["ma20_5"]
    s += (15 if up else 0) + (15 if rising else 0)
    why.append(("MA20>MA60" if up else "MA20<MA60") + ("·MA20 上行" if rising else "·MA20 走平/下行"))
    v = (t["px"] / t["ma20"] - 1) * 100
    pos = 30 if -3 <= v <= 5 else 22 if 5 < v <= 9 or -6 <= v < -3 else 10 if 9 < v <= 14 or -10 <= v < -6 else 0
    s += pos
    why.append(f"距 MA20 {v:+.1f}%" + ("（回踩/贴线）" if -3 <= v <= 5 else "（偏离大）" if abs(v) > 9 else ""))
    if fl is not None:
        s += max(0, min(25, 12.5 + fl * 2.5))
        why.append(f"10 日主力净占比 {fl:+.1f}%")
    s += {"新进": 15, "增仓": 15, "不变": 8}.get(cha, 0)
    return round(s, 1), why


def run(need):
    params = json.load(open(data_path("rules_params.json"), encoding="utf-8"))
    sc = cv.sleeve_cfg(params)
    cfg = dict(cv.DEFAULTS, **((sc.get("core") or {}).get("screen") or {}), **((sc.get("core") or {}).get("valuation") or {}))
    icfg = ih.cfg_of(sc.get("core"))
    H = pull_holdings()
    pos, _ = cv.positions_and_account()
    have = set(pos) | set(cv.watch_codes())
    allc = set().union(*[set(H[d]) for d in Q])
    pool = sorted(c for c in allc if c not in have and c[0] in "036")
    print("社保近 5 期持有过", len(allc), "Q3", len(H[Q[0]]), "去掉持仓/已在自选", len(pool))
    q = cv.fetch_quotes(pool)
    pre = []
    for c in pool:
        x = q.get(c) or {}
        if not x or "ST" in (x.get("name") or "") or (x.get("cap_yi") or 0) < cfg["min_cap_yi"]:
            continue
        pe = x.get("pe_ttm")
        if pe is None or pe <= 0 or pe > 80:
            continue
        pre.append(c)
    print("市值/PE 粗筛后", len(pre))
    errs = []
    hd = ih.build(icfg, errors=errs)
    heat, pol = hd["industries"], hd.get("policy")

    def one(c):
        try:
            return c, cv.fetch_fin(c), cv.fetch_forecast(c), cv.fetch_pe_hist(c)
        except Exception as e:  # noqa: BLE001
            return c, None, None, str(e)

    with ThreadPoolExecutor(max_workers=4) as ex:
        got = list(ex.map(one, pre))
    ok, fail = [], collections.Counter()
    for c, fin, fc, peh in got:
        if fin is None:
            fail["取数失败"] += 1
            continue
        x = q[c]
        hv = ih.stock_view(heat.get(x.get("industry")), ((pol or {}).get("stocks") or {}).get(c), icfg)
        tier = (hv or {}).get("tier", "neutral")
        ecfg = cv.tier_cfg(cfg, tier, icfg)
        passed, why, m = cv.screen(x, fin, fc, ecfg)
        if not passed:
            fail[why[0].split(" ")[0]] += 1
            continue
        v = cv.valuation(x["price"], fc, peh, ecfg)
        if v.get("zone") != "buy":
            fail["估值不在买入区(" + str(v.get("zone")) + ")"] += 1
            continue
        last = next(d for d in Q if c in H[d])
        h = dict(H[last][c], HOLDCHA=H[last][c].get("HOLDCHA") if last == Q[0] else f"{last[:7]} 后退出")
        ok.append(dict(code=c, name=x["name"].replace(" ", ""), ind=x.get("industry"), tier=tier, hv=hv or {},
                       n=sum(c in H[d] for d in Q), cha=h.get("HOLDCHA"), sb_val=(h.get("HOLD_VALUE") or 0) / 1e8,
                       price=x["price"], cap=x["cap_yi"], pe=x["pe_ttm"], **m, **{k: v[k] for k in ("target", "upside_pct", "eps", "pe_fair", "orgs", "capped_by_aim")}))
    print("长线条件 + 买入区 通过", len(ok), "未通过原因", dict(fail.most_common(8)))
    with ThreadPoolExecutor(max_workers=4) as ex:
        tf = {c: (t, fl) for c, t, fl in ex.map(tech_flow, [x["code"] for x in ok])}
    z = lambda v, lo, hi: max(0.0, min(1.0, (v - lo) / (hi - lo)))  # noqa: E731
    for x in ok:
        roe = ((x["roe_prev"] + x["roe_last"]) / 2 + x["roe_ytd_ann"]) / 2
        x["q"] = round(100 * (0.45 * z(roe, 10, 30) + 0.25 * z(min(x["np_yoy"], 80), 0, 80) + 0.15 * z(min(x["rev_yoy"], 40), 0, 40)
                              + 0.15 * z(x["n"], 1, 5)), 1)
        x["u"] = round(100 * z(min(x["upside_pct"], 80), 30, 80), 1)   # 买入线是 30%（低景气 40%），80% 以上不再加分
        t, fl = tf.get(x["code"], ({}, None))
        x["s"], x["s_why"] = short_score(t, fl, x["cha"])
        x["tech"], x["flow"] = t, fl
        x["score"] = round(0.3 * x["q"] + 0.3 * x["u"] + 0.4 * x["s"], 1)
    ok.sort(key=lambda x: -x["score"])
    cnt = collections.Counter(r["行业"] for r in csv.DictReader(open(data_path("watchlist.csv"), encoding="utf-8-sig")))
    pick, spare = [], []
    for x in ok:
        if len(pick) < need and cnt[x["ind"]] < IND_CAP and x["s"] >= 40:
            cnt[x["ind"]] += 1
            pick.append(x)
        else:
            spare.append(x)
    # 严格条件选不满时补位：先补短期动能 ≥ 40 的（放开行业上限），还不够再按综合分
    for x in sorted(spare, key=lambda x: (x["s"] < 40, -x["score"])):
        if len(pick) >= need:
            break
        pick.append(x)
        spare.remove(x)
    pick.sort(key=lambda x: -x["score"])
    json.dump({"pick": pick, "spare": spare, "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")},
              open(OUT + ".json", "w"), ensure_ascii=False, indent=1, default=str)
    write_md(pick, spare, len(H[Q[0]]), len(pool), len(pre), len(ok), fail)
    for i, x in enumerate(pick, 1):
        print(f"{i:2} {x['code']} {x['name']:6} {x['ind']:8} 分{x['score']:5} 质{x['q']:5} 空{x['upside_pct']:5}% 短{x['s']:5} {x['cha']} {'；'.join(x['s_why'])}")
    print("备选", [(x["code"], x["name"], x["score"]) for x in spare[:10]])


def focus(x):
    return (f"ROE {x['roe_prev']:.0f}/{x['roe_last']:.0f}/今年{x['roe_ytd_ann']:.0f}%｜净利+{x['np_yoy']:.0f}%｜PE {x['pe']:.0f}｜"
            f"目标 {x['target']} 空间 {x['upside_pct']:.0f}%｜社保{x['n']}期·{x['cha']}｜短线：{'；'.join(x['s_why'][:2])}")


def write_md(pick, spare, n_all, n_pool, n_pre, n_ok, fail):
    TN = {"hot": "高景气", "neutral": "中性", "cold": "低景气"}
    L = [f"# 社保持仓补充候选 {len(pick)} 只（2026 三季报，{TODAY}）", "",
         f"目的：把候选区从 23 只补到 50 只。数据：东方财富主力持仓（社保，报告期 2026-09-30，共 {n_all} 只）、个股财报、一致预期、5 年 PE 历史、日 K 与主力资金流（取数 {TODAY}）。", "",
         "## 筛选口径", "",
         f"1. 社保近 5 个报告期（25Q3~26Q3）任一期持有，剔除 ST、现有持仓和已在自选的 → {n_pool} 只；市值 ≥ 150 亿、PE(TTM) 0~80 粗筛 → {n_pre} 只。",
         "2. **长线条件**（与 core_value.py 完全同一套）：ROE 前年 ≥ 10%、去年 ≥ 12%、今年年化 ≥ 10%；营收、净利同比 ≥ 0；PE 0~50（高景气行业 0~80）；负债率 ≤ 70%（金融豁免）；机构覆盖 ≥ 3 家。",
         f"3. **上涨空间**：目标价 = 一致预期 EPS × 合理 PE（5 年 PE 中位数，限 8~40，高景气 60），不超过机构最高目标价；空间 ≥ 30%（低景气行业 40%）才算买入区。2、3 两步后剩 {n_ok} 只。",
         "4. **综合分** = 质量 30% + 空间 30% + 短期动能 40%：",
         "   - 质量：ROE 水平 45、净利增速 25、营收增速 15、社保持有期数 15；",
         "   - 空间：30% → 0 分，80% 及以上满分；",
         "   - 短期动能：MA20 在 MA60 上方 15、MA20 上行 15、距 MA20 −3%~+5%（回踩不破或贴线）30、近 10 日主力净占比 25、社保本季新进/增仓 15。",
         f"5. 优先选短期动能 ≥ 40、同一细分行业（含已有自选）不超过 {IND_CAP} 只的，按综合分取；不够 {len(pick)} 只时先补短期动能 ≥ 40 的（放开行业上限），再按综合分补。", "",
         "未通过原因分布：" + "，".join(f"{k} {v}" for k, v in fail.most_common(8)), "",
         "## 名单", "",
         "| # | 代码 | 名称 | 行业（景气） | 综合 | 质量 | 空间 | 短期 | ROE 前年/去年/今年 | 净利 / 营收同比 | PE | 现价 → 目标价 | 社保 | 短期依据 |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, x in enumerate(pick, 1):
        L.append(f"| {i} | {x['code']} | {x['name']} | {x['ind']}（{TN.get(x['tier'], '中性')}） | {x['score']} | {x['q']} | {x['upside_pct']:.0f}% | {x['s']} | "
                 f"{x['roe_prev']:.1f}/{x['roe_last']:.1f}/{x['roe_ytd_ann']:.1f} | {x['np_yoy']:.0f}% / {x['rev_yoy']:.0f}% | {x['pe']:.0f} | "
                 f"{x['price']} → {x['target']}{'（机构上限）' if x['capped_by_aim'] else ''} | {x['n']}期·{x['cha']}·{x['sb_val']:.1f}亿 | {'；'.join(x['s_why'])} |")
    if spare:
        L += ["", "## 备选（通过长线条件且在买入区，但分数靠后 / 行业已满 / 短期动能不足）", "",
              "、".join(f"{x['name']}({x['code']} {x['score']})" for x in spare[:20])]
    L += ["", "## 需要留意", "",
          "- 目标价用一致预期 EPS，周期股（有色、化工、航运）的预期往往偏乐观，空间可能高估。",
          "- 短期动能只是技术面 + 资金面的概率倾向，不是上涨保证；社保持仓是季末快照，披露滞后约 1 个月。",
          "- 加入自选后由盘中规则找买点（👀自选低吸 / 👀自选突破），长线仓空位按行业景气 → 空间排序分配。", "",
          "以上为信息整理，不构成投资建议。"]
    open(OUT + ".md", "w", encoding="utf-8").write("\n".join(L) + "\n")
    with open(OUT + ".csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["代码", "名称", "行业", "景气", "综合分", "质量分", "空间%", "短期分", "ROE前年", "ROE去年", "ROE今年年化", "净利同比%", "营收同比%",
                    "PE_TTM", "现价", "目标价", "社保期数", "社保变动", "社保持仓亿", "选中"])
        for x in pick + spare:
            w.writerow([x["code"], x["name"], x["ind"], TN.get(x["tier"], "中性"), x["score"], x["q"], x["upside_pct"], x["s"], x["roe_prev"], x["roe_last"],
                        x["roe_ytd_ann"], round(x["np_yoy"], 1), round(x["rev_yoy"], 1), x["pe"], x["price"], x["target"], x["n"], x["cha"],
                        round(x["sb_val"], 2), "是" if x in pick else "备选"])


def apply():
    d = json.load(open(OUT + ".json"))
    path = data_path("watchlist.csv")
    have = {r["代码"] for r in csv.DictReader(open(path, encoding="utf-8-sig"))}
    add = [x for x in d["pick"] if x["code"] not in have]
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        for x in add:
            w.writerow([x["code"], x["name"], x["ind"], TODAY, "社保补充2026Q3", focus(x)])
    print("追加", len(add), "只到 watchlist.csv")


if __name__ == "__main__":
    if "--apply" in sys.argv:
        apply()
    else:
        run(int(next((a for a in sys.argv[1:] if a.isdigit()), 27)))
