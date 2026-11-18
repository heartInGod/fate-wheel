#!/usr/bin/env python3
"""长线风险状态与独立执行台账。默认只读，绝不修改持仓或自动下单。

thesis.py contract 登记关键前提及失效纪律；本模块只判断已有证据。
status --snapshot PATH：只读风险结果。
register --snapshot PATH --code CODE：登记本次风险/估值卖出固定目标。
mark --cycle ID --status sent|blocked|filled|closed|reopened [--qty N --fill-id ID]
     [--reason TEXT --source SOURCE]：filled 必须是真实成交；closed 必须有恢复证据。
"""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import math
import os

from fw.paths import ROOT
from fw.datamul import data_path
THESIS_PATH = data_path("thesis.json")
LEDGER_PATH = os.path.join(ROOT, "logs", "core_risk", "events.jsonl")
TTL = {"公司": 30, "行业": 14, "商品价格": 7, "宏观": 14, "default": 14}


def _date(value=None):
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if value is None:
        return dt.date.today()
    s = str(value).split(" ")[0]
    try:
        return dt.datetime.strptime(s, "%Y%m%d").date() if len(s) == 8 else dt.date.fromisoformat(s)
    except ValueError:
        return None


def _num(value, default=0):
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except (ValueError, TypeError):
        return default


def load_thesis(path=None):
    try:
        with open(path or THESIS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
            raise ValueError("thesis 结构错误")
        return data
    except (OSError, ValueError, TypeError) as exc:
        return {"items": {}, "_errors": ["前提文件不可用：" + type(exc).__name__]}


def _fold(lines):
    result = {"cycles": {}, "errors": []}
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            e = json.loads(line)
            cid = e["cycle_id"]
            if e["event"] == "plan":
                result["cycles"].setdefault(cid, dict(e, filled_qty=0, fill_ids=[], status="planned"))
            elif cid in result["cycles"]:
                c = result["cycles"][cid]
                if e["event"] == "filled" and e["fill_id"] not in c["fill_ids"]:
                    c["filled_qty"] += e["qty"]
                    c["fill_ids"].append(e["fill_id"])
                c.update(status=e["event"], last_at=e.get("at"), last_reason=e.get("reason"), source_evidence=e.get("source"))
        except (ValueError, KeyError, TypeError):
            result["errors"].append(f"执行台账第{n}行损坏，暂停新增并人工核对")
    return result


def load_ledger(path=None):
    try:
        with open(path or LEDGER_PATH, encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            return _fold(f)
    except FileNotFoundError:
        return {"cycles": {}, "errors": []}
    except OSError as exc:
        return {"cycles": {}, "errors": ["执行台账不可读：" + type(exc).__name__]}


def _transaction(path, callback):
    path = path or LEDGER_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        ledger = _fold(f)
        if ledger["errors"]:
            raise ValueError("；".join(ledger["errors"]))
        event = callback(ledger)
        if event:
            event["at"] = dt.datetime.now().isoformat(timespec="seconds")
            f.seek(0, os.SEEK_END)
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return event


def assess(code, row, thesis_data=None, today=None, ledger=None):
    data = load_thesis() if thesis_data is None else thesis_data
    ledger = load_ledger() if ledger is None else ledger
    today = _date(today)
    item = (data.get("items") or {}).get(str(code)) or {}
    premises = [p for p in item.get("premises") or [] if p.get("status") != "已归档"]
    contract = item.get("risk_contract") or {}
    coverage = {"premises": len(premises), "contract": bool(contract),
                "complete": bool(premises and contract.get("thesis") and contract.get("metrics")
                                 and contract.get("due_date") and _date(contract["due_date"]) and contract.get("recovery"))}
    reasons = list(data.get("_errors") or []) + list(ledger.get("errors") or [])
    state, source, trigger, frac = "normal", "logic", None, None
    if not coverage["complete"]:
        state = "pause_add"
        reasons.append("投资逻辑/监控指标/兑现日期/恢复条件未登记完整，暂停新增")
    if not today:
        state = "pause_add"
        reasons.append("风险判断日期无效")
    due = _date(contract["due_date"]) if contract.get("due_date") else None
    if due and today and today > due:
        state = "pause_add"
        reasons.append("兑现节点已到期，需重新核实，不因到期自动卖出")
    critical = set(contract.get("critical_premises") or [])
    failed = []
    ttl = dict(TTL, **(data.get("stale_days") or {}))
    for p in premises:
        checked = _date(p.get("checked")) if p.get("checked") else None
        age = (today - checked).days if today and checked else None
        limit = _num(p.get("ttl_days"), ttl.get(p.get("kind"), ttl["default"]))
        fresh = age is not None and 0 <= age <= limit and bool(p.get("evidence") and p.get("source"))
        status = p.get("status")
        if not fresh or status in (None, "待核实", "存疑", "失效"):
            if state == "normal":
                state = "observe"
            reasons.append(f"{p.get('id')} {status or '待核实'}" + ("（证据过期/缺失）" if not fresh else ""))
        if p.get("id") in critical and status == "失效" and fresh:
            failed.append(p["id"])
    if critical - {p.get("id") for p in premises}:
        state = "pause_add"
        reasons.append("关键前提ID缺失，不能凭空推导退出")
    if row.get("review"):
        if state == "normal":
            state = "pause_add"
        reasons.extend(row["review"])
    if failed and coverage["complete"]:
        action = contract.get("failure_action", "pause_add")
        frac = 1.0 if action == "exit" else _num(contract.get("reduce_fraction"), None)
        if action in ("reduce", "exit") and frac is not None and 0 < frac <= 1:
            state = action
            trigger = "logic:" + ",".join(sorted(failed)) + f":{action}:{frac}"
            reasons.append("经有效证据确认关键前提失效：" + ",".join(sorted(failed)))
        else:
            state = "pause_add"
            reasons.append("关键前提失效；未明确减仓比例/退出规则，需明确纪律")
    cycles = ledger.get("cycles") or {}
    unresolved_logic = any(c.get("code") == str(code) and c.get("status") != "closed"
                           and c.get("risk_source", "logic") == "logic" for c in cycles.values())
    override = row.get("risk_override") or {}
    if override and not unresolved_logic and state not in ("reduce", "exit") and override.get("state") in ("reduce", "exit"):
        f = _num(override.get("reduce_fraction"), None)
        if f is not None and 0 < f <= 1:
            state, source, trigger, frac = override["state"], "valuation", override["trigger"], f
            reasons = list(override.get("reasons") or [])
    cycles = ledger.get("cycles") or {}
    active = [c for c in cycles.values() if c.get("code") == str(code) and c.get("status") != "closed"
              and c.get("risk_source", "logic") == source]
    # 已登记逻辑风险即使资料过期/前提恢复，也必须显式带恢复证据解除。
    if source == "logic" and active:
        c = next((c for c in active if c.get("state") == "exit"), active[-1])
        if state not in ("reduce", "exit") or c.get("state") == "exit" or c.get("trigger") == trigger:
            state, trigger, frac = c["state"], c["trigger"], c.get("fraction")
            reasons = list(dict.fromkeys(reasons + c.get("reasons", []) + ["已登记风险未解除，继续跟踪剩余暴露"]))
    shares = max(0, int(_num(row.get("shares"))))
    available = max(0, min(shares, int(_num(row.get("available")))))
    cid, target, wanted, filled, execution = None, shares, 0, 0, {"status": "none", "remaining_qty": 0}
    if state in ("reduce", "exit"):
        base = hashlib.sha256(f"{code}|{source}|{trigger}".encode()).hexdigest()[:16]
        cid = f"core-{code}-{base}"
        c = cycles.get(cid)
        if c and c.get("status") == "closed":
            state = "pause_add"
            reasons.append("该风险周期已解除；需更新前提/契约或显式重新打开，不能自动重新减仓")
        else:
            initial = c["initial_shares"] if c else shares
            target_qty = c["target_qty"] if c else (initial if state == "exit" else int(initial * frac // _lot(code) * _lot(code)))
            target = initial - target_qty
            filled = c.get("filled_qty", 0) if c else 0
            wanted = max(0, min(shares - target, target_qty - filled))
            execution = {"status": c.get("status", "unregistered") if c else "unregistered",
                         "initial_shares": initial, "target_qty": target_qty, "filled_qty": filled,
                         "remaining_qty": wanted, "available_qty": available,
                         "blocked": wanted > available, "registered": bool(c),
                         "exposure_value": shares * _num(row.get("price"))}
            if wanted > available:
                reasons.append("可卖不足/冻结或受阻，剩余风险仍在")
            if wanted == 0:
                reasons.append("该周期待卖数量已完成；不对剩余仓位再次减半，风险恢复仍需复查")
    return {"code": str(code), "state": state, "risk_source": source, "trigger": trigger, "fraction": frac,
            "block_add": state != "normal" or bool(reasons), "reasons": list(dict.fromkeys(reasons)),
            "coverage": coverage, "cycle_id": cid, "target_remaining_shares": target,
            "desired_sell_qty": min(wanted, available), "execution": execution}


def _lot(code):
    return 200 if str(code).startswith("688") else 100


def register_plan(plan, row, path=None):
    if plan.get("state") not in ("reduce", "exit") or not plan.get("cycle_id"):
        raise ValueError("没有可登记的风险卖出计划")
    e = plan["execution"]
    def add(ledger):
        if plan["cycle_id"] in ledger["cycles"]:
            return None
        return {"event": "plan", "cycle_id": plan["cycle_id"], "code": plan["code"],
                "risk_source": plan.get("risk_source", "logic"), "trigger": plan["trigger"],
                "state": plan["state"], "fraction": plan["fraction"], "reasons": plan["reasons"],
                "initial_shares": e["initial_shares"], "target_qty": e["target_qty"],
                "target_remaining_shares": plan["target_remaining_shares"]}
    return _transaction(path, add)


def mark(cycle_id, status, qty=0, fill_id=None, path=None, reason="", source=None):
    if status not in ("sent", "blocked", "filled", "closed", "reopened"):
        raise ValueError("未知执行状态")
    if status in ("closed", "reopened") and not (reason and source):
        raise ValueError("恢复/重新打开必须有原因和证据来源")
    if status == "filled" and (not fill_id or _num(qty) <= 0 or _num(qty) != int(_num(qty))):
        raise ValueError("真实成交必须有唯一fill-id及正整数qty")
    def add(ledger):
        c = ledger["cycles"].get(cycle_id)
        if not c:
            raise ValueError("先登记计划，再记执行事件")
        if c["status"] == "closed" and status not in ("closed", "reopened"):
            raise ValueError("周期已解除；必须有证据显式重新打开，发送/受阻不能重开")
        if status == "filled":
            existing = [x for x in ledger["cycles"].values() if fill_id in x.get("fill_ids", [])]
            if existing:
                if existing[0]["cycle_id"] != cycle_id:
                    raise ValueError("同一成交ID不能关联不同风险周期")
                return None
            if c["status"] == "closed" or c["filled_qty"] + qty > c["target_qty"]:
                raise ValueError("成交超过计划待卖量或周期已解除")
        return {"event": status, "cycle_id": cycle_id, "qty": int(qty), "fill_id": fill_id,
                "reason": reason, "source": source}
    return _transaction(path, add)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for cmd in ("status", "register"):
        s = sub.add_parser(cmd)
        s.add_argument("--snapshot", required=True)
        s.add_argument("--code", required=cmd == "register")
    s = sub.add_parser("mark")
    s.add_argument("--cycle", required=True)
    s.add_argument("--status", required=True, choices=["sent", "blocked", "filled", "closed", "reopened"])
    s.add_argument("--qty", type=int, default=0)
    s.add_argument("--fill-id")
    s.add_argument("--reason", default="")
    s.add_argument("--source")
    args = p.parse_args(argv)
    try:
        if args.cmd == "mark":
            result = mark(args.cycle, args.status, args.qty, args.fill_id, reason=args.reason, source=args.source)
        else:
            import core_value as cv
            import signal_store as st
            with open(args.snapshot, encoding="utf-8") as f:
                snap = json.load(f)
            date = str((snap.get("market") or {}).get("now") or snap.get("generated_at") or "")[:10]
            output = cv.status(st.load_params(), snap, date.replace("-", "") or None)
            rows = [r for r in output["items"] if not args.code or r["code"] == args.code]
            result = [{"code": r["code"], "risk": r.get("risk"), "valuation_plan": r.get("valuation_plan")} for r in rows]
            if args.cmd == "register":
                if not rows:
                    raise ValueError("当前没有该标的计划")
                row = rows[0]
                plan = row.get("valuation_plan") if (row.get("risk") or {}).get("state") not in ("reduce", "exit") else row["risk"]
                result = register_plan(plan or {}, row)
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0
    except (ValueError, OSError, TypeError) as exc:
        p.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
