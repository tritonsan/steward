"""Apply media-reviewed observations for continuous coverage takes 8-11.

Only writes the media ledger and QA records. Does not control app/OBS or alter raws.
Times below are observed source frame timestamps, not asserted exact click times.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'artifacts/video/coverage-session'
MANIFEST = ROOT / 'coverage-manifest.json'
d = json.loads(MANIFEST.read_text(encoding='utf-8-sig'))
seq = {x['id']: x for x in d['sequences']}

def source(stem):
    return f'raw/{stem}.mp4'

def mark(t, label, result):
    return {'observed_at_seconds': t, 'ui_label': label, 'observed_result': result}

def window(a, b, content):
    return {'start_seconds': a, 'end_seconds': b, 'content': content}

def review(stem, basis, edit, cosmetic=None):
    p = ROOT / 'qa' / stem / 'technical-qa.json'
    r = json.loads(p.read_text(encoding='utf-8-sig'))
    r['visual_review'] = {
        'status': 'clean_with_editable_holds', 'full_window_visible': True,
        'content_changes_observed': True, 'black_padding_observed': False,
        'authentication_or_desktop_observed': False, 'debug_infobar_visible': False,
        'review_basis': basis, 'required_edit': edit,
    }
    if cosmetic:
        r['visual_review']['cosmetic_notes'] = cosmetic
    p.write_text(json.dumps(r, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    existing = d['session']['raw_recordings']
    existing[:] = [x for x in existing if x['source'] != r['source']]
    existing.append(r)
    return r

r8 = review('2026-09-14 03-08-26', '5-second contact pages and full-size50s/118.767s appointment source frames.',
             'Keep case/vendor selections, input, intake receipt, worker tick and confirmed result; shorten focus holds.',
             'Browser spellcheck underlines on synthetic appointment reply.')
r9 = review('2026-09-14 03-11-36', '5-second contact sheet and full-size20s Awaiting Verification frame.',
             'Preserve completion entry, command and verification-required result. Role switch occurs in next raw.',
             'Browser spellcheck underlines in demo completion note.')
r10 = review('2026-09-14 03-12-53', '5-second contact sheet and full-size25s resident observation frame.',
              'Keep Refresh, personal card, fresh observation, Confirm and No open cases. Label resident role separately.',
              'Browser spellcheck underlines in demo verification note.')
r11 = review('2026-09-14 03-13-49', '5-second contact pages plus full-size30s memory and80s original message frames.',
              'Preserve search, record open, source-case navigation and source-message open; trim reading holds.')

def take(raw, end, before, after, result, marks, windows, notes, waits=None, start=0):
    return {'raw_file': raw, 'start_seconds': start, 'end_seconds': end,
            'action_marks': marks, 'state_before': before, 'state_after': after,
            'observed_result': result, 'wait_intervals': waits or [],
            'privacy_review': 'Clean full application capture; no authentication, desktop, debug infobar or credentials in reviewed frames.',
            'usable_coverage': windows, 'notes': notes}

seq['08_appointment_reply_confirmation'].update(status='recorded_reply_intake_and_confirmed_appointment', take=take(
    r8['source'], r8['duration_seconds'], 'Awaiting Appointment; no confirmed visit.',
    'Scheduled; confirmed19Sept2026at13:00 Istanbul with retained vendor evidence.',
    'Synthetic vendor reply enters actual intake; Run one tick yields a confirmed appointment visible in case.',
    [mark(0, 'Cases', 'Awaiting Appointment in list.'), mark(5, 'Simulation / Vendor response', 'Empty vendor-response form.'),
     mark(20, 'Case', 'Current A Block elevator case selected.'), mark(35, 'Vendor', 'Meridian Lift Services selected.'),
     mark(50, 'Reply text', 'Appointment2026-09-19T10:00:00+00:00 to11:00:00+00:00; same price/scope, access arrangements agreed.'),
     mark(60, 'Vendor reply entered through the real intake service', 'Input clears and durable intake acknowledgement is visible.'),
     mark(75, 'Run one tick', 'Ordinary worker control visible after scroll.'),
     mark(90, 'Scheduled', 'Case list now shows Scheduled.'), mark(100, 'Attend the confirmed repair visit', 'Current confirmed visit next step.'),
     mark(110, 'Proposal1 · Current confirmed appointment', '19Sept2026,13:00 visible.'),
     mark(115, 'Vendor evidence', 'Exact UTC appointment input retained under confirmed proposal.')],
    [window(0,38,'Cases to Simulation and current case/vendor selection.'), window(43,63,'Reply entry, Add vendor reply and receipt.'),
     window(68,98,'Worker tick, Cases Scheduled and case opening.'), window(100,r8['duration_seconds'],'Confirmed appointment and source evidence.')],
    'Local synthetic vendor response, not a live supplier message. Date displayed in Istanbul matches UTC source. A confirmed appointment is not repair completion.'))

t9 = take(r9['source'], r9['duration_seconds'], 'Scheduled, after offscreen48-hour demo-time advance.',
    'Awaiting Verification; reporting resident or manager must respond.',
    'Management records explicit demo completion evidence. It does not close the case.',
    [mark(0,'Attend the confirmed repair visit','Scheduled case with empty Completion evidence.'),
     mark(10,'Completion evidence','Demo vendor completion:guide shoes replaced and rails aligned.'),
     mark(20,'Completion recorded. Verification is now required.','Awaiting Verification and Did the work solve the problem?')],
    [window(3,24,'Completion evidence entry, Record completion and separate verification requirement.')],
    'Synthetic work completion. Manager verification controls visible but not used; actual resident confirmation is next take.')
t10 = take(r10['source'], r10['duration_seconds'], 'Resident view stale until Refresh; repair awaits verification.',
    'Resident confirms with fresh demo observation; No open cases.',
    'Resident personal response card provides the separate final verification.',
    [mark(0,'Resident space','Stale list still contains parking and elevator.'),
     mark(5,'Did the work solve the problem?','Refresh reveals one current elevator case and personal verification card.'),
     mark(25,'Your answer and what you observed','Demo verification:elevator operates smoothly; reported vibration is resolved.'),
     mark(30,'No open cases','Confirm accepted; personal card and open elevator case disappear.')],
    [window(0,8,'Resident Refresh reveals personal verification card.'), window(20,35,'Fresh observation, Confirm and No open cases.')],
    'Resident role is visible; director identifies actor as James. Do not splice as though management clicked its own verification control. Synthetic observation, not a physical repair inspection.')
seq['09_repair_completion_verification'].update(status='recorded_manager_completion_and_resident_verification', take=t10,
    additional_takes=[t9], take_order=[r9['source'],r10['source']])

t11 = take(r11['source'], r11['duration_seconds'], 'Both principal demo cases are closed.',
    'New maintenance memory record retrieved, linked closed case and original resident source inspected.',
    'Search shudders finds the new verified705USD outcome and preserves work, verification, vendor, appointment, quote versions and original report.',
    [mark(0,'Cases / A clear desk','No open cases.'), mark(5,'Community memory','Historical records before query.'),
     mark(20,'Search shudders','Single matching new demo elevator memory result.'),
     mark(30,'Verified Outcome','Guide-shoe/rail work and separate resident verification retained;705USD;meridian-lift;closed19Sept2026at14:00.'),
     mark(40,'Open case record','Closed originating case opens with Verified outcome saved to community memory.'),
     mark(50,'Vendor appointments / Vendor quotes','Current visit retained in completed history; original quote versions visible.'),
     mark(70,'Activity history and sources','Original Opened case from resident message event and evidence links.'),
     mark(80,'Resident message','DEMO RESIDENT INPUT: A Block elevator shudders/grinding near fourth floor; inspect guide shoes and rail alignment.')],
    [window(0,8,'Navigate from empty Cases to memory.'),window(15,45,'Search shudders, open verified outcome and linked case.'),
     window(45,75,'Retained appointments/quotes and expanded activity history.'),window(75,90,'Open actual original resident message source.')],
    '705USD is recorded demo cost, not payment/invoice evidence. Original report viewed after completion; do not imply fresh intake or live Telegram here. IDs shown are ordinary synthetic source/case IDs.',
    waits=[{'start_seconds':8,'end_seconds':17,'kind':'Search focus hold.'}])
s10=seq['10_memory_search_retrieval']
old=next(x for x in [s10['take']]+s10.get('additional_takes',[]) if x['raw_file']==source('2026-09-14 02-59-44'))
s10.update(status='parking_and_maintenance_verified_memory_and_source_retrieval_recorded', take=t11)
s10['additional_takes']=[x for x in s10.get('additional_takes',[]) if x.get('raw_file')!=old['raw_file']]+[old]
s10['take_order']=[old['raw_file'],r11['source']]
d['case_refs']['maintenance_primary']['case_id']='case-8aacd0957cfd4419bf558e5d83093073'

d['demo_time_advances']=[x for x in d['demo_time_advances'] if x.get('between_raw_files')!=[r8['source'],r9['source']]]
d['demo_time_advances'].append({'raw_file':None,'elapsed_seconds':None,'between_raw_files':[r8['source'],r9['source']],
    'reported_advance_hours':48,'clock_before':None,'clock_after':None,
    'source':'Recording director reports normal demo API advance offscreen; exact clock values not independently read.',
    'reason':'Move beyond the confirmed appointment before synthetic work completion.','label_required':'Demo time advanced48hours'})
d['worker_continuations']=[x for x in d['worker_continuations'] if x.get('raw_file')!=r8['source']]
d['worker_continuations'].append({'raw_file':r8['source'],'elapsed_seconds':None,'case_ref':'maintenance_primary',
    'continuation_kind':'Run one tick UI control; actual normal worker processing',
    'observation_bounds_seconds':[75,90],'observed_result':'Awaiting Appointment advances to Scheduled after ingested proposal.'})
d['unrecorded_or_unavailable_behaviors']=[
    'First resident meeting take contains a top debugbar; clean replacement preferred. Take10 resident verification is clean.',
    'Quorum transition not recorded in this session; first invitation already confirmed.',
    'Original parking report source not opened here. Original maintenance report is now covered in take11.',
    'Supplementary settings, exception branches and technical evidence remain pending this index update.'
]
r=d['session']['raw_recordings']
r.sort(key=lambda x:x['source'])
d['session'].update(indexed_raw_count=len(r),indexed_duration_seconds=round(sum(x['duration_seconds'] for x in r),6),
    indexed_bytes=sum(x['bytes'] for x in r),qa_summary={
    'full_decode_passed':sum(x['full_decode']=='passed' for x in r),
    'clean_takes':sum(x['visual_review'].get('debug_infobar_visible') is False for x in r),
    'takes_needing_infobar_correction':sum(x['visual_review'].get('debug_infobar_visible') is True for x in r),
    'scope':'Completed indexed files only; session open.'})
MANIFEST.write_text(json.dumps(d,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
print(json.dumps({k:d['session'][k] for k in ['indexed_raw_count','indexed_duration_seconds','indexed_bytes','qa_summary']},indent=2))
