"""统一数据源接口。

用法（Python）:
    from datasources import call, INTERFACES
    call("realtime_quotes", codes=["600353", "0100.HK"])

用法（命令行）: 见 fetch_data.py
所有接口返回 list/dict（可直接 json 序列化）；失败抛 DataSourceError。
"""
from . import announcements, archive, capital, commodity, fund, news, profile, quotes, snapshot as _snap
from ._http import DataSourceError, normalize
from .holdings import account, holdings
from . import thesis as _thesis

# 名称: (函数, 类别, 数据源, 适用范围, 参数说明, 返回字段说明)
INTERFACES = {
    "realtime_quotes": (quotes.realtime_quotes, "行情", "腾讯财经 qt.gtimg.cn", "A股/港股/ETF",
                        "codes=代码列表",
                        "名称、现价、昨收、开盘、最高、最低、涨跌额、涨跌幅、成交量、成交额、换手率、市盈率TTM、振幅、"
                        "流通/总市值、市净率、涨跌停价、量比、行情时间；ETF 另含 IOPV、溢价率、上日净值"),
    "index_quotes": (quotes.index_quotes, "行情", "腾讯财经 qt.gtimg.cn", "A股/港股/美股指数",
                     "symbols=腾讯指数代码（可选）",
                     "上证、深成、创业板、科创50、恒指、恒生科技、纳指100、纳指、标普500、道指的点位、涨跌、涨跌幅、高低点、时间"),
    "market_status": (quotes.market_status, "行情", "腾讯财经 qt.gtimg.cn", "A股/港股",
                      "无", "今天 A股/港股 是否开市、最近行情日期、当前是否处于 A 股交易时段"),
    "kline": (quotes.kline, "行情", "腾讯财经 newfqkline，失败回退东方财富 push2his", "A股/港股/ETF",
              "code, period=day|week|month, count=60, adjust=qfq|hfq|",
              "日期、开盘、收盘、最高、最低、成交量（前复权）"),
    "tech_levels": (quotes.tech_levels, "技术面", "基于日K计算（腾讯/东财）", "A股/港股/ETF",
                    "code, count=120",
                    "MA5/10/20/60、近20/60日最高最低、昨日高低点、相对MA20偏离%、近5日涨跌%"),
    "etf_premium": (fund.etf_premium, "基金", "腾讯财经 qt.gtimg.cn", "场内ETF/LOF",
                    "codes=代码列表", "现价、IOPV 实时参考净值、实时溢价率、上一日净值、相对上日净值溢价%"),
    "etf_nav_history": (fund.etf_nav_history, "基金", "东方财富基金 api.fund.eastmoney.com", "基金/ETF",
                        "code, n=10", "净值日期、单位净值、累计净值、日涨跌%、申购/赎回状态"),
    "announcements": (announcements.announcements, "公告", "A股→巨潮，港股→披露易，失败回退东方财富", "A股/港股",
                      "code, days=7, n=20", "公告日期、标题、类别、原文链接、来源"),
    "cninfo_announcements": (announcements.cninfo_announcements, "公告", "巨潮资讯 cninfo.com.cn（官方）", "A股/场内基金",
                             "code, days=7, n=20", "公告日期、标题、PDF 链接"),
    "hkex_announcements": (announcements.hkex_announcements, "公告", "港交所披露易 hkexnews.hk（官方）", "港股",
                           "code, days=14, n=20", "公告时间、标题、类别、PDF 链接"),
    "em_announcements": (announcements.em_announcements, "公告", "东方财富 np-anotice-stock", "A股/港股",
                         "code, n=10", "公告日期、标题、栏目分类、详情链接"),
    "fund_flow_realtime": (capital.fund_flow_realtime, "资金面", "东方财富 push2，失败回退新浪", "A股/ETF（港股不全）",
                           "codes=代码列表", "当日主力净流入及净占比、超大单/大单/中单/小单净额（元）"),
    "fund_flow_daily": (capital.fund_flow_daily, "资金面", "东方财富 push2his，失败回退新浪", "A股/ETF",
                        "code, days=10", "近N日 主力/超大单/大单/中单/小单净额、主力净占比、收盘价、涨跌幅"),
    "margin": (capital.margin, "资金面", "东方财富数据中心 RPTA_WEB_RZRQ_GGMX", "沪深两融标的（T+1）",
               "code, days=5", "融资余额、融资买入/偿还/净买入（当日、5日、10日）、融券余额/卖出量、两融余额、融资余额占流通市值%"),
    "connect_flow": (capital.connect_flow, "资金面", "东方财富 push2 kamt", "沪深港通",
                     "无", "港股通(沪/深)南向当日净买入、额度余额；沪/深股通状态（北向净买入已停披露）"),
    "sector_flow": (capital.sector_flow, "资金面", "东方财富 push2 clist，失败回退新浪", "行业/概念板块",
                    "kind=industry|concept, top=10, ascending=False",
                    "板块名称、涨跌幅、主力净流入及占比、超大/大/中/小单净额（净流入榜或净流出榜；新浪源另含领涨股）"),
    "sina_fund_flow_daily": (capital.sina_fund_flow_daily, "资金面", "新浪财经 MoneyFlow", "A股/ETF",
                             "code, days=10", "近N日 净流入额及占比、特大单净额、收盘价、涨跌幅"),
    "sina_sector_flow": (capital.sina_sector_flow, "资金面", "新浪财经 MoneyFlow", "行业/概念板块",
                         "kind=industry|concept, top=10, ascending=False", "板块涨跌幅、净流入额及占比、领涨股及涨幅"),
    "stock_news": (news.stock_news, "新闻", "东方财富资讯搜索 search-api-web", "任意关键词",
                   "keyword, n=10", "发布时间、标题、媒体、摘要、链接（按时间倒序）"),
    "flash_news": (news.flash_news, "新闻", "新浪财经 7x24 zhibo.sina.com.cn", "全市场快讯",
                   "n=30, keyword=可选过滤词", "快讯时间、内容"),
    "profile": (profile.profile, "资料", "东方财富F10 / 天天基金", "A股/港股/ETF",
                "code",
                "股票：公司概况+主营构成+经营评述（同下两项合并）；基金/ETF：基金档案+前十大持仓"),
    "company_profile": (profile.company_profile, "资料", "东方财富F10（A股 PC_HSF10，港股 RPT_HKF10_INFO_ORGPROFILE）",
                        "A股/港股",
                        "code", "公司全称、曾用名、所属行业（东财/证监会）、上市与成立日期、董事长/总经理、员工数、地址、官网、"
                                "审计机构、公司简介、经营范围（港股为主营业务描述）"),
    "main_business": (profile.main_business, "资料", "东方财富F10 BusinessAnalysis", "A股（港股仅文字描述）",
                      "code, review_chars=600",
                      "最新报告期主营构成（按行业/产品/地区的收入、收入占比、毛利率）、管理层经营评述摘要、经营范围"),
    "fund_profile": (profile.fund_profile, "资料", "天天基金 fundmobapi", "基金/ETF",
                     "code, top=10",
                     "基金全称、类型、成立日、规模、基金公司、经理、托管行、跟踪指数、业绩基准、管理/托管费率、风险等级、"
                     "投资目标、近1/3/6/12月收益、前十大持仓及权重"),
    "sync_profiles": (archive.sync_profiles, "档案", "profile 接口 → 本地 profiles/{代码}.md", "全部持仓",
                      "codes=可选, force=false",
                      "刷新资料档案（当天已更新则跳过）；返回每只 updated/skipped/error 及文件路径"),
    "read_profile": (archive.read_profile, "档案", "本地 profiles/{代码}.md（不存在时先同步）", "A股/港股/ETF",
                     "code", "资料档案 Markdown 全文：基本信息、简介、主营构成、经营评述/基金档案、前十大持仓"),
    "sync_announcements": (archive.sync_announcements, "档案", "announcements 接口 → 本地 data/announcements/{代码}.md",
                           "全部持仓", "codes=可选, days=3, n=30",
                           "增量合并公告档案（首次回溯90天，按链接去重）；返回每只的新增公告 new、今日公告 today、状态"),
    "read_announcements": (archive.read_announcements, "档案", "本地 data/announcements/{代码}.md（不联网）", "A股/港股/ETF",
                           "code, days=30", "近N天公告：日期、标题、类别、来源、收录时间、链接"),
    "holdings": (holdings, "持仓", "本地 holdings.csv", "-", "无",
                 "名称、代码、市场、持仓数量、可用数量、成本价、币种、当前定位"),
    "thesis": (lambda codes=None: _thesis.summaries(codes or [h["code"] for h in holdings()]), "持仓",
               "本地 thesis.json（写入用 thesis.py）", "全部持仓", "codes=可选",
               "每只持仓的定位前提：内容、类别、状态（待核实/成立/存疑/失效）、上次核实日期、距今天数、是否需核实、待确认的定位调整建议"),
    "commodity": (commodity.prices, "商品", "钨：中钨在线 ctia.com.cn 日评；国内期货：新浪期货日K+实时；外盘期货：新浪；BDI：东方财富",
                  "商品 / 行业价格品种", "ids=品种ID列表（可选，默认全部；thesis.py link 查看可选）",
                  "品种名、单位、最新值及日期、1日/1周/1月变化%、近30天高低、最近6个点（钨精矿/APT、沪铝/沪铜/原油/碳酸锂/集运/沪金、布油/WTI/美气、BDI）"),
    "account": (account, "持仓", "本地 account.csv", "-", "无", "总资产、总市值、可用资金、浮动盈亏、仓位"),
    "snapshot": (_snap.snapshot, "汇总", "组合以上接口", "全部持仓",
                 "mode=premarket|intraday",
                 "premarket：开市状态、指数、账户、每只持仓的行情+盈亏+技术位+近5日资金流+两融+近3日公告+新增公告+新闻、"
                 "ETF溢价、南向资金、板块资金榜、7x24快讯、定位前提 thesis、关联商品价格 commodity，并刷新资料档案；intraday：行情+盈亏+技术位+实时资金流+当日公告+新增公告、ETF溢价、南向、板块资金榜（commodity 取盘前缓存）"),
}


def call(name, **kwargs):
    if name not in INTERFACES:
        raise DataSourceError(f"未知接口 {name}，可用: {', '.join(INTERFACES)}")
    return INTERFACES[name][0](**kwargs)


__all__ = ["INTERFACES", "call", "DataSourceError", "normalize"]
