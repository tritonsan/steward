"""Build the offline review page for the narrated Steward cut from its sync plan."""

from __future__ import annotations

import html
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'artifacts/video/coverage-session/assembly-v3'
TITLES = {
    'overview': 'From the community chat',
    'meeting-context': 'Prepare the community discussion',
    'minutes': 'Turn written minutes into proposals',
    'confirm-decision': 'Confirm the rule and the action',
    'community-outcome': 'Verify the agreed outcome',
    'repair-proposals': 'Compare the scope behind each price',
    'order-approval': 'Approve an order with a reason',
    'appointment': 'Agree on the visit',
    'resident-check': 'Ask the resident to verify the result',
    'memory-and-warranty': 'Let verified history change the next step',
    'agentcore-and-restart': 'Inspect the agent and restart evidence',
    'real-channel-checks': 'Inspect the real channel checks',
    'closing': 'A community that remembers',
}


def stamp(seconds: float, precise: bool = False) -> str:
    tenths = round(seconds * 10)
    minutes, remainder = divmod(tenths, 600)
    whole, fraction = divmod(remainder, 10)
    base = f'{minutes:02}:{whole:02}'
    return f'{base}.{fraction}' if precise and fraction else base


def main() -> None:
    data = json.loads((OUT / 'sync-plan.json').read_text(encoding='utf-8'))
    duration = data['duration_seconds']
    public = {key: data[key] for key in ('version', 'duration_seconds', 'chapters', 'captions', 'timing_method')}
    public['narration'] = [
        {**{key: cue[key] for key in ('id', 'start', 'end', 'text')}, 'title': TITLES[cue['id']]}
        for cue in data['narration']
    ]
    chapter_html = '\n'.join(
        f'<button class="chapter" type="button" data-seek="{chapter["start"]}" '
        f'aria-current="{"true" if i == 0 else "false"}">'
        f'<span class="chapter-time">{stamp(chapter["start"], True)}</span>'
        f'<span lang="en">{html.escape(chapter["title"])}</span></button>'
        for i, chapter in enumerate(data['chapters'])
    )
    transcript_html = '\n'.join(
        f'<article class="transcript-card" data-cue="{cue["id"]}">'
        f'<button type="button" class="transcript-jump" data-seek="{cue["start"]}">'
        f'<span class="transcript-time">{stamp(cue["start"], True)}–{stamp(cue["end"], True)}</span>'
        f'<span lang="en">{html.escape(TITLES[cue["id"]])}</span></button>'
        f'<p lang="en">{html.escape(cue["text"])}</p></article>'
        for cue in data['narration']
    )
    page = '''<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; media-src 'self' file: blob:; img-src 'self' file: data:; connect-src 'none'; base-uri 'none'; form-action 'none'">
<title>Steward · Seslendirilmiş demo</title>
<style>
:root{--navy:#092c3a;--ink:#153a48;--green:#607a43;--paper:#f5f6f1;--muted:#596b70;--line:#d9e0d5;--soft:#edf2e6;--white:#fff}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;color:var(--navy);background:var(--paper);font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}button,a,summary{touch-action:manipulation}button{font:inherit;cursor:pointer}a{color:inherit;text-underline-offset:4px}a:hover{color:var(--green)}button:focus-visible,a:focus-visible,summary:focus-visible{outline:3px solid #8fa96c;outline-offset:4px}button:hover{filter:brightness(.98)}header,main,footer{max-width:1640px;margin:auto}header{padding:28px 38px 24px}.brandbar{display:flex;justify-content:space-between;align-items:center;gap:20px;margin-bottom:22px}.brand{font-size:25px;letter-spacing:-1px;font-weight:720}.edition{font-size:12px;letter-spacing:.06em;color:var(--green);font-weight:700;text-transform:uppercase}.eyebrow{color:var(--green);font-weight:750;font-size:12px;letter-spacing:.13em;text-transform:uppercase}h1{font-size:clamp(28px,3.1vw,42px);letter-spacing:-.035em;line-height:1.15;margin:7px 0 12px}h2{font-size:18px;line-height:1.3;margin:0}p{margin:0}.intro{color:var(--muted);max-width:850px;font-size:15px}.header-bottom{display:flex;justify-content:space-between;gap:24px;align-items:flex-end;flex-wrap:wrap;margin-top:20px}.metadata{display:flex;gap:8px;flex-wrap:wrap}.chip{font-size:12px;border:1px solid var(--line);background:white;border-radius:6px;padding:5px 9px;white-space:nowrap}.chip:first-child{background:var(--soft);border-color:#cedabc;color:#435d2d}.downloads{display:flex;flex-wrap:wrap;gap:18px;font-size:13px;align-items:center}.download-primary{background:var(--navy);color:white;padding:9px 15px;border-radius:8px;text-decoration:none;font-weight:600}.download-primary:hover{color:white;background:#164653}main{display:grid;grid-template-columns:minmax(0,1.86fr) minmax(300px,.87fr);gap:24px;align-items:start;padding:0 38px 35px}.viewer{min-width:0}.video-shell{background:var(--navy);border:1px solid #1d4552;border-radius:13px;overflow:hidden;box-shadow:0 6px 22px #092c3a0a}video{display:block;width:100%;aspect-ratio:16/9;background:var(--navy)}video::cue{font:19px system-ui;background:#092c3ae8;color:white}.playback-info{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;margin:13px 1px 22px}.clock{font-size:13px;color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap}.playing-chapter{font-size:14px;font-weight:650;text-align:right}.section-heading{display:flex;justify-content:space-between;align-items:center;gap:15px;margin-bottom:11px}.section-heading h2{font-size:15px}.section-heading small{font-size:12px;color:var(--muted)}.chapters{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.chapter{display:flex;align-items:flex-start;gap:12px;border:1px solid var(--line);border-radius:9px;background:white;color:var(--navy);padding:11px 12px;text-align:left;font-size:13px;line-height:1.4}.chapter-time{font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums;padding-top:2px;min-width:39px}.chapter[aria-current="true"]{color:white;background:var(--navy);border-color:var(--navy)}.chapter[aria-current="true"] .chapter-time{color:#cbdbb5}.review-tip{margin-top:16px;font-size:12px;color:var(--muted);line-height:1.6}.narration-panel{border:1px solid var(--line);border-radius:13px;background:white;overflow:hidden}.panel-head{padding:20px 21px 17px;border-bottom:1px solid var(--line)}.panel-head h2{font-size:17px;margin-bottom:5px}.panel-head p{font-size:12px;color:var(--muted)}.voice-label{display:flex;align-items:center;gap:7px;margin-top:12px;color:var(--green);font-size:11px;font-weight:700;letter-spacing:.025em}.voice-dot{width:6px;height:6px;border-radius:50%;background:var(--green);flex-shrink:0}.current-block{padding:21px;min-height:280px}.cue-meta{display:flex;justify-content:space-between;gap:10px;font-size:11px;font-variant-numeric:tabular-nums;color:var(--muted);margin-bottom:10px}.cue-title{font-size:15px;line-height:1.4;margin:0 0 14px;font-weight:700}.cue-text{font-size:clamp(16px,1.12vw,19px);line-height:1.75;color:#50646b}.cue-text span{border-radius:2px;transition:background-color .12s,color .12s}.cue-text span.is-speaking{background:#e7eedb;color:#15352d;box-shadow:0 2px 0 #a3b985}.cue-text.opening{color:var(--ink);font-size:16px}.panel-foot{border-top:1px solid var(--line);padding:13px 21px;font-size:12px;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}.notice{padding:13px 17px;background:#fff5e5;color:#66491e;font-size:13px}.notice[hidden]{display:none}.wide{grid-column:1/-1}.details-card{background:white;border:1px solid var(--line);border-radius:12px;padding:18px 21px}summary{cursor:pointer;font-size:15px;font-weight:650}summary::marker{color:var(--green)}.detail-lead{color:var(--muted);font-size:13px;margin-top:13px}.transcript{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-top:17px}.transcript-card{border:1px solid var(--line);border-radius:9px;padding:16px 18px}.transcript-card.active{background:var(--soft);border-color:#a7ba90}.transcript-jump{border:0;background:none;color:var(--navy);display:flex;flex-direction:column;gap:5px;font-weight:700;text-align:left;padding:0;font-size:14px}.transcript-time{font-size:11px;font-weight:500;color:var(--green);font-variant-numeric:tabular-nums}.transcript-card p{margin-top:12px;font-size:14px;line-height:1.65;color:var(--muted)}.provenance-grid{display:grid;grid-template-columns:1fr 1fr;gap:26px;margin-top:17px}.provenance-grid h3{font-size:14px;margin:0 0 7px}.provenance-grid p{font-size:13px;line-height:1.65;color:var(--muted);margin:0 0 9px}.file-label{font:12px/1.6 ui-monospace,Consolas,monospace;overflow-wrap:anywhere;color:var(--ink)}footer{padding:0 38px 27px;color:var(--muted);font-size:12px;display:flex;justify-content:space-between;gap:20px;flex-wrap:wrap}.noscript{padding:15px;color:#66491e;background:#fff5e5}.small-link{font-size:12px}.provenance-target{scroll-margin-top:22px}@media(min-width:1050px){.narration-panel{position:sticky;top:20px}}@media(max-width:1000px){main{grid-template-columns:1fr}.narration-panel{position:static}.current-block{min-height:0}.cue-text{font-size:18px}.panel-head{padding-bottom:14px}}@media(max-width:580px){header{padding:22px 18px}.brandbar{margin-bottom:18px}.edition{font-size:10px}main{padding:0 18px 26px;gap:18px}.header-bottom{margin-top:17px;gap:16px}.downloads{gap:14px}.chapters,.transcript,.provenance-grid{grid-template-columns:1fr}.playback-info{margin-bottom:18px}.chapter{font-size:13px}.panel-head,.current-block{padding:18px}.details-card{padding:17px}.section-heading small{display:none}footer{padding:0 18px 24px}.cue-text{font-size:17px}}@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}.cue-text span{transition:none}}
</style>
</head>
<body>
<header>
<div class="brandbar"><span class="brand">Steward</span><span class="edition">Kurgu 3 · Seslendirilmiş sürüm</span></div>
<div class="eyebrow">Demo filmi / inceleme</div>
<h1>Konuşmadan doğrulanmış sonuca.</h1>
<p class="intro">Telegram açılışı, ürün akışları ve teknik kanıtlar; gönderdiğin seslendirmeler ve konuşmanın altında kalan özgün fon müziğiyle bir arada.</p>
<div class="header-bottom"><div class="metadata"><span class="chip">__DURATION__</span><span class="chip">1080p · 30 fps</span><span class="chip">13 kayıt + açılış sesi</span><span class="chip">Özgün fon müziği</span></div>
<nav class="downloads" aria-label="Video dosyaları"><a class="download-primary" href="steward-narrated-1080p.mp4" download>Videoyu indir</a><a href="narration.srt" download>İngilizce altyazı</a><a href="README_TR.md">Kurgu notları</a></nav></div>
</header>
<main>
<section class="viewer" aria-label="Seslendirilmiş Steward videosu">
<div class="video-shell"><video id="player" controls playsinline preload="metadata" aria-label="Steward, seslendirilmiş demo videosu"><source src="steward-narrated-1080p.mp4" type="video/mp4"><track kind="captions" src="narration.vtt" srclang="en" label="English">Videoyu oynatmak için <a href="steward-narrated-1080p.mp4">MP4 dosyasını açın</a>.</video><p class="notice" id="videoError" role="status" hidden>Video yüklenemedi. Sayfayı yenileyebilir veya “Videoyu indir” bağlantısıyla dosyayı açabilirsin.</p></div>
<div class="playback-info"><span class="clock" id="clock">00:00 / __DURATION__</span><span class="playing-chapter" id="nowChapter" lang="en">The conversation</span></div>
<div class="section-heading"><h2>Bölüme git</h2><small>Anlatım ve görüntü aynı zaman çizelgesinde.</small></div>
<nav class="chapters" aria-label="Video bölümleri">__CHAPTERS__</nav>
<p class="review-tip">Oynatıcının CC menüsünden İngilizce altyazıyı açabilirsin. Sağdaki metin o anda anlatılan bölümü izler; altındaki bütün metin üzerinden de ilgili ana atlayabilirsin.</p>
</section>
<aside class="narration-panel" aria-labelledby="narrationTitle">
<div class="panel-head"><h2 id="narrationTitle">Şu an anlatılan</h2><p>Ses kaydıyla eşleştirilmiş İngilizce konuşma metni.</p><div class="voice-label"><span class="voice-dot" aria-hidden="true"></span><span id="voiceLabel">AÇILIŞ SESİ</span></div></div>
<div class="current-block"><div class="cue-meta"><span id="cueTime">00:00–00:30</span><span id="cueCount">Açılış</span></div><h3 class="cue-title" id="cueTitle" lang="en">The conversation</h3><p class="cue-text opening" id="cueText" lang="tr">Onayladığın Telegram açılışı kendi ses kaydıyla oynuyor. Ürün anlatımı 00:30’da başlıyor.</p></div>
<div class="panel-foot"><a href="#fullTranscript" data-open="fullTranscript">Bütün konuşma metni</a><a href="#musicProvenance" data-open="musicProvenance">Müziğin kaynağı</a></div>
</aside>
<details id="fullTranscript" class="details-card wide provenance-target"><summary>İngilizce konuşma metni · 13 bölüm</summary><p class="detail-lead">Her başlık videonun ilgili anına gider. Açılışın mevcut sesi ayrıca korunuyor.</p><div class="transcript">__TRANSCRIPT__</div></details>
<details id="musicProvenance" class="details-card wide provenance-target"><summary>Kurgu bağlamı ve müziğin kaynağı</summary><div class="provenance-grid"><div><h3>Görüntülerin bağlamı</h3><p>İlk sahne temsili Telegram konuşmasıdır. Ardından gönderdiğin asıl Telegram mesajı ve o mesajla eşleşen vakanın sonradan kaydedilmiş canlı uygulama görüntüsü gelir.</p><p>Ayrıntılı ürün akışları yerel simülasyondur. Teknik bölüm, birbirinden ayrı kaydedilmiş AgentCore, yeniden başlatma, Telegram ve kontrollü SES doğrulamalarını gösterir.</p><p>Kesim noktaları gerçek ses kayıtlarının süreleri, konuşma tanımanın cümle zamanları ve görüntüde gözlenen olaylar birlikte kullanılarak düzenlendi. Konuşmanın hızı ve tonu değiştirilmedi.</p></div><div><h3>Steward — Quiet Continuity</h3><p>Bu video için sıfırdan matematiksel ses senteziyle oluşturuldu. Dış ses kaydı, sample, loop, hazır müzik veya kopyalanmış melodi kullanılmadı.</p><p>Yumuşak sinüs dalgaları, düşük düzeyli harmonikler, yavaş akor geçişleri ve seyrek pluck notaları kullanıldı. Fon, seslendirme boyunca düşük seviyede tutulur.</p><p class="file-label">Kaynak: tools/compose_steward_music.py<br>Arşiv: artifacts/video/music-original/PROVENANCE.md</p><p>Üçüncü taraf kaynak müzikten gelen atıf veya lisans şartı yoktur. Otomatik platform/Content ID sonuçları garanti edilemez.</p></div></div></details>
<noscript><p class="noscript wide">Bölüm atlama ve hareketli metin için JavaScript gerekir. Video kontrolleri, metin ve dosya bağlantıları kullanılabilir.</p></noscript>
</main>
<footer><span>Ham görüntüler, ses kayıtları ve önceki kurgular korunuyor. Bu sayfa harici hizmet kullanmaz.</span><a href="../browse.html">Kayıt arşivine dön</a></footer>
<script type="application/json" id="cutData">__DATA__</script>
<script>
'use strict';
const data=JSON.parse(document.getElementById('cutData').textContent);
const player=document.getElementById('player');
const clock=document.getElementById('clock');
const nowChapter=document.getElementById('nowChapter');
const cueTime=document.getElementById('cueTime');
const cueCount=document.getElementById('cueCount');
const cueTitle=document.getElementById('cueTitle');
const cueText=document.getElementById('cueText');
const voiceLabel=document.getElementById('voiceLabel');
const chapterButtons=[...document.querySelectorAll('.chapter')];
const transcriptCards=[...document.querySelectorAll('.transcript-card')];
let lastCue=-2,lastCaption=-2,lastChapter=-2,pendingSeek=null;
function stamp(t,precise=false){let n=Math.max(0,Math.round(t*10));const m=Math.floor(n/600);n%=600;const s=Math.floor(n/10),f=n%10;return String(m).padStart(2,'0')+':'+String(s).padStart(2,'0')+(precise&&f?'.'+f:'');}
function seek(t){if(player.readyState<1){pendingSeek=t;player.load();return;}player.currentTime=t;update();}
for(const button of document.querySelectorAll('[data-seek]'))button.addEventListener('click',()=>seek(Number(button.dataset.seek)));
for(const link of document.querySelectorAll('[data-open]'))link.addEventListener('click',()=>{document.getElementById(link.dataset.open).open=true;});
function showCue(index){
 if(index<0){cueTime.textContent='00:00–00:30';cueCount.textContent='Açılış';cueTitle.textContent='The conversation';cueText.textContent='Onayladığın Telegram açılışı kendi ses kaydıyla oynuyor. Ürün anlatımı 00:30’da başlıyor.';cueText.lang='tr';cueText.className='cue-text opening';voiceLabel.textContent='AÇILIŞ SESİ';return;}
 const cue=data.narration[index];cueTime.textContent=stamp(cue.start,true)+'–'+stamp(cue.end,true);cueCount.textContent=String(index+1).padStart(2,'0')+' / '+data.narration.length;cueTitle.textContent=cue.title;voiceLabel.textContent='KAYDEDİLMİŞ SESLENDİRME';cueText.lang='en';cueText.className='cue-text';cueText.replaceChildren();
 const captions=data.captions.map((c,i)=>({...c,index:i})).filter(c=>c.start>=cue.start-.01&&c.start<cue.end);
 if(!captions.length){cueText.textContent=cue.text;return;}
 for(const c of captions){const span=document.createElement('span');span.dataset.caption=c.index;span.textContent=c.text;cueText.append(span,document.createTextNode(' '));}
}
function update(){
 const t=Number.isFinite(player.currentTime)?player.currentTime:0;clock.textContent=stamp(t)+' / '+stamp(data.duration_seconds,true);
 let chapter=data.chapters.findIndex(c=>t>=c.start&&t<c.end);if(t>=data.duration_seconds-.01)chapter=data.chapters.length-1;
 if(chapter!==lastChapter){chapterButtons.forEach((b,i)=>b.setAttribute('aria-current',i===chapter?'true':'false'));if(chapter>=0)nowChapter.textContent=data.chapters[chapter].title;lastChapter=chapter;}
 let cue=data.narration.findIndex(c=>t>=c.start&&t<c.end);if(t>=data.duration_seconds-.01)cue=data.narration.length-1;
 if(cue!==lastCue){showCue(cue);transcriptCards.forEach((c,i)=>c.classList.toggle('active',i===cue));lastCue=cue;lastCaption=-2;}
 const caption=data.captions.findIndex(c=>t>=c.start&&t<c.end);
 if(caption!==lastCaption){for(const span of cueText.querySelectorAll('[data-caption]'))span.classList.toggle('is-speaking',Number(span.dataset.caption)===caption);lastCaption=caption;}
}
function error(){document.getElementById('videoError').hidden=false;}
player.addEventListener('loadedmetadata',()=>{document.getElementById('videoError').hidden=true;if(pendingSeek!==null){const t=pendingSeek;pendingSeek=null;player.currentTime=t;}update();});
for(const event of ['timeupdate','seeked','ended'])player.addEventListener(event,update);
player.addEventListener('error',error);player.querySelector('source').addEventListener('error',error);
if(location.hash){const target=document.getElementById(location.hash.slice(1));if(target&&target.tagName==='DETAILS')target.open=true;}
update();
</script>
</body>
</html>
'''
    replacements = {
        '__DURATION__': stamp(duration, True), '__CHAPTERS__': chapter_html,
        '__TRANSCRIPT__': transcript_html,
        '__DATA__': json.dumps(public, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/'),
    }
    for marker, value in replacements.items():
        page = page.replace(marker, value)
    (OUT / 'index.html').write_text(page, encoding='utf-8')

    chapter_rows = '\n'.join(
        f'| {stamp(c["start"], True)}–{stamp(c["end"], True)} | {c["title"]} |'
        for c in data['chapters']
    )
    recording_rows = '\n'.join(
        f'| {Path(c["file"]).name} | {stamp(c["start"], True)}–{stamp(c["end"], True)} | {TITLES[c["id"]]} |'
        for c in data['narration']
    )
    readme = f'''# Steward — Seslendirilmiş kurgu 3

{stamp(duration, True)} / {duration:.1f} saniye · 1920×1080 · 30 fps · İngilizce seslendirme

Gönderilen 13 seslendirme kaydı, onaylı Telegram açılışı ve özgün fon müziği bu
sürümde bir araya getirilir. Ana dosya **steward-narrated-1080p.mp4** dosyasıdır.

## İzleme ve dosyalar

- [İnceleme sayfası](index.html): yerel, bağımsız HTML; yerleşik oynatıcı, bölüm
  geçişleri, o anda anlatılan İngilizce metin ve bütün konuşma blokları.
- [Seslendirilmiş 1080p MP4](steward-narrated-1080p.mp4).
- [İngilizce VTT](narration.vtt) ve [SRT](narration.srt): gerçek kayıtlara göre
  zamanlanan konuşma metni. Açılışın önceden hazırlanmış sesi ayrı tutulur.
- [Senkron planı](sync-plan.json): bölüm ve ses kaynakları, cümle zamanları,
  görüntü kesimleri ve kurgu bağlamı.
- [Kurgu manifesti](edit-manifest.json): üretim sırasında kaydedilen kesim bilgileri.
- Teknik render kontrollerinin dosyaları `qa/` altında tutulur. Tam ses/görüntü
  çözümlemesi ve 6.795 kare kontrolü tamamlandı. Son miks -18.2 LUFS, gerçek tepe
  -4.6 dBFS; ayrıntılar üretim kontrol kayıtlarındadır.

İnceleme sayfası internet bağlantısı veya harici yazı tipi/komut dosyası istemez.
Dosyadan açıldığında bazı tarayıcılar ayrı VTT dosyasını kısıtlayabilir; sayfadaki
gömülü konuşma metni çalışmaya devam eder. Mevcut yerel inceleme sunucusunda native
CC menüsü ve bölüm atlama kullanılabilir.

## Bölümler

| Zaman | Bölüm |
|---|---|
{chapter_rows}

## Seslendirme ve görüntü eşlemesi

Kayıtlar içerik ve gerçek süreleriyle eşleştirildi. Konuşma tanımanın cümle/zaman
tahminleri, gözlenen ürün olaylarıyla birlikte kesim noktalarını belirlemek için
kullanıldı. Bu yöntem örnek düzeyinde kusursuz ağız/ses eşlemesi iddiası değildir.
Görüntü süreleri konuşmaya uyarlandı; konuşmanın hızı ve tonu değiştirilmedi.

Teklif karşılaştırmasında önce 540 dolarlık dar kapsamlı teklif, sonra 705 dolarlık
hizalama dahil teklif gösterilir; böylece fiyat ve kapsam açıklamaları doğru kaynakla
eşleşir. Sipariş, randevu ve sonuç doğrulaması ayrı olaylar olarak kalır.

| Ses dosyası | Filmdeki bölüm | Konu |
|---|---|---|
{recording_rows}

İlk 30 saniye, onaylanan açılış videosunun mevcut sesini korur. Yeni kayıtların
orijinalleri `artifacts/video/` altında korunur. Önceki `Steward 2.mp3` kaydı bu
kurgudaki 13 yeni bölümün yerine kullanılmaz.

## Görüntü bağlamı

- İlk Telegram konuşması temsili açılıştır. Sonraki mesaj görseli kullanıcıdan
  gelen özgün ekran görüntüsüdür; mesaj metni ve zaman damgası değiştirilmez.
- Mesajı izleyen canlı uygulama görüntüsü, aynı vakanın sonradan kaydedilmiş
  incelenmesidir. Yeni bir mesajın o anda gönderildiği kesintisiz canlı çekim değildir.
- Ayrıntılı toplantı, bakım ve sakin doğrulama akışları yerel simülasyondur.
  Toplantı zamanı/yeter sayının oluşması bu ana kurguda gösterilmez; tutanak
  bölümüne toplantı sonrası bağlamıyla geçilir.
- Firma randevu yanıtı yerelde simüle edilir. Tamamlanma aşamasından önce demo
  zamanı 48 saat ilerletilir. Tekrarlayan arıza/garanti bölümü ayrı, yeni bir vakadır.
- Teknik görüntüler, daha önce ayrı koşularda kaydedilmiş AgentCore, yeniden
  başlatma, gerçek Telegram teslimi ve kontrollü SES mail doğrulamalarına dayanır.
  Bunlar ürün simülasyonunun aynı anda yapılan gerçek kanal koşusu olarak sunulmaz.

## Özgün fon müziği

**Steward — Quiet Continuity**, bu video için sıfırdan matematiksel sentezle
üretildi. Harici ses kaydı, sample, loop, hazır parça, örnek kütüphanesi veya
kopyalanmış melodi kullanılmadı. Yumuşak sinüs dalgaları, düşük harmonikler,
küçük stereo detune, yavaş akor geçişleri ve seyrek pluck notaları kullanılır.

- Kaynak kod: `tools/compose_steward_music.py`.
- Master ve ayrı stem arşivi: `artifacts/video/music-original/`.
- Tam kaynak notu: `artifacts/video/music-original/PROVENANCE.md`.
- Sayfada çalışan kaynak bağlantısı: [müzik ve kurgu kaynakları](index.html#musicProvenance).
- Orijinal müzik masterı: 300 saniye, stereo 48 kHz PCM WAV; -20.6 LUFS, gerçek
  tepe -12.4 dBFS. Son videoda bu master konuşmanın altında düşük seviyede mikslenir.
- Üçüncü taraf kaynak müzikten gelen atıf veya lisans şartı yoktur. Otomatik
  platform/Content ID sonuçlarına mutlak garanti verilmez.

Müzik üretim kontrolü sayısal ve spektraldir; öznel dinleme yapıldığı iddia edilmez.

## Korunan kaynaklar

Ham OBS kayıtları, ayrı ürün klipleri, kullanıcı ses kayıtları, açılış videosu ve
`assembly-v1` / `assembly-v2` sürümleri korunur. Bu sürüm önceki kurguların üstüne
yazmaz. Sayfa tekrar üreticisi `tools/build_narrated_preview.py`, güncel
`sync-plan.json` içindeki süre ve metinleri kullanır; video veya ses üretmez.
'''
    (OUT / 'README_TR.md').write_text(readme, encoding='utf-8')
    print(json.dumps({'page': str(OUT / 'index.html'), 'readme': str(OUT / 'README_TR.md'),
                      'duration_seconds': duration, 'chapters': len(data['chapters']),
                      'recordings': len(data['narration']), 'captions': len(data['captions'])}, indent=2))


if __name__ == '__main__':
    main()
