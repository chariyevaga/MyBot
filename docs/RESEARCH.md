# Araştırma ve Backtest Sonuçları

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
