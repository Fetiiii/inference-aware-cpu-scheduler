# TASLAK — henüz göndermeyin

Yazar bilimsel/teknik inceleme ve ayrı yürütme onayını tamamlamadan collaborator'a gönderim yok.

---

Merhaba, AMD HX370 üzerinde tek oturumluk özel paket hazırladık. Eski C03 kayıtları doğrulanmış halde pakette. İndirme, kurulum, sudo veya sistem ayarı değişikliği yok. Orijinal runtime öncelikli; eksik/uyumsuzsa yalnız önceden ayrıca izinli sınırlı yerel fallback derlemesi yapılabilir.

Yazarın önceden onayladığı iki schedule ve süre kuralının Türkçe README'sini izle. İlk komut yalnız preflight; derleme/ölçüm hash-bağlı ayrı onay ve --confirm gerektirir. CPU maskesi veya oturum içi metodolojik karar yok. Model/güç/ısı/yük sorunu veya onaysız runtime eksikliği güvenli duruş/arşivdir; elle alternatif deneme.

Süre garantisi yok. Dört teknik smoke sonrası sonuç metriklerine bakmadan FULL72, STATIC_SIGNAL48 veya STOP sabitlenir; sonradan değişmez. Negatif/yavaş sonuçlar da geçerli; daha iyi sonuç için tekrar başlatma. Hata kayıtlarını silme.

Sonunda yalnız son doğrulanmış özel ZIP'i geri göndermen yeterli. Ham kanıt özel yollar içerebilir; public GitHub'a yükleme. Model ağırlıkları ve binaryler son sonuç arşivinde bulunmaz.

---

Bu mesaj gönderilmedi. Her iki schedule, süre kuralı, fallback izni ve plan/source hash yazarca ÖNCEDEN onaylanmalıdır. Pakette yalnız authorized=false şablonu vardır.
