#!/usr/bin/env python3
"""维护持仓定位前提 thesis.json（规则见 datasources/thesis.py）。

  python3 main.py thesis list [--stale] [--json]
      列出每只持仓的定位与前提（age 天数、是否需要核实）；--stale 只看需要核实 / 缺前提 / 有待确认建议的。
  python3 main.py thesis sync
      为 holdings.csv 里还没有条目的持仓建空条目（不删除已清仓的条目）。
  python3 main.py thesis add CODE --text 前提 --kind 商品价格|行业|公司|宏观 [--supports 支撑的定位]
                        [--query 核实用搜索词] [--status 成立|存疑|失效|待核实] [--evidence 依据] [--source 来源] [--since 日期]
      新增一条前提（status 默认 待核实）。
  python3 main.py thesis check CODE PID --status 成立|存疑|失效 --evidence 依据（带数据和日期） [--source 来源] [--text 改写表述]
      记录一次核实：更新 checked 日期、状态、依据，旧值写入 history。
  python3 main.py thesis archive CODE PID [--reason 原因]
      归档不再相关的前提（保留在文件里，不再要求核实）。
  python3 main.py thesis propose CODE --stance 新定位 --reason 理由 [--apply --by 09:00]
      提出定位调整建议（pending，同一只只保留最新一条）；带 --apply 时立即执行（等同随后 apply）。
  python3 main.py thesis apply CODE [--stance 改写后的定位] [--by 09:00|张小凡]
      把建议写入 holdings.csv「当前定位」。定位由模型自动维护：09:00 盘前 propose 后直接 apply --by 09:00，
      不等人工确认；盘中任务、16:00 任务不得调用。
  python3 main.py thesis revert CODE [--reason 原因]
      撤回最近一次 apply，恢复原定位（张小凡不同意自动调整时用）。
  python3 main.py thesis reject CODE [--reason 原因]
      否决尚未执行的建议。
  python3 main.py thesis link CODE [品种ID ...] [--clear]
      设置该持仓关联的商品 / 行业价格品种（如 000657 W_ORE APT），盘前快照会带上这些品种的价格和 1 周 / 1 月变化，
      供核实「商品价格」类前提；不带品种 ID 时列出可选品种。

  python3 main.py thesis contract CODE --text 持有逻辑 --metrics 指标1,指标2 --due YYYY-MM-DD --recovery 恢复条件
      [--critical P1,P2 --on-failure pause_add|reduce|exit --reduce-fraction 比例]
      登记监控/兑现/失效纪律；减仓比例必须显式给出，前提事实仍需 check 核实，不自动修改定位。

日期默认今天，--date YYYY-MM-DD 可指定。
"""
import csv
import datetime
import json
import os
import sys

from datasources import thesis as T

from fw.paths import ROOT
from fw.datamul import data_path
HOLDINGS = data_path("holdings.csv")


def _opt(args, name, default=None):
    if name in args:
        i = args.index(name)
        v = args[i + 1] if i + 1 < len(args) else default
        del args[i:i + 2]
        return v
    return default


def _flag(args, name):
    if name in args:
        args.remove(name)
        return True
    return False


def _read_holdings():
    with open(HOLDINGS, encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        return r.fieldnames, list(r)


def _save(data):
    tmp = T.THESIS_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, T.THESIS_JSON)


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def _item(data, code, create=False):
    c = T.norm(code)
    it = data["items"].get(c)
    if it is None:
        if not create:
            sys.exit(f"thesis.json 里没有 {code}，先运行 thesis.py sync 或 add")
        name = next((r.get("名称") for r in _read_holdings()[1] if T.norm(r.get("代码")) == c), None)
        it = data["items"][c] = {"name": name, "premises": [], "proposal": None, "history": []}
    it.setdefault("premises", [])
    it.setdefault("history", [])
    return c, it


def _premise(it, pid):
    for p in it["premises"]:
        if p.get("id") == pid:
            return p
    sys.exit(f"找不到前提 {pid}，现有：{', '.join(p.get('id', '?') for p in it['premises']) or '无'}")


def cmd_sync(data, args, today):
    added = []
    for r in _read_holdings()[1]:
        if not (r.get("代码") or "").strip():
            continue
        c = T.norm(r["代码"])
        if c not in data["items"]:
            data["items"][c] = {"name": r.get("名称"), "premises": [], "proposal": None, "history": []}
            added.append(c)
    _save(data)
    print(f"已同步，新建 {len(added)} 条：{', '.join(added) or '无'}")


def cmd_add(data, args, today):
    code = args.pop(0)
    text = _opt(args, "--text")
    kind = _opt(args, "--kind", "公司")
    if not text:
        sys.exit("缺少 --text")
    status = _opt(args, "--status", "待核实")
    if status not in T.STATUSES:
        sys.exit(f"status 只能是 {'/'.join(T.STATUSES)}")
    c, it = _item(data, code, create=True)
    n = 1 + max([int(p["id"][1:]) for p in it["premises"] if str(p.get("id", "")).startswith("P")
                 and p["id"][1:].isdigit()] or [0])
    p = {"id": f"P{n}", "text": text, "kind": kind, "supports": _opt(args, "--supports"),
         "query": _opt(args, "--query"), "status": status, "since": _opt(args, "--since", today),
         "checked": today if status != "待核实" else None,
         "evidence": _opt(args, "--evidence"), "source": _opt(args, "--source")}
    it["premises"].append(p)
    it["history"].append({"at": _now(), "op": "add", "id": p["id"], "text": text, "status": status})
    _save(data)
    print(f"{c} {it.get('name')} 新增 {p['id']}：{text}（{status}）")


def cmd_check(data, args, today):
    code, pid = args.pop(0), args.pop(0)
    status = _opt(args, "--status")
    ev = _opt(args, "--evidence")
    if status not in T.STATUSES[1:]:
        sys.exit("--status 只能是 成立/存疑/失效")
    if not ev:
        sys.exit("缺少 --evidence（写清数据和日期）")
    c, it = _item(data, code)
    p = _premise(it, pid)
    old = {k: p.get(k) for k in ("text", "status", "checked", "evidence")}
    new_text = _opt(args, "--text")
    if new_text:
        p["text"] = new_text
    p.update({"status": status, "checked": today, "evidence": ev, "source": _opt(args, "--source", p.get("source"))})
    it["history"].append({"at": _now(), "op": "check", "id": pid, "old": old, "status": status, "evidence": ev})
    _save(data)
    tip = "；该前提已不能作为操作理由，若影响定位请运行 thesis.py propose" if status in ("失效", "存疑") else ""
    print(f"{c} {it.get('name')} {pid}：{old['status']} → {status}{tip}")


def cmd_archive(data, args, today):
    code, pid = args.pop(0), args.pop(0)
    c, it = _item(data, code)
    p = _premise(it, pid)
    p["status"] = "已归档"
    it["history"].append({"at": _now(), "op": "archive", "id": pid, "reason": _opt(args, "--reason")})
    _save(data)
    print(f"{c} {pid} 已归档")


def cmd_propose(data, args, today):
    code = args.pop(0)
    stance, reason = _opt(args, "--stance"), _opt(args, "--reason")
    if not stance or not reason:
        sys.exit("缺少 --stance 或 --reason")
    c, it = _item(data, code, create=True)
    cur = next((r.get("当前定位") for r in _read_holdings()[1] if T.norm(r.get("代码")) == c), None)
    if cur == stance:
        sys.exit("建议与当前定位相同，无需提出")
    it["proposal"] = {"stance": stance, "from": cur, "reason": reason, "date": today, "status": "pending"}
    it["history"].append({"at": _now(), "op": "propose", "stance": stance, "from": cur, "reason": reason})
    _save(data)
    print(f"{c} {it.get('name')} 定位调整建议：{cur} → {stance}")
    if "--apply" in args:
        args.remove("--apply")
        cmd_apply(data, [c] + args, today)


def cmd_apply(data, args, today):
    code = args.pop(0)
    c, it = _item(data, code)
    prop = it.get("proposal") or {}
    stance = _opt(args, "--stance") or prop.get("stance")
    if not stance:
        sys.exit("没有待确认的建议，可用 --stance 直接指定")
    fields, rows = _read_holdings()
    hit = None
    for r in rows:
        if T.norm(r.get("代码")) == c:
            hit = r
    if hit is None:
        sys.exit(f"holdings.csv 里没有 {code}")
    old = hit.get("当前定位")
    hit["当前定位"] = stance
    tmp = HOLDINGS + ".tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, HOLDINGS)
    by = _opt(args, "--by") or "张小凡"
    if prop:
        prop.update({"status": "applied", "applied": today, "stance": stance, "by": by})
    it["history"].append({"at": _now(), "op": "apply", "from": old, "stance": stance, "by": by,
                          "reason": prop.get("reason")})
    _save(data)
    print(f"{c} {it.get('name')} 定位已更新（{by}）：{old} → {stance}")


def _write_stance(c, stance):
    fields, rows = _read_holdings()
    hit = next((r for r in rows if T.norm(r.get("代码")) == c), None)
    if hit is None:
        sys.exit(f"holdings.csv 里没有 {c}")
    old = hit.get("当前定位")
    hit["当前定位"] = stance
    tmp = HOLDINGS + ".tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, HOLDINGS)
    return old


def cmd_revert(data, args, today):
    code = args.pop(0)
    c, it = _item(data, code)
    last = next((h for h in reversed(it.get("history") or []) if h.get("op") == "apply"), None)
    if not last or not last.get("from"):
        sys.exit("没有可撤回的 apply 记录")
    cur = _write_stance(c, last["from"])
    prop = it.get("proposal") or {}
    if prop.get("status") == "applied":
        prop.update({"status": "reverted", "reverted": today})
    reason = _opt(args, "--reason")
    it["history"].append({"at": _now(), "op": "revert", "from": cur, "stance": last["from"], "reason": reason})
    _save(data)
    print(f"{c} {it.get('name')} 定位已撤回：{cur} → {last['from']}")


def cmd_reject(data, args, today):
    code = args.pop(0)
    c, it = _item(data, code)
    prop = it.get("proposal")
    if not prop or prop.get("status") != "pending":
        sys.exit("没有待确认的建议")
    prop.update({"status": "rejected", "rejected": today, "reject_reason": _opt(args, "--reason")})
    it["history"].append({"at": _now(), "op": "reject", "reason": prop["reject_reason"]})
    _save(data)
    print(f"{c} 建议已否决")


def cmd_link(data, args, today):
    from datasources import commodity as CM
    code = args.pop(0) if args else None
    clear = _flag(args, "--clear")
    ids = [a.upper() for a in args]
    if not code or (not ids and not clear):
        print("可选品种：")
        for s in CM.series_list():
            print(f"   {s['id']:<6} {s['name']}（{s['unit']}，{s['source']}）")
        return
    bad = [i for i in ids if i not in CM.SERIES]
    if bad:
        sys.exit(f"未知品种 {bad}，运行 thesis.py link 查看可选品种")
    c, it = _item(data, code, create=True)
    old = it.get("commodities") or []
    it["commodities"] = [] if clear else ids
    it["history"].append({"at": _now(), "op": "link", "old": old, "commodities": it["commodities"]})
    _save(data)
    print(f"{c} {it.get('name')} 关联品种：{', '.join(old) or '无'} → {', '.join(it['commodities']) or '无'}")


def cmd_contract(data, args, today):
    """明确登记风控契约；不核实事实、不改定位、不交易。"""
    code = args.pop(0)
    text, metrics = _opt(args, "--text"), _opt(args, "--metrics")
    due, recovery = _opt(args, "--due"), _opt(args, "--recovery")
    critical = [s.strip() for s in (_opt(args, "--critical", "") or "").split(",") if s.strip()]
    action = _opt(args, "--on-failure", "pause_add")
    fraction = _opt(args, "--reduce-fraction")
    if not all((text, metrics, due, recovery)):
        sys.exit("contract 必须提供 --text 持有逻辑 --metrics 监控指标（逗号分隔） --due YYYY-MM-DD --recovery 恢复条件")
    try:
        datetime.date.fromisoformat(due)
        fraction = float(fraction) if fraction is not None else None
    except ValueError:
        sys.exit("兑现日期或减仓比例格式错误")
    if action not in ("pause_add", "reduce", "exit"):
        sys.exit("--on-failure 只能是 pause_add/reduce/exit")
    if action == "reduce" and (fraction is None or not 0 < fraction <= 1):
        sys.exit("reduce 必须显式指定 --reduce-fraction（0~1，不含0）")
    c, it = _item(data, code)
    ids = {p.get("id") for p in it["premises"] if p.get("status") != "已归档"}
    if any(pid not in ids for pid in critical) or action != "pause_add" and not critical:
        sys.exit("风险卖出必须明确关联已有、未归档的关键前提ID；不得凭空登记")
    contract = {"thesis": text, "metrics": [s.strip() for s in metrics.split(",") if s.strip()],
                "due_date": due, "recovery": recovery, "critical_premises": critical,
                "failure_action": action, "reduce_fraction": fraction}
    it["history"].append({"at": _now(), "op": "risk_contract", "old": it.get("risk_contract"), "contract": contract})
    it["risk_contract"] = contract
    _save(data)
    print(f"{c} 风控契约已登记；关键前提经有效证据确认失效后才生成风险卖出建议，不自动下单")


def cmd_list(data, args, today):
    only_stale, as_json = _flag(args, "--stale"), _flag(args, "--json")
    d = datetime.date.fromisoformat(today)
    out = []
    for r in _read_holdings()[1]:
        if not (r.get("代码") or "").strip():
            continue
        c = T.norm(r["代码"])
        s = T.summary(c, data, d)
        if only_stale and not (s["stale"] or s["missing"] or s["proposal"] or s["invalid"]):
            continue
        out.append({"code": c, "name": r.get("名称"), "stance": r.get("当前定位"), **s})
    if as_json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return
    for o in out:
        print(f"{o['name']} {o['code']} | 定位：{o['stance']}" + (f" | 关联品种：{', '.join(o['commodities'])}" if o.get("commodities") else ""))
        if o["missing"]:
            print("   （无前提）")
        for p in o["premises"]:
            age = "从未核实" if p["age_days"] is None else f"{p['age_days']} 天前核实"
            mark = " ⚠需核实" if p["stale"] else ""
            print(f"   {p['id']} [{p['kind']}] {p['text']} | {p['status']} | {age}（时限 {p['ttl_days']} 天）{mark}")
        if o["proposal"]:
            pr = o["proposal"]
            print(f"   📌 待确认：→ {pr['stance']}（{pr['date']}，{pr['reason']}）")


CMDS = {"list": cmd_list, "sync": cmd_sync, "add": cmd_add, "check": cmd_check, "archive": cmd_archive,
        "propose": cmd_propose, "apply": cmd_apply, "revert": cmd_revert, "reject": cmd_reject, "link": cmd_link,
        "contract": cmd_contract}


def main(argv):
    args = list(argv)
    if not args or args[0] not in CMDS:
        print(__doc__)
        return 1
    cmd = args.pop(0)
    today = _opt(args, "--date") or datetime.date.today().isoformat()
    CMDS[cmd](T.load(), args, today)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
