#!/usr/bin/env python3
"""长线仓（价值仓）：参考基金经理的做法，按「一致预期 EPS × 合理 PE」定目标价，低价买、高价卖、长期持有。

资金分两仓（rules_params.json 的 sleeves 段）：
  * 长线仓 core：最多用总资产 sleeves.core.budget_frac（80%），最多 auto.sizing.max_positions 只，单只 ≤ position_cap；
    不盘中追涨杀跌，只按目标价分区操作，每周复查一次 + 财报/重大公告后复查；盘中只允许做T（交易仓规则）。
  * 交易仓 trade：最多用总资产 sleeves.trade.budget_frac（20%），走盘中规则（signals_engine.py），不受只数上限约束。

归属：sleeves.trade.force 里的代码固定归交易仓；sleeves.core.force 固定归长线仓；其余个股满足下面的选股条件且
有一致预期（机构数 ≥ min_orgs）的归长线仓，否则归交易仓（港股、亏损股都归交易仓）。
ETF 走指数估值法（etf_value.py，参数在 sleeves.core.etf）：指数盈利长期增长、估值历史可比、规模/成交额/费率达标，
且当前在买入区/持有区的进长线仓；同一估值指数只放一只。
已在长线仓的持仓不会因为某个季度没过筛选就自动移出，而是标「逻辑复查」，由人决定是否卖出。

选股条件（与 research/screen.py 的社保优选口径一致，去掉社保持有要求）：
  总市值 ≥ min_cap_yi 亿；最近年报 ROE ≥ roe_last_min、上一年报 ≥ roe_prev_min；今年累计 ROE 年化 ≥ roe_ytd_min；
  最新报告期营收、归母净利同比 ≥ 0；PE(TTM) 在 (0, pe_max]；非金融股资产负债率 ≤ debt_max。

目标价：
  EPS 取机构一致预期：7 月起用下一年度（如 2026-11 用 2027E），之前用当年度；
  合理 PE = 自身近 5 年 PE(TTM) 中位数（只取正值），限制在 [pe_fair_min, pe_fair_max]；
  目标价 = EPS × 合理 PE，且不高于机构最高目标价（cap_by_aim_max，一致预期偏乐观时封顶）；上涨空间 = 目标价 / 现价 − 1。
分区与动作：
  空间 ≥ buy_upside_pct      → 买入区：仓位低于 target_weight 的补到目标仓位（分 2 批，本次给第一批）
  0 < 空间 < buy_upside_pct  → 持有区：不加不减
  现价 ≥ 目标价              → 卖出区：卖出一半
  现价 ≥ 目标价 × sell_all_ratio → 清仓区：全部卖出
  持仓成本约束（cost_gate，只对已持有的长线仓）：
    保本价 = 成本价 + 卖出费用（佣金/印花税/过户费，见 fees.py；成本价按券商口径已含买入费用，cost_includes_buy_fee）；
    深套（现价 < 成本×(1−deep_loss_pct%) 且目标价 < 保本价）→ 不加仓，只做T降成本；
    首批（市值 < 目标仓位一半）之后的加仓，要求现价 ≤ 成本×(1−add_below_cost_pct%)，否则摊薄无效、不加；
    卖出区/清仓区要求现价 ≥ 保本价×(1+sell_min_profit_pct%)，否则继续持有（逻辑复查已触发的除外）。
  逻辑复查（任一）：最新报告期归母净利同比 < 0；ROE 年化 < roe_ytd_min；一致预期 EPS 较上次复查下调 ≥ eps_cut_pct%；
  机构覆盖不足。复查不自动卖，由人看财报/公告后决定。
行业景气（industry_heat.py，参数在 sleeves.core.industry）：按东财二级行业算景气分（营收/净利 CAGR：2023 年报起算（2022 疫情年不可比），之后滚动 3 年；今年净利同比、
  行业指数 1 年涨幅、20 日主力净流入占比，各取全行业百分位加权）。
  高景气（分数 ≥ hot_min_score 且基期以来、今年净利都在增长）：PE 上限 → hot.pe_max、合理 PE 上限 → hot.pe_fair_max；
  低景气（分数 ≤ cold_max_score）：买入线 → cold.buy_upside_pct；
  长线仓空位按 高景气 → 中性 → 低景气、再按空间分配。行业数据拉取失败时全部按中性（即原口径）。
  政策加分（policy_signal.py，参数在 sleeves.core.industry.policy，事件库 policy_signals.json）：
    最终景气分 = 基础分 + 政策分；政策分按事件类型半衰期指数衰减（发布会 7 天、讲话 5 天、文件 20 天…），
    单行业 ±max_points 封顶；分档阈值作用于最终分，但高景气的盈利门槛不变（政策不能把净利下滑的行业抬成高景气）。
    个股 stocks 政策分只用于东财分类与主业不一致的公司。报告新增「政策信号」「个股建议总表」两节。

用法：
  python3 main.py core review [--dry-run]   # 每周复查：拉财报/一致预期/5 年 PE，写 core_auto.json 与 logs/core/yyyyMMdd.md
  python3 main.py core status               # 盘前：读 core_auto.json，用最新价重新分区（不联网拉财报），输出 JSON
定时任务不得手改 core_auto.json（由本脚本生成）。
"""
import copy
import datetime
import json
import math
import os
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor

import etf_value
import fees
import industry_heat as ih
import policy_signal as ps
import signal_store as st
import risk_core as rc
import portfolio_risk as pr
from datasources._http import DataSourceError, http_json, normalize

from fw.paths import ROOT
from fw.datamul import data_path
LOG_DIR = os.path.join(ROOT, "logs", "core")
SNAPS = [os.path.join(ROOT, "cache", "snapshot_intraday.json"), os.path.join(ROOT, "cache", "snapshot_premarket.json")]
DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"
FIN_INDUSTRY = ("银行", "证券", "保险", "多元金融")
ZONE_NAME = {"buy": "买入区", "hold": "持有区", "sell_half": "卖出区（卖一半）", "sell_all": "清仓区", "na": "无法定价"}


def _f(v):
    try:
        x = float(v)
        return x if x == x and abs(x) != float("inf") else None
    except (TypeError, ValueError):
        return None


def sleeve_cfg(params):
    return params.get("sleeves") or {}


def core_path(params):
    return os.path.join(ROOT, (sleeve_cfg(params).get("core") or {}).get("file", "core_auto.json"))


def remember_sleeves(core):
    """仅在成功 review 后保存归属备份；不保存动作，不修改持仓。"""
    if not isinstance(core, dict) or not isinstance(core.get("sleeves"), dict):
        return
    path = data_path("core_sleeves.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"asof": core.get("asof"), "sleeves": core["sleeves"]}, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, path)


def load_core(params, today=None):
    """过期保留归属并标记安全态；缺失读最后确认归属，绝不因取数失败改交易策略。"""
    cfg = sleeve_cfg(params).get("core") or {}
    try:
        with open(core_path(params), encoding="utf-8") as f:
            core = json.load(f)
        if not isinstance(core, dict) or not isinstance(core.get("sleeves"), dict) or not isinstance(core.get("stocks"), dict):
            raise ValueError("估值文件结构不完整")
    except (OSError, ValueError, TypeError) as exc:
        try:
            with open(data_path("core_sleeves.json"), encoding="utf-8") as f:
                saved = json.load(f)
            sl = saved.get("sleeves") if isinstance(saved, dict) else {}
            sl = sl if isinstance(sl, dict) else {}
        except (OSError, ValueError, TypeError):
            sl = {}
        return {"sleeves": sl, "stocks": {}, "_health": {"status": "missing", "block_add": True,
                "note": f"估值文件不可用（{type(exc).__name__}）：保留已确认归属，暂停新增，需复查"}}
    try:
        now = datetime.datetime.strptime(str(today or datetime.date.today().strftime("%Y%m%d")).replace("-", ""), "%Y%m%d").date()
        age = (now - datetime.datetime.strptime(str(core.get("asof", "")), "%Y%m%d").date()).days
        fresh = 0 <= age <= cfg.get("max_stale_days", 10)
    except (ValueError, TypeError):
        age, fresh = None, False
    core["_health"] = {"status": "fresh" if fresh else "stale", "age_days": age, "block_add": not fresh,
                       "note": "估值可用" if fresh else "估值过期或日期无效：保留归属，暂停新增，旧目标价不执行，需复查"}
    return core


def sleeve_of(code, params, core=None):
    """持仓归属：trade.force → 交易仓；core.force → 长线仓；否则看 core_auto.json；没有记录的按交易仓。"""
    sc = sleeve_cfg(params)
    code = str(code)
    if code in ((sc.get("trade") or {}).get("force") or []):
        return "trade"
    if code in ((sc.get("core") or {}).get("force") or []):
        return "core"
    entry = ((core or {}).get("sleeves") or {}).get(code) or {}
    return entry.get("sleeve", "trade") if isinstance(entry, dict) else "trade"


# ----------------------------------------------------------------- 取数
def _dc(report, flt, cols="ALL", size=20, sort=None):
    p = {"reportName": report, "columns": cols, "filter": flt, "pageSize": str(size), "source": "WEB", "client": "WEB"}
    if sort:
        p.update({"sortColumns": sort, "sortTypes": "-1"})
    j = http_json(DC, params=p, timeout=20, retries=2)
    return ((j.get("result") or {}).get("data")) or []


def fetch_quotes(codes):
    """东方财富批量行情 + 估值：现价、PE(TTM)、PB、总市值、行业、最新报告期 ROE/营收/净利同比、负债率。"""
    out = {}
    secids = [("1." if c[0] in "569" else "0.") + c for c in codes]
    for i in range(0, len(secids), 80):
        j = http_json("https://push2.eastmoney.com/api/qt/ulist.np/get", params={
            "fltt": "2", "secids": ",".join(secids[i:i + 80]),
            "fields": "f12,f14,f2,f20,f23,f37,f41,f46,f57,f100,f115"}, timeout=20, retries=2)
        for d in ((j.get("data") or {}).get("diff")) or []:
            out[d["f12"]] = {"name": d.get("f14"), "price": _f(d.get("f2")), "cap_yi": (_f(d.get("f20")) or 0) / 1e8,
                             "pb": _f(d.get("f23")), "pe_ttm": _f(d.get("f115")), "industry": d.get("f100"),
                             "debt_pct": _f(d.get("f57"))}
    return out


def fetch_fin(code):
    """最近 12 个报告期：ROE（加权，累计）、营收/归母净利同比。"""
    rows = _dc("RPT_LICO_FN_CPD", f'(SECURITY_CODE="{code}")',
               "REPORTDATE,WEIGHTAVG_ROE,YSTZ,SJLTZ,BASIC_EPS", 12, "REPORTDATE")
    return [{"date": r["REPORTDATE"][:10], "roe": _f(r.get("WEIGHTAVG_ROE")), "rev_yoy": _f(r.get("YSTZ")),
             "np_yoy": _f(r.get("SJLTZ")), "eps": _f(r.get("BASIC_EPS"))} for r in rows]


def fetch_forecast(code):
    rows = _dc("RPT_WEB_RESPREDICT", f'(SECURITY_CODE="{code}")', size=1)
    if not rows:
        return None
    r = rows[0]
    eps = {}
    for i in range(1, 5):
        y, v = r.get(f"YEAR{i}"), _f(r.get(f"EPS{i}"))
        if y and v is not None:
            eps[int(y)] = {"eps": v, "mark": r.get(f"YEAR_MARK{i}")}
    return {"orgs": r.get("RATING_ORG_NUM") or 0, "eps": eps,
            "aim_min": _f(r.get("DEC_AIMPRICEMIN")), "aim_max": _f(r.get("DEC_AIMPRICEMAX"))}


def fetch_pe_hist(code, years=5):
    rows = _dc("RPT_VALUEANALYSIS_DET", f'(SECURITY_CODE="{code}")', "TRADE_DATE,PE_TTM", 1300, "TRADE_DATE")
    cut = (datetime.date.today() - datetime.timedelta(days=365 * years)).isoformat()
    return [_f(r["PE_TTM"]) for r in rows if r["TRADE_DATE"][:10] >= cut and (_f(r.get("PE_TTM")) or 0) > 0]


# ----------------------------------------------------------------- 计算
def ytd_factor(date):
    return {"03-31": 4.0, "06-30": 2.0, "09-30": 4 / 3}.get(date[5:], 1.0)


def screen(q, fin, fc, cfg):
    """返回 (是否通过, 未通过原因 list, 指标 dict)。"""
    why = []
    m = {}
    latest = fin[0] if fin else {}
    annual = [r for r in fin if r["date"].endswith("12-31")]
    roe_last = annual[0]["roe"] if annual else None
    roe_prev = annual[1]["roe"] if len(annual) > 1 else None
    roe_ytd = latest.get("roe") * ytd_factor(latest["date"]) if latest.get("roe") is not None else None
    m.update({"report": latest.get("date"), "roe_last": roe_last, "roe_prev": roe_prev,
              "roe_ytd_ann": round(roe_ytd, 1) if roe_ytd is not None else None,
              "rev_yoy": latest.get("rev_yoy"), "np_yoy": latest.get("np_yoy"),
              "pe_ttm": q.get("pe_ttm"), "cap_yi": round(q.get("cap_yi") or 0), "debt_pct": q.get("debt_pct"),
              "industry": q.get("industry")})
    if (q.get("cap_yi") or 0) < cfg["min_cap_yi"]:
        why.append(f"市值 {q.get('cap_yi') or 0:.0f} 亿 < {cfg['min_cap_yi']} 亿")
    if roe_last is None or roe_prev is None or roe_ytd is None:
        why.append("ROE 数据缺失")
    else:
        if roe_last < cfg["roe_last_min"] or roe_prev < cfg["roe_prev_min"]:
            why.append(f"年报 ROE {roe_prev:.1f}/{roe_last:.1f}% 偏低")
        if roe_ytd < cfg["roe_ytd_min"]:
            why.append(f"今年 ROE 年化 {roe_ytd:.1f}% < {cfg['roe_ytd_min']}%")
    if latest.get("np_yoy") is None or latest["np_yoy"] < 0:
        why.append(f"归母净利同比 {latest.get('np_yoy') if latest.get('np_yoy') is not None else '缺失'}%")
    if latest.get("rev_yoy") is None or latest["rev_yoy"] < 0:
        why.append(f"营收同比 {round(latest['rev_yoy'], 1) if latest.get('rev_yoy') is not None else '缺失'}%")
    pe = q.get("pe_ttm")
    if pe is None or pe <= 0 or pe > cfg["pe_max"]:
        why.append(f"PE(TTM) {pe} 不在 0~{cfg['pe_max']}")
    ind = q.get("industry") or ""
    if q.get("debt_pct") is not None and q["debt_pct"] > cfg["debt_max"] and not any(k in ind for k in FIN_INDUSTRY):
        why.append(f"负债率 {q['debt_pct']:.0f}% > {cfg['debt_max']}%")
    if not fc or fc["orgs"] < cfg["min_orgs"]:
        why.append(f"机构覆盖 {fc['orgs'] if fc else 0} 家 < {cfg['min_orgs']} 家")
    return not why, why, m


TIER_ORDER = {"hot": 0, "neutral": 1, "cold": 2}


def tier_cfg(cfg, tier, icfg=None):
    """按行业景气分档覆盖筛选/估值参数：hot 用 icfg.hot（放宽 PE 上限、合理 PE 上限），cold 用 icfg.cold（收紧买入线）。"""
    icfg = icfg or {}
    if not icfg.get("enabled", True) or tier not in ("hot", "cold"):
        return cfg
    return dict(cfg, **(icfg.get(tier) or {}))


def eps_year(today=None):
    today = today or datetime.date.today()
    return today.year + 1 if today.month >= 7 else today.year


def valuation(price, fc, pe_hist, cfg, today=None):
    """目标价与分区。"""
    y = eps_year(today)
    e = ((fc or {}).get("eps") or {}).get(y) or ((fc or {}).get("eps") or {}).get(str(y))
    if not e or not e.get("eps") or e["eps"] <= 0 or not pe_hist or not price:
        return {"eps_year": y, "zone": "na", "why": "缺一致预期 EPS 或 PE 历史"}
    med = statistics.median(pe_hist)
    fair = min(max(med, cfg["pe_fair_min"]), cfg["pe_fair_max"])
    target = e["eps"] * fair
    raw = target
    aim_max = (fc or {}).get("aim_max")
    capped = bool(cfg.get("cap_by_aim_max", True) and aim_max and aim_max > 0 and target > aim_max)
    if capped:   # 一致预期 EPS 往往偏乐观（周期股尤甚）：不高于机构最高目标价
        target = aim_max
    up = (target / price - 1) * 100
    buy_below = target / (1 + cfg["buy_upside_pct"] / 100)
    sell_all = target * cfg["sell_all_ratio"]
    return {"eps_year": y, "eps": round(e["eps"], 3), "pe_med5y": round(med, 1), "pe_fair": round(fair, 1),
            "target": round(target, 2), "target_eps_pe": round(raw, 2), "capped_by_aim": capped, "buy_below": round(buy_below, 2), "sell_half_at": round(target, 2),
            "sell_all_at": round(sell_all, 2), "upside_pct": round(up, 1), "zone": zone_of(price, target, cfg),
            "aim_range": [fc.get("aim_min"), fc.get("aim_max")], "orgs": fc.get("orgs")}


def eps_revision(old, current):
    """同一预测年度保留历史基准（高水位），连续小幅下修不能重置基准。"""
    eps = _f(current.get("eps"))
    if not eps or eps <= 0:
        return None, None
    if old.get("eps_year") != current.get("eps_year"):
        return eps, 0.0
    reference = max(_f(old.get("eps_reference")) or 0, _f(old.get("eps")) or 0, eps)
    return reference, round(max(0, 1 - eps / reference) * 100, 6)


def zone_of(price, target, cfg):
    if not price or not target:
        return "na"
    if price >= target * cfg["sell_all_ratio"]:
        return "sell_all"
    if price >= target:
        return "sell_half"
    if (target / price - 1) * 100 >= cfg["buy_upside_pct"]:
        return "buy"
    return "hold"


def breakeven(cost, shares, code, incl_buy=True):
    """保本价：按这个价卖出，扣掉卖出费用（佣金/印花税/过户费）后正好收回持仓成本。
    incl_buy=True 表示 holdings.csv 的成本价已含买入费用（券商口径）；False 时再补一笔买入费用。"""
    if not cost or not shares or cost <= 0 or shares <= 0:
        return None
    basis = cost * shares + (0 if incl_buy else fees.one_way(cost * shares, code, 1))
    b = basis / shares
    for _ in range(6):
        b = (basis + fees.one_way(b * shares, code, -1)) / shares
    return round(b, 4)


def cost_gate(r, price, cfg, TA=None):
    """长线仓的持仓成本约束（只对已持有、有成本价的长线仓）：
      深套：现价 < 成本×(1−deep_loss_pct%) 且目标价 < 保本价 → 不加仓，只做T降成本；
      摊薄：首批（市值 < 目标仓位一半）之后的加仓，要求现价 ≤ 成本×(1−add_below_cost_pct%)；
      卖出：卖出区/清仓区要求现价 ≥ 保本价×(1+sell_min_profit_pct%)，否则持有（逻辑复查已触发的除外）。
    返回 {cost, breakeven, pnl_pct, buy_block, sell_block}，block 为 None 表示放行，否则为原因。"""
    cost, sh = _f(r.get("cost")), _f(r.get("shares")) or 0
    out = {"cost": cost, "breakeven": None, "pnl_pct": None, "buy_block": None, "sell_block": None}
    if not r.get("held") or not cost or not sh or not price:
        return out
    be = breakeven(cost, sh, r.get("code"), cfg.get("cost_includes_buy_fee", True))
    out.update(breakeven=be, pnl_pct=round((price / cost - 1) * 100, 1))
    tgt = r.get("target")
    deep = cfg.get("deep_loss_pct", 30)
    if price < cost * (1 - deep / 100) and tgt and be and tgt < be:
        out["buy_block"] = (f"深套 {out['pnl_pct']:.0f}%，目标价 {tgt:.2f} 低于保本价 {be:.2f}：只做T降成本，不加仓")
    else:
        tw = ((cfg.get("_target_weight") or 0.10) * TA) if TA else None
        first_done = tw is not None and (r.get("value") or 0) >= tw / 2
        lim = cost * (1 - cfg.get("add_below_cost_pct", 8) / 100)
        if first_done and price > lim:
            out["buy_block"] = f"摊薄不足：现价 {price:.2f} 高于成本 {cost:.2f}×{1 - cfg.get('add_below_cost_pct', 8) / 100:.2f}={lim:.2f}，暂不加仓"
    need = be * (1 + cfg.get("sell_min_profit_pct", 5) / 100) if be else None
    if need and price < need and not r.get("review"):
        out["sell_block"] = f"现价 {price:.2f} 低于保本价 {be:.2f}×{1 + cfg.get('sell_min_profit_pct', 5) / 100:.2f}={need:.2f}：持有，不卖"
    return out


def _lot(code):
    return 200 if str(code).startswith("688") else 100


def plan_actions(rows, TA, cash, params, cfg=None):
    """按分区给出动作与数量。rows: code → dict（含 held / value / available / price / zone / review / cost）。
    长线持仓先过 cost_gate（持仓成本约束），被拦的买卖改为提示。"""
    sc = sleeve_cfg(params)
    cc = sc.get("core") or {}
    cfg = dict(DEFAULTS, **(cfg or (cc.get("valuation") or {})), _target_weight=cc.get("target_weight", 0.10))
    sz = (params.get("auto") or {}).get("sizing") or {}
    cap = sz.get("position_cap", 0.2)
    tw = cc.get("target_weight", 0.10)
    maxp = sz.get("max_positions", 8)
    core_val = sum(r["value"] for r in rows.values() if r.get("sleeve") == "core")
    room = cc.get("budget_frac", 0.8) * TA - core_val if TA else 0
    n_core = sum(1 for r in rows.values() if r.get("sleeve") == "core" and (r.get("shares") or 0) > 0)
    cash_left = max(0, (cash or 0) - sz.get("cash_floor", 0) * (TA or 0))
    # 长线持仓优先；候选按行业景气（高 → 中性 → 低）再按空间分配长线仓空位
    order = sorted(rows.values(), key=lambda r: (r.get("sleeve") != "core",
                                                 0 if r.get("sleeve") == "core" else TIER_ORDER.get(r.get("industry_tier"), 1),
                                                 -(r.get("upside_pct") or -999)))
    for r in order:
        z, price = r.get("zone"), r.get("price")
        r["action"], r["qty"] = "无", 0
        risk = r.get("risk") or {}
        if r.get("sleeve") == "core" and risk.get("state") in ("reduce", "exit"):
            r["qty"] = max(0, min(int(risk.get("desired_sell_qty") or 0), int(r.get("available") or 0), int(r.get("shares") or 0)))
            r["action"] = ("风险退出" if risk["state"] == "exit" else "风险减仓") + "：" + "；".join(risk.get("reasons") or [])
            if not r["qty"]:
                r["action"] += "；暂无可执行数量，风险未解除"
            continue  # 硬风险优先于目标价、持仓成本与旧数据闸门
        if r.get("data_block"):
            r["action"] = r["data_block"]
            continue
        if not price or price <= 0:
            r["action"] = "行情缺失，暂停价格型操作"
            continue
        if z == "buy" and risk.get("block_add"):
            r["action"] = "暂停加仓：" + "；".join(risk.get("reasons") or ["投资逻辑待核实"])
            continue
        if r.get("sleeve") == "core":
            g = cost_gate(r, price, cfg, TA)
            r.update(cost=g["cost"], breakeven=g["breakeven"], pnl_pct=g["pnl_pct"],
                     buy_block=g["buy_block"], sell_block=g["sell_block"])
            if r.get("review"):
                r["action"] = "逻辑复查：" + "；".join(r["review"])
            if z in ("sell_all", "sell_half") and g["sell_block"]:
                r["action"] = g["sell_block"]
            elif z == "buy" and g["buy_block"]:
                r["action"] = g["buy_block"] + ("；逻辑复查：" + "；".join(r["review"]) if r.get("review") else "")
            elif z == "buy" and r.get("entry_block"):
                r["action"] = r["entry_block"]
            elif z == "sell_all":
                r["action"], r["qty"] = "清仓（到达清仓价）", int(r.get("available") or 0)
            elif z == "sell_half":
                q = int((r.get("shares") or 0) / 2 // _lot(r["code"]) * _lot(r["code"]))
                r["action"], r["qty"] = "卖出一半（到达目标价）", min(q or int(r.get("available") or 0), int(r.get("available") or 0))
            elif z == "buy" and not r.get("review"):
                gap = min(tw * TA, cap * TA) - r["value"]
                amt = min(gap / 2 if gap > 0.5 * tw * TA else gap, room, cash_left)
                q = int(amt / price // _lot(r["code"]) * _lot(r["code"])) if price and amt > 0 else 0
                if q > 0:
                    r["action"], r["qty"] = f"买入（补到目标仓位 {tw:.0%}，分批）", q
                    room -= q * price
                    cash_left -= q * price
                elif gap <= 0:
                    r["action"] = "买入区，但已到目标仓位"
                else:
                    r["action"] = "买入区，但长线仓预算/现金不足"
        elif r.get("sleeve") == "candidate" and z == "buy" and r.get("screen_ok"):
            if n_core >= maxp:
                r["action"] = f"买入区，但长线仓已满 {maxp} 只"
                continue
            amt = min(tw * TA / 2, room, cash_left)
            q = int(amt / price // _lot(r["code"]) * _lot(r["code"])) if price and amt > 0 else 0
            if q > 0:
                r["action"], r["qty"] = f"新建长线仓（首批，目标仓位 {tw:.0%}）", q
                room -= q * price
                cash_left -= q * price
                n_core += 1
    return {"core_value": round(core_val), "core_budget": round(cc.get("budget_frac", 0.8) * TA) if TA else None,
            "core_room_left": round(room), "n_core": sum(1 for r in rows.values() if r.get("sleeve") == "core"),
            "max_positions": maxp}


# ----------------------------------------------------------------- 持仓与快照
def load_snapshot():
    for p in SNAPS:
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def positions_and_account():
    """持仓市值优先用最近的快照（已含港币折算），没有就用 holdings.csv × 最新价。"""
    from datasources import holdings as H
    snap = load_snapshot()
    acc = snap.get("account") or H.account()
    pos = {}
    for p in snap.get("positions") or []:
        c = str(p.get("code"))
        pos[c] = {"code": c, "name": p.get("position_name") or p.get("name"), "shares": _f(p.get("shares")) or 0,
                  "available": _f(p.get("available")) or 0, "price": _f(p.get("price")),
                  "value": _f(p.get("market_value")) or (_f(p.get("price")) or 0) * (_f(p.get("shares")) or 0),
                  "market": p.get("market"), "is_etf": bool(p.get("is_etf")), "cost": _f(p.get("cost"))}
    if not pos:
        for h in H.holdings():
            pos[h["code"]] = {"code": h["code"], "name": h["name"], "shares": h["shares"] or 0,
                              "available": h["available"] or 0, "price": None, "value": 0, "market": h["market"],
                              "is_etf": False, "cost": h.get("cost")}
    return pos, {"total_assets": _f(acc.get("total_assets")), "cash": _f(acc.get("cash_available"))}


def watch_codes():
    import csv
    try:
        with open(data_path("watchlist.csv"), encoding="utf-8-sig") as f:
            return [(r.get("代码") or "").strip() for r in csv.DictReader(f) if (r.get("代码") or "").strip()]
    except OSError:
        return []


def is_stock(code, p=None):
    n = normalize(code)
    if n["market"] == "hk":
        return False
    if (p or {}).get("is_etf"):
        return False
    return str(code)[:2] not in ("15", "16", "18", "50", "51", "52", "56", "58")


def etf_cfg(sc):
    e = dict((sc.get("core") or {}).get("etf") or {})
    imap = e.pop("index_map", {}) or {}
    return dict(etf_value.DEFAULTS, **{k: v for k, v in e.items() if not k.startswith("_")}), imap


def review_etfs(codes, pos, sc, cfg, errors):
    """持有的 ETF 按指数估值法评估（etf_value.py）。返回 code → row（字段与个股 row 对齐，kind=etf）。"""
    if not codes:
        return {}
    ecfg, imap = etf_cfg(sc)
    try:
        meta = etf_value.fetch_meta(codes)
    except DataSourceError as e:
        errors.append(f"ETF 行情：{e}")
        meta = {c: {"code": c} for c in codes}
    rows = {}
    for c in codes:
        p, mt, ent = pos.get(c) or {}, meta.get(c) or {}, imap.get(c) or {}
        mt["price"] = mt.get("price") or p.get("price")
        r = {"code": c, "name": p.get("name") or mt.get("name"), "kind": "etf", "price": mt.get("price"),
             "tracking": mt.get("tracking"), "val_index": ent.get("index"), "val_code": ent.get("code"),
             "val_note": ent.get("note"), "scale_yi": mt.get("scale_yi"), "amount_wan": mt.get("amount_wan"),
             "fee_pct": mt.get("fee_pct"), "nav": mt.get("nav"), "nav_date": mt.get("nav_date"),
             "premium_pct": mt.get("premium_pct"), "buy_upside_pct": ecfg["buy_upside_pct"],
             "held": True, "shares": p.get("shares", 0), "available": p.get("available", 0),
             "value": p.get("value", 0), "cost": p.get("cost")}
        if not ent.get("code"):
            m = {"ok_data": False, "why": f"index_map 未配置估值指数（跟踪 {mt.get('tracking') or '未知'}）"}
            series = []
        else:
            try:
                series = etf_value.fetch_series(ent)
                m = etf_value.metrics(series, ecfg)
            except (DataSourceError, ValueError, KeyError) as e:
                errors.append(f"{c} 估值指数 {ent.get('code')}：{e}")
                series, m = [], {"ok_data": False, "why": "估值数据拉取失败"}
        ok, why = etf_value.judge(m, mt, ecfg)
        v = etf_value.value(m, mt, series, ecfg, cfg["sell_all_ratio"])
        r.update({k: x for k, x in m.items() if k not in ("ok_data", "fair_ratio")})
        r.update(v)
        r.update(etf_ok=ok, etf_why=why, screen_ok=ok, screen_why=why,
                 review_all=etf_value.review_flags(m, ok, why, ecfg))
        rows[c] = r
    return rows


# ----------------------------------------------------------------- 子命令
def review(params, dry=False):
    cfg = dict(DEFAULTS, **((sleeve_cfg(params).get("core") or {}).get("screen") or {}),
               **((sleeve_cfg(params).get("core") or {}).get("valuation") or {}))
    sc = sleeve_cfg(params)
    prev = None
    try:
        with open(core_path(params), encoding="utf-8") as f:
            prev = json.load(f)
    except (OSError, json.JSONDecodeError):
        prev = None
    prev_sl = (prev or {}).get("sleeves") or {}
    prev_rows = (prev or {}).get("stocks") or {}
    pos, acc = positions_and_account()
    held_stocks = [c for c, p in pos.items() if is_stock(c, p)]
    held_etfs = [c for c in pos if fees.kind_of(c) == "etf"]
    cands = [c for c in watch_codes() if c not in pos]
    codes = held_stocks + cands
    errors = []
    quotes = {}
    try:
        quotes = fetch_quotes(codes)
    except DataSourceError as e:
        errors.append(f"行情/估值：{e}")
    icfg = ih.cfg_of(sc.get("core"))
    heat, span, pol = {}, None, None
    if icfg.get("enabled"):
        try:
            hd = ih.build(icfg, errors=errors)
            heat, span, pol = hd["industries"], ih.span_label(hd["periods"]), hd.get("policy")
        except (DataSourceError, OSError, ValueError, KeyError) as e:
            errors.append(f"行业景气：{e}（全部按中性）")

    def one(c):
        try:
            return c, fetch_fin(c), fetch_forecast(c), fetch_pe_hist(c), None
        except DataSourceError as e:
            return c, [], None, [], str(e)

    with ThreadPoolExecutor(max_workers=3) as ex:
        got = list(ex.map(one, codes))
    rows = {}
    for c, fin, fc, peh, err in got:
        if err:
            errors.append(f"{c}：{err}")
        q = quotes.get(c) or {}
        p = pos.get(c) or {}
        price = q.get("price") or p.get("price")
        ind = q.get("industry")
        hv = heat.get(ind) if ind else None
        hv = ih.stock_view(hv, ((pol or {}).get("stocks") or {}).get(c), icfg)   # 个股政策分（业务与行业分类不一致的公司）
        tier = (hv or {}).get("tier", "neutral") if heat else "neutral"
        ecfg = tier_cfg(cfg, tier, icfg)
        ok, why, m = screen(q, fin, fc, ecfg)
        v = valuation(price, fc, peh, ecfg)
        r = {"code": c, "name": p.get("name") or q.get("name"), "price": price, "screen_ok": ok, "screen_why": why,
             **m, **v, "held": c in pos, "shares": p.get("shares", 0), "available": p.get("available", 0),
             "value": p.get("value", 0), "cost": p.get("cost"),
             "industry_tier": tier, "industry_score": (hv or {}).get("score"), "industry_rank": (hv or {}).get("rank"),
             "industry_score_base": (hv or {}).get("score_base"), "policy_pts": (hv or {}).get("policy_pts") or 0,
             "industry_tier_base": (hv or {}).get("tier_base", tier),
             "policy_brief": "；".join(x for x in ((hv or {}).get("policy_brief"), (hv or {}).get("stock_policy_brief") and
                                                  "个股" + hv["stock_policy_brief"]) if x),
             "industry_note": ih.note(ind, hv, icfg, span) if heat else f"{ind or '未知行业'}｜行业景气未启用或拉取失败（按中性）",
             "pe_max_used": ecfg["pe_max"], "pe_fair_max_used": ecfg["pe_fair_max"],
             "buy_upside_pct": ecfg["buy_upside_pct"]}
        # 逻辑复查：已在长线仓的持仓
        rv = []
        if m.get("np_yoy") is not None and m["np_yoy"] < 0:
            rv.append(f"归母净利同比 {m['np_yoy']:.1f}%")
        if m.get("roe_ytd_ann") is not None and m["roe_ytd_ann"] < cfg["roe_ytd_min"]:
            rv.append(f"ROE 年化 {m['roe_ytd_ann']}%")
        old = prev_rows.get(c) or {}
        reference, cut = eps_revision(old, v)
        r.update(eps_reference=reference, eps_cumulative_cut_pct=cut)
        if cut is not None and cut >= cfg["eps_cut_pct"]:
            rv.append(f"{v['eps_year']}E EPS 累计下调 {reference}→{v['eps']}（{cut:.1f}%）")
        if not fc or (fc.get("orgs") or 0) < cfg["min_orgs"]:
            rv.append(f"机构覆盖 {((fc or {}).get('orgs') or 0)} 家不足，暂停新增并核实")
        if v.get("zone") == "na":
            rv.append("无法定目标价（" + v.get("why", "") + "）")
        r["review_all"] = rv
        rows[c] = r
    etf_rows = review_etfs(held_etfs, pos, sc, cfg, errors)
    rows.update(etf_rows)
    etf_assign = etf_value.assign(etf_rows, prev_sl)
    # 归属
    sleeves = {}
    for c, p in pos.items():
        if c in ((sc.get("trade") or {}).get("force") or []):
            s, why = "trade", "固定归交易仓（sleeves.trade.force）"
        elif c in ((sc.get("core") or {}).get("force") or []):
            s, why = "core", "固定归长线仓（sleeves.core.force）"
        elif c in etf_assign:
            s, why = etf_assign[c]
        elif not is_stock(c, p):
            s, why = "trade", "港股/非个股，不适用公司财报估值"
        elif (prev_sl.get(c) or {}).get("sleeve") == "core":
            s, why = "core", "上次已归长线仓" + ("" if rows.get(c, {}).get("screen_ok") else "（本次未过筛选：" + "；".join(rows.get(c, {}).get("screen_why", [])) + "）")
        elif rows.get(c, {}).get("screen_ok") and rows[c].get("zone") != "na":
            s, why = "core", "满足长线条件"
        else:
            s, why = "trade", "未满足长线条件：" + "；".join(rows.get(c, {}).get("screen_why") or ["无数据"])
        if rows.get(c, {}).get("industry_note"):
            why += "；行业：" + rows[c]["industry_note"]
        sleeves[c] = {"sleeve": s, "why": why, "name": p.get("name"), "value": round(p.get("value") or 0)}
        if c in rows:
            rows[c]["sleeve"] = s
            rows[c]["review"] = rows[c]["review_all"] if s == "core" else []
    for c in cands:
        rows[c]["sleeve"] = "candidate"
        rows[c]["review"] = []
    for c, p in pos.items():
        if c not in rows and sleeves[c]["sleeve"] == "core":   # force 进长线仓但不是个股：只记归属
            rows[c] = {"code": c, "name": p.get("name"), "price": p.get("price"), "zone": "na", "sleeve": "core",
                       "held": True, "shares": p["shares"], "available": p["available"], "value": p["value"],
                       "review": ["非个股，无法按目标价法定价"]}
    TA, cash = acc["total_assets"] or 0, acc["cash"] or 0
    thesis_data, ledger = rc.load_thesis(), rc.load_ledger()
    for c, r in rows.items():
        if r.get("sleeve") == "core":
            r["risk"] = rc.assess(c, r, thesis_data, ledger=ledger)
    summ = plan_actions(rows, TA, cash, params, cfg)
    trade_val = sum(v["value"] for v in sleeves.values() if v["sleeve"] == "trade")
    tb = (sc.get("trade") or {}).get("budget_frac", 0.2)
    summ.update({"trade_value": round(trade_val), "trade_budget": round(tb * TA) if TA else None,
                 "trade_over": round(trade_val - tb * TA) if TA and trade_val > tb * TA else 0,
                 "total_assets": TA, "cash": cash})
    today = datetime.date.today().strftime("%Y%m%d")
    out = {"_说明": "core_value.py review 生成（每周复查），定时任务不得手改。sleeves=持仓归属，stocks=目标价与分区",
           "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "asof": today,
           "params": dict(cfg, etf=etf_cfg(sc)[0], industry=icfg), "summary": summ, "sleeves": sleeves,
           "industry": dict(industry_summary(heat, rows), span=span),
           "policy": policy_summary(pol, heat, rows, icfg),
           "stocks": {c: {k: v for k, v in r.items() if k != "review_all"} for c, r in rows.items()},
           "errors": errors}
    md = render(out)
    if not dry:
        with open(core_path(params), "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
            f.write("\n")
        remember_sleeves(out)
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, f"{today}.md"), "w", encoding="utf-8") as f:
            f.write(md)
    return out, md


_IND_KEYS = ("score", "rank", "of", "tier", "tier_why", "members", "np_cagr3", "rev_cagr3", "ytd_np_yoy", "ret_1y", "flow20", "bk",
             "score_base", "rank_base", "tier_base", "policy_pts", "policy_brief")


def policy_summary(pol, heat, rows, icfg):
    """core_auto.json 里的政策信号：有效事件（衰减权重、减半日、失效日）、政策分不为 0 的行业、个股政策分、ETF 对应行业（只展示）。"""
    if not pol:
        return {"enabled": False}
    pc = ps.cfg_of(icfg)
    ev_all = {e.get("id"): e for e in ps.load_events(pc)}
    act = []
    for a in pol.get("active") or []:
        hl = pc["half_life_days"].get(a.get("type"), pc["half_life_days"]["presser"])
        d0 = datetime.date.fromisoformat(a["date"])
        dead = d0 + datetime.timedelta(days=math.ceil(hl * math.log2(1 / pc["min_weight"])))
        act.append(dict(a, half_life_days=hl, half_at=(d0 + datetime.timedelta(days=hl)).isoformat(), expire=dead.isoformat(),
                        local=(ev_all.get(a.get("id")) or {}).get("local")))
    _why = lambda e: "；".join(dict.fromkeys(i["why"] for i in sorted(e["items"], key=lambda i: -abs(i["s"] * i["w"])) if i.get("why")))
    inds = {k: dict({kk: (heat.get(k) or {}).get(kk) for kk in _IND_KEYS}, why=_why(e))
            for k, e in (pol.get("industries") or {}).items() if e.get("pts")}
    stks = {c: dict(e, why=_why(e)) for c, e in (pol.get("stocks") or {}).items()}
    etf_ind = {c: {"industry": ind, **{kk: (heat.get(ind) or {}).get(kk) for kk in ("score", "score_base", "policy_pts", "tier", "policy_brief")}}
               for c, ind in (pc.get("etf_industry") or {}).items()}
    return {"enabled": True, "asof": pol["asof"], "params": {k: pc[k] for k in ("max_points", "stock_max_points", "full_raw", "min_weight", "half_life_days")},
            "active": act, "industries": inds, "stocks": stks, "etf_industry": etf_ind}


def industry_summary(heat, rows, top=10):
    """core_auto.json 里的行业景气：涉及的行业（持仓 + 候选）全部列出，另附全市场景气前 top。"""
    if not heat:
        return {"enabled": False}
    inv = {}
    for c, r in rows.items():
        ind = r.get("industry")
        if ind and r.get("kind") != "etf":
            e = inv.setdefault(ind, dict({k: (heat.get(ind) or {}).get(k) for k in _IND_KEYS}, codes=[]))
            e["codes"].append(f"{c} {r.get('name')}")
    ranked = sorted([k for k, v in heat.items() if v.get("score") is not None], key=lambda k: -heat[k]["score"])
    return {"enabled": True, "involved": inv,
            "top": [dict({k2: heat[k].get(k2) for k2 in _IND_KEYS}, name=k) for k in ranked[:top]]}


def _tier_cell(r):
    if r.get("kind") == "etf" or not r.get("industry"):
        return "-"
    sc, pp = r.get("industry_score"), r.get("policy_pts") or 0
    return (f"{r['industry']}·{ih.TIER_NAME.get(r.get('industry_tier'), '中性')}" + (f" {sc:.0f}" if sc is not None else "")
            + (f"（政策{pp:+.0f}）" if pp else "") + ("↑" if _tier_up(r) else ("↓" if _tier_down(r) else "")))


_TIER_RANK = {"cold": 0, "neutral": 1, "hot": 2}
_tier_up = lambda r: _TIER_RANK.get(r.get("industry_tier"), 1) > _TIER_RANK.get(r.get("industry_tier_base") or r.get("industry_tier"), 1)
_tier_down = lambda r: _TIER_RANK.get(r.get("industry_tier"), 1) < _TIER_RANK.get(r.get("industry_tier_base") or r.get("industry_tier"), 1)


def advice(r, out):
    """个股建议总表的「建议」：以系统动作为准，政策只解释档位变化；左侧（政策热、业绩/景气冷）只观察。"""
    act, qty, z = r.get("action") or "", r.get("qty") or 0, r.get("zone")
    sl = r.get("sleeve")
    if sl == "trade" or (r.get("kind") == "etf" and sl != "core"):
        over = ((out or {}).get("summary") or {}).get("trade_over")
        base = {"sell_all": "只做T：估值在清仓区，不加仓，反弹分批减", "sell_half": "只做T：估值在卖出区，不加仓",
                "hold": "持有：交易仓按盘中规则",
                "buy": "持有：估值在买入区，但交易仓超预算只卖不买" if over else "持有/逢低做T：估值在买入区"}.get(z, "持有：交易仓按盘中规则")
    elif sl == "core":
        if (r.get("risk") or {}).get("state") in ("reduce", "exit"):
            base = f"{act}（本次可执行 {qty} 股）"
        elif r.get("data_block"):
            base = "观察：" + r["data_block"]
        elif "只做T" in act:
            base = "只做T：" + act
        elif qty and "买" in act:
            base = f"补仓 {qty} 股：{act}"
        elif z == "sell_half":
            base = (f"卖一半（本次 {qty} 股）：" if qty else "待执行/观察：") + (act or "到目标价")
        elif z == "sell_all":
            base = (f"清仓（本次 {qty} 股）：" if qty else "待执行/观察：") + (act or "到目标价×清仓比例")
        else:
            base = "持有" + (f"：{act}" if act and act != "无" else "")
    else:
        if not r.get("screen_ok"):
            base = "不碰：未过筛选（" + "；".join(r.get("screen_why") or []) + "）"
        elif qty:
            base = f"买入（首批 {qty} 股）"
        elif z == "buy":
            base = "候补：买入区但长线仓已满，轮换时优先"
        elif z == "hold":
            base = f"不追：持有区，空间 {_n(r.get('upside_pct'), 1)}%"
        elif z in ("sell_half", "sell_all"):
            base = "不碰：已高于目标价"
        else:
            base = "观察：无法定价"
    pp, sb = r.get("policy_pts") or 0, r.get("industry_score_base")
    tail = []
    if _tier_up(r):
        hl = ((out.get("policy") or {}).get("params") or {}).get("half_life_days", {}).get("presser", 10)
        tail.append(f"档位因政策上调，约 {hl} 天后政策分减半，需业绩验证")
    if pp > 0 and ((sb is not None and sb < 50) or r.get("industry_tier_base") == "cold"):
        tail.append("左侧：政策热、景气冷，只观察")
    return base + ("｜" + "；".join(tail) if tail else "")


def advice_rows(out):
    """个股建议总表：全部持仓（含 ETF、港股）+ 买入区 / 政策分 ≠ 0 / 档位因政策变化的自选股。"""
    st_, pol = out["stocks"], out.get("policy") or {}
    pind, pst, etfi = pol.get("industries") or {}, pol.get("stocks") or {}, pol.get("etf_industry") or {}
    rows = []
    for c, r in st_.items():
        keep = r.get("held") or r.get("zone") == "buy" or (r.get("policy_pts") or 0) != 0 or _tier_up(r) or _tier_down(r)
        if not keep:
            continue
        who = ("持仓-长线" if r.get("sleeve") == "core" else "持仓-交易") if r.get("held") else "候选"
        if r.get("kind") == "etf":
            e = etfi.get(c) or {}
            ind = (e.get("industry") or "-") + "（ETF，只展示）" if e.get("industry") else "-"
            heat = (f"{_n(e.get('score_base'), 0)}→{_n(e.get('score'), 0)}（{(e.get('policy_pts') or 0):+.1f}）"
                    if e.get("score") is not None else "-")
            tier, why = ih.TIER_NAME.get(e.get("tier"), "-") if e else "-", (pind.get(e.get("industry")) or {}).get("why", "")
        else:
            ind = r.get("industry") or "-"
            heat = (f"{_n(r.get('industry_score_base'), 0)}→{_n(r.get('industry_score'), 0)}（{(r.get('policy_pts') or 0):+.1f}）"
                    if r.get("industry_score") is not None else "-")
            tier = ih.TIER_NAME.get(r.get("industry_tier"), "中性") + (
                f"（{ih.TIER_NAME.get(r.get('industry_tier_base'))}→，政策上调）" if _tier_up(r) else
                f"（{ih.TIER_NAME.get(r.get('industry_tier_base'))}→，政策下调）" if _tier_down(r) else "")
            why = "；".join(x for x in ((pst.get(c) or {}).get("why"), (pind.get(ind) or {}).get("why")) if x)
        rows.append({"code": c, "name": r.get("name"), "who": who, "industry": ind, "heat": heat, "tier": tier,
                     "price": r.get("price"), "target": r.get("target"), "upside": r.get("upside_pct"),
                     "zone": ZONE_NAME.get(r.get("zone"), r.get("zone")), "action": r.get("action") or "无",
                     "policy": why or "-", "advice": advice(r, out), "_o": (0 if r.get("held") else 1, -(r.get("policy_pts") or 0))})
    for c, v in (out.get("sleeves") or {}).items():   # 港股等不在 stocks 里的持仓
        if c not in st_:
            rows.append({"code": c, "name": v.get("name"), "who": "持仓-" + ("长线" if v["sleeve"] == "core" else "交易"),
                         "industry": "-", "heat": "-", "tier": "-", "price": None, "target": None, "upside": None, "zone": "-",
                         "action": "-", "policy": "-", "advice": "按交易仓盘中规则（不适用目标价法）", "_o": (0, 0)})
    rows.sort(key=lambda x: x["_o"])
    return rows


def _n(x, nd=2):
    return "-" if x is None else (f"{x:.{nd}f}" if isinstance(x, float) else str(x))


def render(out):
    s = out["summary"]
    L = [f"# 长线仓复查 {out['generated_at']}", "",
         f"- 总资产 {s['total_assets']:.0f}，可用现金 {s['cash']:.0f}",
         f"- 长线仓：{s['n_core']} 只 / 上限 {s['max_positions']} 只，市值 {s['core_value']}，预算 {s['core_budget']}，剩余可买 {s['core_room_left']}",
         f"- 交易仓：市值 {s['trade_value']}，上限 {s['trade_budget']}"
         + (f"，**超出 {s['trade_over']}（交易仓只卖不买，直到回到上限内）**" if s.get("trade_over") else ""), "",
         "## 持仓归属", "", "| 代码 | 名称 | 归属 | 市值 | 原因 |", "|---|---|---|---|---|"]
    for c, v in sorted(out["sleeves"].items(), key=lambda x: (x[1]["sleeve"], -x[1]["value"])):
        L.append(f"| {c} | {v['name']} | {'长线' if v['sleeve'] == 'core' else '交易'} | {v['value']} | {v['why']} |")
    hdr = ["| 代码 | 名称 | 行业景气 | 现价 | EPS(年) | 5年PE中位→合理PE | 目标价 | 空间 | 分区 | 买入价≤ | 动作 | 数量 |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|"]

    def line(r):
        if r.get("kind") == "etf":   # ETF：EPS 列写「指数」，PE 列为 周期修正PE→10年中位PE
            return (f"| {r['code']} | {r.get('name')} | - | {_n(r.get('price'), 3)} | 指数 | "
                    f"{_n(r.get('pe_norm'), 1)}→{_n(r.get('pe_fair'), 1)} | {_n(r.get('target'), 3)} | "
                    f"{_n(r.get('upside_pct'), 1)}% | {ZONE_NAME.get(r.get('zone'), r.get('zone'))} | {_n(r.get('buy_below'), 3)} | "
                    f"{r.get('action', '')} | {r.get('qty') or ''} |")
        return (f"| {r['code']} | {r.get('name')} | {_tier_cell(r)} | {_n(r.get('price'))} | {_n(r.get('eps'), 2)}({r.get('eps_year', '')}E) | "
                f"{_n(r.get('pe_med5y'), 1)}→{_n(r.get('pe_fair'), 1)} | {_n(r.get('target'))}{'*' if r.get('capped_by_aim') else ''} | "
                f"{_n(r.get('upside_pct'), 1)}% | {ZONE_NAME.get(r.get('zone'), r.get('zone'))} | {_n(r.get('buy_below'))} | "
                f"{r.get('action', '')} | {r.get('qty') or ''} |")
    st_ = out["stocks"]
    core = [r for r in st_.values() if r.get("sleeve") == "core"]
    L += ["", "## 长线仓", ""] + hdr + [line(r) for r in sorted(core, key=lambda r: -(r.get("upside_pct") or -999))]
    if core:
        L += ["", "持仓成本：", "", "| 代码 | 名称 | 成本 | 保本价 | 浮盈亏 | 目标价相对保本价 | 成本约束 |", "|---|---|---|---|---|---|---|"]
        for r in sorted(core, key=lambda r: -(r.get("upside_pct") or -999)):
            be, t = r.get("breakeven"), r.get("target")
            blk = r.get("buy_block") if r.get("zone") == "buy" else (
                r.get("sell_block") if r.get("zone") in ("sell_half", "sell_all") else None)
            rel = f"{(t / be - 1) * 100:+.1f}%" if be and t else "-"
            L.append(f"| {r['code']} | {r.get('name')} | {_n(r.get('cost'), 3)} | {_n(be, 3)} | "
                     f"{_n(r.get('pnl_pct'), 1)}% | {rel} | {blk or '无'} |")
    cand = [r for r in st_.values() if r.get("sleeve") == "candidate" and r.get("screen_ok") and r.get("zone") != "na"]
    cand.sort(key=lambda r: (TIER_ORDER.get(r.get("industry_tier"), 1), -(r.get("upside_pct") or -999)))
    L += ["", f"## 候选（自选股中满足长线条件的 {len(cand)} 只，按行业景气、空间排序）", ""] + hdr + [line(r) for r in cand[:15]]
    ind = out.get("industry") or {}
    if ind.get("enabled"):
        L += ["", "## 行业景气（涉及的行业）", "",
              f"| 行业 | 景气分 | 基础分 | 政策分 | 排名 | 分档 | 净利CAGR({ind.get('span') or '-'}) | 营收CAGR({ind.get('span') or '-'}) | 今年净利同比 | 1年指数涨幅 | 20日主力净流入占比 | 涉及标的 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        pct = lambda x: "-" if x is None else f"{x:+.1f}%"
        for k, v in sorted(ind["involved"].items(), key=lambda x: -(x[1].get("score") or -1)):
            L.append(f"| {k} | {_n(v.get('score'), 1)} | {_n(v.get('score_base'), 1)} | {(v.get('policy_pts') or 0):+.1f} | {v.get('rank') or '-'}/{v.get('of') or '-'} | "
                     f"{ih.TIER_NAME.get(v.get('tier'), '中性')}{'（政策调档）' if v.get('tier_base') and v.get('tier_base') != v.get('tier') else ''} | {pct(v.get('np_cagr3'))} | {pct(v.get('rev_cagr3'))} | "
                     f"{pct(v.get('ytd_np_yoy'))} | {pct(v.get('ret_1y'))} | {pct(v.get('flow20'))} | {'、'.join(v['codes'])} |")
        L += ["", "全市场景气前 10：" + "、".join(f"{v['name']}({v['score']:.0f}{'·高' if v.get('tier') == 'hot' else ''})" for v in ind["top"])]
    pol = out.get("policy") or {}
    if pol.get("enabled"):
        pp = pol["params"]
        L += ["", f"## 政策信号（{pol['asof']} 衰减后）", "",
              f"政策分 = {pp['max_points']} × clip(Σ 强度 s × 0.5^(天数/半衰期) / {pp['full_raw']}, −1, 1)，加在基础景气分上；"
              f"半衰期：" + "、".join(f"{ps.TYPE_NAME.get(k, k)} {v} 天" for k, v in pp["half_life_days"].items())
              + f"；权重 < {pp['min_weight']} 失效。盈利门槛不变（政策分不能把净利下滑的行业变成高景气）。", ""]
        if pol.get("active"):
            L += ["| 日期 | 类型 | 事件 | 当前权重 | 减半日 | 失效日 |", "|---|---|---|---|---|---|"]
            for a in pol["active"]:
                L.append(f"| {a['date']} | {ps.TYPE_NAME.get(a['type'], a['type'])} | [{a['title']}]({a.get('url') or ''}) | "
                         f"{a['w']:.2f} | {a['half_at']} | {a['expire']} |")
        else:
            L.append("当前没有有效政策事件（全部已衰减失效）。")
        if pol.get("industries"):
            L += ["", "| 行业 | 基础分 → 最终分 | 政策分 | 档位 | 依据 |", "|---|---|---|---|---|"]
            for k, v in sorted(pol["industries"].items(), key=lambda x: -(x[1].get("policy_pts") or 0)):
                chg = (f"{ih.TIER_NAME.get(v.get('tier_base'))}→{ih.TIER_NAME.get(v.get('tier'))}"
                       if v.get("tier_base") and v.get("tier_base") != v.get("tier") else ih.TIER_NAME.get(v.get("tier"), "中性"))
                L.append(f"| {k} | {_n(v.get('score_base'), 1)} → {_n(v.get('score'), 1)} | {(v.get('policy_pts') or 0):+.1f} | {chg} | {v.get('why') or ''} |")
        if pol.get("stocks"):
            L += ["", "个股政策分（行业分类与主业不一致时使用）：" + "；".join(
                f"{c} {(st_.get(c) or {}).get('name') or ''} {e['pts']:+.1f}（{e.get('why')}）" for c, e in pol["stocks"].items())]
    adv = advice_rows(out)
    if adv:
        L += ["", "## 个股建议总表（持仓 + 买入区 / 政策相关自选）", "",
              "| 代码 | 名称 | 身份 | 行业 | 景气 基础→最终（政策） | 档位 | 现价 | 目标价 | 空间 | 分区 | 建议 | 政策要点 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for a in adv:
            L.append(f"| {a['code']} | {a['name']} | {a['who']} | {a['industry']} | {a['heat']} | {a['tier']} | {_n(a['price'], 3 if a['code'].startswith(('15', '16', '5')) else 2)} | "
                     f"{_n(a['target'], 2)} | {_n(a['upside'], 1)}{'%' if a['upside'] is not None else ''} | {a['zone']} | {a['advice']} | {a['policy']} |")
    et = [r for r in st_.values() if r.get("kind") == "etf"]
    if et:
        L += ["", "## ETF 评估（指数估值法）", "",
              "| 代码 | 名称 | 估值指数 | 历史 | PE→周期修正PE | 10年中位PE | PE分位 | 指数盈利年化 | 盈利同比 | 规模/成交额 | 溢价 | 目标价 | 空间 | 分区 | 归属 | 结论 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in sorted(et, key=lambda r: (r.get("sleeve") != "core", -(r.get("value") or 0))):
            sl = out["sleeves"].get(r["code"]) or {}
            idx = (r.get("val_index") or "-") + ("†" if r.get("val_note") else "")
            L.append(f"| {r['code']} | {r.get('name')} | {idx} | {_n(r.get('hist_years'), 1)}年 | "
                     f"{_n(r.get('pe'), 1)}→{_n(r.get('pe_norm'), 1)}{'（盈利高于趋势 ' + str(r.get('e_vs_trend_pct')) + '%）' if r.get('cyc_capped') else ''} | "
                     f"{_n(r.get('pe_fair'), 1)} | {_n(r.get('pe_pct'))}% | {_n(r.get('earn_cagr'), 1)}%（{r.get('earn_years', '-')}年） | "
                     f"{_n(r.get('earn_yoy'), 1)}% | {_n(r.get('scale_yi'), 1)}亿/{_n(r.get('amount_wan'))}万 | {_n(r.get('premium_pct'), 1)}% | "
                     f"{_n(r.get('target'), 3)} | {_n(r.get('upside_pct'), 1)}% | {ZONE_NAME.get(r.get('zone'), r.get('zone'))} | "
                     f"{'长线' if r.get('sleeve') == 'core' else '交易'} | {sl.get('why', '')} |")
        notes = [f"{r['code']}：{r['val_note']}" for r in et if r.get("val_note")]
        if notes:
            L += ["", "† 估值用代理指数：" + "；".join(notes)]
    tr = [r for r in st_.values() if r.get("sleeve") == "trade" and r.get("kind") != "etf"]
    if tr:
        L += ["", "## 交易仓个股（未满足长线条件，供参考）", ""] + hdr + [line(r) for r in tr]
    L += ["", "口径：目标价 = 一致预期 EPS × 合理 PE（自身 5 年 PE(TTM) 中位数，限制在 "
          f"{out['params']['pe_fair_min']}~{out['params']['pe_fair_max']}）；空间 ≥ {out['params']['buy_upside_pct']}% 为买入区，"
          f"到目标价卖一半，到目标价×{out['params']['sell_all_ratio']} 清仓。"
          "目标价带 * 的被机构最高目标价封顶（EPS×PE 更高）。"
          f"持仓成本约束：深套超过 {out['params'].get('deep_loss_pct', 30)}% 且目标价低于保本价的只做T不加仓；首批之后的加仓要求现价比成本低 "
          f"{out['params'].get('add_below_cost_pct', 8)}%；卖出要求现价 ≥ 保本价×(1+{out['params'].get('sell_min_profit_pct', 5)}%)（逻辑复查触发的除外）。"
          "保本价 = 成本价 + 卖出费用（佣金/印花税/过户费）。"
          + (lambda ic: f"行业景气：高景气（景气分 ≥ {ic.get('hot_min_score')} 且 " + f"{(out.get('industry') or {}).get('span') or '基期'} 净利 CAGR > 0、今年净利同比 ≥ 0；基期从 2023 年报起算，2022 疫情年不用）PE 上限放宽到 "
             f"{(ic.get('hot') or {}).get('pe_max')}、合理 PE 上限 {(ic.get('hot') or {}).get('pe_fair_max')}；低景气（≤ {ic.get('cold_max_score')}）"
             f"买入线收紧到空间 ≥ {(ic.get('cold') or {}).get('buy_upside_pct')}%；长线仓空位按高景气 → 中性 → 低景气分配。"
             + (f"景气分 = 基础分 + 政策分（政策事件按类型半衰期衰减，单行业 ±{(ic.get('policy') or {}).get('max_points', 10)} 分封顶，"
                "政策只能跨中性/低景气档，进高景气仍需盈利门槛）。" if (ic.get('policy') or {}).get('enabled', True) else "")
             if ic.get("enabled") else "")(out["params"].get("industry") or {})
          + "ETF 用指数估值法：目标价 = 净值 × 周期修正盈利 × 10 年中位 PE / 指数点位；准入要求指数盈利长期增长、估值历史 ≥ 5 年、"
          "规模/成交额/费率达标；空间 ≥ " + str((out['params'].get('etf') or {}).get('buy_upside_pct', 20)) + "% 为买入区，场内溢价过高不买。"]
    if out.get("errors"):
        L += ["", "数据缺失：" + "；".join(out["errors"][:10])]
    L += ["", "以上为信息整理，不构成投资建议。", ""]
    return "\n".join(L)


def status(params, snap=None, today=None):
    """只读：以本次快照重算分区、股数、现金和预算；风险退出优先，旧估值仅展示。"""
    core = load_core(params, today) or {"stocks": {}, "sleeves": {}, "_health": {"status": "missing", "block_add": True}}
    health = core.get("_health") or {"status": "fresh", "block_add": False}
    cfg = dict(DEFAULTS, **(core.get("params") or {}))
    snap = snap if snap is not None else load_snapshot()
    held = {str(p.get("code")): p for p in snap.get("positions") or [] if (_f(p.get("shares")) or 0) > 0}
    quotes = {str(p.get("code")): p for p in (snap.get("watchlist") or []) + (snap.get("positions") or [])}
    acc = snap.get("account") or {}
    TA, cash = _f(acc.get("total_assets")), _f(acc.get("cash_available"))
    thesis_data, ledger = rc.load_thesis(), rc.load_ledger()
    originals = dict(core.get("stocks") or {})
    for c, h in held.items():
        sl = sleeve_of(c, params, core)
        if health["status"] == "missing" and c not in (core.get("sleeves") or {}) \
                and c not in ((sleeve_cfg(params).get("trade") or {}).get("force") or []) and "长线仓" in (h.get("stance") or ""):
            sl = "core"  # 仅保留明确定位，不凭技术指标猜归属
        if sl == "core":
            originals[c] = dict(originals.get(c) or {}, sleeve="core", code=c, name=h.get("name"))
    rows = {}
    for c, old in originals.items():
        if old.get("sleeve") not in ("core", "candidate"):
            continue
        h, q = held.get(c), quotes.get(c) or {}
        if h and sleeve_of(c, params, core) == "trade":
            continue  # 最新显式交易仓归属优先于旧review中的core标记
        if old.get("sleeve") == "core" and not h:
            continue  # 快照已无该持仓，不沿用旧股数
        price = _f(q.get("price"))
        prem = _f(q.get("premium_pct"))
        ecfg = dict(etf_value.DEFAULTS, **(cfg.get("etf") or {}))
        is_etf = old.get("kind") == "etf" or bool(q.get("is_etf"))
        val_price = etf_value.fair_price(price, prem) if is_etf else price
        zcfg = dict(cfg, buy_upside_pct=ecfg["buy_upside_pct"] if is_etf else old.get("buy_upside_pct") or cfg["buy_upside_pct"])
        z = zone_of(val_price, old.get("target"), zcfg)
        if old.get("sleeve") == "candidate" and (z != "buy" or not old.get("screen_ok")):
            continue
        rr = dict(old, code=c, price=price, zone=z, kind="etf" if is_etf else "stock", held=bool(h),
                  shares=max(0, _f((h or {}).get("shares")) or 0),
                  available=max(0, min(_f((h or {}).get("available")) or 0, _f((h or {}).get("shares")) or 0)),
                  cost=_f((h or {}).get("cost")), value=_f((h or {}).get("market_value")))
        if rr["value"] is None:
            rr["value"] = (price or 0) * rr["shares"]
        rr["upside_pct"] = round((rr["target"] / val_price - 1) * 100, 1) if val_price and rr.get("target") else None
        rr["entry_block"] = None
        if is_etf and (prem is None or prem > ecfg["prem_max_pct"]):
            rr["entry_block"] = "ETF 溢价数据缺失，暂不买" if prem is None else f"场内溢价 {prem:.1f}% > {ecfg['prem_max_pct']}%，暂不买"
        if health.get("block_add"):
            rr["data_block"] = health.get("note") or "估值不可用，暂停价格型操作"
        elif not price or q.get("is_today") is False and (snap.get("mode") == "intraday"):
            rr["data_block"] = "行情不可用，暂停价格型操作"
        elif z == "buy" and (TA is None or TA <= 0 or cash is None):
            rr["data_block"] = "账户资产/现金缺失，暂停新增"
        if rr["sleeve"] == "core":
            rr["risk"] = rc.assess(c, rr, thesis_data, today, ledger)
        rows[c] = rr
    summary = plan_actions(rows, TA or 0, cash or 0, params, cfg)
    summary.update(total_assets=TA, cash=cash)
    items = []
    for c, rr in rows.items():
        old = originals[c]
        z = rr["zone"]
        if rr["sleeve"] == "core" and z in ("sell_half", "sell_all") and not rr.get("data_block") \
                and not rr.get("sell_block") and (rr.get("risk") or {}).get("state") not in ("reduce", "exit"):
            override = {"state": "exit" if z == "sell_all" else "reduce", "trigger": f"valuation:{z}:{rr.get('target')}",
                        "reduce_fraction": 1.0 if z == "sell_all" else 0.5, "reasons": [rr["action"]]}
            vp = rc.assess(c, dict(rr, risk_override=override), thesis_data, today, ledger)
            rr["valuation_plan"] = vp
            rr["qty"] = vp["desired_sell_qty"]
            if vp["execution"].get("registered") and not rr["qty"]:
                rr["action"] = "本轮估值卖出目标已完成/暂无可卖数量，不对剩余仓位再次减半"
        rr.update(zone_name=ZONE_NAME.get(z), zone_at_review=old.get("zone"), changed=z != old.get("zone"),
                  premium_pct=_f((quotes.get(c) or {}).get("premium_pct")),
                  cost_block=rr.get("buy_block") if z == "buy" else rr.get("sell_block") if z in ("sell_half", "sell_all") else None,
                  review_action=rr["action"], review_qty=rr["qty"])
        if (rr.get("risk") or {}).get("state") in ("reduce", "exit"):
            rr["cost_block"] = None
        items.append(rr)
    risk_report = pr.assess_portfolio(snap, params, core)
    budget_state = copy.deepcopy(risk_report)
    for rr in items:
        rr["scenarios"] = pr.scenarios(rr, pr.load_policy())
        if "买" in rr.get("action", "") and rr.get("qty", 0) > 0:
            cap = pr.buy_capacity(rr["code"], budget_state, rr["price"])
            shadow_qty = min(rr["qty"], int(cap["amount"] / rr["price"] // _lot(rr["code"]) * _lot(rr["code"])))
            rr["risk_budget_check"] = {"mode": risk_report["mode"], "formal_qty": rr["qty"], "shadow_qty": shadow_qty, "reason": cap["reason"]}
            pr.reserve_buy(rr["code"], shadow_qty, rr["price"], budget_state)
            if risk_report["enforced"]:
                rr["qty"] = rr["review_qty"] = shadow_qty
                rr["action"] = rr["review_action"] = rr["action"] + "；风险预算约束已应用"
    items.sort(key=lambda x: (x["sleeve"] != "core", -(x.get("upside_pct") if x.get("upside_pct") is not None else -999)))
    return {"portfolio_risk": risk_report, "enabled": health["status"] == "fresh", "safe_mode": health["status"] != "fresh", "health": health,
            "reviewed_at": core.get("generated_at"), "summary": summary,
            "sleeves": {c: sleeve_of(c, params, core) for c in held} | {c: r["sleeve"] for c, r in rows.items() if r["sleeve"] == "core"},
            "items": items, "note": "风险退出优先于目标价和成本；普通复查只暂停新增。数量按当前快照重算，发送不等于成交。"
                    "安全态保留归属、暂停价格型操作；缺前提/契约需补齐，不凭过期数据自动卖出。"}


DEFAULTS = {"min_cap_yi": 150, "roe_last_min": 12, "roe_prev_min": 10, "roe_ytd_min": 10, "pe_max": 50,
            "debt_max": 70, "min_orgs": 3, "pe_fair_min": 8, "pe_fair_max": 40, "buy_upside_pct": 30,
            "sell_all_ratio": 1.15, "eps_cut_pct": 15,
            "cap_by_aim_max": True, "deep_loss_pct": 30, "add_below_cost_pct": 8, "sell_min_profit_pct": 5,
            "cost_includes_buy_fee": True}


def main(argv):
    params = st.load_params()
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "review":
        out, md = review(params, dry="--dry-run" in argv)
        print(md)
        return 0
    if argv[0] == "status":
        print(json.dumps(status(params), ensure_ascii=False, indent=1))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
