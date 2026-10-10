# LocalAI — Yerel + Bulut Hibrit Kodlama Asistanı

Terminalde çalışan, Claude Code benzeri bir kodlama asistanı. Basit işleri
**bilgisayarındaki ücretsiz yerel modele** (Ollama), zor işleri **Claude'a**
(bulut) gönderir. Dosya okuyabilir, arayabilir, düzenleyebilir ve komut
çalıştırabilir. Testleri çalıştırıp hataları kendisi düzeltmeye çalışır.

```
 Sen ──► Yönlendirici ──┬──► Yerel model (Ollama, ücretsiz) ──┐
                        │                                      ├──► Araçlar: dosya oku/yaz/düzenle,
                        └──► Claude API (ücretli, güçlü) ──────┘     ara, komut çalıştır
```

- **Yönlendirici:** İstek basit görünüyorsa yerel modele, "refactor", "debug",
  "tüm proje" gibi zor iş işaretleri varsa buluta gönderir. Her kararda nedenini
  gösterir. `@bulut` / `@yerel` etiketiyle elle seçebilirsin.
- **Ortak hafıza:** Yerel modelle konuştuklarını buluta geçince Claude da görür
  (ve tersi).
- **Bütçe:** Bulut harcaması her adımda kaydedilir. Aylık sınır dolunca görev
  durur ve istekler yerel modele düşer.
- **Güvenlik:** Model çalışma klasörünün dışına çıkamaz. Dosya yazma ve komut
  çalıştırma için her seferinde onay ister.

## Kurulum (Windows)

**1. Python 3.11+** — <https://www.python.org/downloads/> (kurulumda
"Add python.exe to PATH" kutusunu işaretle).

**2. Ollama** — <https://ollama.com/download> adresinden indir ve kur. Sonra
modelleri indir:

```powershell
ollama pull qwen2.5-coder:7b
```

**3. Bu projeyi kur:**

```powershell
git clone https://github.com/harunfidan6/Local-Ai.git
cd Local-Ai
python -m venv .venv
.venv\Scripts\activate
pip install -e .
```

**4. (İsteğe bağlı) Claude API anahtarı** — Bulut modu için
<https://platform.claude.com> adresinden bir API anahtarı al:

```powershell
setx ANTHROPIC_API_KEY "sk-ant-..."
```

`setx` yeni açılan terminallerde geçerli olur. Anahtar yoksa asistan sadece
yerel modelle çalışır.

> Linux/macOS: Aynı adımlar, sadece `source .venv/bin/activate` ve
> `export ANTHROPIC_API_KEY=...` kullan.

## Kullanım

Üzerinde çalışmak istediğin projenin klasörüne gidip başlat:

```powershell
cd C:\projelerim\benim-projem
localai
```

```
› bu projede ne var, kısaca anlat
[yerel → basit görünüyor (kısa istek)]
  → list_files {}
    ✓ src/
...
› @bulut login akışındaki hatayı bul ve düzelt, testleri de çalıştır
[bulut → @bulut etiketi]
...
[cloud: claude-opus-5-5 · 6 adım · 48,210 girdi / 2,104 çıktı token · $0.0712]
```

Tek seferlik kullanım: `localai "README'ye kurulum bölümü ekle"`

| Komut | Ne yapar |
|---|---|
| `/mod auto\|local\|cloud` | Yönlendirme modunu değiştirir |
| `/yukselt` | Yerel modelin beceremediği son isteği Claude'a tekrar yaptırır |
| `/maliyet` | Bu ayki token kullanımı ve harcama |
| `/sifirla` | Konuşmayı temizler |
| `/cikis` | Çıkar |

Komut satırı seçenekleri: `-m local` (sadece yerel), `-w KLASÖR` (çalışma
klasörü), `-y` (onay sorma — dikkatli kullan).

## Ayarlar

`localai.example.toml` dosyasını projenin köküne `localai.toml` olarak (ya da
tüm projeler için `~/.localai/config.toml` olarak) kopyalayıp düzenle. Her ayar
ortam değişkeniyle de verilebilir, ör. `LOCALAI_LOCAL_MODEL=qwen2.5-coder:3b`.

En önemlileri:

| Ayar | Varsayılan | Açıklama |
|---|---|---|
| `local_model` | `qwen2.5-coder:7b` | Ollama model adı |
| `num_ctx` | `16384` | Yerel modelin bağlam penceresi (token) |
| `cloud_model` | `claude-opus-5-5` | Daha ucuz seçenek: `claude-sonnet-5-5` |
| `effort` | `high` | Claude'un ne kadar derin düşüneceği; `medium` daha az token harcar |
| `monthly_budget_usd` | `5.0` | Aylık bulut sınırı. `0` = bulut kapalı, `-1` = sınırsız |
| `mode` | `auto` | `auto`, `local` veya `cloud` |

### Projeye özel kurallar: `LOCALAI.md`

Projenin köküne bir `LOCALAI.md` koyarsan içeriği her iki modelin talimatına
eklenir (Claude Code'daki `CLAUDE.md` gibi). Örnek: "Testleri `pytest` ile
çalıştır", "Kod yorumları Türkçe olsun", "`legacy/` klasörüne dokunma". Bu
reponun kendi [`LOCALAI.md`](LOCALAI.md) dosyası bir örnek.

## RTX 4060 (8 GB) + 16 GB RAM için model önerileri

| Kullanım | Model | Not |
|---|---|---|
| Ana model | `qwen2.5-coder:7b` | Tamamen GPU'da, hızlı |
| Daha hızlı ama zayıf | `qwen2.5-coder:3b` | Bağlamı büyütmek istersen |
| Editörde otomatik tamamlama | `qwen2.5-coder:1.5b` | VS Code + Continue eklentisiyle |

Model aileleri hızla güncelleniyor. Ollama kütüphanesinde araç çağırma (tools)
destekleyen daha yeni bir kodlama modeli çıktıysa `local_model` ayarını
değiştirmen yeterli. `nvidia-smi` ile VRAM kullanımını izleyebilirsin. VRAM
doluyorsa `num_ctx` değerini 8192'ye düşür.

## Maliyet

- Yerel model: ücretsiz (sadece elektrik).
- Claude API: kullandıkça öde. 1 milyon token başına Opus 5.5 $4 girdi / $20
  çıktı, Sonnet 5.5 $2 / $10. Prompt önbelleği açık olduğu için tekrar
  gönderilen proje bağlamı yaklaşık %90 ucuza gelir.
- Her görevin sonunda token sayısı ve dolar maliyeti yazılır. Toplam
  `~/.localai/usage.json` dosyasında tutulur, `/maliyet` ile görülür.

Claude Code'u sık kullanacaksan Claude Pro aboneliği (~$20/ay) API'den genelde
daha ucuza gelir. Bu proje, rutin işleri ücretsiz yerel modelde tutup bulut
kullanımını azaltmak için var.

## Nasıl çalışır?

| Dosya | Görev |
|---|---|
| `localai/agent.py` | Ajan döngüsü: model → araç çağrısı → sonuç → model … |
| `localai/tools.py` | Araçlar ve güvenlik (klasör sınırı, onay, argüman doğrulama) |
| `localai/router.py` | Yerel/bulut kararı |
| `localai/session.py` | Oturum, iki model arasında bağlam paylaşımı, maliyet kaydı |
| `localai/providers/ollama.py` | Ollama API. Küçük modellerin metin içine yazdığı araç çağrılarını da yakalar |
| `localai/providers/claude.py` | Claude API: önbellek, reddetme fallback'i, güvenli akış |
| `localai/costs.py` | Fiyat tablosu ve aylık harcama defteri |

## Geliştirme

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

Testler gerçek model gerektirmez. Ollama ve Claude API, sahte yerel
sunucularla taklit edilir.

## Yol haritası

- [ ] Cevapların kelime kelime akması (streaming) — yerel modelde bekleme hissini azaltır
- [ ] Repo indeksleme (embedding + arama) — küçük bağlam penceresine sadece ilgili kodu vermek için
- [ ] Uzun konuşmalarda otomatik özetleme (yerel modelin bağlamı dolunca)
- [ ] Değişiklikleri onaydan önce renkli diff olarak gösterme
- [ ] `git` entegrasyonu: her görevden sonra otomatik commit ve geri alma
- [ ] Yönlendiriciyi kendi kullanım verinle iyileştirme
