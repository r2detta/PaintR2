# PaintR2

Blender içinde **katmanlı texture painting** — ve sonucu gerçek doku
dosyaları olarak dışa aktarma.

Blender'ın Texture Paint'i aynı anda tek bir görüntünün üstüne boyar; katman
yoktur. Katman mantığı ancak shader'da kurulabilir, o da dışa aktarılabilir
bir dosya vermez. PaintR2 bu ikisini birleştiriyor: üst üste katmanlar boya,
sonucu PNG/EXR olarak al.

Substance Painter'ın yerine geçmek gibi bir iddiası yok. Amaç, Painter'a
gitmeyi gerektirmeyecek kadar basit işleri Blender'da bitirmek.

---

> ### ⚠️ Geliştirme aşamasında
>
> PaintR2 **henüz geliştiriliyor** ve 1.0'a ulaşmadı (şu anki sürüm serisi
> `0.10.x`). Şu an yapabildiği, **basit düzey** texture painting: katman yığını,
> boyama, dolgu, maske ve kanal başına dışa aktarma. Karmaşık bir materyal
> üretim hattının yerini tutmaz.
>
> Pratikte bunun anlamı:
>
> - **Ciddi bir işte tek başına güvenme.** Önemli dosyalarda önce bir kopya
>   üzerinde dene, sık kaydet.
> - **Arayüz ve davranış değişebilir.** Sürümler arasında bazı şeyler yer
>   değiştirebilir.
> - **Hatalar çıkabilir.** Karşılaşırsan
>   [issue aç](https://github.com/r2detta/PaintR2/issues) — ne yaptığını ve
>   Blender'ın konsolunda ne yazdığını eklersen çok yardımcı olur.
>
> Neyin bilerek yapılmadığı aşağıda: [Bilinen sınırlar](#bilinen-sınırlar).

---

## Gereksinimler

- **Blender 5.2 LTS.** Yalnızca. Daha eski sürümlerde çalışmaz.

## Kurulum

**Hazır paketten (önerilen):**
[Releases](https://github.com/r2detta/PaintR2/releases) sayfasından
`PaintR2-<sürüm>.zip` dosyasını indir, Blender'da
`Edit → Preferences → Add-ons → ▾ → Install from Disk` ile seç.

Yeşil **Code → Download ZIP** düğmesiyle inen dosya da aynı şekilde kurulur —
Blender eklentinin kimliğini klasör adından değil manifest'ten okuyor. Tek
farkı, o zip bake edicinin C++ kaynağını da taşıdığı için kurulum birkaç yüz
kilobayt fazla yer kaplar. Releases'teki paket bundan arındırılmıştır.

**Kaynaktan:** depoyu klonla ve eklenti klasörüne bağla:

```bash
git clone https://github.com/r2detta/PaintR2.git
ln -s "$PWD/PaintR2" ~/.config/blender/5.2/extensions/user_default/PaintR2
```

---

## Hızlı başlangıç

1. 3D görünümde boyayacağın mesh'i (ya da birkaçını) seç.
2. `N` ile kenar çubuğunu aç, **PaintR2** sekmesine geç.
3. Çözünürlüğü ve taban rengi seç, **Set Models**'a bas.
   Objenin materyali yoksa `<obje>_Paint` diye bir tane kurulur; varsa
   **adı korunarak yerinde sıfırlanır** — yani elindeki materyalin içeriği
   gider, adı kalır.
4. **Channels** panelinden bu materyalin hangi kanalları taşıyacağını seç
   (Base Color, Roughness, Metallic, Normal, Height).
5. **Open PaintR2**'ye bas. Katmanlar ve boyama ayrı bir pencerede açılır.
6. İşin bitince o pencereden **Bake & Export**.

Kenar çubuğu yalnız hazırlık içindir; asıl iş PaintR2 penceresinde yapılır.

---

## Neler var

**Katmanlar**
- Ekle, sil, sırala, gizle; sürükle-bırak ile taşıma
- 19 karışım modu ve opaklık
- **Gruplar** — iç içe, grubun maskesi ve opaklığı tüm alt ağaca işler
- Katman maskeleri; maskeyi tek başına görüntüleme
- Renk etiketleri, çoklu seçim, kopyala-yapıştır
- Boya, dolgu, prosedürel, adjustment ve node katman tipleri

**Boyama**
- 3D görünümde, Blender'ın kendi fırça motoruyla (basınç, stroke yumuşatma dahil)
- Fırça, darbe, falloff, imleç ve doku ayarlarının tamamı pencerenin içinde
- Simetri: X / Y / Z ve ayna düzlemi

**Dolgu araçları**
- **Polygon Fill** — üçgen ya da poligon
- **Mesh Fill** — bağlı parça ya da obje
- **UV Island Fill** — UV adası
- Kutu ve kement ile çoklu seçim, sürükleyerek doldurma, kenar yumuşatma ve taşma
- Kendi geri alma halkası (`Ctrl Z`)

**Prosedürel ve düzenleme**
- Gradient, Noise, Checker — hem katman hem maske olarak
- Kendi node grubunu katman olarak kullanma
- Adjustment katmanları: HSV, Levels, Bright/Contrast, Gamma, Invert
- Katman başına efekt yığını

**PBR ve dışa aktarma**
- Beş kanal: Base Color, Roughness, Metallic, Normal, Height
- Kanal başına doğru renk uzayı ve bit derinliği; Normal ve Height 16 bit
- Canlı Principled önizleme; ışıklandırma ve gölgeleme modları
- Kanal başına ayrı dosya olarak dışa aktarma

**Mesh map bake**
- Ambient Occlusion, Curvature, Thickness, Position
- High-poly'den Normal, Height ve Bent Normal

**Çoklu obje ve materyal** — bir küme olarak boyanır ve birlikte dışa aktarılır.

---

## Kısayollar

Pencere içinde `F1` ya da `?` tam listeyi gösterir. Sık kullanılanlar:

| Tuş | Ne yapar |
|---|---|
| `↑` `↓` | Üstteki / alttaki katmanı seç |
| `Ctrl ↑` `Ctrl ↓` | Katmanı taşı |
| `M` | Maskeyi kapat / aç |
| `Shift M` | Maskeyi tek başına göster |
| `I` | Katmanı tek başına göster |
| `X` | Katmanı, maskesini ya da seçili filtreyi sil |
| `Ctrl G` | Seçili katmanları grupla |
| `Ctrl C` / `Ctrl V` | Katmanları kopyala / yapıştır |
| `Ctrl Z` | Dolguyu geri al |

---

## Mesh map bake edici hakkında

Mesh map'ler için pakette hızlandırılmış bir bake edici var
(`bin/paintr2_bake`) — şimdilik yalnızca **Linux x86-64** için derlenmiş.

Başka bir platformdaysan bir şey yapman gerekmiyor: eklenti bunu görüp
sessizce **Cycles** yoluna geçer. Haritalar yine üretilir, sadece daha yavaş.
Tek istisna **Bent Normal**: o harita yalnız hızlandırılmış bake edicide var
ve Cycles yolunda üretilemez — atlandığı raporda söylenir.

Bake edicinin kaynağı `PaintR2Bake/` altında, kendi `build.sh`'ıyla birlikte.
Kendi platformun için derlemek istersen:

```bash
cd PaintR2Bake && ./build.sh
```

---

## Bilinen sınırlar

**Bilerek çizilmiş sınırlar** — bunlar eksik değil, böyle tasarlandı:

- **Çözünürlük sonradan değişmez.** Set Models'ta seçilir ve öyle kalır.
  Yeniden ölçeklemek her katmanı ve maskeyi yeniden örneklemek, yani geri
  alınamaz bir kayıp olurdu. Yanlış başladıysan yol yeni bir materyal kurmak.
- **Aynı anda tek PaintR2 penceresi** açılabilir.
- **UDIM yok.** Tek UV karesi.
- Smart material / smart mask yok.

**Henüz yapılmamış olanlar** — bir gün gelebilir, söz değil:

- Klon (clone) aracı.
- Dolgu araçlarında düz renk yerine doku ile doldurma.
- Katmanları renk etiketine göre süzme (etiketin kendisi var, süzme yok).
- Arayüz ikonlarının çoğu hâlâ çizim; yalnız ikisi dosyadan geliyor.

---

## Lisans

GPL-3.0-or-later. Tam metin [LICENSE](LICENSE) dosyasında.

`PaintR2Bake/third_party/tiny_bvh.h` MIT lisanslıdır; telif ve izin notu
dosyanın kendi başlığındadır.
