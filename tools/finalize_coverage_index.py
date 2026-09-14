"""Validate and close the completed media ledger; no app/runtime mutation."""
import json
from datetime import datetime,timedelta
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]/'artifacts/video/coverage-session'
p=ROOT/'coverage-manifest.json'
d=json.loads(p.read_text(encoding='utf-8'))
raws={r['source']:r for r in d['session']['raw_recordings']}
assert set(raws)=={str(x.relative_to(ROOT)).replace('\\','/') for x in (ROOT/'raw').glob('*.mp4')}
errors=[]
for r in raws.values():
    if (ROOT/r['source']).stat().st_size!=r['bytes']:errors.append('Raw size changed: '+r['source'])
    if r['full_decode']!='passed':errors.append('Decode failed: '+r['source'])
    if not isinstance(r['visual_review'],dict):errors.append('Visual review pending: '+r['source'])
    for f in r.get('sampled_frames',[]):
        if not (ROOT/f['file']).is_file():errors.append('Missing frame: '+f['file'])
for s in d['sequences']:
    for t in [s.get('take',{})]+s.get('additional_takes',[])+s.get('branch_takes',[]):
        raw=t.get('raw_file')
        if not raw:continue
        if raw not in raws:errors.append('Unindexed raw: '+raw);continue
        duration=raws[raw]['duration_seconds']
        for m in t.get('action_marks',[]):
            ts=m.get('observed_at_seconds')
            if ts is not None and not 0<=ts<duration:errors.append('Mark out of bounds: '+raw)
        for w in t.get('usable_coverage') or []:
            if not 0<=w['start_seconds']<w['end_seconds']<=duration:errors.append('Window out of bounds: '+raw)
assert not errors,errors
end=max(datetime.fromisoformat(r['creation_time_utc'].replace('Z','+00:00'))+timedelta(seconds=r['duration_seconds']) for r in raws.values())
d['document_type']='Final observed continuous-capture coverage index; separate editing selects, no final montage'
d['session']['qa_summary']['scope']='All17completed raw recordings; capture session closed. Visual privacy review uses sampled and critical boundary frames.'
d['session']['capture_complete']=True
d['session']['qa_index_complete']=True
d['unrecorded_or_unavailable_behaviors']=[
    'The first confirmed-invitation raw contains a debug infobar; excluded from clean selects. Other resident response/verification coverage is clean.',
    'The principal meeting quorum-to-confirmed transition was not captured; first invitation already confirmed. Separate extra meeting records actual availability and unmet-quorum escalation.',
    'The original principal parking report intake was not captured. Take17 supplies an explicitly separate alternative message-to-prepared-case opening.',
    'No automatic replacement round is shown; expired extra meeting routes to management because no approved retry range was configured.',
    'Not captured: quote rejection/revised quote, action reassignment, rejected completion, appointment exception. These remain planned checklist branches, not recorded outcomes.',
    'Generic next-step text in the expired extra meeting still says waiting. The Meeting Reschedule task and ended response-round controls are the actual captured evidence.',
    'Current product lifecycles use local synthetic inputs and simulated vendor delivery. Technical source viewer shows historical saved evidence, not new live calls.',
    'No physical repair, supplier negotiation, payment, warranty claim submission or approved new warranty job is performed by the recorded scenarios.'
]
d['session_end']={
    'finished_at':end.isoformat().replace('+00:00','Z'),
    'finished_at_basis':'Last raw creation timestamp plus measured duration; UTC',
    'final_case_states':[
        {'case_ref':'parking_primary','observed_state':'Closed; verified parking rule and publication stored in memory','source_raw':'raw/2026-09-14 02-59-44.mp4'},
        {'case_ref':'maintenance_primary','observed_state':'Closed after resident verification;705USD demo outcome retained in memory','source_raw':'raw/2026-09-14 03-13-49.mp4'},
        {'case_ref':'extra_elevator_clarification','observed_state':'Awaiting management warranty review before another paid job','source_raw':'raw/2026-09-14 03-19-44.mp4'},
        {'case_ref':'extra_parking_quorum','observed_state':'Response round ended; management rescheduling review; no replacement submitted','source_raw':'raw/2026-09-14 03-25-38.mp4'},
        {'case_ref':'alternative_opening','observed_state':'Planning; Offer meeting times; prepared discussion and no proposed date','source_raw':'raw/2026-09-14 03-32-07.mp4'}
    ],
    'all_raw_files_verified':True,
    'credentials_or_private_data_visible':False,
    'privacy_review_scope':'Sampled source frames and critical full-size action/source frames; derivative editor additionally inspected selected boundaries. Provider identifiers/addresses are explicitly omitted in saved-source viewer.',
    'notes':'17raw files preserved. OBS stop and original profile/collection restoration reported and verified by recording director. No final montage, narration or additional live transmission performed by the media assembly task.'
}
d['media_deliverables']={'raw_count':len(raws),'raw_directory':'raw','selects_manifest':'selects/edit-manifest.json','review_gallery':'browse.html','readme':'README_TR.md','final_montage_created':False}
p.write_text(json.dumps(d,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
qa={'status':'passed','raw_count':len(raws),'duration_seconds':d['session']['indexed_duration_seconds'],'bytes':d['session']['indexed_bytes'],'source_ranges_and_frames':'all indexed references exist and are within measured duration','full_decode_passed':len(raws),'visual_issue_raws':['raw/2026-09-14 02-50-17.mp4'],'capture_end_utc':d['session_end']['finished_at']}
(ROOT/'qa/final-index-check.json').write_text(json.dumps(qa,indent=2)+'\n',encoding='utf-8')
print(json.dumps(qa,indent=2))
