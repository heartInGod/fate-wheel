import json,urllib.request,time
def get(date,page):
    u=f"https://data.eastmoney.com/dataapi/zlsj/list?date={date}&type=3&zjc=0&sortField=HOLD_VALUE&sortDirec=1&pageNum={page}&pageSize=500"
    r=urllib.request.Request(u,headers={"Referer":"https://data.eastmoney.com/zlsj/","User-Agent":"Mozilla/5.0"})
    return json.load(urllib.request.urlopen(r,timeout=30))
for d in ['2026-09-30','2026-06-30','2026-03-31','2025-12-31','2025-09-30']:
    rows=[];p=1
    while True:
        j=get(d,p);data=j.get('data') or []
        rows+=data
        if len(data)<500: break
        p+=1;time.sleep(0.5)
    json.dump(rows,open(f'{d}.json','w'),ensure_ascii=False)
    print(d,len(rows),j.get('pages'),j.get('count') if 'count' in j else '')
