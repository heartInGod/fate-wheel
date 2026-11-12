#!/usr/bin/env python3
"""记录信号的判断与发送状态（写 logs/signals/yyyyMMdd.jsonl，只追加）。

  python3 record_signal.py decide <id> accept|reject [--note 理由] [--back 低~高]
      对规则引擎的候选做判断。采纳的才写进飞书提醒。采纳做T时可用 --back 改写回补/卖回区。
  python3 record_signal.py add --code 600353 --type 减仓 --price 19.84 [--zone 20.0~20.2] [--qty 500] [--back 低~高] [--tp 止盈价] [--note 理由] [--time HH:MM] [--force]
      加仓 / 自选低吸 / 自选突破（交易仓买入类）自动附 tp_price = 现价（无现价用区间上沿）× (1 + sleeves.trade.min_move_pct%)
      和 hold_days，--tp 可改写止盈价
      大模型自己补充的信号（规则没覆盖到的，如公告/新闻驱动）。
      做T必须成对：做T高抛必须带 --back 回补区（低于高抛价），做T低吸必须带 --back 卖回区（高于低吸价），
      数量相同；缺少或方向不对时拒绝记录（返回码 2）。不打算接回/卖回的请改用 减仓/加仓。type 可用：
      加仓 / 做T低吸 / 做T高抛 / 减仓 / 止损 / 溢价 / 不加仓 / 异动（或 add/t_low/t_high/reduce/stop/premium/hold_off/alert）
      自选股（watchlist.csv，未持有）只用：自选低吸 / 自选突破（或 watch_dip/watch_break）
  python3 record_signal.py sent <id> [<id> ...]          飞书发送成功后标记
  python3 record_signal.py send-failed <id> [<id> ...]   发送失败（之后允许重提）
  python3 record_signal.py list [--date yyyyMMdd]         查看当天信号与状态
  python3 record_signal.py import-log logs/20261030.md    从旧格式日志导入历史信号（回填，用于评估）

日期默认今天；--date yyyyMMdd 可指定。
"""
import csv
import datetime
import os
import re
import sys

import signal_store as st

ROOT = os.path.dirname(os.path.abspath(__file__))
HOLDINGS = os.path.join(ROOT, "holdings.csv")
WATCHLIST = os.path.join(ROOT, "watchlist.csv")


def _opt(args, name, default=None):
    if name in args:
        i = args.index(name)
        v = args[i + 1] if i + 1 < len(args) else default
        del args[i:i + 2]
        return v
    return default


def _norm_code(c):
    c = (c or "").strip().upper().replace(".HK", "")
    if c[:2] in ("SH", "SZ", "HK") and c[2:].isdigit():
        c = c[2:]
    if c.isdigit() and len(c) <= 5:
        c = c.zfill(5)
    return c


def _name_of(code):
    """先查持仓 holdings.csv，再查自选股 watchlist.csv。"""
    for path in (HOLDINGS, WATCHLIST):
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if _norm_code(r.get("代码")) == code:
                    return r.get("名称", "")
    return ""


def _zone(s):
    if not s:
        return None
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", s)]
    if not nums:
        return None
    return [min(nums), max(nums)]


def cmd_decide(day, args):
    if len(args) < 2 or args[1] not in ("accept", "reject"):
        print("用法: record_signal.py decide <id> accept|reject [--note 理由]")
        return 1
    note = _opt(args, "--note", "")
    sid, dec = args[0], args[1]
    back = _zone(_opt(args, "--back"))
    sigs = st.load_signals(day)
    if sid not in sigs:
        print(f"找不到信号 {sid}（日期 {day}）。用 list 查看。")
        return 1
    s = sigs[sid]
    ev = {"event": "decision", "id": sid, "decision": dec, "note": note}
    if dec == "accept" and s.get("type") in st.T_TYPES:
        pair = back or s.get("pair_zone")
        err = st.check_pair(s["type"], s.get("zone"), s.get("price"), pair)
        if err:
            print(f"❌ 未记录：{err}（用 --back 低~高 补上，或否决后改用 add 记成 减仓/加仓）")
            return 2
        if back:
            ev["pair_zone"] = back
    st.append_events(day, [ev])
    print(f"✅ {sid} → {dec}" + (f"（{note}）" if note else "")
          + (f"  {st.pair_text({**s, 'pair_zone': ev.get('pair_zone') or s.get('pair_zone')})}"
             if dec == "accept" and s.get("type") in st.T_TYPES else ""))
    return 0


def cmd_add(day, args, source="llm"):
    code = _norm_code(_opt(args, "--code", ""))
    typ = st.norm_type(_opt(args, "--type", ""))
    price = _opt(args, "--price")
    zone = _zone(_opt(args, "--zone"))
    back = _zone(_opt(args, "--back"))
    qty = _opt(args, "--qty")
    note = _opt(args, "--note", "")
    tm = _opt(args, "--time") or datetime.datetime.now().strftime("%H:%M")
    if not code or typ not in st.TYPES:
        print(f"需要 --code 和合法的 --type（{', '.join(st.TYPES)}）")
        return 1
    err = st.check_pair(typ, zone, float(price) if price else None, back)
    if err:
        print(f"❌ 未记录：{err}")
        return 2
    force = "--force" in args
    sid = f"{day}-{tm.replace(':', '')}-{code}-{typ}-{source}"
    sigs = st.load_signals(day)
    if sid in sigs:
        print(f"已存在 {sid}，跳过")
        return 0
    side = st.side_of(typ)
    if price and side and not force:
        lim = st.load_params()["dedup"]["price_change_pct"]
        d = st.find_dup(sigs, code, side, float(price), tm, lim)
        if d:
            print(f"⚠️ 去重：今天已有同标的同方向信号 {d}，价格变化不到 {lim}%，未记录。"
                  f"确有新理由（如新公告）可加 --force，并在日志「被去重跳过」或理由里说明。")
            return 3
    # 交易仓买入类：附止盈价（+min_move_pct，基准为现价，盘前没现价用区间上沿）与最长持有天数，供 signals_engine.open_trades 提醒
    tp = {}
    tc = (st.load_params().get("sleeves") or {}).get("trade") or {}
    base = float(price) if price else (max(zone) if zone else None)
    if typ in ("add", "watch_dip", "watch_break") and base and tc.get("min_move_pct"):
        tp_opt = _opt(args, "--tp")
        tp = {"tp_price": float(tp_opt) if tp_opt else round(base * (1 + tc["min_move_pct"] / 100), 3),
              "hold_days": tc.get("hold_days", 5)}
    ev = {"event": "add", "id": sid, "date": day, "time": tm, "code": code, "name": _name_of(code), **tp,
          "type": typ, "label": st.label_of(typ), "side": st.side_of(typ),
          "price": float(price) if price else None, "zone": zone,
          **({"pair_zone": back} if typ in st.T_TYPES else {}),
          "qty": int(float(qty)) if qty else None, "reason": note, "rule": "llm_premarket" if tm < "09:25" else source,
          "source": source, "status": "new", "decision": "accept"}
    st.append_events(day, [ev])
    print(f"✅ 已记录 {sid}")
    print(sid)
    return 0


def cmd_mark(day, args, event):
    sigs = st.load_signals(day)
    bad = [a for a in args if a not in sigs]
    ok = [a for a in args if a in sigs]
    if ok:
        st.append_events(day, [{"event": event, "id": a} for a in ok])
    for a in ok:
        print(f"✅ {a} → {event}")
    for a in bad:
        print(f"⚠️ 找不到 {a}")
    return 0 if ok else 1


def cmd_list(day):
    sigs = st.load_signals(day)
    if not sigs:
        print(f"{day} 没有信号记录")
        return 0
    for sid, s in sigs.items():
        state = s.get("status")
        if state == "new":
            state = {"accept": "采纳", "reject": "否决"}.get(s.get("decision"), "待判断")
            if s.get("sent"):
                state += "·已发"
            elif s.get("send_failed"):
                state += "·发送失败"
        print(f"{s.get('time')} {s.get('label')} {s.get('name')}({s.get('code')}) {s.get('price')} "
              f"区间{s.get('zone')}{(' ' + st.pair_text(s)) if st.pair_text(s) else ''} 数量{s.get('qty')} "
              f"[{s.get('source')}] {state}  id={sid}")
    return 0


# ---------------------------------------------------------------- 旧日志导入
_SEC = re.compile(r"^##\s+(\d{1,2}:\d{2})\s*(.*)$")
_ICON = re.compile(r"^(?:-\s*信号[:：]\s*|\s*-\s*)(🟢|🔴|🔄|⚡|⚠️)\s*(.+)$")
_CODE = re.compile(r"(?<!\d)(\d{6}|\d{5})(?!\d)")
_RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*[~～]\s*(\d+(?:\.\d+)?)")


def _qty(text):
    m = re.search(r"(?<![\d.~])(\d+(?:\.\d+)?)\s*(万)?(?:\s*[~～]\s*\d+(?:\.\d+)?\s*万?)?\s*股", text)
    if not m:
        return None
    q = float(m.group(1)) * (10000 if m.group(2) else 1)
    return int(q)


def _infer_types(label, action):
    """返回 [(type, zone)]。"""
    txt = label + " " + action
    ranges = [[float(a), float(b)] for a, b in _RANGE.findall(action)]
    if "不加仓" in label:
        return [("hold_off", None)]
    if "做T" in label or "做t" in label:
        out = []
        m_lo = re.search(r"低吸区?\s*(\d+(?:\.\d+)?)\s*[~～]\s*(\d+(?:\.\d+)?)", action)
        m_hi = re.search(r"高抛区?\s*(\d+(?:\.\d+)?)\s*[~～]\s*(\d+(?:\.\d+)?)", action)
        m_hi2 = re.search(r"(\d+(?:\.\d+)?)\s*[~～]\s*(\d+(?:\.\d+)?)\s*高抛", action)
        if m_lo:
            out.append(("t_low", [float(m_lo.group(1)), float(m_lo.group(2))]))
        if m_hi or m_hi2:
            m = m_hi or m_hi2
            out.append(("t_high", [float(m.group(1)), float(m.group(2))]))
        return out or [("t_high" if "高抛" in action else "t_low", ranges[0] if ranges else None)]
    zone = ranges[0] if ranges else None
    if "加仓" in label:
        return [("add", zone)]
    if "止损" in label:
        return [("stop", zone)]
    if "溢价" in label:
        return [("premium", None)]
    if "减仓" in label or "清仓" in label:
        return [("reduce", zone)]
    if "异动" in label:
        if re.search(r"卖|减|清仓|止损", action):
            return [("reduce", zone)]
        return [("alert", None)]
    return []


def cmd_import_log(path):
    day = re.search(r"(\d{8})", os.path.basename(path)).group(1)
    lines = open(path, encoding="utf-8").read().split("\n")
    # 先按段落切分
    sections, cur = [], None
    for ln in lines:
        m = _SEC.match(ln)
        if m:
            cur = {"time": m.group(1).zfill(5), "title": m.group(2), "lines": []}
            sections.append(cur)
        elif cur:
            cur["lines"].append(ln)
    existing = st.load_signals(day)
    events = []
    for sec in sections:
        if not re.search(r"盘前|盘中|收盘", sec["title"]):
            continue
        sent = any("飞书" in l and "已发送" in l for l in sec["lines"])
        premarket = "盘前" in sec["title"]
        for ln in sec["lines"]:
            m = _ICON.match(ln)
            if not m:
                continue
            icon, body = m.group(1), m.group(2)
            if premarket:
                # 盘前格式：「🔴 减仓·清仓：旭光电子 600353 —— 理由 … 参考价 19.8~20.2」
                mm = re.match(r"([^：:]+)[：:]\s*(.+)$", body)
                if not mm or not _CODE.search(mm.group(2)[:40]):
                    continue
                label, rest = mm.group(1), mm.group(2)
                codes = [_CODE.search(rest[:40]).group(1)]
                action, reason, price_field = rest, rest, ""
            else:
                parts = [p.strip() for p in body.split("|")]
                if len(parts) < 3:
                    continue
                label = parts[0]
                codes = _CODE.findall(parts[1])
                price_field = parts[2]
                action = parts[3] if len(parts) > 3 else ""
                reason = parts[4] if len(parts) > 4 else ""
            for code in codes:
                code = _norm_code(code)
                price = None
                if premarket:
                    price = None  # 评估时取开盘价
                else:
                    pm = re.search(rf"{code[-5:]}\D{{0,3}}现价\s*(\d+(?:\.\d+)?)", price_field) \
                        or re.search(r"(\d+(?:\.\d+)?)", price_field.replace("现价", ""))
                    price = float(pm.group(1)) if pm else None
                for typ, zone in _infer_types(icon + label, action):
                    tm = "09:30" if premarket else sec["time"]
                    sid = f"{day}-{sec['time'].replace(':', '')}-{code}-{typ}-log"
                    if sid in existing or any(e["id"] == sid for e in events):
                        continue
                    ev = {"event": "add", "id": sid, "date": day, "time": tm, "code": code,
                          "name": _name_of(code) or parts_name(body), "type": typ,
                          "label": st.label_of(typ), "side": st.side_of(typ), "price": price,
                          "zone": zone, "qty": _qty(action), "reason": (reason or action)[:200],
                          "rule": "llm_premarket" if premarket else "llm", "source": "log",
                          "status": "new", "decision": "accept", "orig_label": icon + label,
                          "log_section": f"{sec['time']} {sec['title']}"}
                    events.append(ev)
                    if sent:
                        events.append({"event": "sent", "id": sid})
    if events:
        st.append_events(day, events)
    n = sum(1 for e in events if e["event"] == "add")
    print(f"✅ 从 {path} 导入 {n} 条信号到 {st.day_path(day)}")
    return 0


def parts_name(body):
    m = re.search(r"\|\s*([^|\d]+?)\s*\d{5,6}", body)
    return m.group(1).strip() if m else ""


def main(argv):
    args = list(argv)
    if not args:
        print(__doc__)
        return 1
    cmd = args.pop(0)
    day = _opt(args, "--date") or datetime.date.today().strftime("%Y%m%d")
    if cmd == "decide":
        return cmd_decide(day, args)
    if cmd == "add":
        return cmd_add(day, args)
    if cmd == "sent":
        return cmd_mark(day, args, "sent")
    if cmd == "send-failed":
        return cmd_mark(day, args, "send_failed")
    if cmd == "list":
        return cmd_list(day)
    if cmd == "import-log":
        return cmd_import_log(args[0])
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
