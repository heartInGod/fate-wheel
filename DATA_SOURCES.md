# 数据接口说明

统一入口：`python3 main.py fetch <接口名> key=value ...`，输出 JSON；Python 内可用 `from datasources import call; call("接口名", ...)`。
`python3 main.py fetch list` 可随时重新生成下表。代码支持 `600353` / `159813` / `0100.HK` / `00100` / `sh600353` 等写法。

## 本地数据文件归档（datamul）

根目录下的持久化数据文件已按内容性质归档到 `data/` 子目录，文中出现的文件名均指归档后的位置：

| 目录 | 文件 | 性质 |
|---|---|---|
| `data/portfolio/` | `account.csv`、`holdings.csv`、`operations.csv`、`thesis.json` | 账户与持仓台账（个人资产） |
| `data/config/` | `rules_params.json`、`risk_policy.json`、`policy_signals.json`、`core_sleeves.json` | 人维护的规则与策略配置 |
| `data/derived/` | `rules_auto.json`、`core_auto.json` | 脚本生成的校准 / 复查产物 |
| `data/watchlist/` | `watchlist.csv` | 自选股名单 |

代码统一通过 `datamul.py`（`common/datamul.py`）的 `data_path(文件名)` 读写这些文件，读写路径自动一致；`cache/`、`logs/`、`profiles/` 等运行时产物仍在原处；`announcements/`、`research/` 下的数据已归档到 `data/announcements/`、`data/research/`。可用 `python3 main.py datamul check` 校验归档完整性。

## 接口一览

| 接口 | 类别 | 数据源 | 适用范围 | 参数 | 获取的数据 |
|---|---|---|---|---|---|
| `realtime_quotes` | 行情 | 腾讯财经 qt.gtimg.cn | A股/港股/ETF | codes=代码列表 | 名称、现价、昨收、开盘、最高、最低、涨跌额、涨跌幅、成交量、成交额、换手率、市盈率TTM、振幅、流通/总市值、市净率、涨跌停价、量比、行情时间；ETF 另含 IOPV、溢价率、上日净值 |
| `index_quotes` | 行情 | 腾讯财经 qt.gtimg.cn | A股/港股/美股指数 | symbols=腾讯指数代码（可选） | 上证、深成、创业板、科创50、恒指、恒生科技、纳指100、纳指、标普500、道指的点位、涨跌、涨跌幅、高低点、时间 |
| `market_status` | 行情 | 腾讯财经 qt.gtimg.cn | A股/港股 | 无 | 今天 A股/港股 是否开市、最近行情日期、当前是否处于 A 股交易时段 |
| `kline` | 行情 | 腾讯财经 newfqkline，失败回退东方财富 push2his | A股/港股/ETF | code, period=day/week/month, count=60, adjust=qfq/hfq/ | 日期、开盘、收盘、最高、最低、成交量（前复权） |
| `tech_levels` | 技术面 | 基于日K计算（腾讯/东财） | A股/港股/ETF | code, count=120 | MA5/10/20/60、近20/60日最高最低、昨日高低点、相对MA20偏离%、近5日涨跌% |
| `etf_premium` | 基金 | 腾讯财经 qt.gtimg.cn | 场内ETF/LOF | codes=代码列表 | 现价、IOPV 实时参考净值、实时溢价率、上一日净值、相对上日净值溢价% |
| `etf_nav_history` | 基金 | 东方财富基金 api.fund.eastmoney.com | 基金/ETF | code, n=10 | 净值日期、单位净值、累计净值、日涨跌%、申购/赎回状态 |
| `announcements` | 公告 | A股→巨潮，港股→披露易，失败回退东方财富 | A股/港股 | code, days=7, n=20 | 公告日期、标题、类别、原文链接、来源 |
| `cninfo_announcements` | 公告 | 巨潮资讯 cninfo.com.cn（官方） | A股/场内基金 | code, days=7, n=20 | 公告日期、标题、PDF 链接 |
| `hkex_announcements` | 公告 | 港交所披露易 hkexnews.hk（官方） | 港股 | code, days=14, n=20 | 公告时间、标题、类别、PDF 链接 |
| `em_announcements` | 公告 | 东方财富 np-anotice-stock | A股/港股 | code, n=10 | 公告日期、标题、栏目分类、详情链接 |
| `fund_flow_realtime` | 资金面 | 东方财富 push2，失败回退新浪 | A股/ETF（港股不全） | codes=代码列表 | 当日主力净流入及净占比、超大单/大单/中单/小单净额（元） |
| `fund_flow_daily` | 资金面 | 东方财富 push2his，失败回退新浪 | A股/ETF | code, days=10 | 近N日 主力/超大单/大单/中单/小单净额、主力净占比、收盘价、涨跌幅 |
| `margin` | 资金面 | 东方财富数据中心 RPTA_WEB_RZRQ_GGMX | 沪深两融标的（T+1） | code, days=5 | 融资余额、融资买入/偿还/净买入（当日、5日、10日）、融券余额/卖出量、两融余额、融资余额占流通市值% |
| `connect_flow` | 资金面 | 东方财富 push2 kamt | 沪深港通 | 无 | 港股通(沪/深)南向当日净买入、额度余额；沪/深股通状态（北向净买入已停披露） |
| `sector_flow` | 资金面 | 东方财富 push2 clist，失败回退新浪 | 行业/概念板块 | kind=industry/concept, top=10, ascending=False | 板块名称、涨跌幅、主力净流入及占比、超大/大/中/小单净额（净流入榜或净流出榜；新浪源另含领涨股） |
| `sina_fund_flow_daily` | 资金面 | 新浪财经 MoneyFlow | A股/ETF | code, days=10 | 近N日 净流入额及占比、特大单净额、收盘价、涨跌幅 |
| `sina_sector_flow` | 资金面 | 新浪财经 MoneyFlow | 行业/概念板块 | kind=industry/concept, top=10, ascending=False | 板块涨跌幅、净流入额及占比、领涨股及涨幅 |
| `stock_news` | 新闻 | 东方财富资讯搜索 search-api-web | 任意关键词 | keyword, n=10 | 发布时间、标题、媒体、摘要、链接（按时间倒序） |
| `flash_news` | 新闻 | 新浪财经 7x24 zhibo.sina.com.cn | 全市场快讯 | n=30, keyword=可选过滤词 | 快讯时间、内容 |
| `profile` | 资料 | 东方财富F10 / 天天基金 | A股/港股/ETF | code | 股票：公司概况+主营构成+经营评述（同下两项合并）；基金/ETF：基金档案+前十大持仓 |
| `company_profile` | 资料 | 东方财富F10（A股 PC_HSF10，港股 RPT_HKF10_INFO_ORGPROFILE） | A股/港股 | code | 公司全称、曾用名、所属行业（东财/证监会）、上市与成立日期、董事长/总经理、员工数、地址、官网、审计机构、公司简介、经营范围（港股为主营业务描述） |
| `main_business` | 资料 | 东方财富F10 BusinessAnalysis | A股（港股仅文字描述） | code, review_chars=600 | 最新报告期主营构成（按行业/产品/地区的收入、收入占比、毛利率）、管理层经营评述摘要、经营范围 |
| `fund_profile` | 资料 | 天天基金 fundmobapi | 基金/ETF | code, top=10 | 基金全称、类型、成立日、规模、基金公司、经理、托管行、跟踪指数、业绩基准、管理/托管费率、风险等级、投资目标、近1/3/6/12月收益、前十大持仓及权重 |
| `sync_profiles` | 档案 | profile 接口 → 本地 profiles/{代码}.md | 全部持仓 | codes=可选, force=false | 刷新资料档案（当天已更新则跳过）；返回每只 updated/skipped/error 及文件路径 |
| `read_profile` | 档案 | 本地 profiles/{代码}.md（不存在时先同步） | A股/港股/ETF | code | 资料档案 Markdown 全文：基本信息、简介、主营构成、经营评述/基金档案、前十大持仓 |
| `sync_announcements` | 档案 | announcements 接口 → 本地 data/announcements/{代码}.md | 全部持仓 | codes=可选, days=3, n=30 | 增量合并公告档案（首次回溯90天，按链接去重）；返回每只的新增公告 new、今日公告 today、状态 |
| `read_announcements` | 档案 | 本地 data/announcements/{代码}.md（不联网） | A股/港股/ETF | code, days=30 | 近N天公告：日期、标题、类别、来源、收录时间、链接 |
| `holdings` | 持仓 | 本地 holdings.csv + operations.csv | - | 无 | 名称、持仓数量、**今日可用数量**（按 T+1 自动推导）、成本价、币种、当前定位 |
| `thesis` | 持仓 | 本地 thesis.json（写入用 thesis.py） | 全部持仓 | codes=可选 | 每只持仓的定位前提：内容、类别、状态（待核实/成立/存疑/失效）、上次核实日期、距今天数、是否需核实、近 3 天的自动定位调整（changed）、14 天内被撤回的调整（reverted） |
| `commodity` | 商品 | 钨：中钨在线 ctia.com.cn 日评；国内期货：新浪期货日K+实时；外盘期货：新浪；BDI：东方财富 | 商品 / 行业价格品种 | ids=品种ID列表（可选，默认全部；thesis.py link 查看可选） | 品种名、单位、最新值及日期、1日/1周/1月变化%、近30天高低、最近6个点（钨精矿/APT、沪铝/沪铜/原油/碳酸锂/集运/沪金、布油/WTI/美气、BDI） |
| `account` | 持仓 | 本地 account.csv | - | 无 | 总资产、总市值、可用资金、浮动盈亏、仓位 |
| `snapshot` | 汇总 | 组合以上接口 | 全部持仓 | mode=premarket/intraday | premarket：开市状态、指数、账户、每只持仓的行情+盈亏+技术位+近5日资金流+两融+近3日公告+新增公告+新闻、ETF溢价、南向资金、板块资金榜、7x24快讯、定位前提 thesis、关联商品价格 commodity，并刷新资料档案；intraday：行情+盈亏+技术位+实时资金流+当日公告+新增公告、ETF溢价、南向、板块资金榜（commodity 取盘前缓存） |

## 本地档案

| 目录 | 内容 | 更新方式 |
|---|---|---|
| `profiles/{代码}.md` | 股票：基本信息、公司简介、主营构成（按行业/产品/地区，含收入占比和毛利率）、经营评述、经营范围；ETF：基金档案、投资目标、前十大持仓 | `snapshot mode=premarket` 自动调用 `sync_profiles`，每天最多刷新一次（当天已更新则跳过）；手动强制：`python3 main.py fetch sync_profiles force=true` |
| `data/announcements/{代码}.md` | 公告档案表：公告日期、标题、类别、来源、收录时间、原文链接 | 盘前和盘中 snapshot 都会调用 `sync_announcements`：首次建档回溯 90 天，之后每次拉近 2~3 天、按链接去重后增量追加，只增不删。snapshot 里的 `announcements_new` 就是本次新收录的公告 |

- 文件名用代码，港股沿用 holdings.csv 写法（`0100.HK.md`）。holdings.csv 新增持仓后，下次运行会自动建档；已清仓的标的档案保留不删。
- 两个目录都由脚本生成，不要手动编辑（公告档案靠解析表格行来去重）。
- 「收录时间」是本地第一次抓到这条公告的时间，可用来区分盘后新出的公告。
- 读档案不联网：`python3 main.py fetch read_profile code=600353`、`python3 main.py fetch read_announcements code=0100.HK days=30`。

## 使用说明

- **定时任务入口**：盘前 `snapshot mode=premarket`，盘中 `snapshot mode=intraday`。单个接口失败只记入 `errors`，不影响其余数据。
- **限速与熔断**（`datasources/_http.py`）：同一站点（同一主域的子域名共用节奏，如腾讯 gtimg.cn/qq.com）两次请求间隔 = 基础间隔 + 随机抖动（`_PACE`，腾讯 0.25+0~0.35 秒、东财 0.4+0~0.25 秒），每连续 3 次再随机停 1~3 秒；同一 host 连续 3 次被拦 / 5xx / 连不上即熔断 10 分钟（期间直接报错不发请求，到期放一个试探）。因此批量任务明显变慢：盘中快照约 2~3 分钟。
- **本地缓存**：`quotes.kline` 每次成功拉前复权日 K 都合并写入 `cache/kline/<代码>.json`（最多 250 根）；`ops.trading_days` 当天开盘后抓过就直接用缓存；看板 `build_dashboard.py` 默认只读缓存（日 K、自选技术位 `cache/watch_tech.json`、汇率 `cache/hkd_cny.json`），`fresh=true` 才联网。
- **商品价格**（`datasources/commodity.py`）：只在盘前 snapshot 联网拉持仓关联的品种（关联关系存 thesis.json 的 `commodities`，用 `python3 main.py thesis link 代码 品种…` 设置），结果合并写入 `cache/commodity.json`；盘中 snapshot 和看板只读这份缓存。钨精矿/APT 来自中钨在线 ctia.com.cn 的「钨制品价格走势」日评（WordPress REST，交易日约 14:00 发布，正文句子解析+离群过滤）；国内期货用新浪日 K（不含当天）+ hq.sinajs.cn 实时价补当天；外盘 OIL/CL/NG 用新浪外盘日 K；BDI 用东财 `RPT_INDUSTRY_INDEX`（去掉同值占位条）。100ppi 生意社有 JS cookie 反爬、chinatungsten 新闻页 10-19 起停更，均未采用。
- **官方优先**：公告 A股走巨潮资讯、港股走港交所披露易，失败时自动回退东方财富。
- **自动回退**：东方财富对高频请求会临时封 IP（断开连接），`fund_flow_realtime` / `fund_flow_daily` / `sector_flow` 失败时自动改用新浪财经；脚本对东方财富强制每次请求间隔 0.4 秒。
- **单位**：资金类为元（`connect_flow` 为万元），涨跌幅/占比为 %，市值为亿元。

## 已知限制

- 港股（MINIMAX-W）没有可靠的资金流和两融数据，东方财富失败时无回退。
- 北向资金自 2024 年起不再披露实时净买入，`connect_flow` 只有南向数据有意义。
- 两融数据 T+1 更新；QDII 基金净值滞后 1~2 个交易日，159659 溢价以实时 IOPV 计算的 `premium_pct` 为准。
- 财联社电报接口需要签名，未接入，快讯改用新浪 7x24。
- 场内 ETF 的公告在巨潮的 fund 栏目（不分沪深）；兜底源为天天基金公告。
- 腾讯旧日 K 路径 `fqkline` 自 2026-11-06 被 WAF 拦截（返回 501），`kline` 改用新路径 `newfqkline`（依次尝试 ifzq.gtimg.cn、web.ifzq、proxy.finance.qq.com），全部失败或熔断中则回退东财 `push2his.eastmoney.com/api/qt/stock/kline/get`（须带 `end` 参数）。两源前复权算法相同，120 日实测 A股/ETF/港股逐根一致。`evaluate.py` 的日线也走 `kline`。
- 以上均为非官方公开网页接口，可能随网站改版失效；失效时 `errors` 会报出，需要修脚本。

## 持仓看板

- 生成：`python3 main.py dashboard`（默认读 `cache/snapshot_latest.json`；加 `fresh=true` 先重跑一次盘中 snapshot）。输出数据文件 `data/derived/dashboard.json` 及同内容的 `data/derived/dashboard.js`（均含持仓隐私、已 gitignore）。前端根目录 `dashboard.html` 优先用 `<script src>` 加载 JS，双击 `file://` 即可打开、无需后台服务；仅当 JS 缺失且通过 HTTP 访问时回退 fetch JSON。
- 定时任务写完日志后会自动刷新看板。
- 页面：持仓总览 / 个股详情（日K、技术位、资金流、公告、资料）/ 分析日志 / 操作日志。浏览器联网时每 30 秒从腾讯行情刷新实时价。
- 实际成交记录在 `operations.csv`（UTF-8 BOM），表头 `日期,时间,操作,名称,代码,数量,价格,备注`，例如 `2026-10-30,10:05,卖出,旭光电子,600353,1000,12.30,止损`。用 `python3 main.py record-op 买入 600353 500 19.80 --note 理由` 追加。

## 可用数量（T+1 自动推导）

A 股和境内 ETF 是 T+1：当日买入的份额次日才能卖。券商 App 里的「可用数量」每天自动变化，但 `holdings.csv` 里的「可用数量」是**某一天的快照**，跨过交易日就会失真——昨天买入被冻结的部分今天已经解冻，CSV 里却还是昨天的数字，导致给出的可卖数量被低估。

因此 `holdings.csv` 增加一列 **`可用基准日`**，标明这批「可用数量」对应哪一天；`datasources/ops.py` 按交易日历（取上证指数日 K 的日期序列，天然跳过周末与休市日）把它推进到今天：

```
available(今天) = shares(今天) − locked(今天)
```

- 基准日的冻结份额（`持仓数量 − 可用数量`）在**基准日的下一个交易日**解冻。
- `operations.csv` 中基准日之后的每笔成交逐笔推进：当日买入 → 计入冻结，当日卖出 → 立即减少可用。
- 港股通和跨境 QDII ETF（159659）是 T+0，当日买入即可卖，不冻结。
- 没有 `可用基准日`、或基准日不是交易日时，沿用 CSV 原值并在快照里标记 `available_derived=false`。

**日常维护**：

1. 成交后跑 `python3 main.py record-op 买入 600353 500 19.80`（或 `卖出`）。港股写 `0100.HK` 即可。
2. 把 `holdings.csv` 的 `持仓数量` 同步为最新值，并把 `可用基准日` 更新为**该快照对应的日期**（通常是最后一次改仓当天）。
3. 「可用数量」列不再需要手工改——它由基准日 + `operations.csv` 推导；建议把它当作基准日的快照值，只在重设基准日时更新。

推导逻辑的单元测试：`python3 main.py test`（覆盖跨交易日、跨周末、当日买入冻结、次日解冻、卖出、T+0、无基准日等场景）。

## 规则量化与收盘评估

盘中信号分两步产生：**Python 规则引擎先算候选，大模型再逐条判断**；每条信号落盘为结构化记录，收盘后统一打分。

| 文件 | 作用 |
|---|---|
| `rules_params.json` | 量化阈值：角色关键词、做T/加仓/减仓/止损/异动/溢价阈值、去重比例、按代码覆盖（MINIMAX-W 止损 155/警戒 165、159659 溢价上限 3%、半导体二选一）、评估口径；`auto` 段是动态阈值的开关、搜索上下限、单日限幅（±30%）与仓位约束（单只 ≤ 总资产 20%、不设现金底线、单笔 ≤ 5%、1/4 凯利；长线仓 ≤ 8 只、单只目标 ≥ 3 万、< 1 万算碎仓；单笔 EV < `min_ev_yuan` 20 元不操作）；`sleeves` 段是两仓制：`trade` 交易仓（预算 20%、目标价差 `min_move_pct` 3%、持有 `hold_days` 5 天、固定归交易仓的代码）、`core` 长线仓（80%、单只目标 10%、估值参数 `valuation`、选股 `screen`）。**只由人改**，改前先看 `logs/eval/summary.md` 和 `logs/calib/` |
| `calibrate.py` | 动态阈值校准（16:00 任务运行，约 1.5 分钟）：拉新浪 5 分钟 K（约 41 个交易日，增量缓存 `cache/calib/m5/`）、腾讯不复权日 K（`cache/calib/day/`）、沪深300 日 K；按时点重放引擎规则、扣手续费算收益，坐标搜索 k 值，写 `rules_auto.json`（自动生成，人和定时任务都不要手改）与报告 `logs/calib/yyyyMMdd.md`。`show` 看当前生效值，`ev --code --type --price [--qty]` 给大模型自补信号算 EV，`--offline` 只用缓存，`--dry-run` 不写文件 |
| `auto_params.py` / `fees.py` | 动态阈值换算（阈值 = 标的 ATR20% × k）、EV 查表（规则×品种×市场状态，分层收缩）、按 EV 反推保本金额/有效金额（不设固定最低金额）、凯利定量与仓位/现金约束；`fees.py` 按银河证券收费说明估算手续费 |
| `signals_engine.py` | 读快照算候选：同标的同方向按优先级合并、二选一分组、与当天已有信号去重（价格变化 < 3%）、卖出量按 `available` 封顶、加仓量按可用资金取整手；做T候选自动附成对区间 `pair_zone`（偏移 d=max(`t.pair_min_pct`, 当日振幅×`t.pair_amp_frac`, `sleeves.trade.min_move_pct` 3%)，宽 `t.pair_width_pct`）。`rules_auto.json` 有效（≤4 天）时阈值按标的 ATR 动态换算，每条买卖候选附 `ev` 并按 EV/仓位重算 `qty`，EV ≤ 0、EV < 20 元或仓位/现金/交易仓预算到顶的标 `ev_negative`（止损只标注），买入类上方空间 < 3% 的标 `blocked`、够的附 `tp_price`（+3%）与 `hold_days`；持仓按两仓评估（长线仓只做T），只数上限与碎仓只看长线仓，碎仓减仓改为清仓；输出 `auto` 段（市场状态、k）、`portfolio` 段（长线仓只数/碎仓、交易仓市值/预算）和 `open_trades`（交易仓买入的止盈/到期提醒）。`--mode premarket` 输出各标的支撑/压力位，以及动态阈值 `thresholds`、仓位占比、可买空间 `buy_room_yuan`（交易仓叠加剩余预算，长线仓为 null）/ `cash_room_yuan`，以及 `core`（长线仓分区、目标价）与 `open_trades` |
| `core_value.py` | 长线仓（两仓制的 80%）：东方财富 datacenter 拉估值（PE/PB 及 5 年分位）、一致预期 EPS 与机构目标价、财报（营收/净利同比、ROE），目标价 = 一致预期 EPS × 合理 PE（5 年 PE 中位数，限制在 [8, 40]，不高于机构最高目标价）；按现价相对目标价分 买入区（空间 ≥ 30%，补到目标仓位 10%，分 2 批）/ 持有区 / 卖出区（到目标价卖一半）/ 清仓区（≥ 目标×1.15）；净利同比转负、ROE 不达标、一致预期 EPS 下调 ≥ 15% 时标「逻辑复查」（不自动卖）。港股、亏损股和 `sleeves.trade.force` 归交易仓；ETF 由 `etf_value.py` 按指数估值法判断（见下一行）。长线持仓还要过持仓成本约束 `cost_gate`：保本价 = 成本价 + 卖出费用；深套 > 30% 且目标价 < 保本价的只做T不加仓，首批之后的加仓要求现价 ≤ 成本×0.92，卖出要求现价 ≥ 保本价×1.05（逻辑复查触发的除外）；`status` 输出 cost / breakeven / pnl_pct / cost_block。`review` 每周复查（周五任务），写 `core_auto.json`（持仓分仓 + 长线候选，自动生成，不要手改）与 `logs/core/yyyyMMdd.md`；`status` 输出当前分区 JSON；`--dry-run` 不写文件 |
| `industry_heat.py` | 行业景气分（被 `core_value.py review` 调用，也可单独 `python3 main.py industry [--refresh] [行业名…]` 看排名）：东方财富 datacenter `RPT_LICO_FN_CPD` 按报告期整页拉全市场营收/归母净利/东财二级行业（每页 500 条），`push2 clist m:90+t:2` 行业板块列表，`push2his kline`（板块 1 年日 K 与成交额）与 `fflow/daykline`（近 20 日主力净流入）。每个二级行业算营收/净利 CAGR（同口径样本加总；基期从 2023 年报起算、2022 疫情年不用，之后滚动 3 年）、今年累计净利同比、1 年指数涨幅、20 日主力净流入占比，取全行业百分位加权得景气分；高景气（≥70 且基期以来、今年净利都增长）放宽 PE 上限到 80、合理 PE 上限到 60，低景气（≤30）买入线收紧到 40%，长线仓空位高景气优先。参数在 `rules_params.json` `sleeves.core.industry`；缓存在 `cache/industry/`（年报永久，季报和景气结果当天有效），首次拉取约 1~2 分钟。景气分 = 基础分 + 政策分（见下一行） |
| `policy_signal.py` / `policy_signals.json` | 政策加分（被 `industry_heat.py` 调用；`python3 main.py policy [日期]` 看当天政策分，`python3 main.py policy check` 校验）。`policy_signals.json` 是人/Agent 按 `data/research/policy/PROMPT.md` Part A 录入的国家级政策事件库（发改委/国务院新闻办发布会实录：`{直播页URL}wzsl/wenzi.json`；政策文件、部委文章等），每个事件给东财二级行业打强度 s∈[−3,+3]，个别主业与东财分类不一致的公司可在 stocks 里打分（≤±2）。权重 = 0.5^(天数/半衰期)，半衰期 讲话 5 / 发布会 7 / 监管规则 15 / 政策文件 20 / 资金落地 30 天，权重 < 0.05 失效；行业政策分 = 10 × clip(Σ s·w / 3, −1, 1)，个股上限 6。政策分不缓存（每天衰减）；高景气的盈利门槛不变。参数在 `sleeves.core.industry.policy`。定时任务只能追加事件，不得改已有事件的 s |
| `etf_value.py` | ETF 长线评估（被 `core_value.py review` 调用）：中证指数官网 `csindex-home/perf/index-perf`（指数日线，`peg` 字段为 PE TTM，2010 年起）；蛋卷基金 `djapi/index_eva/pe_history/{代码}`（海外指数 PE 历史，约每周一点）+ 新浪 `US_MinKService.getDailyK`（美股指数点位）；东方财富 F10 `jbgk_{代码}.html`（跟踪标的、管理费）、`ulist.np`（ETF 现价/成交额/规模）、`f10/lsjz`（净值）；腾讯行情 IOPV 溢价。指数盈利 = 点位 / PE；准入看盈利 10 年年化、估值历史长度、规模/成交额/费率；目标价 = 净值 × 周期修正盈利 × 10 年中位 PE / 指数点位。ETF → 估值指数的对应写在 `rules_params.json` `sleeves.core.etf.index_map` |
| `record_signal.py` | 大模型的写入口：`decide <id> accept\|reject --note`、`add`（规则外信号，自带去重，`--force` 强制；加仓/自选买入自动附 `tp_price` = 现价×1.03 与 `hold_days`，`--tp` 可改写；做T必须带 `--back 低~高` 反向区间，否则返回 2 不记录）、`sent` / `send-failed`、`list` |
| `signal_store.py` | `logs/signals/yyyyMMdd.jsonl` 事件流（candidate / add / decision / sent / send_failed），只追加；被否决的信号仍参与去重，发送失败的不参与 |
| `evaluate.py` | 收盘评估，输出 `logs/eval/yyyyMMdd.{md,json}` 和 `logs/eval/summary.md` |

评估口径（详见 `evaluate.py` 文件头）：

- **方向类**（加仓/减仓/止损/暂不加仓/溢价不追）：按建议方向操作的收益 edge = 方向 × (价格 / 信号价 − 1)，超额扣同期沪深300（港股扣恒指）。有 T+1 收盘用 T+1，否则用当日收盘暂评；|edge| < 0.3% 记持平。14:50 之后的信号只等 T+1。
- **做T**（必须成对）：价格先进入建议区间才算成交（高抛挂区间下沿、低吸挂区间上沿）；有成对区间 pair_zone 时，成交后当日或次日到达回补区上沿 / 卖回区下沿记 ✅“回补成交”（t_pair_edge 为两腿价差），没到记 ❌；旧信号无 pair_zone 仍按反向波动 ≥ `eval.t_target_pct`（3%）判定。当天没到价记 ⚪（不计胜率，也不算收益）。
- **交易仓买入**（带 `tp_price` 的加仓/自选低吸/自选突破）：另算 `tp_edge` / `tp_hit`：次日起 5 个交易日内最高价碰到止盈价记 +3%，否则按第 5 日收盘/信号价−1；不满 5 日不出结果。校准的实盘样本用 tp_edge。
- **异动**：只记录涨跌，不评对错。
- **实际操作**：operations.csv 的每笔成交匹配最近的同向建议，算执行偏差（成交价相对信号价）和持有到收盘的效果。
- **分组统计**：按信号类型、规则、来源/判断（规则候选被采纳/否决/未判断、大模型自提）、是否发送。被否决的候选也打分，用来检验“大模型否决得对不对”。

港股历史日只有日 K（报告标 ≈）；A 股历史日用 5 分钟 K（约 6 个交易日内）。每天运行会刷新近 8 个交易日的 T+1/T+3/T+5。

定时任务：16:00 收盘评估任务运行 `evaluate.py`，写复盘、发飞书简报，并更新经验文档 `experience.md`（追加当日复盘段、维护经验条目 / 标的笔记 / 执行偏差），最后归档当天本项目的定时任务会话（purpose 为 `cron:main:*`，手动会话不动；非交易日也会执行归档）；09:00 盘前任务读取上一交易日的评估报告、summary 和 `experience.md` 做复盘（16:00 漏跑时由 09:00 盘前补写经验文档的复盘段），盘中任务每次分析前读 `experience.md` 的经验条目与标的笔记。16:00 任务还运行 `calibrate.py` 更新次日的动态阈值并在飞书简报里报告参数变化。每周五 17:00 的长线复查任务运行 `core_value.py review`，更新 `core_auto.json`（持仓分仓、目标价、分区）并发飞书周报；09:00 盘前任务通过 `signals_engine.py --mode premarket` 的 `core` 段读取长线仓分区，长线持仓出财报/重大公告时也会补跑 review。机器休眠漏跑时，catchup 会在 16:10 之后补跑 evaluate 和 calibrate（不发飞书）；盘中补跑的时段也会用引擎回放候选（source=catchup）。

```bash
python3 main.py signals --no-record                     # 看当前快照会出哪些候选（不记录）
python3 main.py signals --snapshot cache/snapshots/20261030_1315.json --no-record
python3 main.py record list                             # 今天各信号的状态
python3 main.py evaluate [--date yyyyMMdd]                     # 生成评估报告
python3 main.py calibrate show                                 # 当前市场状态、k 值、各持仓阈值与仓位空间
python3 main.py calibrate ev --code 159813 --type add --price 0.53   # 自补信号的 EV 与建议数量
python3 main.py test                                   # 引擎/记录/评估/EV 的离线测试
```

## 自选股（社保优选 30 只）

和持仓同频（盘中每 10 分钟）判断，**只出买点信号**，不做做T、减仓或异动提醒。名单来源和筛选依据见 `data/research/社保优选30_2026Q3.{md,csv}`。

| 文件 / 字段 | 作用 |
|---|---|
| `watchlist.csv` | 名单：代码、名称、行业、加入日期、来源、关注要点。**只由人改**；已经持有的代码会自动从自选里剔除 |
| `datasources/watchlist.py` | `watch_tech()` 用前一交易日及以前的 130 根日 K 算 MA5/10/20/60、20/60 日高低点、5/20 日涨幅、5 日均量，按天缓存到 `cache/watch_tech.json`；`watch_rows()` 批量取实时行情并合并技术位 |
| 快照 `watchlist` 字段 | `snapshot.py` 每次采集都写入；盘前同时给自选股同步近 3 天公告（写入 `announcements_new`）和公司资料（超过 7 天才刷新） |
| `rules_params.json` 的 `watch` 段 | 阈值和开关（`enabled`） |

信号类型：

- **👀自选低吸 `watch_dip`**：当日涨跌幅在 -7%~+3% 之间、没有触及跌停，距 MA20/MA60/20 日低/60 日低不超过 1.5%（或盘中触及后收回），并且从日内低点回升至少 1.5%。
- **👀自选突破 `watch_break`**：现价突破 20 日高点，涨幅 2%~7%，量比 ≥ 1.5，处在日内高位（≥ 0.7），还没到涨停。
- 建议数量：可用资金 × 3%，单笔不超过 1 万元，按整手取整（688 最少 200 股）。不足一手时按最小单位给出，并在 flags 里注明超出建议金额。
- 每个时点最多出 3 条自选信号，按优先级和得分排序；超出的标为 `capped`，不落库、不推送（会出现在 suppressed 列表里），下个时点仍满足条件时重新参与排序。
- 收盘评估按买入方向打分（和加仓一样）；看板“自选股”页签展示行情、技术位、近 10 天自选信号和近 30 天公告。

## 大盘脉搏（国家队护盘迹象，只作背景）

用来判断“是不是国家队在托盘”，**不单独出信号**。引擎在等级为 strong / watch 时，只给卖出类候选（止损、减仓、做T高抛）加一条 flag：“大盘：…，勿追跌、优先等反抽”，不改变候选本身。

| 文件 / 字段 | 作用 |
|---|---|
| `datasources/market_pulse.py` `pulse()` | 11 只宽基 ETF（hs300：510300/510310/510330/159919；sz50：510050；a500：512050/563360；small：510500/512100；growth：588000/159915）的腾讯 5 分钟 K。**量比** = 今天开盘到当前的累计成交量 ÷ 前 5 个交易日同一时刻的均值（盘中尚未走完的最后一根 K 不算；9:25~9:35 今天只有一根竞价 K 时，回退为上一交易日全天），另有 15 分钟涨跌、距日内低点的反弹；附沪深300 / 上证50 / 中证500 / 中证1000 指数。约 0.6 秒 |
| `market_pulse.py` `etf_shares()` | 上交所 ETF 规模接口（`query.sse.com.cn` COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L，按 STAT_DATE 查全部 ETF 总份额，单位万份），算沪市宽基 ETF 近 5 个交易日的**份额日变化（= 净申购）**。当天份额收盘后才公布，**只在盘前快照里采集**。接口并发会被限流，按交易日串行拉，约 2 秒。深市 159919/159915 没有历史份额接口，不含 |
| `datasources/news.py` `policy_news(since)` | 翻页读新浪 7x24（zhibo_id 152），只匹配【】标题：强词（汇金、国家队、证金公司、中证金融、国新、诚通、平准、护盘、稳市、增持再贷款、降准、降息…）直接命中；弱词（证监会、央行…）要同时带股市相关词；排除词（定增、划转、欧洲央行、美联储…）剔除 |
| 快照 `market_pulse` / `policy_news` | 两种模式都写。盘中：量比到当前时刻，快讯取近 35 分钟（读 3 页）；盘前：量比 = 上一交易日全天，快讯取上一交易日 15:00 以来（读 25 页），另加 `market_pulse.shares` |
| `rules_params.json` 的 `market_pulse` 段 | 阈值和开关 |
| `signals_engine.classify_pulse()` | 输出 `market_pulse`：level / label / reasons / hs300_ratio / group_ratios / 份额字段 / 前 5 条政策快讯。盘中引擎和盘前 `--mode premarket` 都会输出 |
| 看板“大盘脉搏”卡片 | 概览右栏：等级、5 个分组量比条（≥3 红、≥2 橙）、份额日变化（盘中取最近一次盘前快照）、政策快讯 |

等级口径（量能和份额各判一次，取较高的）：

- 量能：沪深300ETF 量比中位 ≥ 3 → **strong**；≥ 2 且沪深300 从日内低点回升 ≥ 1% → **watch**；≥ 1.5 且近 15 分钟中位涨幅 ≥ 0.8% → **watch**（急拉）；其余 normal；没数据 none。
- 份额（只有盘前）：沪市 3 只沪深300ETF 合计份额单日 ≥ +4.5% → **strong**；≥ +2.5% 或近 5 日累计 ≥ +8% → **watch**。
- 只有中小盘放量、沪深300ETF 不放量，算题材反弹，不算国家队。

历史校准：

| 日期 | 沪深300ETF 量比（全天） | 沪深300ETF 份额日变化 |
|---|---|---|
| 2025-04-07（汇金公告增持） | 7~18 倍 | +5.2% |
| 2025-04-08 | 5~14 倍 | +6.0% |
| 2024-02-07 | 约 3 倍 | +7.1% |
| 平常交易日 | 约 1 倍 | ±1% |
| 2026-11-02（小盘反弹） | 约 0.95（中小盘 2.25） | +4.0%，近 5 日累计 +11%（中证500/1000ETF +15%~+25%） |

## 休眠兜底（定时采集 + 错过时段补跑）

MiniMax Code 的 cron 调度器跑在 **GUI 进程**里（应用是 Electron，没有 headless CLI）。机器睡眠时：

- `pmset` 的 `sleep 0` 挡不住**合盖睡眠**——没有外接显示器时合盖必然进入硬件深睡，软件层面无法阻止。
- 调度器在应用启动时重算 `next_run`，把已错过的触发点**直接跳过，不补跑**（可在 `~/.minimax/v2/sqlite/runtime-state.sqlite` 的 `local_runtime_v2_scheduler_jobs.updated_at_ms` 看到它等于应用启动时刻）。

因此装两个 launchd agent 兜底（`./setup_sleep_guard.sh install`）：

| Agent | 频率 | 作用 |
|---|---|---|
| `com.fatewheel.collect` | 每个分析时点前 1 分钟 | 跑 `collect_snapshot.py`，把行情快照按时间戳归档到 `cache/snapshots/{yyyyMMdd}_{HHMM}.json`。**这一步保证错过时段后数据还在**——否则补跑时内存里的快照已经没了 |
| `com.fatewheel.catchup` | 每 10 分钟 + 每次唤醒 | 跑 `catchup.py`，比对当天日志里已有的 `## HH:MM` 段落与计划时点表，把缺失的时段补写进日志。时点过后 7 分钟内不判缺失（cron 可能还在跑），避免和正式段落重复 |

补跑的两个原则：

1. **有快照才写行情结论**，没有快照就如实写「数据缺失」，绝不编造。
2. **补跑只记录数据，不发飞书**——避免用过期数据做投资提醒。

另有一层定时唤醒（`./setup_sleep_guard.sh wake`，需要 sudo），在每个分析时点前 2 分钟尝试唤醒机器。`pmset repeat` 只能设一对重复事件，所以拆成两层：`repeat` 负责工作日 07:58 唤醒，长期有效；其余时点用 `pmset schedule` 登记一次性唤醒（owner=fatewheel），覆盖未来 30 天，到期要重跑 `wake` 续期，`status` 里能看到最后一个唤醒点。合盖深睡时唤醒可能失效，属于尽力而为；真正的兜底是上面的 catchup。

手动使用：

```bash
python3 main.py snapshot --slot 09:40 --mode intraday   # 补采某个时点
python3 main.py snapshot --all                           # 补采当天所有已过时点
python3 main.py catchup --dry-run                                # 只看缺哪些
python3 main.py catchup                                          # 补写
./setup_sleep_guard.sh status                               # 查看 agent / 唤醒 / 快照状态
```

## 迁移 / 重新配置

1. 把整个 `fate_wheel` 目录拷到新位置（新电脑或新路径都可以）。
2. 运行 `./setup.sh`（可加 `--check` 验证数据源、`--test-feishu` 发测试消息、`--install-deps` 自动装 python/node/lark-cli、`--open-id ou_xxx` 换飞书接收人）。脚本会检查环境、建目录、把 `intraday_rules.md` 和 `setup/crons.json` 中的旧路径换成新路径、安装 `fate` 命令。
3. 新电脑上的飞书：`lark-cli config init` 配置机器人应用（app-id / app-secret）。
4. 定时任务：脚本生成 `setup/CRON_SETUP_PROMPT.md` 并复制到剪贴板，在 MiniMax Code 中新建会话（工作目录选项目目录）粘贴即可重建 6 个任务（09:00 盘前、盘中 4 条、16:00 收盘评估）。
5. `setup/crons.json` 是定时任务的备份；以后修改定时任务的 prompt 或时间时要同步更新它。
