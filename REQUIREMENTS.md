# Bot İstekleri (tek yerde)

Bu dosya, sohbet boyunca istenen her şeyi, nasıl yorumlandığını ve nerede uygulandığını
listeler. Bir istek değişirse önce burayı güncelleyin.

Durum: ✅ yapıldı · ⚙️ yapıldı ama varsayılanı araştırmaya göre ayarlandı · ⚠️ dikkat

## 1. Strateji

| # | İstek | Durum | Nasıl / Nerede |
|---|---|---|---|
| 1.1 | Smart Money Concept'e göre al-sat (long + short) | ✅ | `smcbot/strategy.py`. Short için Binance USDⓈ-M Futures kullanılıyor (spot'ta short yok). |
| 1.2 | Liquidity sweep | ✅ | PDH/PDL, PWH/PWL, equal high/low (1H+15m), 1H/15m swing, Asya seansı. `smcbot/smc/liquidity.py` |
| 1.3 | Order Block | ✅ | Displacement'tan önceki son ters mum. Giriş bölgesi veya FVG ile çakışma (FVG+OB) |
| 1.4 | FVG (Fair Value Gap) | ✅ | 3 mumluk boşluk, displacement bacağında, doldurulmamış olmalı. `smcbot/smc/fvg.py` |
| 1.5 | "Araştır, daha iyisini bul" | ✅ | Eklenenler: MSS/CHoCH, displacement şartı, 4H/1H trend uyumu, killzone, premium/discount, OTE, SMT divergence, draw-on-liquidity, confluence skoru. 2 yıl / 10 coin üzerinde test edildi → `docs/RESEARCH.md` |
| 1.6 | Günlük işlem (day trade), en az ~1 saatlik; 5-15 dk'lık scalp değil | ✅ | Yön 4H, likidite 1H, giriş 15m. Stop en az 1×ATR(1H) ve en az %0.8 → işlemler ortalama ~11 saat sürüyor. En fazla 24 saat açık kalır. |

## 2. Risk ve hedef

| # | İstek | Durum | Nasıl / Nerede |
|---|---|---|---|
| 2.1 | İşlem başına risk en fazla bakiyenin %4'ü | ⚙️ | Kodda sert sınır 4.0 (`config.py`). **Varsayılan %2**: backtestte skor sonucu tahmin etmedi ve %4 riskte kötü yılda drawdown %60+'ya çıkıyor. `config.yaml > risk.tiers` ile %4'e kadar çıkarılabilir. |
| 2.2 | RR normalde 2:1, en fazla 3:1 | ✅ | `risk.base_rr: 2.0`, `risk.max_rr: 3.0` (kodda da 3.0 ile sınırlı). A+ setup'ta 3R. |
| 2.3 | Zaten işlem varsa yeni işlem açma | ⚙️ | 10 coin istendiği için: **aynı coinde en fazla 1 pozisyon/emir** + **toplamda en fazla 3 pozisyon** (`max_open_positions`) + toplam açık risk en fazla %10. Tek pozisyon isteniyorsa `max_open_positions: 1` yapın. |
| 2.4 | (Eklendi) Hesap koruma | ✅ | Günlük zarar limiti %6, 3 ardışık zararda 6 saat mola, %25 drawdown'da durma, coin başına 60 dk bekleme. |

## 3. Tarama ve zamanlama

| # | İstek | Durum | Nasıl / Nerede |
|---|---|---|---|
| 3.1 | Her 5 dakikada bir işlem taraması | ✅ | 5m mum kapanışından 8 sn sonra. `smcbot/engine.py > cycle()` |
| 3.2 | Tarama bittikten sonra açık pozisyonları optimize et | ✅ | Döngü sırası: temizlik → **tarama** → **optimizasyon**. |
| 3.3 | (Eklendi) Pozisyon asla korumasız kalmasın | ✅ | 15 sn'de bir dolum/kapanış kontrolü; SL konulamazsa pozisyon anında kapatılır; eksik SL/TP her döngüde yeniden konur. |

## 4. Pozisyon optimizasyonu

| # | İstek | Durum | Nasıl / Nerede |
|---|---|---|---|
| 4.1 | Duruma göre stop-loss değiştir | ✅ | +1R'de break-even, 1.5R/2R/2.5R'de kâr kilidi, 15m swing trailing. `smcbot/optimizer.py` |
| 4.2 | TP uzat | ⚙️ | Hazır (momentum + likidite hedefi, en fazla 3R) ama **varsayılan kapalı**: backtestte sonucu düşürdü. `optimizer.extend_tp: true` |
| 4.3 | Anında çıkış | ⚙️ | Hazır: 15m yapı kırılımı, 1H dönüş, zaman stopu. **Varsayılan kapalı** (backtestte her iki yılda da zarar ettirdi). Açık olanlar: 24 saat maksimum süre, haber öncesi koruma, Telegram `/close`. |
| 4.4 | İlk 1 saat erken çıkış yok | ✅ | `min_hold_minutes: 60` (SL/TP her zaman çalışır). |

## 5. Coinler

| # | İstek | Durum | Nasıl / Nerede |
|---|---|---|---|
| 5.1 | En popüler 10 coin | ⚙️ | Piyasa değerine göre: BTC, ETH, XRP, BNB, SOL, DOGE, ADA, TRX, LINK, AVAX. Hacme göre otomatik seçim (`universe.mode: top_volume`) de var ama hacim listesinde XAU/SOXL gibi TradFi kontratları çıkıyor; sadece kripto (`underlyingType=COIN`) ve 180 günden eski listeler alınır. |

## 6. Haber filtresi

| # | İstek | Durum | Nasıl / Nerede |
|---|---|---|---|
| 6.1 | Haber günlerini ve saatlerini bir yerden al | ✅ | ForexFactory haftalık takvimi (bu hafta + gelecek hafta), saatte bir yenilenir, Redis'te cache. `smcbot/news.py` |
| 6.2 | İşleme girmeden önce 4 saat içinde haber var mı kontrol et | ✅ | USD + yüksek etkili haber 4 saat içindeyse (veya son 30 dk içinde olduysa) yeni işlem açılmaz. Veri alınamazsa güvenli mod (işlem yok). |
| 6.3 | (Eklendi) Haber öncesi pozisyon koruması | ✅ | Haberden 30 dk önce kârdaki (+0.5R) pozisyonun stopu girişe çekilir. |

## 7. Telegram

| # | İstek | Durum | Nasıl / Nerede |
|---|---|---|---|
| 7.1 | İşleme girerken mesaj | ✅ | Emir: giriş/SL/TP/RR/risk/kaldıraç/skor + **neden** listesi. Dolum mesajı ayrıca. |
| 7.2 | `/positions` ile açık pozisyonlar | ✅ | `/positions` (ayrıca `/postions`, `/pozisyonlar`, `/p`). PnL, R, SL/TP, süre. |
| 7.3 | Optimizasyon uygulanınca bilgi ver | ✅ | Stop taşındı 🛡️, TP uzatıldı 🎯, anında çıkış ⚡, kapanış 🏁 — her biri sebebiyle. |
| 7.4 | (Eklendi) Diğer komutlar | ✅ | `/status`, `/news`, `/pause`, `/resume`, `/close BTC`, `/help`. Sadece `TELEGRAM_CHAT_ID`'den gelen komutlar kabul edilir. |
| 7.5 | Kaç kâr/zarar ettik (PostgreSQL'den) | ✅ | `/stats`: toplam PnL, kazanma oranı, PF, komisyon, bugün/7/30 gün, en iyi/kötü coin, bulunan/alınan setup |
| 7.6 | Telegram kurulumu kolay olsun | ✅ | `python -m smcbot check` token'ı doğrular ve chat ID'yi bulup gösterir |

## 8. Altyapı

| # | İstek | Durum | Nasıl / Nerede |
|---|---|---|---|
| 8.1 | Binance hesabı | ✅ | `paper` (sanal), `demo` (Binance Demo Trading), `live`. Aralık 2025 Algo Order API değişikliği destekleniyor (ccxt ≥ 4.5). |
| 8.2 | Docker | ✅ | `Dockerfile`, `docker-compose.yml` (bot + Redis + PostgreSQL), healthcheck, otomatik yeniden başlama. |
| 8.3 | Redis (kısa süreli hafıza) | ✅ | Aktif işlemler, risk durumu, işlenmiş setup'lar (TTL), coin bekleme süreleri, haber cache, paper hesap, heartbeat. Redis yoksa `state/state.json`. |
| 8.4 | PostgreSQL: nerede işleme girildi, açılış sebepleri | ✅ | `trades` (sebepler, setup'ın tüm özellikleri, MFE/MAE, süre), `trade_events` (her SL/TP değişikliği), `setups` (**alınmayanlar ve neden alınmadığı**), `scan_runs`, `equity_snapshots`. |
| 8.5 | Sonuçları analiz etmek için büyük loglar | ✅ | `bot_logs` tablosu + dönen dosya logları (`logs/bot.log`, 10×20MB) + hazır analiz view'ları (`v_summary`, `v_by_symbol`, `v_by_score`, `v_by_exit_reason`, `v_by_feature`, `v_optimizer_effect`, `v_rejected_setups`...). `python -m smcbot report` |
| 8.6 | Sadece backend | ✅ | Arayüz yok; kontrol Telegram'dan. |
| 8.6b | Sunucuya kurulum, dışarıya port açmadan | ✅ | Redis portu yok; PostgreSQL sadece 127.0.0.1 (SSH tüneliyle erişim). Sunucuda sadece SSH açık. README > "Sunucuya kurulum" |
| 8.6c | Önce paper'da test, hatalardan öğrenerek geliştirme | ✅ | Varsayılan mod `paper`; tüm işlemler/reddedilen setup'lar PostgreSQL'de |
| 8.7 | Tüm dokümanlar | ✅ | `README.md` (kurulum), `docs/STRATEGY.md` (algoritma), `docs/RESEARCH.md` (backtest sonuçları), bu dosya. |

## Açık konular / karar bekleyenler

- ⚠️ **Canlı para**: 2 yıllık backtestte kanıtlanmış bir avantaj yok (bkz. `docs/RESEARCH.md`). Önce en az 4-8 hafta `paper` veya `demo` önerilir.
- Tek pozisyon mu, 3 pozisyon mu? (2.3)
- Risk %2 mi, %4 mü? (2.1)
- Telegram saat dilimi `Asia/Ashgabat` (UTC+5) olarak ayarlandı; farklıysa `telegram.display_timezone`.
