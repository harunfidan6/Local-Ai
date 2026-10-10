# LocalAI — Yerel + Bulut Hibrit Kodlama Asistanı

Terminalde çalışan, Claude Code benzeri bir kodlama asistanı. Basit işleri
**bilgisayarındaki ücretsiz yerel modele** (Ollama), zor işleri **Claude'a**
(bulut) gönderir. Dosya okuyabilir, arayabilir, düzenleyebilir ve komut
çalıştırabilir. Testleri çalıştırıp hataları kendisi düzeltmeye çalışır.

Ayrıca **orkestra modu** var: Bilgisayarındaki Claude Code ve Google
Antigravity aboneliklerini ve yerel modeli bir ekip gibi yönetir. İşi böler,
dağıtır, çapraz denetletir ve kotaları dengeli kullanır
([aşağıda](#orkestra-claude-code--antigravity--yerel-model-birlikte)).

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
  çalıştırma için her seferinde onay ister; değişiklikleri onaydan önce renkli
  diff olarak gösterir. Beğenmezsen `/gerial` son görevin dosya değişikliklerini
  geri alır.
- **Küçük bağlam penceresine uyum:** Yerel modelin konuşması bağlam penceresini
  doldurunca önce eski araç çıktıları, sonra en eski turlar atılır. Böylece
  Ollama'nın sessizce sistem talimatını kesmesi engellenir.

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
| `/gerial` | Son görevin yazdığı/düzenlediği dosyaları eski haline döndürür |
| `/maliyet` | Bu ayki token kullanımı ve harcama |
| `/sifirla` | Konuşmayı temizler |
| `/cikis` | Çıkar |

Komut satırı seçenekleri: `-m local` (sadece yerel), `-w KLASÖR` (çalışma
klasörü), `-y` (onay sorma — dikkatli kullan).

## Orkestra: Claude Code + Antigravity + yerel model birlikte

`localai orkestra`, bilgisayarındaki **Claude Code** ve **Google Antigravity**
aboneliklerini ve **yerel modeli** bir ekip gibi yöneten bir şeftir. Sen hedefi
yazarsın; şef işi parçalara böler, işçilere dağıtır, sonuçları başka bir işçiye
denetletir ve onaylananları birleştirir. Kotası dolan işçiyi dinlendirip işi
diğerine verir, kota açılınca kaldığı yerden devam eder.

```
 Sen: "localai orkestra ekle 'Şifre sıfırlama özelliği ekle'"
            │
            ▼
 Şef ── planlayıcı (Claude Code) ──► 3 alt görev
   │
   ├─ basit iş  ─► yerel model (ücretsiz) ─┐
   ├─ normal iş ─► en az kullanılmış abonelik ├─► testler ─► çapraz denetim ─► "orkestra" dalı
   └─ zor iş    ─► Claude Code             ─┘   (başarısız)  (diğer firma)
                         ▲                          │            │ reddetti
                         └──── geri bildirimle tekrar ◄──────────┘
```

**Nasıl karar verir?**

- **Basit işler** ücretsiz yerel modele gider; abonelik kotası korunur.
- **Zor işler** önce Claude Code'a gider.
- **Normal işler**, son 5 saatte en az kullanılmış aboneliğe gider. Böylece iki
  aboneliğin kotası dengeli tükenir, biri boşta beklemez. Ağırlıklarla
  (`claude_weight`, `antigravity_weight`) birine daha çok iş verebilirsin.
- **Denetçi her zaman uygulayıcıdan farklı firmadan** seçilir: Claude'un işini
  Antigravity, Antigravity'nin işini Claude denetler.
- **Testler denetimden önce** çalışır (`check_command`). Testler geçmiyorsa iş
  denetçiye hiç gitmez, yani denetçinin kotası boşa harcanmaz.
- **Limit dolunca:** Hata mesajındaki sıfırlanma saati okunur, o işçi o saate
  kadar dinlenir ve iş diğerine geçer. Limit hatası deneme hakkından düşmez.
- **Takılınca:** Bir iş 3 denemede olmazsa "insan gerekli" diye işaretlenir.
  Üçüncü denemede farklı bir işçi denenir.

**Güvenlik:**

- Her görev kendi git worktree'sinde ve dalında çalışır. İşçiler paralel çalışır
  ama birbirinin dosyasını ezmez.
- Onaylanan işler sadece `orkestra` dalına birleştirilir. **Ana dalına
  dokunulmaz**; `git log orkestra` ile inceleyip kendin birleştirirsin.
- Planlayıcı ve denetçi salt-okunurdur. Yine de bir dosya değiştirirlerse
  değişiklik geri alınır.
- Claude Code onay sorulmadan sadece dosya düzenleyebilir, `git status/diff/log`
  ve senin `check_command` komutunu çalıştırabilir.

### Kurulum

1. Claude Code kurulu, güncel (`claude update`) ve aboneliğinle giriş yapılmış
   olsun (`claude` komutu çalışıyor olmalı).
2. Antigravity CLI'yi kur ve bir kez giriş yap:
   ```powershell
   irm https://antigravity.google/cli/install.ps1 | iex
   agy
   ```
   Kurulum adresi değişmiş olabilir; güncel yöntem için Antigravity'nin kendi
   belgelerine bak. Masaüstündeki Antigravity uygulaması tek başına yetmez,
   şef `agy` komut satırı aracını kullanır.
3. Projen bir git deposu olmalı ve en az bir commit'i olmalı.
4. İsteğe bağlı: `localai.example.toml`'daki `[orkestra]` bölümünü projenin
   `localai.toml` dosyasına kopyala. En azından `check_command` ayarlamanı
   öneririm (ör. `"python -m pytest -q"`).

> **Antigravity ile ilgili not:** `agy`'nin başsız mod seçenekleri yeni ve
> belgeleri az. Varsayılan olarak Antigravity sadece **planlar ve denetler**.
> Kod da yazmasını istersen `agy --help` çıktısında otomatik onay seçeneğini
> kontrol et ve ayarlara `antigravity_extra_args = ["--dangerously-skip-permissions"]`
> ile `antigravity_can_edit = true` ekle. Bu seçenek Antigravity'nin her komutu
> sormadan çalıştırmasına izin verir.

### Kullanım

```powershell
cd C:\projelerim\benim-projem

# Hedef ekle: şef alt görevlere böler
localai orkestra ekle "Kullanıcı girişine şifre sıfırlama ekle, e-posta ile token gönder"

# Parçalamadan tek görev ekle
localai orkestra ekle --direkt --zorluk basit "README'deki yazım hatalarını düzelt"

# Şefi başlat: durmadan çalışır, yeni görevleri bekler, kota dolunca bekleyip devam eder
localai orkestra calistir

# Başka bir terminalden izle
localai orkestra durum          # görevler + işçilerin durumu ve kullanımı
localai orkestra gunluk         # ne oldu, kim ne yaptı
localai orkestra gunluk 7       # sadece #7'nin geçmişi
localai orkestra tekrar 7       # takılan görevi tekrar sıraya al
localai orkestra iptal 7
```

Şef kapanıp açılsa da hiçbir şey kaybolmaz. Görevler
`~/.localai/orkestra/` altındaki veritabanında durur, yarım kalan işler tekrar
sıraya alınır. Bilgisayar açıkken sürekli çalışsın istersen `localai orkestra
calistir` komutunu ayrı bir terminalde açık bırak ya da Windows Görev
Zamanlayıcısı'na oturum açılışında başlayacak şekilde ekle.

### Aboneliklerin kuralları hakkında

Şef, Claude Code ve Antigravity'nin **resmi, değiştirilmemiş komut satırı
araçlarını** senin kendi girişinle çalıştırır. Hiçbir şifreye veya oturum
anahtarına dokunmaz. Anthropic'in belgelerine göre aboneliğin limitleri
"sıradan, bireysel kullanım" varsayımıyla belirlenir; şef de limitleri aşmaya
çalışmaz, dolunca bekler. Aboneliklerin kuralları değişebilir. Claude Code ve
Antigravity'nin güncel kullanım koşullarını kontrol etmeni öneririm.

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
| `localai/orchestra/conductor.py` | Orkestra şefi: planla → uygula → test → denetle → birleştir |
| `localai/orchestra/policy.py` | Kredi dağıtımı: hangi işi hangi işçi yapar |
| `localai/orchestra/workers.py` | Claude Code / Antigravity / yerel model işçileri, limit tanıma |
| `localai/orchestra/workspace.py` | Git worktree yalıtımı ve birleştirme |
| `localai/orchestra/store.py` | Görev kuyruğu ve kullanım kaydı (SQLite) |

## Geliştirme

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

Testler gerçek model gerektirmez. Ollama ve Claude API sahte yerel
sunucularla, `claude` ve `agy` komutları da sahte programlarla
(`tests/fake_agent.py`) taklit edilir. Git işlemleri gerçek depolarla test edilir.

## Yol haritası

- [x] Cevapların kelime kelime akması (streaming)
- [x] Değişiklikleri onaydan önce renkli diff olarak gösterme
- [x] Son görevi geri alma (`/gerial`)
- [x] Bağlam penceresi dolunca eski mesajları budama
- [ ] Gerçek bilgisayarda (RTX 4060) farklı yerel modellerle deneme ve varsayılanları ayarlama
- [ ] Repo indeksleme (embedding + arama) — küçük bağlam penceresine sadece ilgili kodu vermek için
- [ ] Budanan eski turları silmek yerine yerel modelle özetleme
- [ ] `git` entegrasyonu: her görevden sonra otomatik commit
- [ ] Yönlendiriciyi kendi kullanım verinle iyileştirme
- [x] Orkestra: Claude Code + Antigravity + yerel model iş bölümü, çapraz denetim, kota yönetimi
- [ ] Orkestra için tarayıcıda açılan panel (görev ekleme, canlı durum, kota grafikleri)
- [ ] Gerçek `agy` çıktılarıyla Antigravity işçisini ince ayarlama
