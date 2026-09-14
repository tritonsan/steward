"""Render a versioned editorial cut from preserved, recorded source footage."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse
import hashlib
import html
import json
import re
import subprocess
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'artifacts/video/coverage-session/assembly-v1'
SELECTS = OUT.parent / 'selects'
INTRO = ROOT / 'artifacts/video/telegram-animated-v2/steward-opening-1080p.mp4'
ENDCARD = ROOT / 'artifacts/video/telegram-animated-v2/previews/frame-29.0s.png'
FONT = 'C:/Windows/Fonts/segoeui.ttf'
BOLD = 'C:/Windows/Fonts/seguisb.ttf'


def run(args):
    return subprocess.run(args, capture_output=True, text=True, check=True)


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


def stamp(seconds, comma=False):
    ms = round(seconds * 1000)
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f'{h:02}:{m:02}:{s:02}{"," if comma else "."}{ms:03}'


def load():
    d = json.loads((OUT / 'timeline.json').read_text(encoding='utf8'))
    t = 0
    for index, shot in enumerate(d['shots']):
        shot['index'] = index
        shot['start'] = t
        shot['frames'] = round(shot['duration'] * 30)
        assert abs(shot['frames'] / 30 - shot['duration']) < 0.0001
        t += shot['duration']
        shot['end'] = t
    assert t == 285, t
    return d


def source(shot):
    if shot['source'] == 'opening':
        return INTRO
    if shot['source'] == 'closing':
        return ENDCARD
    return SELECTS / f"steward-{shot['source']}-action-result-1080p.mp4"


def heading(shot):
    im = Image.new('RGBA', (1920, 1080))
    draw = ImageDraw.Draw(im)
    title = ImageFont.truetype(BOLD, 28)
    scope = ImageFont.truetype(FONT, 21)
    draw.text((64, 19), shot['title'], font=title, fill='#f4f6ef')
    w = draw.textlength(shot['scope'], font=scope)
    assert draw.textlength(shot['title'], font=title) + w < 1720
    draw.text((1856-w, 24), shot['scope'], font=scope, fill='#c8d8b1')
    draw.rectangle((64, 65, 1856, 67), fill='#355849')
    p = OUT / 'graphics' / f"{shot['index']:02}-header.png"
    im.save(p)
    return p


def render_shot(shot):
    p = OUT / 'parts' / f"{shot['index']:02}-{shot['source']}.mp4"
    if shot['source'] == 'closing':
        inputs = ['-loop', '1', '-framerate', '30', '-i', str(ENDCARD)]
        vf = 'format=yuv420p,setsar=1,fade=t=in:st=0:d=0.5:color=white'
        graph = ['-vf', vf]
    else:
        inputs = ['-ss', str(shot.get('in', 0)), '-i', str(source(shot))]
        source_duration = shot.get('out', shot['duration']) - shot.get('in', 0)
        assert source_duration > 0
        speed = source_duration / shot['duration']
        base = f'trim=duration={source_duration},setpts=(PTS-STARTPTS)/{speed},fps=30,setsar=1'
        if shot['source'] == 'opening':
            graph = ['-vf', base]
        else:
            if 'crop' in shot:
                x, y, w, h = shot['crop']
                assert x >= 0 and y >= 0 and x+w <= 1920 and y+h <= 1080
                base += f',crop={w}:{h}:{x}:{y}'
            base += ',scale=1792:1008:flags=lanczos,pad=1920:1080:64:72:color=0x092c3a'
            inputs += ['-loop', '1', '-framerate', '30', '-i', str(heading(shot))]
            graph = ['-filter_complex', f'[0:v]{base}[base];[base][1:v]overlay=0:0:shortest=1[v]', '-map', '[v]']
    run(['ffmpeg', '-v', 'error', '-y', '-filter_complex_threads', '1', *inputs, *graph,
         '-frames:v', str(shot['frames']), '-an', '-c:v', 'libx264', '-threads', '4',
         '-preset', 'fast', '-crf', '18', '-pix_fmt', 'yuv420p', '-video_track_timescale', '15360',
         '-movflags', '+faststart', str(p)])
    print(f"Rendered {shot['index']:02} {shot['source']} {shot['duration']}s", flush=True)
    return p


def documents(d):
    cues = ['WEBVTT', '']
    srt = []
    md = ['# Steward — ilk kurguya göre seslendirme', '',
          'Hedef: 4:45. Açılışın mevcut sesi korunur. Diğer metinleri ayrı dosyalar halinde, doğal hızda kaydedin. Başına ve sonuna 1 saniye sessizlik bırakın. Kayıt hızına göre görüntü kesimleri ayarlanabilir.', '',
          'Altyazı dosyası planlanan anlatım metnidir; henüz kaydedilmemiş konuşmanın transkripti değildir.', '']
    for i, c in enumerate(d['narration'], 1):
        name = f"{i:02}-{c['id']}.txt"
        (OUT/'voiceover'/name).write_text(c['text']+'\n', encoding='utf8')
        words = len(c['text'].split())
        md += [f"## {name} · {stamp(c['start'])[:8]}–{stamp(c['end'])[:8]}", '',
               f"{words} kelime · {c['end']-c['start']:g} saniye", '', c['text'], '']
        # Short sentence cues keep the optional narration guide readable.
        sentences = re.split(r'(?<=[.!?])\s+', c['text'])
        total = sum(len(s.split()) for s in sentences)
        at = c['start']
        for sent in sentences:
            end = at + (c['end']-c['start']) * len(sent.split()) / total
            cues += [f'{stamp(at)} --> {stamp(end)}', sent, '']
            srt += [str(sum(1 for line in srt if '-->' in line)+1), f'{stamp(at, True)} --> {stamp(end, True)}', sent, '']
            at = end
    (OUT/'voiceover/VOICEOVER_EN.md').write_text('\n'.join(md), encoding='utf8')
    (OUT/'narration-guide.vtt').write_text('\n'.join(cues), encoding='utf8')
    (OUT/'narration-guide.srt').write_text('\n'.join(srt), encoding='utf8')
    meta = [';FFMETADATA1', 'title=Steward - editorial cut v1', 'comment=Local simulated product workflows and separate recorded verification. Narration work in progress.']
    for c in d['chapters']:
        meta += ['[CHAPTER]', 'TIMEBASE=1/1000', f"START={round(c['start']*1000)}", f"END={round(c['end']*1000)}", f"title={c['title']}"]
    (OUT/'chapters.ffmeta').write_text('\n'.join(meta)+'\n', encoding='utf8')


def assemble(d, overview_audio=False):
    parts = [OUT/'parts'/f"{s['index']:02}-{s['source']}.mp4" for s in d['shots']]
    listing = OUT/'parts/concat.txt'
    listing.write_text(''.join(f"file '{p.name}'\n" for p in parts), encoding='utf8')
    silent = OUT/'steward-picture-cut-1080p.mp4'
    run(['ffmpeg','-v','error','-y','-f','concat','-safe','0','-i',str(listing),'-map','0:v','-c','copy','-movflags','+faststart',str(silent)])
    audio_inputs = ['-i',str(INTRO)]
    audio_filter = '[0:a]atrim=end=30,asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,apad=whole_dur=285[a]'
    if overview_audio:
        audio_inputs += ['-i',str(ROOT/'artifacts/video/Steward 2.mp3')]
        audio_filter = ('[0:a]atrim=end=30,asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,apad=whole_dur=285[a0];'
                        '[1:a]aresample=48000,aformat=channel_layouts=stereo,loudnorm=I=-19:TP=-2:LRA=7,adelay=31000:all=1,apad=whole_dur=285[a1];'
                        '[a0][a1]amix=inputs=2:normalize=0:duration=longest,atrim=end=285[a]')
    wav = OUT/'audio-guide.wav'
    run(['ffmpeg','-v','error','-y',*audio_inputs,'-filter_complex',audio_filter,'-map','[a]','-t','285','-c:a','pcm_s16le',str(wav)])
    movie = OUT/'steward-first-edit-1080p.mp4'
    run(['ffmpeg','-v','error','-y','-i',str(silent),'-i',str(wav),'-f','ffmetadata','-i',str(OUT/'chapters.ffmeta'),
         '-map','0:v','-map','1:a','-map_metadata','2','-map_chapters','2','-c:v','copy','-c:a','aac','-b:a','192k','-t','285','-movflags','+faststart',str(movie)])
    d['audio_status'] = 'Opening plus user-confirmed overview recording; later narration pending' if overview_audio else 'Original approved opening audio only; post-opening narration pending'
    d['sources'] = {str(source(s).relative_to(ROOT)): sha(source(s)) for s in d['shots']}
    d['output_sha256'] = sha(movie)
    d['transform_policy'] = 'Real recorded frames; straight cuts and explicit overview speed-up. Editorial title band outside the scaled image. Any crop recorded per shot. Original raw/select files unchanged. Closing uses approved logo card.'
    (OUT/'edit-manifest.json').write_text(json.dumps(d, indent=2, ensure_ascii=False)+'\n', encoding='utf8')
    print(f'ASSEMBLED {movie}', flush=True)


def qa(d):
    movie = OUT/'steward-first-edit-1080p.mp4'
    probe = json.loads(run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(movie)]).stdout)
    v = next(s for s in probe['streams'] if s['codec_type']=='video')
    assert (v['width'],v['height'],v['r_frame_rate'],int(v['nb_frames'])) == (1920,1080,'30/1',8550)
    assert abs(float(probe['format']['duration'])-285) < .04
    decoded = run(['ffmpeg','-v','info','-i',str(movie),'-vf','blackdetect=d=0.08:pix_th=0.1','-f','null','-'])
    (OUT/'qa/decode.log').write_text(decoded.stderr,encoding='utf8')
    assert 'black_start:' not in decoded.stderr
    frames = []
    for s in d['shots']:
        for t in [s['start']+.2,(s['start']+s['end'])/2,s['end']-.2]:
            p = OUT/'qa'/f'frame-{t:07.2f}.jpg'
            run(['ffmpeg','-v','error','-y','-ss',str(t),'-i',str(movie),'-frames:v','1','-q:v','2',str(p)])
            frames.append((t,p))
    font = ImageFont.truetype(FONT,20)
    for n in range(0,len(frames),8):
        page=Image.new('RGB',(1280,1568),'#092c3a'); dr=ImageDraw.Draw(page)
        for j,(t,p) in enumerate(frames[n:n+8]):
            x,y=(j%2)*640,(j//2)*392
            with Image.open(p) as im: page.paste(im.resize((640,360),Image.Resampling.LANCZOS),(x,y))
            dr.text((x+10,y+363),stamp(t),font=font,fill='white')
        page.save(OUT/'qa'/f'contact-{n//8+1:02}.jpg',quality=92)
    (OUT/'qa/technical.json').write_text(json.dumps({'width':1920,'height':1080,'fps':30,'frames':8550,'duration':285,'full_decode':'passed','black_intervals':[],'visual_review':'pending','sha256':sha(movie)},indent=2),encoding='utf8')
    print('Technical QA complete; encoded contact sheets ready',flush=True)


if __name__ == '__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--render',action='store_true');ap.add_argument('--assemble',action='store_true');ap.add_argument('--qa',action='store_true');ap.add_argument('--overview-audio',action='store_true');ap.add_argument('--only',type=int)
    args=ap.parse_args()
    for folder in ['parts','graphics','qa','voiceover']:(OUT/folder).mkdir(parents=True,exist_ok=True)
    d=load();documents(d)
    if args.render:
        shots=[s for s in d['shots'] if args.only is None or s['index']==args.only]
        with ThreadPoolExecutor(max_workers=3) as pool:
            for f in as_completed([pool.submit(render_shot,s) for s in shots]):f.result()
    if args.assemble:assemble(d,args.overview_audio)
    if args.qa:qa(d)
