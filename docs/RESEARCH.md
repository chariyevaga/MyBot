# Araştırma ve Backtest Sonuçları

## Özet (güncel)

| Strateji | 2024-25 | 2025-26 | Karar |
|---|---|---|---|
| SMC (sweep → MSS → FVG/OB), 10 coin | −%9 | +%17.6 | Tutarlı avantaj yok → **gözlem hesabında** (sanal) |
| SMC + ağırlık modeli (meta-labeling), 30 coin | model AUC 0.51 | model AUC 0.52 | Model kazananı ayıramadı → kullanılmıyor |
| **4H trend takibi**, 30 coin, long %1 / short %0.5 risk | **+%48.0** (DD %19.5) | **+%52.6** (DD %19.9) | **Ana strateji** (önce paper) |

Detaylar: [Tur 2](#tur-2-30-coin-ağırlık-sistemi-ve-trend-takibi) (aşağıda), ilk tur SMC araştırması hemen altında.

---

# Tur 1: SMC araştırması

Tarih: 2026-09-30 · Veri: Binance USDⓈ-M, 5 dakikalık mumlar, 10 coin (BTC, ETH, XRP, BNB, SOL,
DOGE, ADA, TRX, LINK, AVAX), 2024-09-30 → 2026-09-30.

**Kısa sonuç:** Test edilen SMC kural setlerinin hiçbiri, komisyon sonrası iki yılda da tutarlı
kâr üretmedi. En iyi (varsayılan) ayar bir yıl +%17.6, önceki yıl −%9. İstatistiksel olarak
anlamlı değil (t ≈ 0.75 ve −0.44). Bot bu yüzden `paper` modda gelir.

## Yöntem

- Backtester (`smcbot/backtest.py`) canlı botla **aynı** setup tespiti, risk hesabı ve optimizer
  kodunu kullanır; 5m mumlarla ilerler: mum içi dolum/SL/TP → tarama → optimizasyon.
- Muhafazakâr varsayımlar: aynı 5m mumda hem SL hem TP → SL; stop boşlukta mum açılışından dolar;
  stop ve market çıkışlarda %0.02 kayma; komisyon maker %0.02, taker %0.05.
- Simüle edilmeyen: haber filtresi (geçmiş takvim yok), funding ödemeleri.
- Look-ahead testi: `tests/test_smc.py`.
- Aşırı uydurmayı önlemek için: filtreler **önceden** SMC teorisine göre seçildi, sonra
  1. yıl (2025-26) üzerinde bakıldı ve **hiç kullanılmamış** önceki yıl (2024-25) ile doğrulandı.

## 1. Tek tek setup analizi (2025-26, gevşek filtre)

Her setup portföy kısıtı olmadan ayrı simüle edildi (24 saat içinde SL veya hedef). Değerler
işlem başına ortalama **net R** (komisyon dahil), hedef 2R.

| Model | Örnek | Ortalama R (2R hedef) |
|---|---|---|
| Sweep → MSS → FVG CE'ye **limit** emir | 1484 setup, 406 dolum (%27) | **−0.21** |
| Aynı setup'lar, MSS kapanışında **market** giriş | 1484 | **−0.055** |
| Trend devamı: 4H/1H yönünde BOS + FVG (market) | 22 434 | −0.07 |
| Trend devamı, FVG'ye limit | 3 565 | −0.125 |

Limit emirlerin kötü olmasının sebebi **ters seçilim**: fiyatın geri gelip limit emri doldurduğu
setup'lar ortalamada zayıf olanlar; güçlü hareketler geri dönmeden gidiyor.

Özellik bazında (market giriş, eğitim 2025-10→2026-05 / test 2026-05→2026-09):

| Filtre | Eğitim n / R | Test n / R |
|---|---|---|
| Hepsi | 912 / −0.02 | 572 / −0.11 |
| 4H uyumlu | 449 / −0.02 | 295 / −0.07 |
| 4H + killzone | 212 / +0.17 | 179 / −0.09 |
| **4H + killzone + önemli seviye (ağırlık ≥10)** | 90 / **+0.18** | 74 / **+0.06** |
| Skor ≥ 75 | 142 / +0.02 | 78 / −0.24 |
| Sadece PDH/PDL/PWH/PWL sweep | 119 / +0.03 | 88 / −0.03 |

Sadece önceden tanımlanmış SMC çekirdeği (4H + killzone + önemli seviye) iki dönemde de pozitif
kaldı; bu yüzden varsayılan filtre bu. Skor, sonucu tahmin etmedi.

## 2. Portföy backtesti (2% risk, en fazla 3 pozisyon, 1000 USDT)

### Optimizer kural analizi (2025-26)

| Ayar | İşlem | Getiri | Max DD | Net R/işlem | PF |
|---|---|---|---|---|---|
| Optimizer yok | 125 | +%15.4 | %25.0 | +0.081 | 1.08 |
| **Break-even + kâr kilidi** | 147 | **+%19.2** | %22.5 | +0.078 | 1.11 |
| Break-even + trailing | 147 | +%17.7 | %22.5 | +0.073 | 1.10 |
| Sadece 24 saat limiti | 145 | +%5.7 | %23.5 | +0.038 | 1.03 |
| BE + TP uzatma | 119 | −%0.2 | %26.0 | +0.018 | 1.00 |
| Sadece zaman stopu (6 saat) | 145 | −%1.4 | %20.3 | +0.010 | 0.99 |
| Sadece 1H dönüş çıkışı | 145 | −%2.9 | %21.3 | +0.007 | 0.98 |
| Sadece 15m yapı çıkışı | 154 | −%9.2 | %18.7 | −0.021 | 0.92 |
| BE + hedef 1.5R | 136 | −%6.7 | %24.8 | −0.011 | 0.95 |
| BE + hedef 3R | 117 | +%11.7 | %26.0 | +0.069 | 1.09 |
| BE + en fazla 12 saat | 140 | −%17.0 | %25.3 | −0.053 | 0.87 |

Erken çıkış kuralları (yapı, zaman, 1H dönüş) ve TP uzatma performansı düşürdü → varsayılan kapalı.

### İki yıl, devre kesiciler kapalı (ham performans)

| Ayar | 2024-25 | 2025-26 |
|---|---|---|
| **Varsayılan** (market, 4H+killzone+seviye≥10, BE+kilit+trailing, 24s) | −%27.0 (net −0.11R, DD %39) | +%10.9 (net +0.05R) |
| Optimizer yok | −%5.0 (±0.00R) | +%3.5 (+0.04R) |
| Tüm optimizer kuralları açık | −%40.1 (−0.19R) | −%13.8 (−0.04R) |
| Killzone filtresi yok | −%39.8 | −%47.2 |
| 15m swing sweep'leri de dahil | −%44.3 | −%16.9 |
| 4H uyumu şartı yok | −%29.2 | +%17.5 |
| FVG'ye limit giriş | −%25.7 | −%3.9 |

### Varsayılan ayar, devre kesiciler açık

| Dönem | İşlem | Getiri | Max DD | Kazanma | Net R | PF | Ortalama süre |
|---|---|---|---|---|---|---|---|
| 2024-25 | 66* | −%9.0 | %24.9 | %39 | −0.056 | 0.87 | 12.6 saat |
| 2025-26 | 147 | +%17.6 | %22.5 | %42 | +0.073 | 1.10 | 11.1 saat |

\* %25 drawdown kilidi devreye girip botu durdurdu (canlıda Telegram'dan `/resume` gerekir).
İşlemlerin ~%6-9'u stop nedeniyle 1 saatten kısa sürdü; medyan süre 9-10 saat.

## Her iki yılda da tutarlı bulgular

1. **Erken çıkışlar zarar ettiriyor.** 15m yapı kırılımı / 1H dönüş / zaman stopu ile çıkmak,
   toparlanacak işlemleri kesiyor ve ekstra komisyon ödetiyor.
2. **FVG'ye limit emir, MSS kapanışında girişten kötü** (ters seçilim).
3. **Killzone ve önemli seviye filtresi, filtresizden iyi** — ama tek başına kâra yetmiyor.
4. **Komisyon belirleyici.** Brüt avantaj işlem başına ~0.05-0.1R; gidiş-dönüş %0.1 taker
   komisyonu %0.8'lik stopta ~0.12R ediyor. Daha dar stop = daha fazla komisyon yükü.
   BNB ile komisyon indirimi / VIP seviyesi sonucu doğrudan iyileştirir.

## Sonraki adımlar

- Paper/demo modda veri biriktirin; `python -m smcbot report` ile `v_by_feature`,
  `v_by_swept_level`, `v_by_hour`, `v_rejected_setups` view'larına bakın.
- Yeni bir fikri denemek için:
  `python -m smcbot backtest --days 365 --end 2025-09-30 --set strategy.min_sweep_weight=12`
  Her değişikliği **iki ayrı yılda** test edin; sadece birinde iyi olanı almayın.
- Denemeye değer fikirler: sadece PDH/PDL + PWH/PWL sweep'leri; 1H zaman diliminde aynı model
  (daha az işlem, daha geniş stop, daha düşük komisyon yükü); maker (post-only) çıkışlar;
  funding rate / open interest filtreleri.

---

# Tur 2: 30 coin, ağırlık sistemi ve trend takibi

Veri: Binance arşivi (data.binance.vision), 30 coin, 5 dakikalık mumlar + taker alış hacmi,
5 dakikalık open interest ve long/short oranları, funding. 2024-07 → 2026-09.

Coinler (2 yıldan uzun listelenmiş, son 3 ayın en yüksek hacmi, günlük ortalama hareketi %20'nin
altında): BTC, ETH, SOL, XRP, DOGE, BNB, ADA, LINK, AVAX, TRX, ZEC, WLD, 1000PEPE, NEAR, ENA, SUI,
TAO, XLM, AAVE, UNI, BCH, ONDO, FIL, LTC, DOT, 1000SHIB, INJ, XMR, ARB, FET.

Tekrar üretmek için:

```bash
python -m research.download            # veriyi indir (~30 dk, data/research/)
python -m research.dataset             # 8.496 SMC setup + özellikler + sonuç
python -m research.meta_model          # ağırlık modeli, iki yönlü walk-forward
python -m research.trend --n 20 --atr-mult 3
python -m smcbot backtest --strategy trend --source archive --days 365 --end 2025-09-30
```

## Ağırlık sistemi (meta-labeling) — olumsuz

- 8.496 SMC setup (gevşek filtre), her birine ~45 özellik: SMC özellikleri + open interest değişimi
  (sweep sırasında / 24 saat), funding (seviye, z-skoru), taker alış oranı (son 1 saat / displacement
  bacağı), long/short oranları, volatilite rejimi, çok ufuklu momentum, BTC durumu, saat/gün.
- Etiket: 2R hedef, stop, 24 saat sınırı (üçlü bariyer), komisyon dahil.
- Lojistik regresyon ve gradient boosting; 2024-25'te öğren → 2025-26'da test, ve tersi.

| | Y1→Y2 lojistik | Y1→Y2 GBM | Y2→Y1 lojistik | Y2→Y1 GBM |
|---|---|---|---|---|
| AUC (0.50 = yazı-tura) | 0.522 | 0.510 | 0.517 | 0.502 |
| Tüm setup'lar R | −0.068 | −0.068 | −0.091 | −0.091 |
| Modelin en iyi %20'si R | −0.054 | −0.103 | −0.053 | −0.087 |

- Komisyonsuz kazanma ile en güçlü korelasyon 0.05 (pratikte sıfır). Net R ile görülen "güçlü"
  korelasyonlar (stop genişliği, volatilite) komisyon etkisidir: geniş stopta komisyon R'nin daha
  küçük kısmı.
- 4H trend uyumu kazanmayı artırmıyor (korelasyon −0.04 / −0.01).
- Sonuç: SMC setup'larında öğrenilebilir bir avantaj yok; ağırlık modeli olmayan avantajı yaratamaz.

## Trend takibi (4H Donchian kırılımı + günlük EMA50 filtresi + 3×ATR iz süren stop)

Tüm sinyaller (portföy sınırı olmadan), işlem başına net R (komisyon + kayma + funding dahil):

| Ayar | 2024-25 | 2025-26 |
|---|---|---|
| **20 mum, 3×ATR, filtreli (varsayılan)** | +0.083 (n=1249) | +0.119 (n=1132) |
| 55 mum | +0.094 | +0.139 |
| 2×ATR | +0.060 | +0.075 |
| 4×ATR | +0.106 | +0.134 |
| Filtresiz | +0.108 | +0.029 |
| Sadece long | **+0.262** | **+0.204** |
| En fazla 24 saat tutma | +0.015 | +0.021 |
| En fazla 72 saat tutma | +0.023 | +0.020 |

Sağlamlık (varsayılan ayar, 2 yıl, 2.381 işlem):
- Bootstrap %95 güven aralığı: [+0.026, +0.184] R; ortalamanın > 0 olma olasılığı %99.8.
- 8 tam çeyreğin 7'si pozitif; 30 coinin 22'si pozitif.
- Long: +0.26 / +0.21 R (iki yıl da güçlü) · Short: −0.11 / +0.07 R (tutarsız) → short yarım risk.
- Kazanma oranı ~%36, medyan işlem −0.36R. Kâr az sayıda büyük trendden gelir (en iyi 20 işlem
  toplam kârın tamamı; XLM tek işlemde +77R). En uzun kayıp serisi 29 işlem.
- Ortalama tutma süresi ~4.5 gün; 24 saat sınırıyla avantaj kaybolur.
- Ortalama stop mesafesi fiyatın %8'i → küçük hesaplarda BTC (min 50 USDT) / ETH (min 20 USDT)
  pozisyonu açılamayabilir.

### Botun kendi backtest'i (canlı kodla aynı; long %1 / short %0.5, max 6 pozisyon, toplam risk %8)

| | 2024-25 | 2025-26 |
|---|---|---|
| İşlem | 422 | 392 |
| Getiri | +%48.0 | +%52.6 |
| En büyük düşüş | %19.5 | %19.9 |
| Kazanma oranı | %41 | %36 |
| Net R / işlem | +0.095 | +0.158 |
| Kâr faktörü | 1.33 | 1.39 |
| Ortalama süre | 107 saat | 115 saat |
| Long / short R | +0.27 / −0.05 | +0.23 / +0.15 |

Hesap korumaları (günlük %6 zarar, %35 drawdown) bu iki yılda devreye girmedi. Haber filtresi
trend için kapalı (geçmiş takvim olmadığı için test edilemedi).

**Beklenti yönetimi:** iyi yıllarda ayda ortalama %3-5, arada %20'ye varan düşüşler ve uzun kayıp
serileri. Aylık %10-15 bu stratejiyle gerçekçi değil. Geçmiş sonuç geleceği garanti etmez.
