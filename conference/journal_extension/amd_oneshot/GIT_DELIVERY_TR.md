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
bash conference/journal_extension/amd_oneshot/amd_oneshot.sh preflight
```

Varsayılan model başka yerdeyse yalnız ikinci komuta `--model /mevcut/model.gguf`
ekleyin. Eski runtime farklı yerdeyse `--server-bin /mevcut/llama-server` ekleyin.
Maske veya metodoloji seçmeyin. Locate yalnız aday yolları gösterir; kimlik
doğrulaması preflight'tadır. Prompt otomatik olarak frozen dosyadan alınır.

Preflight inference/derleme başlatmaz. `PASS` tüm mevcut kapıların geçtiğini
gösterir, yürütme izni değildir. `RUNTIME_FALLBACK_PENDING` yalnız runtime kapısı
ertelenmiş demektir (çıkış kodu2): ancak ayrı fallback izni varsa ilerlenebilir.
Diğer `NOT_READY`/hatalarda durun; üretilen arşivi koruyun, elle alternatif denemeyin.

## 4. Yalnız yeni Git-native yazar onayından sonra

Yazarın commit/source/plan/iki schedule/süre kuralı/fallback politikasına bağlı
onay JSON'unu `.je_amd_authorization/execution_approval.json` yoluna koyun. Bu
dizin Git tarafından yok sayılır. Eski ZIP onayını kopyalamak geçersizdir.
Örnek/template `authorized=false` olup izin değildir; collaborator bunu açmaz.

```bash
bash conference/journal_extension/amd_oneshot/amd_oneshot.sh run --confirm --approval .je_amd_authorization/execution_approval.json
```

`--confirm` sizin bilinçli rızanızdır; onayın yerine geçmez. Yazarın ayrıca izinli
fallback'i gerekiyorsa bu komut onu yapar ve yeniden tam preflight bekler.
Toplam bütçe preflight başlangıcından itibaren10800s'dir. Dört hariç teknik smoke
sonrası yalnız sürelerden FULL72, STATIC_SIGNAL48 veya STOP dondurulur.600s
arşiv rezervi ve300s güvenlik payı tahmine dahildir; süre garantisi değildir.
Bilimsel kapı başarısızsa politika atlanır; kötü/negatif/no-trigger sonuç gizlenmez.

## 5. Hata/iptal ve geri gönderim

Ctrl-C güvenli duruş ister; aktif istek/derleme kesilirse otomatik tekrar yoktur.
Dosya silmeyin, onay/plan değiştirmeyin, yeni oturum açıp kötü koşuyu değiştirmeyin.
Araç başarısızlığı da arşivler. Güç kesintisi/SIGKILL sonrası yalnız paketleme:

```bash
bash conference/journal_extension/amd_oneshot/amd_oneshot.sh package
bash conference/journal_extension/amd_oneshot/amd_oneshot.sh verify results/journal_extension_amd_01_private_TIMESTAMP.zip
```

`TIMESTAMP` yerine ekranda verilen gerçek dosya adını kullanın. Gönderilecek dosya
**son PASS doğrulanmış** `results/journal_extension_amd_01_private_*.zip` dosyasıdır;
ekrandaki SHA256'yı da iletin. Eski preflight ara arşivlerini ayrıca göndermeniz
gerekmez. Model/build ürünleri arşive girmez. Yeni ham izler/ortam kayıtları özel
kalmalıdır; public GitHub'a yüklemeyin. C03 eski raw dosyaları burada varmış gibi
işaretlenmez; tarihsel karşılaştırmayı yazarlar kendi özel arşivlerinde yapar.

## Yazar için onay modeli

Temiz, commit edilmiş checkout'ta `fingerprint` kaynakları doğrular.
`approval-template --output .je_amd_authorization/review.template.json` yalnız
false şablon üretir. Şablon exact commit + gerekli tracked dosya SHA listesi/
özeti + plan/FULL/STATIC_SIGNAL/rule/patch/policy bağlarını kapsar. Ayrı inceleme
sonrası yazar aynı commit'e özel yeni onay verir. JSON kriptografik imzalı değildir;
güvenilir yazar iletişimi ve hash doğrulaması gerekir. Public tracked dosyalarda
gerçek yürütme izni/secret bulunmaz. Bu dağıtım görevi otomatik izin üretmez.
