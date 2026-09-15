# İkon klasörü

PaintR2'nin arayüz ikonlarını kendi çizdiklerinle değiştirebilirsin.

Buraya konan bir PNG, o ikonun yerine geçer. Dosya yoksa eklentinin kendi
çizimi kullanılır — yani **seti parça parça değiştirebilirsin**, eksik dosya
hata değildir. İki ikonluk bir klasör de çalışan bir klasördür.

## Format

- **PNG, RGBA.** `.ico` kullanılamaz — Blender o uzantıyı tanımıyor.
- **64x64**, ikon başına tek dosya. Ölçek varyantı tutulmuyor.
- **Beyaz siluet + saydamlık.** Renk çizim sırasında veriliyor; üzerine
  gelince, soluk ve vurgulu hallerin hepsi aynı dosyadan çıkıyor. Renkli bir
  dosya koyarsan o ikon tonlanamaz, hep aynı renkte kalır.
- Kenarda biraz boşluk bırak: mark 64 piksellik karenin kenarından ~4-8 piksel
  içeride dursun.

## Dosya adları

Dosyanın adı, değiştirdiği ikonun adıdır. Geçerli adların tamamı:

```
adjustment     arrow_down     arrow_up       brush          check
chevron_down   chevron_left   chevron_right  cross          cube
effects        eye_closed     eye_open       fill_bucket    folder
gradient       mask_square    mesh_fill      mirror         nodes
plus           polygon_fill   pressure       select_box     select_lasso
stroke         swap           trash          uv_island_fill wireframe
```

Hiçbirine uymayan bir PNG sessizce yok sayılır.

## Kendi klasörünü kullanmak

Eklentiyi güncellediğinde dosyaların silinmesin diye, ikonlarını paketin
dışında bir klasörde tutabilirsin:

`Edit → Preferences → Add-ons → PaintR2 → Icon Folder`

Orayı gösterdiğinde ikonlar önce senin klasöründen okunur; bir ad için orada
dosya yoksa paketinki, o da yoksa çizim kullanılır.

## Değiştirdikten sonra

PaintR2 penceresini kapat-aç, yeter. Blender'ı yeniden başlatman gerekmiyor.

## Şu an bu klasörde olanlar

```
brush.png   fill_bucket.png
```

Kalan ikonlar eklentinin kendi çizimlerinden geliyor.
