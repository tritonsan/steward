"""Create revision 2 without modifying the first edit or source recordings."""
from pathlib import Path
import hashlib
import json
import re
import shutil

ROOT=Path(__file__).resolve().parents[1]
OLD=ROOT/'artifacts/video/coverage-session/assembly-v1'
NEW=OLD.parent/'assembly-v2'
PHOTO=ROOT/'.codex-remote-attachments/01a0818b-24b2-7280-9594-997be8e5a579/32c11951-65ce-4e28-80df-35e9105ff65b/1-Photo-1.jpg'
(NEW/'assets').mkdir(parents=True,exist_ok=True)
shutil.copy2(PHOTO,NEW/'assets/telegram-original.jpg')
d=json.loads((OLD/'timeline.json').read_text(encoding='utf8'))
d['version']=2
d['status']='Revision 2: original Telegram message to recorded hosted meeting preparation; narration in progress'
d['chapters'][1]['title']='From the Telegram conversation'
d['shots'][1:4]=[
    {'source':'telegram_original','file':(NEW/'assets/telegram-original.jpg').relative_to(ROOT).as_posix(),
     'duration':12.5,'title':'It starts in the community chat','scope':'Original Telegram message · recorded',
     'screenshot_detail_crop':[64,874,571,1121],
     'note':'Original screenshot shown whole alongside a gently enlarged crop. No synthetic sending animation; all message and timestamp pixels preserved.'},
    {'source':'hosted_meeting','file':'artifacts/video/manual-production/clips/01-hosted-meeting-preparation-1080p.mp4',
     'in':3,'out':9,'duration':6,'title':'A prepared discussion is ready for management','scope':'Recorded hosted demo · meeting preparation'}
]
d['narration'][0]['text']='Residents stay in their usual Telegram group. Steward picks up relevant concerns from the conversation, without a separate reporting form. Here, the original parking message leads to a case, a meeting topic, an agenda, and options. Maintenance follows another path, with vendor outreach and quote comparison. Managers stay in control.'
d['continuity_notes'][0]='The overview shows the supplied original Telegram message, followed by later hosted footage of the matching parking case. A saved processed message record confirms their link. This is an editorial sequence of the original message and later product recording, not a new live send or continuous real-time capture. The repair preview and later detailed walkthrough are separate local examples.'
d['telegram_provenance']={
    'supplied_original':PHOTO.relative_to(ROOT).as_posix(),
    'sha256':hashlib.sha256(PHOTO.read_bytes()).hexdigest(),
    'validation_source':'artifacts/validation/cloud-telegram-current.json',
    'case_id':'case-b0c6986252e248c89fa6881f0ee6746d',
    'recorded_message_time':'2026-09-11T11:32:58Z',
    'screenshot_display_time':'2:32 PM',
    'hosted_source_clip_range':[3,9],
    'scope':'Original text and recorded processed status matched. Hosted video is a later inspection of the corresponding case, not capture of the initial transition.'
}
(NEW/'timeline.json').write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
page=(OLD/'index.html').read_text(encoding='utf8')
page=re.sub(r'(<script type="application/json" id="timelineData">).*?(</script>)',lambda m:m[1]+json.dumps(d,ensure_ascii=False)+m[2],page,flags=re.S)
page=page.replace('Steward — İlk kurgu','Steward — Telegram odaklı kurgu')
page=page.replace('İlk kurgu · 4:45 · 1080p30','Kurgu 2 · Telegram’dan başlayan akış · 4:45')
page=page.replace('Telegram açılışındaki mevcut ses korunuyor. 00:30’dan sonrası seslendirme bekliyor; sağdaki İngilizce metin görüntüyle birlikte ilerleyen kayıt rehberi. Bu sürüm, kurgu ve anlatımı değerlendirmek için hazırlandı.',
    'Web’den mesaj girme sahnesi, gönderdiğin asıl Telegram mesajı ve aynı vakanın canlı uygulamadaki kaydıyla değiştirildi. Açılış sesi korunuyor; 00:30’dan sonrası için güncellenmiş İngilizce anlatım sağda ilerliyor.')
page=page.replace('Ürün sahneleri yerel simülasyon; zaman atlamaları ve ayrı örnekler videoda belirtiliyor.',
    '00:30–00:48.5: asıl Telegram mesajı ve ilgili vakanın kaydedilmiş canlı uygulama görüntüsü. Ayrıntılı devam sahneleri yerel demo örnekleridir.')
page=page.replace('İlk düzenleme · Açılışın ardından yeni seslendirme eklenecek.', 'İkinci düzenleme · Telegram konuşması ön planda. Açılışın ardından yeni seslendirme eklenecek.')
(NEW/'index.html').write_text(page,encoding='utf8')
(NEW/'README_TR.md').write_text('''# Steward — Telegram odaklı kurgu 2

4:45, 1920×1080, 30 fps. [İzle](index.html) · [MP4](steward-first-edit-1080p.mp4) · [İngilizce anlatım](voiceover/VOICEOVER_EN.md)

Bu revizyonda 00:30–00:48.5 değişti:

- 00:30–00:42.5: Kullanıcının gönderdiği asıl Telegram ekranı. Tam ekran görüntüsü ile aynı mesajın büyütülmüş ayrıntısı birlikte görünür; mesaj metni, saat, grup ve teslim işaretleri değiştirilmez.
- 00:42.5–00:48.5: İlgili otopark vakasının kaydedilmiş canlı uygulama görüntüsü; vaka açılıp konu, gündem ve 24/72 saat seçenekleri incelenir.
- 00:48.5 sonrası önceki kurgu korunur. Bakım önizlemesi ve ayrıntılı devam, ayrı yerel demo örnekleridir.

Web mesaj formu ve manuel worker düğmesi ilk genel akış bölümünden çıkarıldı. Anlatım artık sakinlerin kullandıkları Telegram grubunda konuşmasına ve Steward’ın oradaki konuyu takip etmesine odaklanıyor.

Mesajın saklanmış işlenme kaydı `artifacts/validation/cloud-telegram-current.json` içinde bulundu: metin, 11 Eylül 14:32 İstanbul saati ve vaka bağlantısı eşleşir. Görüntü geçişi asıl mesaj ile daha sonraki ürün incelemesini bir araya getirir; yeni canlı mesaj gönderimi değildir. Kaynak ve dosya özeti `timeline.json` içindedir.

Açılışın mevcut sesi korunur. Güncellenen ilk anlatım ve devam bölümleri seslendirme bekler. `assembly-v1`, hamlar ve kaynak görüntüler korunmuştur.
''',encoding='utf8')
print('Revision 2 timeline and review page prepared')
