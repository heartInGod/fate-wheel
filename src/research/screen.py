import json,collections
Q=['2026-09-30','2026-06-30','2026-03-31','2025-12-31','2025-09-30']
H={d:{r['SECURITY_CODE']:r for r in json.load(open(d+'.json'))} for d in Q}
q=json.load(open('quote.json')); a=json.load(open('annual.json'))
def f(x):
    try: return float(x)
    except: return None
own={'000657','605090'}
rows=[]
for c,r in H[Q[0]].items():
    d=q.get(c); 
    if not d or 'ST' in d['f14'] or c in own: continue
    n=sum(c in H[x] for x in Q)
    roe25=f((a['2025-12-31'].get(c) or {}).get('WEIGHTAVG_ROE')); roe24=f((a['2024-12-31'].get(c) or {}).get('WEIGHTAVG_ROE'))
    roe9=f(d['f37']); g=f(d['f46']); rv=f(d['f41']); debt=f(d['f57']); pe=f(d['f115']); cap=f(d['f20']) or 0
    rows.append(dict(code=c,name=d['f14'],ind=d['f100'],n=n,roe24=roe24,roe25=roe25,roe9=roe9,np=g,rev=rv,gm=f(d['f49']),debt=debt,pe=pe,pb=f(d['f23']),cap=cap/1e8,
        val=r['HOLD_VALUE']/1e8,ratio=r['FREESHARES_RATIO'],cha=r['HOLDCHA'],org=r['HOULD_NUM'],y=f(d['f25']),p=d['f2']))
fin={'银行','证券Ⅱ','保险Ⅱ','多元金融'}
ok=[]
for x in rows:
    why=[]
    if x['n']<3: why.append('持有<3期')
    if x['cap']<150: why.append('市值<150亿')
    if x['roe24'] is None or x['roe25'] is None or x['roe9'] is None: why.append('ROE缺')
    else:
        if x['roe25']<12 or x['roe24']<10: why.append('年报ROE低')
        if x['roe9']<7.5: why.append('今年ROE低')
    if x['np'] is None or x['np']<0: why.append('净利负增长')
    if x['rev'] is None or x['rev']<0: why.append('营收负增长')
    if x['pe'] is None or x['pe']<=0 or x['pe']>50: why.append('PE')
    if x['debt'] is not None and x['debt']>70 and x['ind'] not in fin: why.append('负债高')
    x['why']=why
    if not why: ok.append(x)
print('总',len(rows),'通过',len(ok))
import math
def z(v,lo,hi): return max(0,min(1,(v-lo)/(hi-lo)))
for x in ok:
    x['score']=round(35*z((x['roe24']+x['roe25'])/2+x['roe9']*4/3,20,60)+20*z(min(x['np'],60),0,60)+10*z(min(x['rev'],40),0,40)+15*z(50-x['pe'],0,40)+10*z(x['n'],3,5)+10*z(math.log10(x['cap']),2.2,3.5),1)
ok.sort(key=lambda x:-x['score'])
cnt=collections.Counter();pick=[]
for x in ok:
    if cnt[x['ind']]>=2: continue
    cnt[x['ind']]+=1; pick.append(x)
json.dump({'ok':ok,'pick':pick[:30]},open('screen.json','w'),ensure_ascii=False)
for i,x in enumerate(pick[:40],1):
    print(f"{i:2} {x['code']} {x['name']:6} {x['ind']:8} 期{x['n']} ROE24/25/今{x['roe24']:.1f}/{x['roe25']:.1f}/{x['roe9']:.1f} 净利{x['np']:.0f}% 营收{x['rev']:.0f}% PE{x['pe']:.0f} 市值{x['cap']:.0f} 社保{x['val']:.1f}亿{x['cha']} 年内{x['y']}% 分{x['score']}")
