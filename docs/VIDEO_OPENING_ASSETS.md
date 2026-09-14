# Steward — Telegram açılış görselleri

Altı kare, aynı kurgusal Northgate konuşmasının ilerleyen halleridir. 2026-09-13'te
yerleşik image_gen aracıyla üretildi ve aşağıdaki klasöre kaydedildi. Altı PNG de
841 × 1870 pikseldir. Mesaj metinleri, sırası, kurgusal isimler ve İngilizce etiketler
görsel olarak kontrol edildi. Video henüz oluşturulmadı.

Referans: `.codex-remote-attachments/01a0818b-24b2-7280-9594-997be8e5a579/a20d7e20-3138-499a-af96-4da8bc53e4aa/1-Photo-1.jpg`.
Referansın Telegram teması, Northgate Demo grup başlığı ve dikey ekran oranı
korunur. Sahnedeki sakinler kurgusaldır. İsimler, avatarlar, saatler ve mevcut
mesajlar kareler arasında aynı kalır; her kare yalnız yeni mesajları ekler ve
gerektiği kadar yukarı kayar.

## Kareler ve metinler

Çıktı klasörü: `artifacts/video/telegram-opening-v1/`.
Toplu paket: `artifacts/video/telegram-opening-v1.zip`.
Kullanılan prompt seti: çıktı klasöründeki `prompts.json`.

| Dosya | Videodaki süre | Bu karede eklenen mesajlar |
|---|---|---|
| `frame-01.png` | 0:00–0:04 | Maya: “Visitor parking is full again. Some cars stay for days.” |
| `frame-02.png` | 0:04–0:08 | Daniel: “Could we set a 24-hour limit?” Nora: “I’d prefer 72 hours for weekend guests.” |
| `frame-03.png` | 0:08–0:12 | Liam: “Has anyone seen a blue parcel?” Olivia: “Pool keys are at reception.” |
| `frame-04.png` | 0:12–0:16 | Maya: “Can we discuss the parking options at a residents’ meeting?” |
| `frame-05.png` | 0:16–0:20 | Maya: “Is anyone following up on this?” |
| `frame-06.png` | 0:20–0:25 | Steward: “I’m here. Let’s turn this into a plan.” |

Altıncı görsel, bot mesajı eklenmiş **Telegram ekranıdır**. Logo geçişi bu kareye
çizilmez; mevcut Steward logosu video kurgusunda daha sonra ayrı katman olarak
eklenir. İlk beş karenin metinleri `docs/VIDEO_PLAN.md` ile aynıdır; altıncı kare
orada belirtilen bot balonu alternatifini kullanır.

## Kısa montaj notu

- Yaklaşık 25 saniye kullan. Dikey görselleri 1920×1080 tuvale oranlarını bozmadan
  yerleştir; metni okunabilir boyutta tut.
- Yeni mesajlarda küçük, kısa yukarı kaydırmalar kullan. Grup başlığı ve yazma
  alanı sabit kalmalı; bütün ekranı kaydırmak yerine sohbet alanı ilerlemeli.
- İlk soruna ve 24/72 saat seçeneklerine okuma zamanı ver. Günlük iki mesajın
  gelişi biraz hızlanabilir; beşinci karede ses ve hareketi sakinleştir.
- Altıncı karede bot balonunu okunacak kadar tut. Logo geçişini kısa yapıp aynı
  otopark konusunun gerçek Steward vaka ekranına geç; toplam açılışı yaklaşık
  25 saniyede bitirmek için geçişi son karenin son kısmıyla örtüştür.
- Açılış boyunca küçük, okunur ve tutarlı konumda **Illustrative conversation**
  etiketi bulunmalı. Etiket görselde yoksa montajda eklenmeli; iki kez eklenmemeli.
  Gerçek ürün kaydına geçildiğinde kaldırılır.

Botun “I’m here. Let’s turn this into a plan.” cümlesi bu **kurgusal açılışın
yazılmış repliğidir**. Uygulamanın bu konuşmaya gerçekten verdiği otomatik yanıt
veya alınmış bir Telegram teslim kanıtı olarak sunulmaz. Gerçek kanal kanıtları
videonun kendi kayıtlarıyla gösterilen bölümünde yer alır.
