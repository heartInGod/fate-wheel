#!/usr/bin/env python3
"""统一数据获取入口。

  python3 main.py fetch list                          # 列出全部接口（Markdown 表格）
  python3 main.py fetch snapshot mode=intraday        # 盘中汇总数据
  python3 main.py fetch snapshot mode=premarket       # 盘前汇总数据
  python3 main.py fetch realtime_quotes codes=600353,0100.HK
  python3 main.py fetch tech_levels code=159813
  python3 main.py fetch stock_news keyword=旭光电子 n=5
  python3 main.py fetch sector_flow kind=concept top=10 ascending=true
  python3 main.py fetch commodity ids=W_ORE,APT,AL

参数写成 key=value；codes/symbols/ids 用逗号分隔；数字和 true/false 自动转换。输出 JSON。
"""
import json
import sys

from datasources import INTERFACES, DataSourceError, call

LIST_PARAMS = {"codes", "symbols", "ids"}


def parse_args(argv):
    kw = {}
    for a in argv:
        if "=" not in a:
            raise SystemExit(f"参数格式应为 key=value: {a}")
        k, v = a.split("=", 1)
        if k in LIST_PARAMS:
            kw[k] = [x for x in v.split(",") if x]
        elif v.lower() in ("true", "false"):
            kw[k] = v.lower() == "true"
        elif v.lstrip("-").isdigit():
            kw[k] = int(v)
        else:
            kw[k] = v
    return kw


def interface_table():
    lines = ["| 接口 | 类别 | 数据源 | 适用范围 | 参数 | 获取的数据 |", "|---|---|---|---|---|---|"]
    for name, (_, cat, src, scope, params, fields) in INTERFACES.items():
        cells = [c.replace("|", "/") for c in (cat, src, scope, params, fields)]
        lines.append(f"| `{name}` | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return
    name = sys.argv[1]
    if name == "list":
        print(interface_table())
        return
    try:
        result = call(name, **parse_args(sys.argv[2:]))
    except DataSourceError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(1)
    print(json.dumps(result, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
