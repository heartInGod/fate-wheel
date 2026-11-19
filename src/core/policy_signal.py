#!/usr/bin/env python3
"""政策信号（行业景气分的政策项）：政策事件 → 行业/个股政策分，按事件类型的半衰期指数衰减。

为什么单独一项、而且要衰减：政策信号对股价的作用集中在发布后的几天到几周，之后要么被基本面（营收/净利）验证、
进入景气分的财务项，要么被市场遗忘。所以政策分只做「加分项」，按天快速衰减；分档的盈利门槛（hot 要求净利增长）
不变——政策热、业绩冷的行业最多从 cold 拉回 neutral，不会直接变成 hot。

数据：policy_signals.json（项目根目录，人/Agent 按 data/research/policy/PROMPT.md 的规则录入；不由脚本生成）
  events[]: {id, date: YYYY-MM-DD, type: speech|presser|document|funding|regulation, title, url, local?,
             industries: {东财二级行业名: {s: -3..+3, why}}, stocks?: {代码: {s, why}}}
  s 的口径：+3 点名为核心任务且有量化目标/资金；+2 点名为重点任务；+1 提及/间接受益；
            −1 提及收紧/压减；−2 明确限制；−3 明确打压/清理整顿。

计算（参数在 rules_params.json 的 sleeves.core.industry.policy）：
  事件权重 w = 0.5 ^ (距今天数 / half_life_days[type])；w < min_weight 或事件日期晚于今天 → 失效（不计）
  行业原始值 raw = Σ s × w（同一行业多场事件叠加，新的事件相当于「续命」）
  行业政策分 pts = max_points × clip(raw / full_raw, −1, 1)          （默认一场 +3 的新事件 = +10 分）
  个股政策分 = stock_max_points × clip(Σ s × w / full_raw, −1, 1)    （用于行业分类和业务不一致的公司，如光伏分类下的储能逆变器）
  最终景气分 = 基础景气分（5 项财务/行情百分位）+ 行业政策分 [+ 个股政策分]，截到 0~100；排名、分档都按最终分。

用法：
  python3 main.py policy [YYYY-MM-DD]        # 打印当天有效事件、各行业/个股政策分（衰减后）
  python3 main.py policy check               # 校验事件文件（字段、类型、行业名是否存在于最近一次景气缓存）
"""
import datetime
import glob
import json
import os
import sys

from paths import ROOT
from datamul import data_path
DEFAULTS = {"enabled": True, "file": "policy_signals.json", "max_points": 10, "stock_max_points": 6, "full_raw": 3,
            "min_weight": 0.05,
            "half_life_days": {"speech": 5, "presser": 7, "regulation": 15, "document": 20, "funding": 30},
            "etf_industry": {}}
TYPE_NAME = {"speech": "讲话/表态", "presser": "发布会", "regulation": "监管规则", "document": "政策文件", "funding": "资金/项目落地"}


def cfg_of(icfg):
    """icfg: sleeves.core.industry 段（或其 cfg_of 结果）。"""
    p = dict((icfg or {}).get("policy") or {})
    c = dict(DEFAULTS, **{k: v for k, v in p.items() if not k.startswith("_")})
    c["half_life_days"] = dict(DEFAULTS["half_life_days"], **(p.get("half_life_days") or {}))
    return c


def load_events(cfg=None):
    cfg = cfg or DEFAULTS
    path = cfg["file"] if os.path.isabs(cfg["file"]) else data_path(cfg["file"])
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("events") or []
    except FileNotFoundError:
        return []


def _date(x):
    if isinstance(x, datetime.date):
        return x
    return datetime.datetime.strptime(str(x)[:10].replace("/", "-"), "%Y-%m-%d").date()


def weight(ev, today, cfg):
    """事件在 today 的衰减权重；未来事件或衰减到 min_weight 以下返回 0。"""
    age = (_date(today) - _date(ev["date"])).days
    if age < 0:
        return 0.0
    hl = cfg["half_life_days"].get(ev.get("type"), cfg["half_life_days"]["presser"])
    w = 0.5 ** (age / hl)
    return w if w >= cfg["min_weight"] else 0.0


def _clip(x):
    return max(-1.0, min(1.0, x))


def compute(events, today=None, cfg=None):
    """→ {industries: 行业 → {pts, raw, items[]}, stocks: 代码 → {pts, raw, items[]}, active: [事件摘要]}。"""
    cfg = cfg or DEFAULTS
    today = _date(today or datetime.date.today())
    ind, stk, active = {}, {}, []
    for ev in events:
        w = weight(ev, today, cfg)
        if w <= 0:
            continue
        active.append({"id": ev.get("id"), "date": ev["date"], "type": ev.get("type"), "title": ev.get("title"),
                       "url": ev.get("url"), "w": round(w, 3), "age_days": (today - _date(ev["date"])).days})
        for key, dst in (("industries", ind), ("stocks", stk)):
            for name, x in (ev.get(key) or {}).items():
                s = float((x or {}).get("s", 0) if isinstance(x, dict) else x)
                e = dst.setdefault(str(name), {"raw": 0.0, "items": []})
                e["raw"] += s * w
                e["items"].append({"id": ev.get("id"), "date": ev["date"], "type": ev.get("type"), "s": s, "w": round(w, 3),
                                   "why": (x or {}).get("why") if isinstance(x, dict) else None})
    for dst, mx in ((ind, cfg["max_points"]), (stk, cfg["stock_max_points"])):
        for e in dst.values():
            e["raw"] = round(e["raw"], 3)
            e["pts"] = round(mx * _clip(e["raw"] / cfg["full_raw"]), 1)
    return {"asof": today.isoformat(), "industries": ind, "stocks": stk, "active": active}


def brief(e, n=2):
    """「政策 +8.7（发布会 11-03 s+3×0.87）」这样的一句话。e: compute() 里的行业/个股项。"""
    if not e or not e.get("pts"):
        return ""
    its = sorted(e["items"], key=lambda i: -abs(i["s"] * i["w"]))[:n]
    src = "、".join(f"{TYPE_NAME.get(i['type'], i['type'])} {i['date'][5:]} s{i['s']:+.0f}×{i['w']:.2f}" for i in its)
    return f"政策 {e['pts']:+.1f}（{src}）"


def validate(events, known_industries=None):
    """返回问题列表（空 = 通过）。known_industries: 已知东财二级行业名集合（None 不校验行业名）。"""
    errs, ids = [], set()
    for i, ev in enumerate(events):
        tag = ev.get("id") or f"#{i}"
        for k in ("id", "date", "type", "title"):
            if not ev.get(k):
                errs.append(f"{tag}：缺 {k}")
        if ev.get("id") in ids:
            errs.append(f"{tag}：id 重复")
        ids.add(ev.get("id"))
        if ev.get("type") and ev["type"] not in DEFAULTS["half_life_days"]:
            errs.append(f"{tag}：type={ev['type']} 不在 {list(DEFAULTS['half_life_days'])}")
        try:
            _date(ev.get("date"))
        except (TypeError, ValueError):
            errs.append(f"{tag}：date 格式应为 YYYY-MM-DD")
        for key in ("industries", "stocks"):
            for name, x in (ev.get(key) or {}).items():
                s = (x or {}).get("s") if isinstance(x, dict) else x
                if not isinstance(s, (int, float)) or not -3 <= s <= 3:
                    errs.append(f"{tag}：{name} 的 s={s} 应在 −3..+3")
                if key == "industries" and known_industries is not None and name not in known_industries:
                    errs.append(f"{tag}：行业「{name}」不是东财二级行业名（见 cache/industry/heat_*.json）")
    return errs


def known_industries():
    fs = sorted(glob.glob(os.path.join(ROOT, "cache", "industry", "heat_*.json")))
    if not fs:
        return None
    with open(fs[-1], encoding="utf-8") as f:
        return set(json.load(f).get("industries") or {})


def main(argv):
    import signal_store as st
    import industry_heat as ih
    cfg = cfg_of(ih.cfg_of((st.load_params().get("sleeves") or {}).get("core")))
    evs = load_events(cfg)
    if argv and argv[0] == "check":
        errs = validate(evs, known_industries())
        print("\n".join(errs) if errs else f"✅ {len(evs)} 个事件校验通过")
        return 1 if errs else 0
    r = compute(evs, argv[0] if argv else None, cfg)
    print(f"政策信号 {r['asof']}：有效事件 {len(r['active'])}/{len(evs)}")
    for a in r["active"]:
        print(f"  {a['date']} {TYPE_NAME.get(a['type'], a['type'])} 权重 {a['w']:.2f}（{a['age_days']} 天）{a['title']}")
    print("行业政策分：")
    for k, e in sorted(r["industries"].items(), key=lambda x: -x[1]["pts"]):
        print(f"  {k}: {e['pts']:+.1f}（raw {e['raw']:+.2f}）")
    if r["stocks"]:
        print("个股政策分：")
        for k, e in sorted(r["stocks"].items(), key=lambda x: -x[1]["pts"]):
            print(f"  {k}: {e['pts']:+.1f}（raw {e['raw']:+.2f}）" + "；".join(i.get("why") or "" for i in e["items"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
