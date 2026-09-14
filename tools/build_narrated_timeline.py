"""Sentence-aware edit decisions for the user's recorded narration."""
from pathlib import Path
import copy
import json
import re

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'artifacts/video/coverage-session/assembly-v3'
PRIOR=OUT.parent/'assembly-v2'
old=json.loads((PRIOR/'timeline.json').read_text(encoding='utf8'))
inventory=json.loads((ROOT/'artifacts/video/narration-analysis/narration-inventory.json').read_text(encoding='utf8'))
files={x['number']:x for x in inventory['files']}
plan={'version':3,'status':'Complete narrated edit with thirteen verified user recordings','maximum_video_minutes':5,
      'music':{'file':'artifacts/video/music-original/steward-original-ambient-300s.wav','provenance':'artifacts/video/music-original/PROVENANCE.md'},
      'shots':[copy.deepcopy(old['shots'][0])],'narration':[],'captions':[],'chapters':[],
      'continuity_notes':old['continuity_notes'][:5], 'telegram_provenance':old['telegram_provenance'],
      'timing_method':'Actual recording durations and Whisper sentence/clause estimates, matched to observed UI events. Voice speed and pitch unchanged.'}
cursor=30.0


def frame(v):return round(v*30)/30


def shot(source,a,b,duration,title,scope='Local simulation',**extra):
    speed=(b-a)/duration if source not in ('closing','telegram_original') else 1
    if speed>1.05:scope+=f' · {speed:.2f}×'
    return dict(source=source,**({'in':a,'out':b} if source not in ('closing','telegram_original') else {}),duration=frame(duration),title=title,scope=scope,**extra)


def add(number,length,recipes):
    global cursor
    idx=number-3
    ref=copy.deepcopy(old['narration'][idx])
    start=cursor; length=frame(length)
    ref.update(start=start,end=start+length,audio_offset=.35)
    actual=files.get(number)
    if actual and not actual.get('duplicate_of'):
        ref['file']=actual['file']
        # Audio completeness is checked against the approved narration, never inferred solely from filename.
        assert actual['best_script']['id']==ref['id'],(number,actual['best_script'],ref['id'])
        ref['audio_out']=actual['duration']
        assert .35+actual['duration']<=length
        for s in actual['sentences']:
            text=s['text'].replace('Stewart','Steward').replace('telegram','Telegram').replace('A Scent order','A sent order').replace('Agent Core','AgentCore').replace('Missing Facts','Missing facts').replace('specialist in evidence tools','specialist and evidence tools')
            plan['captions'].append({'start':start+.35+s['start'],'end':min(start+length-.1,start+.35+s['end']),'text':text})
    else:
        ref['file']=None
        ref['status']='Corrected recording requested: supplied file duplicates resident-check'
    local=0
    for s in recipes:
        s['narration_id']=ref['id']; local+=s['duration']
        plan['shots'].append(s)
    # Rounding is absorbed into final frame of each section.
    plan['shots'][-1]['duration']=frame(plan['shots'][-1]['duration']+length-local)
    assert plan['shots'][-1]['duration']>0
    plan['narration'].append(ref)
    cursor=frame(cursor+length)


add(3,22.8,[
 shot('telegram_original',0,0,10.5,'It starts in the community chat','Original Telegram message · recorded',file='artifacts/video/coverage-session/assembly-v2/assets/telegram-original.jpg'),
 shot('hosted_meeting',3,9,5.83,'A prepared discussion is ready for management','Recorded hosted demo · meeting preparation',file='artifacts/video/manual-production/clips/01-hosted-meeting-preparation-1080p.mp4'),
 shot('quotes',0,6.5,6.47,'Or brings a repair decision to management','Workflow preview · separate repair example')])
add(4,15.2,[
 shot('history',0,5,4.8,'01 / A community decision','Local simulation · main parking example'),
 shot('history',7,8.5,1.3,'Prepare the agenda and recover the context'),
 shot('history',13.5,18.75,4.17,'Compare options and unresolved questions'),
 shot('history',19.5,24,4.93,'Open the earlier decision at its source','Local simulation · historical record')])
add(5,11.8,[
 shot('minutes',11,16.5,3.4,'After the meeting: submit the written minutes','Local simulation · after the meeting'),
 shot('minutes',35.5,39.2,2.1,'Prepare decisions for review','Local simulation · submitted minutes'),
 shot('minutes',50,53,1.1,'The proposed decisions await confirmation','Local simulation · after processing'),
 shot('confirm',6,9,5.2,'A proposed rule still needs authorized confirmation','Local simulation · decisions not yet confirmed')])
add(6,10.2,[
 shot('confirm',9.5,11.8,1.5,'Confirm the decision and its action','Local simulation · manager'),
 shot('confirm',14.8,17,1.2,'Confirm the decision and its action','Local simulation · manager'),
 shot('confirm',23.5,27,1.8,'The manager authorizes what was agreed','Local simulation · manager'),
 shot('confirm',33.5,39,5.7,'An owner. A deadline. A tracked commitment.')])
add(7,11,[
 shot('outcome',6,14,4.13,'Record completion of the agreed action','Local simulation · completion report'),
 shot('outcome',27,33,3.1,'Verify the outcome before closing the case','Local simulation · separate verification'),
 shot('outcome',33,36,3.77,'The verified outcome is saved to community memory','Local simulation · verified result')])
add(8,17.3,[
 shot('quotes',0,3,2.2,'02 / A repair, verified','Local simulation · repair example'),
 shot('quotes',21.7,26.9,3.47,'The lower price covers guide-shoe replacement','Local simulation · original $540 quote'),
 shot('quotes',11.3,16,4.1,'The $705 proposal also covers rail alignment','Local simulation · original $705 quote'),
 shot('quotes',16,21,7.53,'Compare scope, sources, and the recommendation','Local simulation · source review')])
add(9,11.7,[
 shot('approval',0,3,1.5,'Approve the proposal with a reason','Local simulation · manager'),
 shot('approval',7.8,9.4,2.0,'Approve the proposal with a reason','Local simulation · manager'),
 shot('approval',9.4,11.5,1.5,'Commit the approved service order','Local simulation · dry-run dispatch'),
 shot('approval',18.8,20,3.23,'An approved order awaits an appointment','Local simulation · dry-run dispatch'),
 shot('approval',20,23.5,3.47,'A sent order still needs an agreed visit','Local simulation · awaiting vendor')])
add(10,14.5,[
 shot('appointment',17,25.5,8.77,'Supply the vendor’s dated appointment reply','Local simulation · simulated vendor'),
 shot('appointment',31,35.5,1.3,'The workflow processes the reply','Local simulation · runtime step'),
 shot('appointment',53.5,59.5,4.43,'The appointment is now confirmed','Local simulation · same scope and access')])
add(11,12.5,[
 shot('completion',3,9,3,'Move to the completion stage','Demo time advanced · 48 hours'),
 shot('completion',9,14,3.27,'Completion still needs a result check','Local simulation · completion report'),
 shot('resident',6,11,3.6,'The resident gets one small response card','Local simulation · resident'),
 shot('resident',11.7,16,2.63,'A resident confirms that the work helped','Local simulation · resident verification')])
add(12,24.1,[
 shot('maintenance_memory',8,12,1.3,'03 / Experience changes the next step','Local simulation · verified repair history'),
 shot('maintenance_memory',14,18,3.07,'The verified result remains attached to the work','Local simulation · saved outcome'),
 shot('clarification_warranty',0,5,5.13,'A new report: ask for the missing location','Local simulation · separate repeat-fault example'),
 shot('clarification_warranty',5,12.5,3.3,'A small reply adds the missing location','Local simulation · resident clarification'),
 shot('clarification_warranty',29,33,2.17,'The new report connects to a verified repair','Local simulation · separate repeat-fault example'),
 shot('clarification_warranty',33,40.5,9.13,'Check the previous repair before another paid job','Local simulation · warranty review requested')])
add(13,15.8,[
 shot('recorded_sources',0,3,3.5,'04 / Strands on AgentCore','Recorded verification · synthetic input'),
 shot('recorded_sources',3.5,11,5.7,'A procurement specialist and evidence tools','Recorded verification · Amazon Nova Pro'),
 shot('recorded_sources',.2,2.7,2.6,'Missing facts lead to human review','Recorded verification · synthetic input'),
 shot('recorded_sources',11,20,4,'Preserve the cases and delivery records','Recorded verification · restart check')])
add(14,19.6,[
 shot('recorded_sources',24,34,4.3,'A real Telegram group delivery','Recorded verification · separate hosted test'),
 shot('recorded_sources',44.5,50,4.6,'Real mail between controlled addresses','Recorded verification · separate SES test'),
 shot('recorded_sources',60.6,65.1,5.97,'One logical order through restart and scheduling','Recorded verification · controlled SES test'),
 shot('recorded_sources',34,41,4.73,'Channel tests were recorded separately','Recorded verification · separate controlled test')])
add(15,10,[shot('closing',0,0,10,'A community that remembers','Approved brand card')])

boundaries=[(0,30,'The conversation'),(30,plan['narration'][1]['start'],'From the Telegram conversation'),
 (plan['narration'][1]['start'],plan['narration'][5]['start'],'A community decision'),
 (plan['narration'][5]['start'],plan['narration'][9]['start'],'A repair, verified'),
 (plan['narration'][9]['start'],plan['narration'][10]['start'],'Experience changes the next step'),
 (plan['narration'][10]['start'],plan['narration'][12]['start'],'Recorded technical evidence'),
 (plan['narration'][12]['start'],cursor,'A community that remembers')]
plan['chapters']=[dict(start=a,end=b,title=t) for a,b,t in boundaries]
plan['duration_seconds']=cursor
intro_captions=[{'start':20.8+s['start'],'end':min(20.8+s['end'],28.558),'text':s['text']} for s in files[1]['sentences']]
end_start=plan['narration'][-1]['start']+.35
plan['captions']=intro_captions+[c for c in plan['captions'] if c['start']<end_start]+[
 {'start':end_start+.06,'end':end_start+6.3,'text':'Steward helps communities remember what was reported, what was agreed, and what still needs to happen.'},
 {'start':end_start+6.36,'end':end_start+8.26,'text':'A community that remembers.'}]
OUT.mkdir(parents=True,exist_ok=True)
(OUT/'sync-plan.json').write_text(json.dumps(plan,indent=2,ensure_ascii=False)+'\n',encoding='utf8')
print(f'Created {cursor:.3f}s narration-led cut; {len(plan["shots"])} shots; {sum(bool(n.get("file")) for n in plan["narration"])} recordings mapped.')
