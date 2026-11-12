"""公司/基金资料接口：东方财富 F10（公司概况、主营构成、经营评述）、天天基金（ETF 档案、持仓）。"""
from ._http import DataSourceError, http_json, normalize, to_float

_FUND_PARAMS = {"deviceid": "1", "plat": "Android", "product": "EFund", "version": "1"}
_MAINOP_TYPE = {"1": "按行业", "2": "按产品", "3": "按地区"}


def is_fund(code):
    n = normalize(code)
    return n["market"] != "hk" and n["code"][:2] in ("15", "16", "18", "50", "51", "52", "56", "58")


def _strip(s, limit=None):
    s = " ".join((s or "").split())
    return s[:limit] + "…" if limit and len(s) > limit else s


def _em_code(n):
    return n["market"].upper() + n["code"]


def company_profile(code):
    """公司概况：全称、行业、上市/成立日期、管理层、员工、官网、公司简介、经营范围/主营业务（A股/港股）。"""
    n = normalize(code)
    if is_fund(code):
        raise DataSourceError("基金请用 fund_profile")
    if n["market"] == "hk":
        j = http_json("https://datacenter.eastmoney.com/securities/api/data/v1/get", params={
            "reportName": "RPT_HKF10_INFO_ORGPROFILE", "columns": "ALL",
            "filter": f'(SECUCODE="{n["code"]}.HK")', "source": "F10", "client": "PC"})
        rows = ((j.get("result") or {}).get("data")) or []
        if not rows:
            raise DataSourceError(f"{n['code']} 无港股公司资料")
        r = rows[0]
        return {"code": n["code"], "name": r.get("SECURITY_NAME_ABBR"), "full_name": r.get("ORG_NAME"),
                "industry": r.get("INDUSTRY_TYPE") or r.get("BELONG_INDUSTRY"), "market": r.get("BELONG_MARKET"),
                "listing_date": (r.get("LISTING_DATE") or "")[:10], "found_date": (r.get("FOUND_DATE") or "")[:10],
                "chairman": r.get("CHAIRMAN"), "employees": r.get("EMP_NUM"), "website": r.get("ORG_WEB"),
                "address": r.get("ADDRESS"), "auditor": r.get("ACCOUNT_FIRM"),
                "profile": _strip(r.get("ORG_PROFILE")), "main_business": _strip(r.get("MAIN_BUSINESS")),
                "source": "东方财富港股F10"}
    j = http_json("https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax",
                  params={"code": _em_code(n)})
    base = (j.get("jbzl") or [{}])[0]
    issue = (j.get("fxxg") or [{}])[0]
    if not base:
        raise DataSourceError(f"{n['code']} 无公司资料")
    return {"code": n["code"], "name": base.get("SECURITY_NAME_ABBR"), "full_name": base.get("ORG_NAME"),
            "former_names": base.get("FORMERNAME"),
            "industry": base.get("EM2016"), "industry_csrc": base.get("INDUSTRYCSRC1"),
            "market": base.get("SECURITY_TYPE"), "listing_date": (issue.get("LISTING_DATE") or "")[:10],
            "found_date": (issue.get("FOUND_DATE") or "")[:10], "chairman": base.get("CHAIRMAN"),
            "president": base.get("PRESIDENT"), "employees": base.get("EMP_NUM"),
            "province": base.get("PROVINCE"), "address": base.get("ADDRESS"), "website": base.get("ORG_WEB"),
            "auditor": base.get("ACCOUNTFIRM_NAME"),
            "profile": _strip(base.get("ORG_PROFILE")), "business_scope": _strip(base.get("BUSINESS_SCOPE")),
            "source": "东方财富F10"}


def main_business(code, review_chars=600):
    """主营构成（最新报告期按行业/产品/地区拆分的收入、占比、毛利率）+ 经营评述摘要。仅 A 股；港股返回主营业务描述。"""
    n = normalize(code)
    if is_fund(code):
        raise DataSourceError("基金无主营构成，请用 fund_profile 看持仓")
    if n["market"] == "hk":
        p = company_profile(code)
        return {"code": n["code"], "name": p["name"], "report_date": None, "composition": {},
                "main_business": p["main_business"], "review": None,
                "note": "港股无结构化主营构成，仅有业务描述；分部收入需看财报", "source": p["source"]}
    j = http_json("https://emweb.securities.eastmoney.com/PC_HSF10/BusinessAnalysis/PageAjax",
                  params={"code": _em_code(n)})
    rows = j.get("zygcfx") or []
    if not rows:
        raise DataSourceError(f"{n['code']} 无主营构成数据")
    latest = max(r["REPORT_DATE"] for r in rows)
    comp = {}
    for r in sorted((r for r in rows if r["REPORT_DATE"] == latest), key=lambda x: x.get("RANK") or 0):
        comp.setdefault(_MAINOP_TYPE.get(str(r.get("MAINOP_TYPE")), "其他"), []).append({
            "item": r.get("ITEM_NAME"),
            "revenue_yi": round(r["MAIN_BUSINESS_INCOME"] / 1e8, 2) if r.get("MAIN_BUSINESS_INCOME") else None,
            "revenue_ratio_pct": round(r["MBI_RATIO"] * 100, 2) if r.get("MBI_RATIO") is not None else None,
            "gross_margin_pct": round(r["GROSS_RPOFIT_RATIO"] * 100, 2) if r.get("GROSS_RPOFIT_RATIO") is not None else None,
        })
    review = (j.get("jyps") or [{}])[0]
    return {"code": n["code"], "report_date": latest[:10], "composition": comp,
            "review_date": (review.get("REPORT_DATE") or "")[:10],
            "review": _strip(review.get("BUSINESS_REVIEW"), review_chars),
            "business_scope": _strip(((j.get("zyfw") or [{}])[0]).get("BUSINESS_SCOPE"), 300),
            "source": "东方财富F10"}


def fund_profile(code, top=10):
    """基金/ETF 档案：全称、类型、成立日、规模、基金公司、经理、跟踪指数、费率、投资目标、阶段收益、前十大持仓。"""
    c = normalize(code)["code"]
    p = dict(_FUND_PARAMS, FCODE=c)
    d = (http_json("https://fundmobapi.eastmoney.com/FundMNewApi/FundMNDetailInformation", params=p).get("Datas")) or {}
    b = (http_json("https://fundmobapi.eastmoney.com/FundMApi/FundBaseTypeInformation.ashx", params=p).get("Datas")) or {}
    if not d and not b:
        raise DataSourceError(f"{c} 无基金资料")
    try:
        h = (http_json("https://fundmobapi.eastmoney.com/FundMNewApi/FundMNInverstPosition", params=p).get("Datas")) or {}
    except DataSourceError:
        h = {}
    stocks = [{"code": s.get("GPDM"), "name": s.get("GPJC"), "weight_pct": to_float(s.get("JZBL")),
               "change": s.get("PCTNVCHGTYPE")} for s in (h.get("fundStocks") or [])[:int(top)]]
    nav = to_float(d.get("ENDNAV"))
    return {"code": c, "name": d.get("SHORTNAME") or b.get("SHORTNAME"), "full_name": d.get("FULLNAME"),
            "type": d.get("FTYPE"), "found_date": d.get("ESTABDATE"),
            "aum_yi": round(nav / 1e8, 2) if nav else None, "aum_date": d.get("FEGMRQ"),
            "company": d.get("JJGS"), "manager": d.get("JJJL"), "custodian": d.get("TGYH"),
            "index_code": d.get("INDEXCODE"), "index_name": d.get("INDEXNAME"), "benchmark": d.get("BENCH"),
            "mgmt_fee": d.get("MGREXP"), "custody_fee": d.get("TRUSTEXP"), "risk_level": d.get("RISKLEVEL"),
            "objective": _strip(d.get("INVTGT"), 200),
            "returns_pct": {"1m": to_float(b.get("SYL_Y")), "3m": to_float(b.get("SYL_3Y")),
                            "6m": to_float(b.get("SYL_6Y")), "1y": to_float(b.get("SYL_1N"))},
            "top_holdings": stocks, "source": "天天基金"}


def profile(code):
    """通用资料接口：股票返回公司概况+主营构成，基金/ETF 返回基金档案+持仓。"""
    if is_fund(code):
        return {"kind": "fund", **fund_profile(code)}
    out = {"kind": "stock", **company_profile(code)}
    try:
        mb = main_business(code)
        out.update({"main_business_report_date": mb.get("report_date"), "composition": mb.get("composition"),
                    "business_review": mb.get("review")})
    except DataSourceError as e:
        out["main_business_error"] = str(e)
    return out
