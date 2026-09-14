"""Render a narration-led edition without modifying preserved earlier edits."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import wave
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import render_submission_cut as picture

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'artifacts/video/coverage-session/assembly-v3'
RATE = 48000


def run(args):
    return subprocess.run(args, capture_output=True, text=True, check=True)


def read_plan():
    d = json.loads((OUT / 'sync-plan.json').read_text(encoding='utf8'))
    at = 0
    for i, s in enumerate(d['shots']):
        s.update(index=i, start=at, frames=round(s['duration'] * 30))
        s['duration'] = s['frames'] / 30
        at += s['duration']
        s['end'] = at
    d['duration_seconds'] = round(at, 6)
    assert at <= 300, f'Cut exceeds five minutes: {at}'
    for n in d['narration']:
        assert n['start'] >= 30
        assert n['end'] <= at
    return d


def render(d):
    picture.OUT = OUT
    prior_dir = OUT if (OUT/'previous-render-manifest.json').exists() else OUT.parent / 'assembly-v2'
    prior_file = prior_dir/('previous-render-manifest.json' if prior_dir==OUT else 'edit-manifest.json')
    prior = json.loads(prior_file.read_text(encoding='utf8'))
    # Snapshot before any reindexing: a shifted destination must never replace
    # an older source part that another later shot still needs to reuse.
    prior_parts = prior_dir/'parts'
    if prior_dir == OUT:
        snapshot=OUT/'reuse-snapshot'
        snapshot.mkdir(exist_ok=True)
        for old in prior['shots']:
            filename=f"{old['index']:02}-{old['source']}.mp4"
            shutil.copy2(prior_parts/filename,snapshot/filename)
        prior_parts=snapshot
    def signature(s):
        return {k:v for k,v in s.items() if k not in ('index','start','end','frames','narration_id','sync_note')}
    todo = []
    for s in d['shots']:
        dest = OUT/'parts'/f"{s['index']:02}-{s['source']}.mp4"
        old = next((v for v in prior['shots'] if signature(v) == signature(s)), None)
        if old:
            previous_file = prior_parts/f"{old['index']:02}-{old['source']}.mp4"
            if previous_file != dest:
                shutil.copy2(previous_file, dest)
        else:
            todo.append(s)
    with ThreadPoolExecutor(max_workers=3) as pool:
        for future in as_completed([pool.submit(picture.render_shot, s) for s in todo]):
            future.result()
    for s in d['shots']:
        p = OUT/'parts'/f"{s['index']:02}-{s['source']}.mp4"
        probe = json.loads(run(['ffprobe','-v','error','-select_streams','v:0','-show_entries','stream=nb_frames','-of','json',str(p)]).stdout)
        actual = int(probe['streams'][0]['nb_frames'])
        assert actual == s['frames'], (s['index'],actual,s['frames'])
    listing = OUT/'parts/concat.txt'
    listing.write_text(''.join(f"file '{s['index']:02}-{s['source']}.mp4'\n" for s in d['shots']),encoding='utf8')
    run(['ffmpeg','-v','error','-y','-f','concat','-safe','0','-i',str(listing),'-map','0:v','-c','copy','-movflags','+faststart',str(OUT/'steward-picture-cut-1080p.mp4')])


def loudnorm(src, target, start=0, end=None):
    trim = f'atrim=start={start}' + (f':end={end}' if end is not None else '')
    first = f'{trim},asetpts=PTS-STARTPTS,loudnorm=I=-18:TP=-2:LRA=7:print_format=json'
    p = run(['ffmpeg','-v','info','-i',str(src),'-af',first,'-f','null','-'])
    stats = json.loads(p.stderr[p.stderr.rfind('{'):p.stderr.rfind('}')+1])
    norm = ('loudnorm=I=-18:TP=-2:LRA=7:linear=true:'
            f"measured_I={stats['input_i']}:measured_TP={stats['input_tp']}:"
            f"measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}:offset={stats['target_offset']}")
    run(['ffmpeg','-v','error','-y','-i',str(src),'-af',f'{trim},asetpts=PTS-STARTPTS,{norm},aresample=48000',
         '-ar','48000','-ac','2','-c:a','pcm_s16le',str(target)])
    return stats


def load_pcm(path):
    with wave.open(str(path),'rb') as w:
        assert w.getframerate() == RATE and w.getnchannels() == 2 and w.getsampwidth() == 2
        return np.frombuffer(w.readframes(w.getnframes()),dtype='<i2').reshape(-1,2).astype(np.float32)/32768


def save_pcm(path, x):
    with wave.open(str(path),'wb') as w:
        w.setparams((2,2,RATE,0,'NONE','not compressed'))
        w.writeframes((np.clip(x,-1,1)*32767).astype('<i2').tobytes())


def mix(d):
    total = round(d['duration_seconds']*RATE)
    voice = np.zeros((total,2),dtype=np.float32)
    intro = OUT/'audio/approved-opening.wav'
    run(['ffmpeg','-v','error','-y','-i',str(picture.INTRO),'-t','30','-vn','-ar',str(RATE),'-ac','2','-c:a','pcm_s16le',str(intro)])
    data = load_pcm(intro)
    voice[:len(data)] += data
    records = []
    intervals = []
    for n in d['narration']:
        if not n.get('file'):
            records.append({'id':n['id'],'status':'awaiting corrected user recording'})
            continue
        src = ROOT/n['file']
        norm = OUT/'audio'/f"{n['id']}.wav"
        stats = loudnorm(src,norm)
        samples = load_pcm(norm)
        chunks = n.get('audio_segments') or [{'in':n.get('audio_in',0),'out':n.get('audio_out',len(samples)/RATE),'at':n['start']+n.get('audio_offset',0)}]
        for c in chunks:
            a,b = round(c['in']*RATE),round(c['out']*RATE)
            chunk = samples[a:b].copy()
            # Tiny edge ramps only; never change voice tempo or pitch.
            ramp = min(240,len(chunk)//2)
            chunk[:ramp] *= np.linspace(0,1,ramp,dtype=np.float32)[:,None]
            chunk[-ramp:] *= np.linspace(1,0,ramp,dtype=np.float32)[:,None]
            first = round(c['at']*RATE)
            assert first+len(chunk) <= total
            assert all(first+len(chunk)<=a or first>=b for a,b in intervals), 'Narration clips overlap'
            intervals.append((first,first+len(chunk)))
            voice[first:first+len(chunk)] += chunk
        records.append({'id':n['id'],'file':str(src.relative_to(ROOT)),'sha256':picture.sha(src),'normalization_input':stats,'segments':chunks})
    save_pcm(OUT/'audio/narration-only.wav',voice)
    music_src = ROOT/d['music']['file']
    music_convert = OUT/'audio/music-source.wav'
    run(['ffmpeg','-v','error','-y','-i',str(music_src),'-t',str(d['duration_seconds']),'-ar',str(RATE),'-ac','2','-c:a','pcm_s16le',str(music_convert)])
    music = load_pcm(music_convert)[:total]
    assert len(music) == total
    rms = float(np.sqrt(np.mean(music**2)))
    target_rms = 10**(-38/20)
    gain = target_rms / max(rms,1e-12)
    music *= gain
    # Follow speech in 20ms blocks. Attack 40ms, release 650ms, maximum 5dB duck.
    block = 960
    env = 0.0
    levels=[]
    for a in range(0,total,block):
        b=min(a+block,total)
        energy=float(np.sqrt(np.mean(voice[a:b]**2)))
        want=min(1,max(0,(energy-.012)/.055))
        rate=1-math.exp(-.02/(.04 if want>env else .65))
        env += rate*(want-env)
        levels.append(10**(-5*env/20))
    gain_curve=np.interp(np.arange(total,dtype=np.float64),np.arange(len(levels))*block,levels).astype(np.float32)
    music *= gain_curve[:,None]
    fade=min(3*RATE,total//2)
    music[:fade] *= np.linspace(0,1,fade,dtype=np.float32)[:,None]
    music[-fade:] *= np.linspace(1,0,fade,dtype=np.float32)[:,None]
    mixed=voice+music
    peak=float(np.max(np.abs(mixed)))
    limiter_gain=min(1,10**(-1.5/20)/max(peak,1e-9))
    mixed *= limiter_gain
    save_pcm(OUT/'audio/music-under-narration.wav',music)
    save_pcm(OUT/'steward-final-mix.wav',mixed)
    report={'sample_rate':RATE,'duration':total/RATE,'peak_before_final_gain_dbfs':20*math.log10(max(peak,1e-12)),
            'final_gain_db':20*math.log10(limiter_gain),'music_base_rms_dbfs':-38,'additional_speech_duck_db':5,
            'music_source':str(music_src.relative_to(ROOT)),'music_source_sha256':picture.sha(music_src),
            'music_gain_db':20*math.log10(gain),'voice_tempo_pitch':'unchanged','narration':records}
    (OUT/'qa/audio-mix.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    run(['ffmpeg','-v','error','-y','-i',str(OUT/'steward-picture-cut-1080p.mp4'),'-i',str(OUT/'steward-final-mix.wav'),
         '-f','ffmetadata','-i',str(OUT/'chapters.ffmeta'),'-map','0:v','-map','1:a','-map_metadata','2','-map_chapters','2',
         '-c:v','copy','-c:a','aac','-b:a','192k','-t',str(d['duration_seconds']),'-movflags','+faststart',str(OUT/'steward-narrated-1080p.mp4')])


def documents(d):
    meta=[';FFMETADATA1','title=Steward - narrated edit','comment=Original recordings with synchronized user narration and original synthesized music.']
    for c in d['chapters']:
        meta += ['[CHAPTER]','TIMEBASE=1/1000',f"START={round(c['start']*1000)}",f"END={round(c['end']*1000)}",f"title={c['title']}"]
    (OUT/'chapters.ffmeta').write_text('\n'.join(meta)+'\n',encoding='utf8')
    cues=['WEBVTT',''];srt=[]
    for i,c in enumerate(d.get('captions',[]),1):
        cues += [f"{picture.stamp(c['start'])} --> {picture.stamp(c['end'])}",c['text'],'']
        srt += [str(i),f"{picture.stamp(c['start'],True)} --> {picture.stamp(c['end'],True)}",c['text'],'']
    (OUT/'narration.vtt').write_text('\n'.join(cues),encoding='utf8')
    (OUT/'narration.srt').write_text('\n'.join(srt),encoding='utf8')
    d['sources']={str(picture.source(s).relative_to(ROOT)):picture.sha(picture.source(s)) for s in d['shots']}
    (OUT/'edit-manifest.json').write_text(json.dumps(d,indent=2,ensure_ascii=False)+'\n',encoding='utf8')


def qa(d):
    movie=OUT/'steward-narrated-1080p.mp4'
    probe=json.loads(run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(movie)]).stdout)
    v=next(s for s in probe['streams'] if s['codec_type']=='video')
    expected=sum(s['frames'] for s in d['shots'])
    assert (v['width'],v['height'],v['r_frame_rate'],int(v['nb_frames']))==(1920,1080,'30/1',expected)
    assert abs(float(probe['format']['duration'])-d['duration_seconds'])<.06
    decode=run(['ffmpeg','-v','info','-i',str(movie),'-vf','blackdetect=d=0.08:pix_th=0.1','-af','ebur128=peak=true','-f','null','-'])
    (OUT/'qa/decode-loudness.log').write_text(decode.stderr,encoding='utf8')
    assert 'black_start:' not in decode.stderr
    frames=[]
    # Check exactly the intended sentence/display transitions, plus each shot midpoint.
    times=sorted(set(round(t,3) for t in [*[(s['start']+s['end'])/2 for s in d['shots']],*[c['start']+.12 for c in d.get('captions',[])]]))
    for t in times:
        p=OUT/'qa'/f'frame-{t:07.2f}.jpg'
        run(['ffmpeg','-v','error','-y','-ss',str(t),'-i',str(movie),'-frames:v','1','-q:v','2',str(p)])
        frames.append((t,p))
    font=ImageFont.truetype(picture.FONT,20)
    for n in range(0,len(frames),8):
        page=Image.new('RGB',(1280,1568),'#092c3a');dr=ImageDraw.Draw(page)
        for j,(t,p) in enumerate(frames[n:n+8]):
            x,y=(j%2)*640,(j//2)*392
            with Image.open(p) as im:page.paste(im.resize((640,360),Image.Resampling.LANCZOS),(x,y))
            dr.text((x+10,y+363),picture.stamp(t),font=font,fill='white')
        page.save(OUT/'qa'/f'contact-{n//8+1:02}.jpg',quality=92)
    (OUT/'qa/technical.json').write_text(json.dumps({'width':1920,'height':1080,'fps':30,'frames':expected,'duration':d['duration_seconds'],'full_decode':'passed','black_intervals':[],'visual_review':'pending','sha256':picture.sha(movie)},indent=2),encoding='utf8')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--render',action='store_true');parser.add_argument('--mix',action='store_true');parser.add_argument('--qa',action='store_true')
    args=parser.parse_args()
    for f in ['parts','graphics','qa','audio']:(OUT/f).mkdir(parents=True,exist_ok=True)
    picture.OUT=OUT
    d=read_plan();documents(d)
    if args.render:render(d)
    if args.mix:mix(d)
    if args.qa:qa(d)
