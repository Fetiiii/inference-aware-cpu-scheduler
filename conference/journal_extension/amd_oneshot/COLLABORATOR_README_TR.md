# AMD tek oturum paketi — JE-AMD-01-v3 / son hazırlık

> Git-native dağıtımda güncel ve tek operatör kılavuzu **GIT_DELIVERY_TR.md**'dir.
> Aşağıdaki metin önceki özel ZIP hazırlığının tarihsel kaydıdır; artık ZIP almanız
> veya ham C03 kayıtlarını collaborator tarafına taşımanız gerekmez. Eski ZIP
> onayı yeni Git commit'ini yetkilendirmez.

**Henüz çalıştırma/gönderim onayı yok.** Yazar ayrı hash-bağlı onay vermeden derleme/ölçüm başlamaz. execution_approval.template.json ONAY DEĞİLDİR; authorized=false kalır, collaborator bunu kendisi etkinleştirmez. Paket model/program indirmez, paket kurmaz, sudo kullanmaz veya sistem ayarı değiştirmez. Derleme yalnız önceden ayrıca izinli yerel runtime fallback'iyle mümkündür.

## Paket ne içeriyor?

Özel paket, eski C03'ün doğrulanmış orijinal ZIP'ini zaten içerir. Eski kayıtları yeniden toplamanız istenmez. Ham kayıtlar özel kalmalıdır; bu paketi public GitHub'a yüklemeyin. Yeni sonuçlar eski C03'e eklenmez: `results/journal_extension_amd_01/` kullanılır.

Gerekli mevcut kurulum: AMD Ryzen AI 9 HX 370, Linux, Python3.10+, bash/git/ps/lscpu/taskset/readelf; repo ve pinned llama.cpp yerel git nesnesi; frozen prompt ve Qwen3.5-9B-Q4_K_M.gguf. Öncelik orijinal diagnostic binary+bilinen marker kütüphanesidir. Bunlar eksik/uyumsuzsa yalnız yazarın ayrıca izin verdiği fallback kullanılabilir; mevcut C/C++ derleyicileri, CMake, make ve OpenMP geliştirme bağımlılıkları gerekir. Eksik bağımlılık/modelde durur, otomatik kurmaz. CPU maskesi seçmeniz gerekmez.

## Yalnız ayrı onaydan sonra

ZIP'i ayrı bir klasöre açın; mevcut depoyu/branch'i değiştirmeyin. Mevcut collaborator depo kökünde çalışın. PAKET açılan klasörün mutlak yoludur. Server ve model yollarını yazardan gelen talimata göre verin; metodolojik seçim yapmanız gerekmez.

```bash
"$PAKET/amd_oneshot.sh" preflight --repo "$PWD" --server-bin /path/to/llama-server --model /path/to/Qwen3.5-9B-Q4_K_M.gguf
"$PAKET/amd_oneshot.sh" run --confirm --session "$PWD/results/journal_extension_amd_01" --approval execution_approval.json
```

Preflight inference veya derleme başlatmaz. Önce eski ZIP'i doğrular/korur, sonra model/kaynak/runtime/makineyi denetler. PASS ölçüm yetkisi değildir. Yalnız runtime uyumsuzluğu varsa RUNTIME_FALLBACK_PENDING oluşabilir: run komutu ancak ayrı fallback izniyle en fazla900s yerel derleme ve yeniden tam preflight yapar. Başka kapı başarısızsa veya fallback izni yoksa durur/arşivler; elle onarım/farklı dosya denemeyin. Yazar onayı iki schedule'ı, süre kuralını, fallback politikasını ve plan/source hash'ini ÖNCEDEN bağlamalıdır; oturum sırasında karar gerekmez.

Adaptör takılı, makine serin ve güç profili sabit olmalı. Başka inference, derleme veya CPU-ağır işler kapalı olmalı; araç bunları kendisi öldürmez. Normal tarayıcı/chat tek başına otomatik başarısızlık nedeni değildir; gerçek CPU yükü denetlenir.

## Oturum boyunca

Tur/deney/detector seçmeniz veya arada yazarlara mesaj atmanız gerekmez. Tam dört hariç teknik smoke sonrası yalnız teknik süreler FULL72, STATIC_SIGNAL48 veya STOP seçimini otomatik dondurur. FULL sığıyorsa seçilir; sığmazsa STATIC_SIGNAL denenir; ikisi de sığmazsa arşivlenir. TTFT/ITL/TPS seçimde kullanılmaz. Tier sonradan değiştirilemez. FULL'de bilimsel kapı geçmezse politika NOT_ELIGIBLE olarak atlanır,48 istek kalır; negatif sonuç gizlenmez. Geçerli EXTERNAL no-trigger korunur. Isınma/smoke nihai performans örnekleri değildir.

Süre garantisi yok.10800s bütçe preflight başlangıcından itibaren derleme/hazırlık/smoke/analiz/arşivi kapsar; tahmine600s arşiv+300s güvenlik payı dahildir. Örnek sayısı/karar kuralı değiştirilmez. Geçerli yavaş/negatif/termal uyarılı sonuçlar değiştirilmez.

İptal için Ctrl-C kullanın. İstekler arasında kesilme güvenliyse aynı frozen oturum devam edebilir; aktif istek kesildiyse otomatik tekrar yoktur. Hata/iptal kayıtlarını silmeyin. Paket arşiv oluşturur; güç kesilmesi/SIGKILL gibi durumlarda sonradan yalnız paketleme yapın:

```bash
"$PAKET/amd_oneshot.sh" package --session "$PWD/results/journal_extension_amd_01"
"$PAKET/amd_oneshot.sh" verify /path/to/son_ozel_arsiv.zip
```

Yalnız **son doğrulanmış tek özel ZIP'i** geri gönderin. Ekrandaki SHA256'yı mesajınıza ekleyebilirsiniz; yan checksum dosyası kolaylıktır, ikinci zorunlu teslim değildir. Modeller/binary dosyaları son arşive girmez; yeni ve eski ham kanıtlar, başarısız denemeler, plan ve analizler girer. Ortam kayıtları özel yollar içerebilir; yayın redaksiyonu ayrı kopyada yapılır.
