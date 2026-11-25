#!/usr/bin/env python3
"""大盘资金面命令：采集市场级指标 + 输出分档摘要。

用法：
  python3 main.py market              # 读当天缓存，输出分档摘要（给人/模型读）
  python3 main.py market brief        # 同上，明确写 brief
  python3 main.py market raw          # 输出原始指标 JSON（调试）
  python3 main.py market refresh      # 强制重新采集并输出分档摘要

只读缓存与联网采集，不改任何业务数据；缓存写 cache/market_flow_YYYYMMDD.json。
"""
import json
import sys

from paths import setup_sys_path  # noqa: F401  触发模块路径
from datasources import market_flow
from core import market_regime


def summary(flow, reg):
    """人/模型可读的紧凑摘要（约 1 KB）。"""
    lines = ["## 大盘资金面"]
    m = reg.get("metrics") or {}
    lines.append(f"- 档位：**{reg.get('label')}**（{reg.get('regime')}）"
                 + (f"｜取数 {flow.get('generated_at')}" if flow.get("generated_at") else ""))
    for e in reg.get("evidence") or []:
        lines.append(f"  - {e}")
    for c in reg.get("crosses") or []:
        lines.append(f"  - 交叉验证：{c}")
    ext = flow.get("external") or {}
    ext_s = "、".join(f"{k}={v}" for k, v in (
        ("美元离岸人民币", ext.get("usdcnh")), ("伦敦金", ext.get("gold")),
        ("WTI原油", ext.get("oil")), ("纳指100", ext.get("nasdaq100"))) if v is not None)
    if ext_s:
        lines.append(f"  - 外盘：{ext_s}")
    if reg.get("gate"):
        lines.append(f"- 闸门提醒：{reg['gate']}")
    errs = flow.get("errors") or []
    if errs:
        lines.append(f"- 数据缺失（{len(errs)} 项）：" + "；".join(str(x)[:90] for x in errs[:4])
                     + "。缺口指标请结合当日 WebSearch 交叉验证，不要臆造数值。")
    return "\n".join(lines)


def main(argv):
    args = list(argv)
    refresh = "refresh" in args
    raw = "raw" in args
    flow = market_flow.collect(refresh=refresh)
    if raw:
        print(json.dumps(flow, ensure_ascii=False, indent=1, default=str))
        return 0
    reg = market_regime.classify(flow)
    print(summary(flow, reg))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
