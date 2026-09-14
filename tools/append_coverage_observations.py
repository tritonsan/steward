"""Merge editor-reviewed per-take observations into the coverage media ledger.

Input JSON is explicitly authored from media review; this performs no UI action.
"""
import argparse
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]/'artifacts/video/coverage-session'
p=argparse.ArgumentParser()
p.add_argument('observations',type=Path)
args=p.parse_args()
o=json.loads(args.observations.read_text(encoding='utf-8-sig'))
mp=ROOT/'coverage-manifest.json'
d=json.loads(mp.read_text(encoding='utf-8-sig'))
qp=ROOT/'qa'/o['stem']/'technical-qa.json'
q=json.loads(qp.read_text(encoding='utf-8-sig'))
q['visual_review']=o['visual_review']
qp.write_text(json.dumps(q,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
r=d['session']['raw_recordings']
r[:]=[x for x in r if x['source']!=q['source']]+[q]
r.sort(key=lambda x:x['source'])
for update in o.get('sequence_updates',[]):
    s=next((x for x in d['sequences'] if x['id']==update['id']),None)
    if s is None:
        s={'id':update['id'],**update['new_sequence']}
        d['sequences'].append(s)
    s['status']=update['status']
    t=update['take']
    t.setdefault('raw_file',q['source'])
    t.setdefault('start_seconds',0)
    t.setdefault('end_seconds',q['duration_seconds'])
    if s.get('take',{}).get('raw_file') and s['take']['raw_file']!=q['source']:
        extra=s.setdefault('additional_takes',[])
        extra[:]=[x for x in extra if x.get('raw_file')!=s['take']['raw_file']]+[s['take']]
    s['take']=t
    if update.get('branch_take'):
        branches=s.setdefault('branch_takes',[])
        branches[:]=[x for x in branches if x.get('raw_file')!=q['source']]+[t]
for name, additions in o.get('ledger_additions',{}).items():
    items=d.setdefault(name,[])
    for entry in additions:
        items[:]=[x for x in items if x.get('raw_file')!=entry.get('raw_file')]+[entry]
for entry in o.get('branch_cases',[]):
    cases=d['case_refs'].setdefault('separate_branch_cases',[])
    cases[:]=[x for x in cases if x.get('title')!=entry['title']]+[entry]
d['session'].update(indexed_raw_count=len(r),indexed_duration_seconds=round(sum(x['duration_seconds'] for x in r),6),
    indexed_bytes=sum(x['bytes'] for x in r),qa_summary={
    'full_decode_passed':sum(x['full_decode']=='passed' for x in r),
    'clean_takes':sum(isinstance(x['visual_review'],dict) and x['visual_review'].get('debug_infobar_visible') is False for x in r),
    'takes_needing_infobar_correction':sum(isinstance(x['visual_review'],dict) and x['visual_review'].get('debug_infobar_visible') is True for x in r),
    'scope':'Completed indexed files only; session open.'})
mp.write_text(json.dumps(d,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
print(json.dumps({k:d['session'][k] for k in ['indexed_raw_count','indexed_duration_seconds','indexed_bytes','qa_summary']},indent=2))
