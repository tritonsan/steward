"""Build an offline media index from the recorded coverage/edit manifests.

No media is changed. Run from any directory with the project's Python runtime.
"""
from __future__ import annotations

import html
import json
from pathlib import Path
from urllib.parse import quote, unquote
from html.parser import HTMLParser

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "artifacts/video/coverage-session"

# Turkish navigation labels; the full underlying observation remains verbatim.
LABELS = {
    "history": ("Toplantı", "Gündem ve geçmiş karar", "Hazırlanan gündemden geçmiş kararın kaynağına erişim."),
    "minutes": ("Toplantı", "Tutanak ve karar hazırlığı", "Yazılı tutanak, İngilizce takvim ve yönetici incelemesine hazırlanan kararlar."),
    "confirm": ("Toplantı", "Kararı ve görevi onaylama", "Karar ile görevin ayrı seçilmesi; sorumlu ve son tarihin görünmesi."),
    "outcome": ("Toplantı", "Görevden doğrulanmış sonuca", "Tamamlanma bildirimi, ayrı sonuç doğrulaması ve hafızadan yeniden bulma."),
    "quotes": ("Bakım", "Teklifleri kaynaklarıyla inceleme", "Önerilen teklif ve daha ucuz alternatifin özgün teklif metinlerini açma."),
    "approval": ("Bakım", "Sipariş onayı ve randevu bekleyişi", "Gerekçeli onay, gönderim devamı ve firmadan randevu önerisi bekleyen vaka."),
    "appointment": ("Bakım", "Firma önerisinden kesin randevuya", "Simüle firma yanıtının alınması, worker adımı ve kesinleşen randevu."),
    "completion": ("Bakım", "Tamamlanma bildirimi", "Yönetici işin tamamlandığını kaydeder; vaka sonuç doğrulamasını bekler."),
    "resident": ("Sakin", "Sakinin sonuç doğrulaması", "Sakine özel yanıt kartı, gözlem notu ve doğrulama sonrası güncellenen görünüm."),
    "maintenance_memory": ("Hafıza", "Doğrulanmış onarımı yeniden bulma", "Hafızada arama, kapalı vakayı açma ve ilk bildirimin kaynağını okuma."),
    "settings": ("Yönetim", "Çalışma kuralları ve yetki sınırları", "Randevu saatleri, erişim ve operasyon yetkilerinin salt okunur incelemesi."),
    "clarification_warranty": ("İstisna", "Eksik konumdan önceki onarımı kontrol etmeye", "Sakinin açıklaması aynı vakayı ilerletir; yakın geçmiş yeni ücretli işten önce kontrol gerektirir."),
    "quorum_wait": ("İstisna", "Uygunluk yanıtı ve yeter sayı bekleyişi", "Ayrı toplantıda sakin yanıt verir; tanımlı yeter sayı korunur."),
    "deadline_review": ("İstisna", "Süresi dolan toplantı turu", "Yönetici inceleme görevi, kapanan yanıt turu ve yeni toplantı düzenleme kontrolü."),
    "recorded_sources": ("Teknik kayıt", "Kaydedilmiş teknik doğrulama kaynakları", "AgentCore, yeniden başlatma, Telegram ve kontrollü SES için önceden kaydedilmiş doğrulama dosyalarının okunması."),
    "message_to_case": ("Yeni bildirim", "Sakin mesajından yeni vakaya", "Ayrı bir demo mesajının kalıcı alımı ve normal işleme adımıyla oluşan yeni vaka."),
}


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def url(path: str) -> str:
    return quote(path.replace("\\", "/"), safe="/.-_")


def duration(value: float) -> str:
    seconds = float(value)
    minutes, remainder = divmod(seconds, 60)
    return f"{int(minutes):02d}:{remainder:06.3f}"


def exact(value: float) -> str:
    return format(value, ".6f").rstrip("0").rstrip(".") + " sn"


def info_for(clip: dict) -> tuple[str, str, str]:
    name = clip["name"]
    if name in LABELS:
        return LABELS[name]
    text = " ".join(str(clip.get(k, "")) for k in ("name", "title", "result", "limits")).lower()
    if any(token in text for token in ("agentcore", "recorded verification", "technical evidence")):
        return ("Teknik kayıt", "Kaydedilmiş teknik doğrulama kaynakları", "Önceden kaydedilmiş doğrulama sonuçlarının kaynak görünümü; çekim anında yeni bir canlı test değildir.")
    return ("Diğer", clip["title"], clip.get("result", ""))


def video(path: str, title: str, poster: str = "") -> str:
    poster_attr = f' poster="{url(poster)}"' if poster else ''
    return (f'<video controls preload="none" playsinline aria-label="{esc(title)}"{poster_attr}>'
            f'<source src="{url(path)}" type="video/mp4">'
            f'Tarayıcınız videoyu açamıyorsa <a href="{url(path)}">dosyayı açın</a>.</video>')


def clip_card(clip: dict, number: int, raw_by_source: dict[str, dict]) -> str:
    category, title, description = info_for(clip)
    output = "selects/" + clip["output"]
    source = clip.get("source", "")
    source_start = (clip.get("edits") or [{}])[0].get("source_start_seconds", 0)
    samples = raw_by_source.get(source, {}).get("sampled_frames", [])
    poster = min(samples, key=lambda s: abs(s["elapsed_seconds"] - source_start))["file"] if samples else ""
    search = " ".join((category, title, description, clip["title"], clip["output"], clip.get("result", "")))
    rows = "".join(
        f'<tr><td>{duration(e["select_start_seconds"])}–{duration(e["select_end_seconds"])}</td>'
        f'<td>{duration(e["source_start_seconds"])}–{duration(e["source_end_seconds"])}</td>'
        f'<td>{esc(e.get("context", ""))}</td></tr>'
        for e in clip.get("edits", [])
    )
    sources = clip.get("sources", [])
    if source:
        source_links = f'<a href="{url(source)}">Ham kaydı aç</a>'
    else:
        source_links = " ".join(
            f'<a href="{url(s if isinstance(s, str) else s.get("source", ""))}">Ham kaynak {i + 1}</a>'
            for i, s in enumerate(sources)
        )
    return f'''<article class="clip" data-category="{esc(category)}" data-search="{esc(search)}">
      <div class="clip-top"><span class="tag">{esc(category)}</span><span class="index">{number:02d}</span></div>
      <h3>{esc(title)}</h3><p class="description">{esc(description)}</p>
      {video(output, title, poster)}
      <div class="meta"><strong>{duration(clip['duration_seconds'])}</strong><span>{exact(clip['duration_seconds'])} · {clip['width']} × {clip['height']} · {clip['fps']} fps · normal hız</span></div>
      <p class="filename">{esc(clip['output'])}</p>
      <div class="links"><a href="{url(output)}">Klibi aç</a><a href="{url(output)}" download>İndir</a>{source_links}</div>
      <details class="notes"><summary>Kaynak, kesim aralıkları ve kapsam</summary>
        <p><strong>Gözlenen sonuç:</strong> {esc(clip.get('result', ''))}</p>
        <p><strong>Kapsam notu:</strong> {esc(clip.get('limits', ''))}</p>
        <p class="filename">Kaynak: {esc(source or sources)}</p>
        <div class="table-wrap"><table><thead><tr><th>Bu klip</th><th>Ham kayıt</th><th>Gözlenen adım</th></tr></thead><tbody>{rows}</tbody></table></div>
      </details>
    </article>'''


def raw_card(raw: dict, takes: dict[str, list[dict]], clip_names: dict[str, list[str]], index: int) -> str:
    source = raw["source"]
    related = clip_names.get(source, [])
    source_takes = takes.get(source, [])
    seen = set()
    observations = []
    for take in source_takes:
        observation = take.get("observed_result", "")
        if observation and observation not in seen:
            observations.append(observation)
            seen.add(observation)
    review = raw.get("visual_review", {})
    warning = '<p class="warning">Bu ham çekimde üstte tarayıcı hata ayıklama çubuğu görünüyor. Temiz seçkilere alınmadı.</p>' if review.get("debug_infobar_visible") else ""
    search = " ".join([source] + related + observations)
    return f'''<details class="raw-item" data-search="{esc(search)}">
      <summary><span>{index:02d} · {esc(Path(source).name)}</span><span>{duration(raw['duration_seconds'])}</span></summary>
      <div class="raw-body">{warning}{video(source, Path(source).name, (raw.get('sampled_frames') or [{}])[0].get('file', ''))}
      <p class="meta">{exact(raw['duration_seconds'])} · {raw['width']} × {raw['height']} · {esc(raw['fps'])} fps · {raw['bytes'] / 1_000_000:.1f} MB</p>
      <p><strong>İlgili seçkiler:</strong> {esc(' · '.join(related) or 'Bu kayıttan henüz seçki yok.')}</p>
      {''.join('<p>'+esc(o)+'</p>' for o in observations)}
      <p><strong>Görüntü incelemesi:</strong> {esc(review.get('required_edit', 'Manifestte ek düzenleme notu yok.'))}</p>
      <div class="links"><a href="{url(source)}">Ham dosyayı aç</a><a href="{url(source)}" download>İndir</a></div>
      </div></details>'''


CSS = '''
:root{color-scheme:light;--navy:#082f44;--green:#426747;--paper:#f4f6f3;--ink:#173540;--muted:#52626b;--line:#d5ded8}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}a{color:#245e41;text-underline-offset:3px}a:hover{color:#083626}button,input{font:inherit}button,a,summary,input{outline-offset:4px}:focus-visible{outline:3px solid #ba7d26}header{background:var(--navy);color:white;padding:48px max(24px,calc((100vw - 1280px)/2)) 36px}.eyebrow{color:#b8d3a4;text-transform:uppercase;letter-spacing:.12em;font-size:13px;font-weight:700}h1{font-size:clamp(30px,5vw,46px);line-height:1.15;margin:12px 0 18px}header p{max-width:900px;color:#e0e8eb;margin:10px 0}header a{color:#c9e3b9}.stats{display:flex;gap:12px;flex-wrap:wrap;margin:25px 0 16px}.stats span{border:1px solid #52707c;border-radius:8px;padding:8px 14px}.stats strong{font-size:21px;margin-right:6px}main{max-width:1328px;margin:auto;padding:30px 24px 70px}.intro{background:white;border-left:4px solid var(--green);padding:14px 20px;margin-bottom:24px}.intro p{margin:4px 0}.toolbar{display:flex;gap:14px;flex-wrap:wrap;align-items:end;margin:24px 0}.search{display:block;flex:1;min-width:230px;max-width:500px;font-weight:650}.search input{display:block;margin-top:6px;width:100%;padding:11px 13px;border:1px solid #8b9f93;border-radius:7px;background:white;color:var(--ink)}.filters{display:flex;gap:6px;flex-wrap:wrap}.filters button{cursor:pointer;border:1px solid #9eb0a3;background:#fff;color:#284d36;padding:9px 11px;border-radius:6px}.filters button[aria-pressed=true]{background:var(--green);border-color:var(--green);color:white}.count{color:var(--muted);font-size:14px;margin-bottom:20px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:24px}.clip{background:white;padding:22px;border:1px solid var(--line);border-radius:12px;min-width:0}.clip-top{display:flex;justify-content:space-between;align-items:center}.tag{background:#eaf0e5;color:#315436;padding:4px 10px;border-radius:20px;font-size:13px;font-weight:700}.index{color:#6c8088;font-variant-numeric:tabular-nums}h3{font-size:22px;line-height:1.3;margin:14px 0 9px}.description{min-height:50px;color:var(--muted);margin-top:0}video{display:block;width:100%;aspect-ratio:16/9;background:#061f2d;border-radius:6px}.meta{display:flex;gap:8px;flex-wrap:wrap;font-size:13px;color:var(--muted);margin:12px 0 4px}.meta strong{color:var(--ink);font-size:15px}.filename{font:12px/1.6 ui-monospace,Consolas,monospace;overflow-wrap:anywhere;color:#52656c}.links{display:flex;gap:16px;flex-wrap:wrap;font-size:14px;margin:12px 0}.notes{border-top:1px solid var(--line);margin-top:16px;padding-top:12px;font-size:14px}.notes summary{cursor:pointer;font-weight:650;color:#345541}.table-wrap{overflow:auto}table{border-collapse:collapse;width:100%;font-size:12px}th,td{text-align:left;vertical-align:top;border-bottom:1px solid var(--line);padding:8px}td:first-child,td:nth-child(2){white-space:nowrap;font-variant-numeric:tabular-nums}h2{font-size:27px;margin:34px 0 10px}.section-note{color:var(--muted);max-width:900px}.technical{border-top:2px solid var(--line);margin-top:38px;padding-top:6px}.raw-section{border-top:2px solid var(--line);margin-top:38px;padding-top:6px}.raw-item{background:white;border:1px solid var(--line);border-radius:8px;margin:10px 0}.raw-item>summary{padding:15px 18px;cursor:pointer;font-weight:650;display:flex;justify-content:space-between;gap:16px}.raw-body{padding:0 18px 18px}.raw-body video{max-width:920px}.raw-body p{max-width:1000px;font-size:14px}.warning{padding:10px 14px;background:#fff0d6;border-left:3px solid #98702d}.empty{padding:30px;border:1px dashed #97ab9d;background:white}.footer{margin-top:35px;color:var(--muted);font-size:13px}[hidden]{display:none!important}@media(max-width:800px){.grid{grid-template-columns:1fr}header{padding:32px 22px}main{padding:24px 16px 50px}.clip{padding:16px}.description{min-height:0}.raw-item>summary{font-size:13px}.filters{gap:5px}}
'''

CSS += '''
.raw-item>summary::before{content:"+";color:var(--green);font-size:20px;line-height:1.2;min-width:14px}.raw-item[open]>summary::before{content:"−"}.raw-item>summary>span:first-of-type{flex:1}.raw-item>summary>span:last-of-type{font-variant-numeric:tabular-nums}
'''

JS = '''
const search = document.querySelector('#search');
const buttons = [...document.querySelectorAll('[data-filter]')];
let category = 'Tümü';
const normalize = value => value.toLocaleLowerCase('tr-TR');
function filter() {
 const query = normalize(search.value.trim());
 let count = 0;
 document.querySelectorAll('.clip').forEach(card => {
   const visible = (category === 'Tümü' || card.dataset.category === category) && normalize(card.dataset.search).includes(query);
   card.hidden = !visible; if (visible) count++;
   if (!visible) card.querySelector('video').pause();
 });
 document.querySelectorAll('.raw-item').forEach(card => {
   card.hidden = query !== '' && !normalize(card.dataset.search).includes(query);
   if(card.hidden) card.querySelector('video').pause();
 });
 document.querySelector('#visible-count').textContent = count + ' seçki gösteriliyor';
 document.querySelector('#empty').hidden = count !== 0;
 document.querySelectorAll('[data-clip-section]').forEach(section => {
   section.hidden = ![...section.querySelectorAll('.clip')].some(card => !card.hidden);
 });
}
search.addEventListener('input', filter);
buttons.forEach(button => button.addEventListener('click', () => {
 category = button.dataset.filter;
 buttons.forEach(item => item.setAttribute('aria-pressed', String(item === button)));
 filter();
}));
document.addEventListener('play', event => {
 if(event.target.tagName === 'VIDEO') document.querySelectorAll('video').forEach(other => {
   if(other !== event.target) other.pause();
 });
}, true);
document.querySelectorAll('.raw-item').forEach(item => item.addEventListener('toggle', () => {
 if(!item.open) item.querySelector('video').pause();
}));
'''


def main() -> None:
    selections = json.loads((BASE / "selects/edit-manifest.json").read_text(encoding="utf-8"))
    coverage = json.loads((BASE / "coverage-manifest.json").read_text(encoding="utf-8"))
    clips = selections["clips"]
    raws = coverage["session"]["raw_recordings"]
    raw_by_source = {raw["source"]: raw for raw in raws}
    takes: dict[str, list[dict]] = {}
    for sequence in coverage["sequences"]:
        for take in [sequence.get("take", {})] + sequence.get("additional_takes", []) + sequence.get("branch_takes", []):
            if take.get("raw_file"):
                takes.setdefault(take["raw_file"], []).append(take)
    clip_names: dict[str, list[str]] = {}
    for clip in clips:
        if clip.get("source"):
            clip_names.setdefault(clip["source"], []).append(info_for(clip)[1])
    categories = list(dict.fromkeys(info_for(c)[0] for c in clips))
    product, technical = [], []
    for i, clip in enumerate(clips, 1):
        (technical if info_for(clip)[0] == "Teknik kayıt" else product).append(clip_card(clip, i, raw_by_source))
    technical_html = (f'<section class="technical" data-clip-section><h2>Teknik doğrulama kaynakları</h2>'
                      '<p class="section-note">Bu bölüm önceden kaydedilmiş sonuçları gösterir. Yerel ürün akışından ve çekim anında yürütülen canlı testten ayrı değerlendirin.</p>'
                      f'<div class="grid">{"".join(technical)}</div></section>') if technical else ''
    filters = ''.join(f'<button type="button" data-filter="{esc(c)}" aria-pressed="{str(c == "Tümü").lower()}">{esc(c)}</button>' for c in ['Tümü'] + categories)
    total_select = sum(c['duration_seconds'] for c in clips)
    total_raw = sum(r['duration_seconds'] for r in raws)
    document = f'''<!doctype html><html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Steward · Çekim arşivi</title><style>{CSS}</style></head><body>
    <header><div class="eyebrow">Steward · 14 Eylül 2026</div><h1>Kurgu için çekim arşivi</h1>
      <p>Akışları tek tek izleyin; istediğiniz seçkiyi veya ham kaydı açın. Beklemeler seçkilerden çıkarıldı, ham dosyalar korundu. Bu bir son montaj değildir.</p>
      <div class="stats"><span><strong>{len(clips)}</strong> seçki · {duration(total_select)}</span><span><strong>{len(raws)}</strong> ham kayıt · {duration(total_raw)}</span><span><strong>1080p</strong> 30 fps</span></div>
      <p><a href="README_TR.md">Çekim notları</a> · <a href="selects/edit-manifest.json">Kesim manifesti</a> · <a href="coverage-manifest.json">Ham kayıt manifesti</a></p>
    </header><main><div class="intro"><p><strong>İzleme:</strong> Videolar yalnız oynat düğmesine bastığınızda yüklenir. Kapaklar ham kaydın mevcut inceleme karelerinden alınmıştır; seçkinin tam başlangıç karesi olmayabilir. Bir video başlatıldığında diğerleri durur.</p><p>Ürün çekimleri Northgate'in izole yerel simülasyonudur. Firma girdileri ve sonuç notları demo verisidir. Seçkiler normal hızdadır; seslendirme içermez. Kapsam ve kesim ayrıntıları her kartta bulunur.</p></div>
    <div class="toolbar"><label class="search" for="search">Akış veya dosya ara<input id="search" type="search" placeholder="Örn. toplantı, randevu, hafıza" autocomplete="off"></label><div class="filters" role="group" aria-label="Seçki kategorisi">{filters}</div></div>
    <p class="count" id="visible-count" role="status" aria-live="polite">{len(clips)} seçki gösteriliyor</p>
    <p class="empty" id="empty" hidden>Bu aramaya uygun seçki yok. Aramayı temizleyin veya başka kategori seçin.</p>
    <section data-clip-section><h2>Ürün akışları</h2><div class="grid">{''.join(product)}</div></section>
    {technical_html}
    <section class="raw-section"><h2>Ham kayıtlar</h2><p class="section-note">Dosyayı genişleterek kesilmemiş çekimi izleyin. Arama burada da çalışır; kategori filtresi yalnız seçkileri etkiler. Süreler manifestten alınmıştır.</p>{''.join(raw_card(raw,takes,clip_names,i) for i,raw in enumerate(raws,1))}</section>
    <p class="footer">Yerel dosya arşivi · İnternet, hesap veya sunucu gerektirmez. Görünen içerik iki manifestten üretilmiştir. Yenilemek için tools/build_coverage_gallery.py çalıştırılır.</p>
    </main><script>{JS}</script></body></html>'''
    out = BASE / "browse.html"
    out.write_text(document, encoding="utf-8")
    class LinkCheck(HTMLParser):
        def __init__(self):
            super().__init__()
            self.targets = set()
        def handle_starttag(self, tag, attrs):
            for key, value in attrs:
                if key in ("src", "href", "poster") and value:
                    self.targets.add(value)
    check = LinkCheck()
    check.feed(document)
    missing = [p for p in check.targets if not (BASE / unquote(p)).is_file()]
    if missing:
        raise SystemExit(f"Missing local targets: {missing}")
    print(json.dumps({"output": str(out), "clips": len(clips), "raws": len(raws), "checked_local_targets": len(check.targets), "missing": missing}, ensure_ascii=False))


if __name__ == "__main__":
    main()
