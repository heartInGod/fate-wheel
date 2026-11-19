"""信号存储：data/report/signals/yyyyMMdd.jsonl，只追加的事件流。

事件类型（event 字段）：
  candidate  规则引擎算出的候选信号（source=engine / catchup / backfill）
  add        大模型自行补充的信号（source=llm），或从日志导入的历史信号（source=log）
  decision   大模型对候选的判断：accept（采纳并提醒）/ reject（否决，附理由）
  sent       已发送通知
  send_failed 发送失败

load_signals() 把事件折叠成「每条信号的最终状态」。
"""
import datetime
import json
import os

from paths import ROOT
from datamul import data_path
SIG_DIR = os.path.join(ROOT, "data", "report", "signals")
PARAMS_PATH = data_path("rules_params.json")

# 信号类型 → (图标标签, 方向)。方向 +1=买入方向，-1=卖出/回避买入，0=中性提示
TYPES = {
    "add": ("🟢加仓", 1),
    "t_low": ("🔄做T低吸", 1),
    "t_high": ("🔄做T高抛", -1),
    "reduce": ("🔴减仓", -1),
    "stop": ("🔴止损", -1),
    "premium": ("⚡溢价不追", -1),
    "hold_off": ("⚠️暂不加仓", -1),
    "alert": ("⚡异动", 0),
    "watch_dip": ("👀自选低吸", 1),
    "watch_break": ("👀自选突破", 1),
}
WATCH_TYPES = ("watch_dip", "watch_break")
# 同一标的同一方向多条时的优先级（高的保留）
PRIORITY = {"stop": 6, "reduce": 5, "t_high": 4, "premium": 3, "hold_off": 2,
            "add": 5, "t_low": 4, "alert": 1, "watch_dip": 5, "watch_break": 4}

TYPE_ALIASES = {
    "加仓": "add", "低吸": "t_low", "做T低吸": "t_low", "做t低吸": "t_low",
    "高抛": "t_high", "做T高抛": "t_high", "做t高抛": "t_high",
    "减仓": "reduce", "清仓": "reduce", "止损": "stop", "溢价": "premium",
    "不加仓": "hold_off", "暂不加仓": "hold_off", "异动": "alert",
    "自选低吸": "watch_dip", "建仓": "watch_dip", "自选突破": "watch_break", "突破": "watch_break",
}


def load_params(path=PARAMS_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def side_of(sig_type):
    return TYPES.get(sig_type, ("", 0))[1]


def label_of(sig_type):
    return TYPES.get(sig_type, (sig_type, 0))[0]


def norm_type(t):
    t = (t or "").strip()
    if t in TYPES:
        return t
    return TYPE_ALIASES.get(t, TYPE_ALIASES.get(t.replace(" ", ""), t))


def day_path(day):
    return os.path.join(SIG_DIR, f"{day}.jsonl")


def append_events(day, events):
    os.makedirs(SIG_DIR, exist_ok=True)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(day_path(day), "a", encoding="utf-8") as f:
        for e in events:
            e.setdefault("ts", now)
            f.write(json.dumps(e, ensure_ascii=False, default=str) + "\n")


def read_events(day):
    p = day_path(day)
    if not os.path.exists(p):
        return []
    out = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


T_TYPES = ("t_high", "t_low")
PAIR_RULE = ("做T必须成对：高抛要同时给回补区（低于高抛价），低吸要同时给卖回区（高于低吸价），数量相同。"
             "不打算接回/卖回的，请改用 减仓 / 加仓。")


def check_pair(typ, zone, price, pair):
    """校验做T成对区间，合格返回 None，否则返回错误说明。非做T类型直接通过。"""
    if typ not in T_TYPES:
        return None
    if not pair or len(pair) != 2:
        return "缺少反向区间。" + PAIR_RULE
    lo, hi = min(pair), max(pair)
    ref = [x for x in (zone or []) + ([price] if price else []) if x]
    if not ref:
        return None
    if typ == "t_high" and hi >= min(ref):
        return f"回补区 {lo}~{hi} 必须低于高抛价（{min(ref)}）。"
    if typ == "t_low" and lo <= max(ref):
        return f"卖回区 {lo}~{hi} 必须高于低吸价（{max(ref)}）。"
    return None


def pair_text(sig):
    """'回补 a~b' / '卖回 a~b'，没有则空串。"""
    p = sig.get("pair_zone")
    if not p or sig.get("type") not in T_TYPES:
        return ""
    return f"{'回补' if sig['type'] == 't_high' else '卖回'} {p[0]}~{p[1]}"


def load_signals(day):
    """折叠事件，返回 {id: signal}，按时间排序的 dict。

    signal 额外字段：decision(accept/reject/None)、decision_note、sent(bool)、send_failed(bool)
    """
    sigs = {}
    for e in read_events(day):
        ev = e.get("event")
        if ev in ("candidate", "add"):
            s = {k: v for k, v in e.items() if k not in ("event",)}
            s.setdefault("decision", "accept" if ev == "add" else None)
            s.setdefault("decision_note", "")
            s.setdefault("sent", False)
            s.setdefault("send_failed", False)
            sigs.setdefault(e["id"], s)
        elif ev == "decision" and e.get("id") in sigs:
            sigs[e["id"]]["decision"] = e.get("decision")
            sigs[e["id"]]["decision_note"] = e.get("note", "")
            if e.get("pair_zone"):   # 采纳时可以改写做T的回补/卖回区
                sigs[e["id"]]["pair_zone"] = e["pair_zone"]
        elif ev == "sent" and e.get("id") in sigs:
            sigs[e["id"]]["sent"] = True
            sigs[e["id"]]["send_failed"] = False
        elif ev == "send_failed" and e.get("id") in sigs:
            sigs[e["id"]]["send_failed"] = True
    return dict(sorted(sigs.items(), key=lambda kv: (kv[1].get("time", ""), kv[0])))


def is_effective(sig):
    """参与去重的信号：未被判为重复/屏蔽的候选（含已被否决的，避免同一条每个时点反复冒出），
    以及采纳/已发送的信号；发送失败的不算（张小凡没收到，允许下次重提）。"""
    if sig.get("status") in ("dup", "group_suppressed", "blocked", "capped", "ev_negative", "slots_full"):
        return False
    if sig.get("send_failed"):
        return False
    return True


def find_dup(sigs, code, side, price, before_time, pct_limit, include_all=False):
    """在 before_time（含）之前找同标的同方向、价格变化 < pct_limit% 的有效信号，返回其 id。
    include_all=True 时连被屏蔽/落选的记录也算（用于屏蔽类候选自身去重，避免每个时点重复记录）。"""
    best = None
    for sid, s in sigs.items():
        if s.get("code") != code or side_of(s.get("type")) != side:
            continue
        if s.get("time", "") > before_time:
            continue
        if not include_all and not is_effective(s):
            continue
        p0 = s.get("price")
        if not p0 or not price:
            continue
        if abs(price / p0 - 1) * 100 < pct_limit:
            best = sid
    return best
