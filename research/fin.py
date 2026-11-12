import json,urllib.request,urllib.parse,time
codes=sorted({r['SECURITY_CODE'] for r in json.load(open('2026-09-30.json'))})
def mk(c): return ('1.' if c[0] in '69' else '0.')+c
out={}
for i in range(0,len(codes),80):
    s=','.join(mk(c) for c in codes[i:i+80])
    u="https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&fields=f12,f14,f2,f9,f23,f20,f37,f41,f46,f49,f57,f100,f115,f129,f24,f25&secids="+s
    j=json.load(urllib.request.urlopen(urllib.request.Request(u,headers={"User-Agent":"Mozilla/5.0"}),timeout=30))
    for d in j['data']['diff']: out[d['f12']]=d
    time.sleep(0.4)
json.dump(out,open('quote.json','w'),ensure_ascii=False); print('quote',len(out))
ann={}
for y in ['2025-12-31','2024-12-31']:
    p=1;m={}
    while True:
        q=urllib.parse.urlencode({"reportName":"RPT_LICO_FN_CPD","columns":"SECURITY_CODE,WEIGHTAVG_ROE,YSTZ,SJLTZ,XSMLL","pageSize":"500","pageNumber":str(p),"filter":f"(REPORTDATE='{y}')","source":"WEB","client":"WEB","sortColumns":"SECURITY_CODE","sortTypes":"1"})
        j=json.load(urllib.request.urlopen("https://datacenter-web.eastmoney.com/api/data/v1/get?"+q,timeout=30))
        r=j['result']
        for d in r['data']: m[d['SECURITY_CODE']]=d
        if p>=r['pages']: break
        p+=1;time.sleep(0.3)
    ann[y]=m; print(y,len(m))
json.dump(ann,open('annual.json','w'),ensure_ascii=False)
