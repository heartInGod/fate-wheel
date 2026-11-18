"""本地档案：把变化慢的资料和随时新增的公告落到 Markdown 文件，按代码一只一个文件。

  profiles/{代码}.md       公司/基金资料，每天最多刷新一次（当天已更新则跳过，force=True 强制）
  announcements/{代码}.md  公告档案，增量合并、按链接去重、只增不删，记录每条的「收录时间」

港股文件名沿用 holdings.csv 写法，如 0100.HK.md。
"""
import datetime
import os
import re
from concurrent.futures import ThreadPoolExecutor

from . import announcements as ann
from . import profile as prof
from ._http import DataSourceError, normalize
from .holdings import ROOT, holdings
from .watchlist import watchlist

PROFILE_DIR = os.path.join(ROOT, "profiles")
ANN_DIR = os.path.join(ROOT, "announcements")
_ROW = re.compile(r"^\| (?P<date>[^|]*) \| (?P<title>[^|]*) \| (?P<cat>[^|]*) \| (?P<src>[^|]*) \| "
                  r"(?P<seen>[^|]*) \| \[原文\]\((?P<url>[^)]*)\) \|$")


def file_key(code):
    n = normalize(code)
    return f"{int(n['code']):04d}.HK" if n["market"] == "hk" else n["code"]


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _cell(s):
    return " ".join(str(s if s is not None else "").replace("|", "/").split()) or "-"


# ---------------- 资料 ----------------

def _render_profile(p, name_hint, key):
    lines = [f"# {p.get('name') or name_hint}（{key}）资料", "",
             f"> 更新时间：{_now()}　数据源：{p.get('source', '-')}　（自动生成，每天刷新一次，请勿手改）", ""]
    if p.get("kind") == "fund":
        r = p.get("returns_pct") or {}
        rows = [("全称", p.get("full_name")), ("类型", p.get("type")), ("成立日", p.get("found_date")),
                ("规模", f"{p.get('aum_yi')} 亿（{p.get('aum_date')}）"), ("基金公司", p.get("company")),
                ("基金经理", p.get("manager")), ("托管行", p.get("custodian")),
                ("跟踪指数", f"{p.get('index_name')}（{p.get('index_code')}）"), ("业绩基准", p.get("benchmark")),
                ("管理费/托管费", f"{p.get('mgmt_fee')} / {p.get('custody_fee')}"), ("风险等级", p.get("risk_level")),
                ("近1/3/6/12月收益%", f"{r.get('1m')} / {r.get('3m')} / {r.get('6m')} / {r.get('1y')}")]
        lines += ["## 基本信息", "", "| 项目 | 内容 |", "|---|---|"] + [f"| {k} | {_cell(v)} |" for k, v in rows]
        lines += ["", "## 投资目标", "", p.get("objective") or "-", "", "## 前十大持仓", "",
                  "| 代码 | 名称 | 占净值% | 变动 |", "|---|---|---|---|"]
        lines += [f"| {_cell(s['code'])} | {_cell(s['name'])} | {_cell(s['weight_pct'])} | {_cell(s['change'])} |"
                  for s in p.get("top_holdings") or []]
        return "\n".join(lines) + "\n"
    rows = [("全称", p.get("full_name")), ("曾用名", p.get("former_names")), ("行业", p.get("industry")),
            ("证监会行业", p.get("industry_csrc")), ("上市板块", p.get("market")),
            ("上市日期", p.get("listing_date")), ("成立日期", p.get("found_date")), ("董事长", p.get("chairman")),
            ("总经理", p.get("president")), ("员工数", p.get("employees")), ("地址", p.get("address")),
            ("官网", p.get("website")), ("审计机构", p.get("auditor"))]
    lines += ["## 基本信息", "", "| 项目 | 内容 |", "|---|---|"]
    lines += [f"| {k} | {_cell(v)} |" for k, v in rows if v not in (None, "")]
    lines += ["", "## 公司简介", "", p.get("profile") or "-", ""]
    if p.get("main_business"):
        lines += ["## 主营业务", "", p["main_business"], ""]
    comp = p.get("composition") or {}
    if comp:
        lines += [f"## 主营构成（{p.get('main_business_report_date')}）", ""]
        for k, items in comp.items():
            lines += [f"**{k}**", "", "| 项目 | 收入(亿) | 收入占比% | 毛利率% |", "|---|---|---|---|"]
            lines += [f"| {_cell(i['item'])} | {_cell(i['revenue_yi'])} | {_cell(i['revenue_ratio_pct'])} | "
                      f"{_cell(i['gross_margin_pct'])} |" for i in items]
            lines.append("")
    elif p.get("main_business_error"):
        lines += ["## 主营构成", "", f"暂无：{p['main_business_error']}", ""]
    if p.get("business_review"):
        lines += ["## 经营评述（摘要）", "", p["business_review"], ""]
    if p.get("business_scope"):
        lines += ["## 经营范围", "", p["business_scope"], ""]
    return "\n".join(lines).rstrip() + "\n"


def _profile_date(path):
    try:
        with open(path, encoding="utf-8") as f:
            m = re.search(r"更新时间：(\d{4}-\d{2}-\d{2})", f.read(400))
        return m.group(1) if m else None
    except OSError:
        return None


def _names():
    """代码→名称：持仓优先，其次自选股（watchlist.csv）。"""
    names = {w["code"]: w["name"] for w in watchlist()}
    names.update({h["code"]: h["name"] for h in holdings()})
    return names


def sync_profiles(codes=None, force=False, max_age_days=0):
    """刷新 profiles/{代码}.md；默认全部持仓。资料日期距今 ≤ max_age_days 天的跳过
    （默认 0 = 当天已更新才跳过；自选股用 7）。返回每只的状态。"""
    names = _names()
    codes = codes or [h["code"] for h in holdings()]
    fresh_from = (datetime.date.today() - datetime.timedelta(days=int(max_age_days))).isoformat()

    def one(c):
        path = os.path.join(PROFILE_DIR, f"{file_key(c)}.md")
        d = _profile_date(path)
        if not force and d and d >= fresh_from:
            return {"code": c, "status": "skipped", "file": os.path.relpath(path, ROOT)}
        try:
            p = prof.profile(c)
        except Exception as e:  # noqa: BLE001
            return {"code": c, "status": "error", "error": str(e), "file": os.path.relpath(path, ROOT)}
        _write(path, _render_profile(p, names.get(normalize(c)["code"], c), file_key(c)))
        return {"code": c, "status": "updated", "file": os.path.relpath(path, ROOT)}

    with ThreadPoolExecutor(max_workers=4) as ex:
        return list(ex.map(one, codes))


def read_profile(code):
    """读取本地资料档案（Markdown 文本）；没有则先同步一次。"""
    path = os.path.join(PROFILE_DIR, f"{file_key(code)}.md")
    if not os.path.exists(path):
        sync_profiles([code])
    with open(path, encoding="utf-8") as f:
        return {"code": code, "file": os.path.relpath(path, ROOT), "content": f.read()}


# ---------------- 公告 ----------------

def _load_ann(path):
    rows = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = _ROW.match(line.rstrip("\n"))
                if m:
                    rows.append({"date": m["date"], "title": m["title"], "category": m["cat"],
                                 "source": m["src"], "seen": m["seen"], "url": m["url"]})
    return rows


def _render_ann(name, key, rows):
    lines = [f"# {name}（{key}）公告档案", "",
             f"> 最后同步：{_now()}　共 {len(rows)} 条，按公告日期倒序。"
             "「收录时间」为本地首次抓到该公告的时间。（自动维护，只增不删，请勿手改）", "",
             "| 公告日期 | 标题 | 类别 | 来源 | 收录时间 | 链接 |", "|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {_cell(r['date'])} | {_cell(r['title'])} | {_cell(r.get('category'))} | "
                     f"{_cell(r['source'])} | {_cell(r['seen'])} | [原文]({r['url']}) |")
    return "\n".join(lines) + "\n"


def sync_announcements(codes=None, days=3, n=30):
    """拉取近 days 天公告并合并进 announcements/{代码}.md（首次建档自动回溯 90 天）。
    返回每只的新增条目（new）和今日公告（today），供盘前/盘中分析直接使用。"""
    names = _names()
    codes = codes or [h["code"] for h in holdings()]
    today = datetime.date.today().isoformat()
    now = _now()

    def one(c):
        key = file_key(c)
        path = os.path.join(ANN_DIR, f"{key}.md")
        old = _load_ann(path)
        first = not os.path.exists(path)
        res = {"code": c, "file": os.path.relpath(path, ROOT)}
        try:
            fetched = ann.announcements(c, days=90 if first else days, n=100 if first else n)
        except Exception as e:  # noqa: BLE001
            res.update(status="error", error=str(e), new=[],
                       today=[r for r in old if r["date"][:10] == today])
            return res
        seen = {r["url"] for r in old}
        new = []
        for a in fetched:
            if a["url"] in seen:
                continue
            seen.add(a["url"])
            new.append({"date": a.get("date") or "", "title": _cell(a.get("title")),
                        "category": _cell(a.get("category")), "source": a.get("source") or "-",
                        "seen": now, "url": a["url"]})
        rows = sorted(old + new, key=lambda r: r["date"], reverse=True)
        if new or first:
            _write(path, _render_ann(names.get(normalize(c)["code"]) or c, key, rows))
        res.update(status="created" if first else ("updated" if new else "unchanged"),
                   new=[] if first else new,  # 首次建档的历史公告不算「新增」
                   new_count=len(new), today=[r for r in rows if r["date"][:10] == today])
        return res

    with ThreadPoolExecutor(max_workers=4) as ex:
        return list(ex.map(one, codes))


def read_announcements(code, days=30):
    """读取本地公告档案中近 days 天的条目（不联网）。"""
    path = os.path.join(ANN_DIR, f"{file_key(code)}.md")
    start = (datetime.date.today() - datetime.timedelta(days=int(days))).isoformat()
    return [r for r in _load_ann(path) if r["date"][:10] >= start]
