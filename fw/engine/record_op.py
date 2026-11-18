#!/usr/bin/env python3
"""记录一笔成交到 operations.csv。

用法：
  python3 main.py record-op 买入 600353 500 19.80
  python3 main.py record-op 卖出 600353 1000 19.50 --note 止损
  python3 main.py record-op 买入 0100.HK 10 160.0

参数：
  操作   买入/卖出（含 买/卖/申购/赎回/增持/减持）
  代码   600353 / sh600353 / 159813 / 0100.HK / 00100
  数量   股数
  价格   成交价
  --note 备注（可选）
  --time HH:MM（可选，默认当前时间）

记录后，holdings.csv 的可用数量会由 datasources.ops 按 T+1 规则自动推导，
无需手工改「可用数量」列；但「持仓数量」列仍需同步更新（可用 --sync 自动改）。
"""
import csv
import datetime
import os
import sys

from fw.paths import ROOT
from fw.datamul import data_path
OPERATIONS_CSV = data_path("operations.csv")
HOLDINGS_CSV = data_path("holdings.csv")

HEADER = ["日期", "时间", "操作", "名称", "代码", "数量", "价格", "备注"]


def _norm_code(raw):
    c = str(raw or "").strip().upper()
    if len(c) == 8 and c[:2] in ("SH", "SZ", "HK") and c[2:].isdigit():
        c = c[2:]
    c = c.replace(".HK", "")
    if c.isdigit() and len(c) <= 5 and c.startswith("0"):
        return c.zfill(5)
    return c


def _read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def find_holding(code):
    for r in _read_csv(HOLDINGS_CSV):
        if _norm_code(r.get("代码")) == code:
            return r
    return None


def main():
    args = sys.argv[1:]
    note = ""
    when = None
    if "--note" in args:
        i = args.index("--note")
        note = args[i + 1] if i + 1 < len(args) else ""
        del args[i:i + 2]
    if "--time" in args:
        i = args.index("--time")
        when = args[i + 1] if i + 1 < len(args) else None
        del args[i:i + 2]

    if len(args) < 4:
        print(__doc__)
        sys.exit(1)

    op, code_raw, qty_s, price_s = args[0], args[1], args[2], args[3]
    code = _norm_code(code_raw)
    try:
        qty = float(qty_s)
        price = float(price_s)
    except ValueError:
        print("数量和价格必须是数字")
        sys.exit(1)

    if any(w in op for w in ("卖", "赎回", "减持")):
        op_out = op if op in ("买入", "卖出") else ("卖出" if "卖" in op or "减持" in op or "赎回" in op else "买入")
    else:
        op_out = op if op in ("买入", "卖出") else "买入"

    holding = find_holding(code)
    name = holding.get("名称") if holding else ""
    if not name:
        print(f"⚠️  在 holdings.csv 里找不到 {code}，名称留空")

    today = datetime.date.today().strftime("%Y-%m-%d")
    tm = when or datetime.datetime.now().strftime("%H:%M")

    exists = os.path.exists(OPERATIONS_CSV)
    with open(OPERATIONS_CSV, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(HEADER)
        w.writerow([today, tm, op_out, name, code, f"{qty:g}", f"{price:g}", note])

    print(f"✅ 已记录 {today} {tm} {op_out} {name or code} {qty:g} 股 @ {price:g}"
          + (f"（{note}）" if note else ""))

    if holding:
        print(f"   提示：记得把 holdings.csv 里 {code} 的「持仓数量」同步为最新值"
              f"（当前 {holding.get('持仓数量')}），并把「可用基准日」更新为 {today}")


if __name__ == "__main__":
    main()
