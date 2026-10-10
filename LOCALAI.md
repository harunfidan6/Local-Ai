# Bu proje için kurallar

- Dil: Python 3.11+. Sadece standart kütüphane ve `anthropic` paketi kullanılır; yeni bağımlılık ekleme.
- Kod yorumları ve kullanıcıya gösterilen mesajlar Türkçe.
- Değişiklikten sonra testleri çalıştır: `python -m pytest -q`
- Stil kontrolü: `ruff check localai tests`
- Testler gerçek modele bağlanmaz; yeni sağlayıcı kodu için `tests/` altındaki sahte sunucu örneklerini kullan.
