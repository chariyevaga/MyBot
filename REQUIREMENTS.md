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
| 1.7 | Ağırlık (weight) sistemi | ⚙️ | Meta-labeling modeli (OI, funding, taker hacmi, long/short, volatilite, momentum...) 30 coin / 8.496 SMC setup üzerinde test edildi: kazananı ayıramadı (AUC 0.51) → kullanılmıyor. Veriyle desteklenen ağırlıklar: trend'de long tam / short yarım risk ve **son 7 günde >%9 hareket etmiş coinde yarım risk (R3)** — 3 yılın üçünde de düşüşü azalttı. |
| 1.10 | Aylık en az +%2 için optimizasyon | ⚙️ | `research/optimize.py`: 13 varyant, 3 yıl (biri bağımsız). Ortalama ayda +%2 karşılanıyor (%3.4-8.0); her ay +%2 hiçbir ayarda mümkün değil (yılda 3-4 negatif ay). docs/RESEARCH.md > Tur 3 |
| 1.8 | İkinci strateji: trend takibi | ✅ | **Ana strateji.** 4H 20 mum Donchian kırılımı + günlük EMA50 + 3×ATR iz süren stop, TP yok. Backtest: 2024-25 +%48, 2025-26 +%53 (DD ~%20). `smcbot/trend.py` |
| 1.9 | SMC'nin durumu | ⚙️ | Kullanıcı kararı: **gözlem hesabında** (her zaman sanal, gerçek emir yok) çalışmaya devam eder, ayrı istatistik tutar. `strategies.smc.account: shadow` |
| 1.6 | Günlük işlem (day trade), en az ~1 saatlik; 5-15 dk'lık scalp değil | ✅ SMC / ⚙️ Trend | Yön 4H, likidite 1H, giriş 15m. Stop en az 1×ATR(1H) ve en az %0.8 → işlemler ortalama ~11 saat sürüyor. En fazla 24 saat açık kalır. |

## 2. Risk ve hedef

| # | İstek | Durum | Nasıl / Nerede |
|---|---|---|---|
| 2.1 | İşlem başına risk en fazla bakiyenin %4'ü | ⚙️ | Kodda sert sınır 4.0 (`config.py`). **Varsayılan %2**: backtestte skor sonucu tahmin etmedi ve %4 riskte kötü yılda drawdown %60+'ya çıkıyor. `config.yaml > risk.tiers` ile %4'e kadar çıkarılabilir. |
| 2.2 | RR normalde 2:1, en fazla 3:1 | ✅ | `risk.base_rr: 2.0`, `risk.max_rr: 3.0` (kodda da 3.0 ile sınırlı). A+ setup'ta 3R. |
| 2.3 | Zaten işlem varsa yeni işlem açma | ⚙️ | Aynı coinde en fazla 1 pozisyon. Trend: en fazla 6 pozisyon, toplam risk %8. SMC (gözlem): en fazla 3. |
| 2.5 | Trend riski | ✅ | Kullanıcı kararı: **long %1, short %0.5**. Trend pozisyonları **birkaç gün** açık kalabilir (kullanıcı onayladı; 24 saat sınırıyla avantaj kayboluyordu). |
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
| 5.2 | 30 coin (düşük maliyetli / önerilen) | ✅ | 2 yıldan uzun listelenmiş, son 3 ayda en yüksek hacimli 30 kripto; aşırı oynak küçük coinler hariç (`research/universe.py`). Komisyon yüzdesi tüm coinlerde aynı; farkı kayma (likidite) yapar. |
| 5.1 | En popüler 10 coin | ⚙️ | Piyasa değerine göre: BTC, ETH, XRP, BNB, SOL, DOGE, ADA, TRX, LINK, AVAX. Hacme göre otomatik seçim (`universe.mode: top_volume`) de var ama hacim listesinde XAU/SOXL gibi TradFi kontratları çıkıyor; sadece kripto (`underlyingType=COIN`) ve 180 günden eski listeler alınır. |

## 6. Haber filtresi

| # | İstek | Durum | Nasıl / Nerede |
|---|---|---|---|
| 6.1 | Haber günlerini ve saatlerini bir yerden al | ✅ | ForexFactory haftalık takvimi (bu hafta + gelecek hafta), saatte bir yenilenir, Redis'te cache. `smcbot/news.py` |
| 6.2 | İşleme girmeden önce 4 saat içinde haber var mı kontrol et | ✅ SMC / ⚙️ Trend | SMC: USD + yüksek etkili haber 4 saat içindeyse (veya son 30 dk içinde olduysa) yeni işlem açılmaz. Trend: varsayılan **kapalı** (çok günlük pozisyonlarda test edilemedi) → `strategies.trend.news_filter: true` ile açılabilir. |
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

- ⚠️ **Canlı para**: Trend stratejisi backtestte iki yılda da pozitif, ama canlı (paper) sonuçlar henüz yok. Önce en az 4-8 hafta `paper`, sonra kısa bir `demo`, sonra 100 USD ile `live` planlandı.
- **100 USD ile canlı**: trend stopları ortalama fiyatın %8'i uzakta; %1 riskle pozisyon ~12 USD olur. BTC (en az 50 USDT) ve ETH (en az 20 USDT) açılamaz, altcoinler açılabilir.
- **"10-15" beklentisi**: aylık getiri mi, maksimum düşüş toleransı mı? Düşüş toleransıysa trend riskini %0.5'e indirmek gerekir (backtestte %1 ile düşüş ~%20).
- Telegram saat dilimi `Asia/Ashgabat` (UTC+5) olarak ayarlandı; farklıysa `telegram.display_timezone`.
