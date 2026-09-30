# SMC Trading Bot (Binance USDⓈ-M Futures)

Smart Money Concepts (liquidity sweep → market structure shift → FVG / Order Block) ile
10 büyük coinde günlük (day trade) işlem arayan, pozisyonları optimize eden, Telegram'dan
bildirim ve komut alan, Redis + PostgreSQL ile her şeyi kaydeden bir backend.

- İstekler ve kararlar: [REQUIREMENTS.md](REQUIREMENTS.md)
- Algoritma: [docs/STRATEGY.md](docs/STRATEGY.md)
- Backtest ve araştırma sonuçları: [docs/RESEARCH.md](docs/RESEARCH.md)

> ⚠️ **Önemli:** 2 yıllık, 10 coinlik backtestte bu strateji (ve test edilen ~30 varyantı)
> komisyon sonrası **tutarlı bir kâr göstermedi** (bir yıl +%17, önceki yıl −%9).
> Bot varsayılan olarak `paper` modda gelir. Gerçek parayla çalıştırmadan önce en az
> 4-8 hafta paper/demo sonuçlarını `python -m smcbot report` ile inceleyin.
> Bu bir yatırım tavsiyesi değildir.

---

## Nasıl çalışır (özet)

```
her 5 dakikada (5m mum kapanışı + 8 sn)
 ├─ 1. temizlik   : bakiye, risk limitleri, haber takvimi, süresi dolan emirler
 ├─ 2. TARAMA     : 10 coin × (4H yön, 1H likidite, 15m sweep+MSS+FVG/OB) → uygun setup'a emir
 └─ 3. OPTİMİZE   : her açık pozisyon için break-even / kâr kilidi / trailing / 24s limiti / haber koruması
her 15 saniyede  : emir doldu mu? → SL/TP'yi borsaya koy · pozisyon kapandı mı? → kaydet + Telegram
```

Bir işlemin açılması için (varsayılan ayarlar):
1. 4H trend işlem yönünde,
2. önemli bir likidite seviyesi süpürülmüş (1H swing, equal high/low, önceki gün/hafta high/low),
3. sweep sonrası 15m'de displacement'lı (≥1 ATR gövde) market structure shift + FVG/OB,
4. London (07-10 UTC) veya New York (12-15 UTC) killzone'unda,
5. 4 saat içinde yüksek etkili USD haberi yok,
6. risk limitleri müsait (coin başına 1, toplam 3 pozisyon, günlük zarar limiti vb.).

Stop: sweep ucunun ötesi (en az 1×ATR(1H) ve %0.8) · Hedef: 2R (A+ setup'ta 3R) · Risk: %2 (maks. %4).

---

## Kurulum

### 1) Ayarlar

```bash
cp .env.example .env
```

`.env` içinde:

| Değişken | Açıklama |
|---|---|
| `BOT_MODE` | `paper` (sanal, anahtar gerekmez) · `demo` (Binance Demo Trading) · `live` (gerçek para) |
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | demo veya live için. Sadece **Futures** izni verin, **Withdraw kapalı**, IP kısıtlaması önerilir. Hesap **One-way** pozisyon modunda olmalı. |
| `TELEGRAM_BOT_TOKEN` | @BotFather'dan |
| `TELEGRAM_CHAT_ID` | Bota bir mesaj atın, `https://api.telegram.org/bot<TOKEN>/getUpdates` → `chat.id` |
| `POSTGRES_PASSWORD` | Değiştirin |

Strateji/risk ayarları: [config.yaml](config.yaml) (Türkçe açıklamalı).

### 2) Docker ile çalıştırma (önerilen)

```bash
docker compose up -d --build
```

```bash
docker compose logs -f bot
```

Servisler: `bot` (healthcheck'li), `redis` (AOF kalıcı), `postgres` (16). Veriler Docker
volume'larında kalır; `state/`, `logs/`, `data/`, `backtests/` klasörleri host'a bağlıdır.

Durdurma (pozisyonların SL/TP emirleri borsada kalır):

```bash
docker compose stop bot
```

### Telegram botunu bağlama

1. Telegram'da **@BotFather** → `/newbot` → isim → kullanıcı adı (`...bot` ile bitmeli) → token'ı `.env`'de
   `TELEGRAM_BOT_TOKEN=` satırına yazın. Token'ı kimseyle paylaşmayın; sızarsa BotFather'da `/revoke`.
2. Telegram'da kendi botunuzu açıp **/start** yazın.
3. Chat ID'yi bot kendisi bulur:

```bash
docker compose run --rm bot python -m smcbot check
```

4. Çıktıdaki `TELEGRAM_CHAT_ID=...` satırını `.env`'e yazın, `check`'i tekrar çalıştırın (test mesajı gelir), sonra:

```bash
docker compose up -d --force-recreate bot
```

### Sunucuya kurulum (VPS)

- Dışarıya **hiçbir port açmaya gerek yok**. Bot sadece dışarıya bağlanır (Binance, Telegram, haber sitesi).
  Redis'in portu yok; PostgreSQL sadece `127.0.0.1`'e bağlı (internetten erişilemez).
- Güvenlik duvarında sadece SSH (22) açık kalsın.
- Kurulum: Docker + Docker Compose kurun, proje klasörünü kopyalayın (`.env` dahil, git'e koymadan), `docker compose up -d --build`.
- Sonuçlara bakmak: Telegram'da `/stats`, sunucuda `docker compose exec bot python -m smcbot report`,
  veya bilgisayarınızdan SSH tüneli ile (DBeaver/TablePlus → `localhost:5432`):

```bash
ssh -L 5432:127.0.0.1:5432 kullanici@sunucu-ip
```

### 3) Docker'sız (geliştirme)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/python -m smcbot run
```

Redis/PostgreSQL yoksa bot otomatik olarak `state/state.json` ve `state/trades.csv`'ye yazar.

---

## Komutlar

| Komut | Ne yapar |
|---|---|
| `python -m smcbot run` | Botu çalıştırır (mod: `.env`/`config.yaml`) |
| `python -m smcbot run --once` | Tek döngü (tarama + optimizasyon) çalıştırır ve çıkar |
| `python -m smcbot scan --hours 24` | Son 24 saatteki setup'ları gösterir, emir göndermez |
| `python -m smcbot backtest --days 365` | Geçmiş veride test (5m veri `data/`'ya indirilir) |
| `python -m smcbot backtest --days 365 --compare` | Optimizasyonlu / optimizasyonsuz karşılaştırma |
| `python -m smcbot backtest --set strategy.entry_mode=limit --set risk.base_rr=3` | Ayar değiştirerek deney |
| `python -m smcbot backtest --save-db` | Backtest işlemlerini PostgreSQL'e yazar (aynı analiz view'ları çalışır) |
| `python -m smcbot check` | Binance, Redis, PostgreSQL, haber, Telegram bağlantı testi |
| `python -m smcbot news` | Yaklaşan önemli haberler ve şu an işlem engeli var mı |
| `python -m smcbot report` | PostgreSQL'den performans analizi (tüm view'lar) |

Docker içinde: `docker compose exec bot python -m smcbot report`

### Telegram

| Komut | |
|---|---|
| `/positions` | Açık pozisyonlar (PnL, R, SL/TP, süre) ve bekleyen emirler |
| `/status` | Mod, bakiye, bugünkü PnL, açık risk, yeni işlem engeli (haber/limit), son/sonraki tarama |
| `/stats` | PostgreSQL'den performans: toplam PnL, kazanma oranı, PF, komisyon, bugün/7/30 gün, en iyi/kötü coin |
| `/news` | Önümüzdeki 72 saatteki önemli haberler |
| `/pause` · `/resume` | Yeni işlem açmayı durdur / devam et (açık pozisyonlar yönetilmeye devam eder) |
| `/close BTC` | Bot'un BTC pozisyonunu (veya bekleyen emrini) kapatır |

Otomatik mesajlar: yeni işlem (sebepleriyle) · dolum · stop taşındı · TP uzatıldı · anında çıkış ·
kapanış (PnL, R, süre) · risk limiti devreye girdi · hatalar.

---

## Analiz (PostgreSQL)

| Tablo / View | İçerik |
|---|---|
| `trades` | Her işlem: sebepler, süpürülen seviyeler, setup'ın tüm özellikleri (`setup` JSONB), giriş/çıkış, PnL, R, MFE/MAE, süre |
| `trade_events` | ORDER_PLACED, FILLED, SL_MOVED, TP_EXTENDED, OPTIMIZER_EXIT, CLOSED... |
| `setups` | Bulunan **her** setup; alındı mı, alınmadıysa neden (haber, limit, fiyat kaçtı...) |
| `scan_runs` | Her 5 dk'lık tarama: süre, bulunan setup, gönderilen emir, engel sebebi |
| `equity_snapshots` | 15 dk'da bir bakiye / açık risk |
| `bot_logs` | INFO ve üstü tüm loglar |
| `v_summary`, `v_by_symbol`, `v_by_score`, `v_by_exit_reason`, `v_by_poi`, `v_by_swept_level`, `v_by_feature`, `v_by_hour`, `v_optimizer_effect`, `v_rejected_setups` | Hazır analizler |

Örnek: hangi özellik işe yarıyor?

```sql
SELECT * FROM v_by_feature WHERE mode = 'paper';
```

PostgreSQL host'ta sadece `127.0.0.1:5432`'den erişilebilir (DBeaver, TablePlus vb. ile bağlanabilirsiniz).

---

## Proje yapısı

```
smcbot/
  smc/structure.py     swing pivotları, BOS / CHoCH (ileriye bakmayan)
  smc/liquidity.py     likidite seviyeleri + ilk dokunuş
  smc/fvg.py           fair value gap
  strategy.py          sweep → MSS → FVG/OB setup tespiti + confluence skoru
  optimizer.py         pozisyon optimizasyon kuralları (saf mantık, backtest ile ortak)
  risk.py              pozisyon büyüklüğü, kaldıraç, günlük/ardışık/drawdown limitleri
  engine.py            canlı döngü, emir yönetimi, Telegram komutları
  backtest.py          aynı kurallarla olay-tabanlı backtest
  news.py              ekonomik takvim filtresi
  telegram.py          bildirim + komutlar
  brokers/             binance.py (demo/live), paper.py (sanal hesap)
  storage/             kv.py (Redis), db.py + schema.sql (PostgreSQL)
tests/                 birim testleri (look-ahead testi dahil)
```

Testler:

```bash
.venv/bin/python -m pytest -q
```

## Canlıya geçiş kontrol listesi

1. `paper` modda en az 4-8 hafta, `report` sonuçları pozitif mi?
2. `demo` modda emir/SL/TP akışı Binance Demo'da doğru mu (`/positions`, borsadaki koşullu emirler)?
3. Risk: canlıda %0.5-1 ile başlayın; `max_open_positions`, `max_daily_loss_pct` gözden geçirin.
4. API anahtarı: sadece Futures, Withdraw kapalı, IP kısıtlı. Hesap One-way modunda.
5. `BOT_MODE=live`, `docker compose up -d`, Telegram'da başlangıç mesajını görün.
