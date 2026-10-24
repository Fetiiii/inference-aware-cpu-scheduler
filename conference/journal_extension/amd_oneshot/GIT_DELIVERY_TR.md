# AMD tek oturum — Git üzerinden teslim

Bu dal **yürütme adayıdır, yürütme izni değildir**. Eski preparation ZIP'ini
almanız/açmanız/birleştirmeniz gerekmez. Eski C03 ham kayıtları yazarlarda zaten
doğrulanmıştır; yeniden göndermeyin. Sizden dönecek tek ZIP yeni özel sonuç arşividir.

## 1. Kodu alın

Depo: https://github.com/Fetiiii/inference-aware-cpu-scheduler.git

Dal yayınlandıktan sonra mevcut temiz klonda (yerel değişiklik varsa durun;
reset/clean kullanmayın):

```bash
git fetch origin
git switch journal-extension-amd
git pull --ff-only
git submodule update --init --recursive
```

Yeni klon için:

```bash
git clone --branch journal-extension-amd https://github.com/Fetiiii/inference-aware-cpu-scheduler.git
cd inference-aware-cpu-scheduler
git submodule update --init --recursive
```

Bu Git komutları yalnız kod/alt modül teminidir; deney aracı indirme yapmaz.
Alt modülün sabit revision'ı `571d0d540df04f25298d0e159e520d9fc62ed121` olmalı.
Yazarın ayrı onayındaki **tam commit** ile `git rev-parse HEAD` eşleşmelidir;
hareketli dal adı tek başına yeterli değildir. Onaydan sonra pull/checkout yapmayın.

## 2. Önceden gerekenler

AMD Ryzen AI 9 HX370/Linux, AC adaptör, sabit güç profili, serin/boş makine;
Python3.10+, bash, git, ps, lscpu, taskset, readelf. Model Git'te yoktur:
`models/Qwen3.5-9B-Q4_K_M.gguf` veya `--model` ile kendi mevcut dosyanız gerekir.
Model SHA256:
`03b74727a860a56338e042c4420bb3f04b2fec5734175f4cb9fa853daf52b7e8`.

Öncelik mevcut `llama.cpp/build-diag/bin/llama-server` ve bilinen marker
kütüphanesidir. Farklı konum için `--server-bin` kullanın. Dosya adı kimlik
kanıtı değildir; preflight hash/kaynak/flags/kütüphaneleri denetler.

Yeni checkout'ta runtime olmayabilir. Yalnız önceden ayrıca izin verilmiş offline
fallback, mevcut C/C++ derleyici + CMake + make + OpenMP bağımlılıkları ile
izole bir dizinde en fazla900s derleme yapabilir. Yeni prospektif runtime kimliği
kaydedilir; tarihsel binary'nin yeniden üretildiği iddia edilmez. Eksik model/
araçlarda durun. Kurulum, model indirme, sudo veya makine ayarı onarımı yoktur.

## 3. Yollar ve salt-okunur preflight

Depo kökünde:

```bash
bash conference/journal_extension/amd_oneshot/amd_oneshot.sh locate
bash conference/journal_extension/amd_oneshot/amd_oneshot.sh preflight --session results/journal_extension_amd_retry01
```

Varsayılan model başka yerdeyse yalnız ikinci komuta `--model /mevcut/model.gguf`
ekleyin. Eski runtime farklı yerdeyse `--server-bin /mevcut/llama-server` ekleyin.
`results/journal_extension_amd_01` ve önceki ZIP'ler korunur; yeni commit ile bu
oturum sürdürülmez, taşınmaz veya üzerine yazılmaz. `retry01` de varsa durun ve
yazarla kullanılmamış yeni bir yol belirleyin. Bu uyumluluk düzeltmesi sonrası
preflight/run/package/verify aynı **yeni** oturum yolunu kullanmalıdır.
Maske veya metodoloji seçmeyin. Locate yalnız aday yolları gösterir; kimlik
doğrulaması preflight'tadır. Prompt otomatik olarak frozen dosyadan alınır.

Preflight inference/derleme başlatmaz. `PASS` tüm mevcut kapıların geçtiğini
gösterir, yürütme izni değildir. `RUNTIME_FALLBACK_PENDING` yalnız runtime kapısı
ertelenmiş demektir (çıkış kodu2): ancak ayrı fallback izni varsa ilerlenebilir.
Diğer `NOT_READY`/hatalarda durun; üretilen arşivi koruyun, elle alternatif denemeyin.

## 4. Yalnız yeni Git-native yazar onayından sonra

Yazarın commit/source/plan/iki schedule/süre kuralı/fallback politikasına bağlı
onay JSON'unu `.je_amd_authorization/hotfix_execution_approval.json` yoluna koyun. Bu
dizin Git tarafından yok sayılır. Eski ZIP onayını kopyalamak geçersizdir.
Örnek/template `authorized=false` olup izin değildir; collaborator bunu açmaz.

```bash
bash conference/journal_extension/amd_oneshot/amd_oneshot.sh run --session results/journal_extension_amd_retry01 --confirm --approval .je_amd_authorization/hotfix_execution_approval.json
```

`--confirm` sizin bilinçli rızanızdır; onayın yerine geçmez. Yazarın ayrıca izinli
fallback'i gerekiyorsa bu komut onu yapar ve yeniden tam preflight bekler.
Toplam bütçe preflight başlangıcından itibaren10800s'dir. Dört hariç teknik smoke
sonrası yalnız sürelerden FULL72, STATIC_SIGNAL48 veya STOP dondurulur.600s
arşiv rezervi ve300s güvenlik payı tahmine dahildir; süre garantisi değildir.
Bilimsel kapı başarısızsa politika atlanır; kötü/negatif/no-trigger sonuç gizlenmez.
Eski `execution_approval.json` kopyalanmaz veya yükseltilmez. Yukarıdaki yeni
onay dosyası ancak hotfix'in tam commit/kaynak/yeni plan hash'leri incelendikten
sonra ayrıca verilebilir; bu doküman veya false template yürütme izni değildir.

## 5. Son arşivi seçme, hata/iptal ve geri gönderim

Tüm özel çıktılar `results/<oturum-adı>_private_<tür>_<benzersiz-id>.zip`
adını kullanır; türler farklı durumları belirtir:

| Çıktı | Anlamı / teslim seçimi |
|---|---|
| `preflight` — `PREFLIGHT-ONLY ARCHIVE` | Yalnız hazırlık snapshot'ı; sonraki deney sonuçları değildir. |
| `run_complete` — `FINAL RUN ARCHIVE` | Tamamlanan deney oturumunun sonuçları; run'ın döndürdüğü TAM yolu kullanın. |
| `run_stopped` veya `run_failed` | Duran/başarısız deney veya hazırlık; mevcut tam/kısmi kanıt korunur, başarılı deney diye sunulmaz. |
| `recovery` — `MANUAL RECOVERY ARCHIVE` | `package` komutunun mevcut oturumdan ürettiği yeni kurtarma snapshot'ı. |

**Dosya adı glob'u, sıralama veya yalnız ZIP'in CRC/hash PASS olmasıyla seçim
yapmayın.** Birden fazla geçerli ZIP olabilir. `run` sonunda yazılan
`FINAL RUN ARCHIVE:` satırındaki kesin dosya deney tesliminin adayıdır; yanındaki
`FINAL RUN STATUS` COMPLETE veya STOPPED durumunu belirtir. Run başlamadan hata
olursa `FAILED RUN/PREPARATION ARCHIVE` yolu yalnız tanı teslimidir. Preflight
arşivini daha sonraki ölçümlerin yerine göndermeyin.

Ctrl-C güvenli duruş ister; aktif istek/derleme kesilirse otomatik tekrar yoktur.
Dosya silmeyin, onay/plan değiştirmeyin, yeni oturum açıp kötü koşuyu değiştirmeyin.
Araç başarısızlığı da arşivler. Güç kesintisi/SIGKILL sonrası yalnız paketleme:

```bash
bash conference/journal_extension/amd_oneshot/amd_oneshot.sh package --session results/journal_extension_amd_retry01
```

Manuel kurtarmada bu komutun `MANUAL RECOVERY ARCHIVE:` satırındaki TAM yolu
kullanın. Araç her üretimde **o kesin dosyaya ait** `VERIFY BEFORE DELIVERY:`
komutunu yazdırır. Göndermeden önce bu komutu aynen çalıştırın; biçimi:

```bash
bash conference/journal_extension/amd_oneshot/amd_oneshot.sh verify "TAM_ARSIV_YOLU.zip" --session results/journal_extension_amd_retry01
```

`TAM_ARSIV_YOLU.zip` bir örnek yer tutucudur; `FINAL RUN ARCHIVE` veya manuel
kurtarma satırındaki gerçek TAM yolu kullanın. `--session` kontrolü zorunludur:
CRC/SHA, dosya envanteri, son ledger durumu ve her tamamlanan isteğin record/raw
referanslarını doğrular. Deney çalıştıysa tamamlanan ölçüm kayıtları ZIP'te olmalı;
`completed_requests` smoke dahil, `measured_requests` smoke hariç sayıdır.
Yalnız archive_elapsed ve boş koordinasyon lock'u bilimsel snapshot karşılaştırmasına
dahil edilmez; tamamlanma/durma olayları veya yeni kayıtlar göz ardı edilmez.
Eski preflight veya sonradan değişmiş snapshot FAIL olur. `verify` tek başına,
`--session` olmadan yalnız bütünlük denetimidir ve teslim hazırlığı kanıtı değildir.

**Yalnız final yerel oturumla eşleşen PASS arşivini** ve onun ekrandaki SHA256'sını
gönderin. Son doğrulamadan sonra oturumda devam/değişiklik olduysa yeniden
`package` üretip onun kesin yolunu doğrulayın. Eski ara arşivleri silmeniz veya
ayrıca göndermeniz gerekmez. Model/build ürünleri arşive girmez. Yeni ham izler ve
ortam kayıtları özel kalmalıdır; public GitHub'a yüklemeyin. Tarihsel C03
karşılaştırmasını yazarlar kendi özel arşivlerinde yapar.

## Yazar için onay modeli

Temiz, commit edilmiş checkout'ta `fingerprint` kaynakları doğrular.
`approval-template --output .je_amd_authorization/review.template.json` yalnız
false şablon üretir. Şablon exact commit + gerekli tracked dosya SHA listesi/
özeti + plan/FULL/STATIC_SIGNAL/rule/patch/policy bağlarını kapsar. Ayrı inceleme
sonrası yazar aynı commit'e özel yeni onay verir. JSON kriptografik imzalı değildir;
güvenilir yazar iletişimi ve hash doğrulaması gerekir. Public tracked dosyalarda
gerçek yürütme izni/secret bulunmaz. Bu dağıtım görevi otomatik izin üretmez.
Paketleme düzeltmesi de yeni commit/kaynak hash'i üretir; eski onay dosyası
taşınmaz veya otomatik yükseltilmez. Yeni incelenmiş commit'e yeni onay gerekir.

## Hotfix bellek ve kaynak kapıları

Model yüklenmeden önce en az **8 GiB MemAvailable** gerekir; bildirilen 6,6 GiB
bu kapıyı geçmez. Normal uygulamaları kullanıcı kapatabilir; araç süreç öldürmez,
swap'ı kapasite olarak saymaz ve bellek bypass seçeneği sunmaz. Model yüklüyken
ayrı **1 GiB** acil duruş tabanı kullanılır; 8 GiB yüklenmiş modele tekrar
uygulanmaz. Offline derlemeden önce **12 GiB**, derleme sırasında da 1 GiB acil
tabanı gerekir. Bu eşikler tarihsel tepe ölçümü değildir; gerekçeler ve belirsizlik
`HOTFIX_COMPATIBILITY.md` içindedir. Sıcaklık, AC, disk ve ağır iş kapıları korunur.
