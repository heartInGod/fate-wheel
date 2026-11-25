"""大盘资金面分档与宽度闸门（只做背景判断，不产生交易信号、不改阈值）。

输入 market_flow.collect() 的原始指标，输出：
  regime     ice | weak | normal | strong | none(数据不足)
  label      中文标签
  evidence   支撑该档位的证据链（每条带具体数值）
  gate       买入类信号的提醒（仅 flag，不自动改写信号）
  crosses    交叉验证说明（哪些指标同向、哪些背离）

分档规则（阈值在 rules_params.json 的 market_regime 段，可配置）：
  先看宽度（up_ratio + ud_ratio），再用成交额分位与主力资金 20 日累计做交叉验证：
  - ice:   上涨占比 ≤ ice_up_ratio 且 涨跌停比 ≤ ice_ud_ratio，
           且（成交额分位 ≤ low_pct_rank 或 主力20日累计净流出 ≤ -big_out_yi）
  - weak:  上涨占比 ≤ weak_up_ratio 或 涨跌停比 ≤ weak_ud_ratio
  - strong: 上涨占比 ≥ strong_up_ratio 且 成交额分位 ≥ strong_pct_rank
  - normal: 其余
  数据不足（宽度缺失）时为 none，gate 为空。

设计原则：脚本只给「档位 + 数值证据 + 提醒文案」，因果叙事（为何冰点、外部冲击是什么）
由大模型用 WebSearch 补，且必须标注来源与软/硬催化属性。
"""
import json
import os

# market_regime.py 位于 <ROOT>/src/core/ 下，上溯三级即项目根
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULTS = {
    "enabled": True,
    "ice_up_ratio": 15.0, "ice_ud_ratio": 0.8,
    "weak_up_ratio": 35.0, "weak_ud_ratio": 1.0,
    "strong_up_ratio": 65.0, "strong_pct_rank": 60.0,
    "low_pct_rank": 20.0, "big_out_yi": -500.0,
    "sell_types": ["stop", "reduce", "t_high"],
    "buy_types": ["add", "t_low", "watch_dip", "watch_break"],
}

_LABEL = {
    "ice": "市场宽度冰点", "weak": "市场宽度偏弱",
    "normal": "市场宽度中性", "strong": "市场宽度强势", "none": "数据不足",
}


def _params():
    try:
        p = os.path.join(ROOT, "data", "config", "rules_params.json")
        with open(p, encoding="utf-8") as f:
            cfg = json.load(f).get("market_regime") or {}
        return {**DEFAULTS, **cfg}
    except Exception:  # noqa: BLE001
        return dict(DEFAULTS)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def classify(flow, params=None):
    """返回 {regime,label,evidence,crosses,gate,metrics}。gate 为买入类提醒文案或 None。"""
    # 始终以 DEFAULTS 为底合并：调用方可能传完整 rules_params、market_regime 子段或空 dict
    _p = params or _params()
    P = {**DEFAULTS, **_p} if isinstance(_p, dict) else dict(DEFAULTS)
    res = {"regime": "none", "label": _LABEL["none"], "evidence": [],
           "crosses": [], "gate": None, "metrics": {}}
    if not P.get("enabled", True) or not flow:
        return res
    br = flow.get("breadth") or {}
    tr = flow.get("turnover") or {}
    mf = flow.get("main_flow") or {}
    mg = flow.get("margin") or {}

    up = _f(br.get("up_ratio"))
    ud = _f(br.get("ud_ratio"))
    rank = _f(tr.get("pct_rank"))
    cum = _f(mf.get("cum_yi"))
    down_days = mg.get("down_days")
    res["metrics"] = {
        "up_ratio": up, "ud_ratio": ud, "up": br.get("up"), "down": br.get("down"),
        "limit_up": br.get("limit_up"), "limit_down": br.get("limit_down"),
        "turnover_yi": tr.get("yi"), "turnover_rank": rank,
        "main_today_yi": mf.get("today_yi"), "main_20d_yi": cum,
        "margin_balance_yi": mg.get("balance_yi"), "margin_down_days": down_days,
    }

    if up is None and ud is None:
        res["evidence"].append("涨跌家数/涨跌停数据缺失，无法分档")
        return res

    # 证据链：只列有数值的
    if up is not None:
        res["evidence"].append(f"上涨家数占比 {up:g}%（{br.get('up')} 涨 / {br.get('down')} 跌 / {br.get('flat')} 平）")
    if ud is not None:
        res["evidence"].append(f"涨跌停比 {br.get('limit_up')} 涨停 / {br.get('limit_down')} 跌停 = {ud:g}")
    if tr.get("yi") is not None:
        s = f"两市成交额 {tr['yi']:g} 亿元"
        if rank is not None:
            s += f"（近 250 日 {rank:g}% 分位" + ("，年内低位" if rank <= P["low_pct_rank"] else "") + "）"
        res["evidence"].append(s)
    if cum is not None:
        res["evidence"].append(f"主力资金近 {mf.get('days', 20)} 日累计净额 {cum:+.1f} 亿元"
                               + ("（持续净流出）" if cum <= P["big_out_yi"] else ""))
    if down_days:
        res["evidence"].append(f"融资余额 {mg.get('balance_yi')} 亿元，连续 {down_days} 个交易日下降")

    ice_hit = (up is not None and up <= P["ice_up_ratio"]) and (ud is not None and ud <= P["ice_ud_ratio"])
    cross_hit = (rank is not None and rank <= P["low_pct_rank"]) or (cum is not None and cum <= P["big_out_yi"])
    strong_hit = (up is not None and up >= P["strong_up_ratio"] and
                  rank is not None and rank >= P["strong_pct_rank"])
    weak_hit = ((up is not None and up <= P["weak_up_ratio"]) or
                (ud is not None and ud <= P["weak_ud_ratio"]))

    if ice_hit and cross_hit:
        regime = "ice"
    elif weak_hit:
        regime = "weak"
    elif strong_hit:
        regime = "strong"
    else:
        regime = "normal"
    res["regime"] = regime
    res["label"] = _LABEL[regime]

    # 交叉验证说明
    if rank is not None and cum is not None:
        if rank <= P["low_pct_rank"] and cum <= P["big_out_yi"]:
            res["crosses"].append("成交额低位 + 主力持续净流出，交叉验证存量资金不足")
        elif rank >= P["strong_pct_rank"] and cum > 0:
            res["crosses"].append("成交额高位 + 主力净流入，交叉验证增量资金入场")
        else:
            res["crosses"].append(f"成交额分位 {rank:g}% 与主力 {cum:+.1f} 亿方向需结合判断")

    # 闸门：只提醒，不改信号、不改阈值
    if regime == "ice":
        res["gate"] = ("市场宽度冰点（上涨占比/涨跌停比双低，且成交额低位或主力持续净流出）："
                       "指数微涨不视为市场转健康，反弹更可能是存量轮动而非趋势反转；"
                       "买入类建议从严，等宽度修复后再放松，止损不加宽。")
    elif regime == "weak":
        res["gate"] = ("市场宽度偏弱：加仓/低吸从严，优先等回踩关键支撑且放量确认；"
                       "反弹按存量轮动对待，止损不加宽。")
    return res


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.join(ROOT, "src", "datasources"))
    import market_flow
    refresh = "refresh" in sys.argv
    flow = market_flow.collect(refresh=refresh)
    print(__import__("json").dumps(classify(flow), ensure_ascii=False, indent=1, default=str))
