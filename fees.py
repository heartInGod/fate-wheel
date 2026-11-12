"""交易成本（手续费）估算，口径与 intraday_rules.md「交易成本（手续费）」一致。

费率来自中国银河证券对账单「收费说明」（2026-11）：
  佣金：买卖双向，每笔 max(5 元, 成交额 × 佣金率)；佣金率对账单没写，按万 2.5 估算
  A 股：卖出印花税 0.05%，过户费买卖各 0.001%
  ETF（15/16/18/50/51/52/56/58 开头）：免印花税、过户费
  港股通：港股印花税买卖各约 0.1%，交易征费/交易费/结算费买卖各约 0.01%
金额统一按人民币计（港股按成交额折算即可，误差可忽略）。
"""

COMMISSION_RATE = 0.00025
COMMISSION_MIN = 5.0
A_STAMP_SELL = 0.0005
A_TRANSFER = 0.00001
HK_EACH_SIDE = 0.0011
ETF_PREFIX = ("15", "16", "18", "50", "51", "52", "56", "58")


def kind_of(code):
    """etf / hk / stock"""
    c = str(code or "").upper()
    if c.endswith(".HK") or (c.isdigit() and len(c) == 5):
        return "hk"
    if c[:2] in ETF_PREFIX:
        return "etf"
    return "stock"


def one_way(amount, code, side):
    """单边手续费（元）。side: 1 买入，-1 卖出。"""
    if not amount or amount <= 0:
        return 0.0
    k = kind_of(code)
    fee = max(COMMISSION_MIN, amount * COMMISSION_RATE)
    if k == "hk":
        fee += amount * HK_EACH_SIDE
    elif k == "stock":
        fee += amount * A_TRANSFER
        if side < 0:
            fee += amount * A_STAMP_SELL
    return fee


def round_trip(amount, code):
    """一买一卖往返手续费（元），两腿按同一成交额估算。"""
    return one_way(amount, code, 1) + one_way(amount, code, -1)


def cost_pct(amount, code, legs="round"):
    """手续费占成交额的百分比。legs: round 往返 / buy 单买 / sell 单卖。"""
    if not amount or amount <= 0:
        return None
    fee = {"round": round_trip(amount, code),
           "buy": one_way(amount, code, 1),
           "sell": one_way(amount, code, -1)}[legs]
    return fee / amount * 100
