# Strateji ve Algoritma

Bot iki strateji çalıştırır; her biri kendi **hesabında**:

| Strateji | Hesap | Neden |
|---|---|---|
| 📈 Trend takibi (4H) | `main` → `mode`'a göre paper / demo / live | 2 yıllık, 30 coinlik testte iki yılda da pozitif |
| 🎯 SMC (15m) | `shadow` → her zaman sanal, gerçek emir yok | Avantajı kanıtlanmadı; canlı veriyle gözlem ve veri toplama |

Hesaplar ayrı bakiye, ayrı risk korumaları ve ayrı istatistik tutar (PostgreSQL `mode` = paper/demo/live
veya `shadow`, `strategy` = trend/smc).

## 0. Trend takibi (`smcbot/trend.py`)

Her 4H mum kapanışında (00, 04, 08, 12, 16, 20 UTC; tarama en geç 30 dk içinde):

1. **Giriş (long):** 4H kapanış > önceki 20 mumun en yükseği **ve** son kapanmış günlük mum
   EMA50'nin üstünde. Short: tersi (en düşük altı kapanış + günlük EMA50 altı).
2. **Emir:** piyasa emri; fiyat sinyal kapanışından 0.3R'den fazla kaçtıysa girilmez.
3. **Stop:** giriş − 3 × ATR(4H, 14). Borsaya STOP_MARKET (mark price) olarak konur. **TP yok.**
4. **İz süren stop (chandelier):** her 4H kapanışında `girişten beri en iyi kapanış − 3 × ATR`;
   sadece sıkılaşır, en az 0.1R ilerleyince güncellenir. Fiyat yeni stopun gerisindeyse pozisyon kapatılır.
5. **Risk:** long %1, short %0.5 (short tarafı backtestte tutarsızdı); en fazla 6 pozisyon, coin
   başına 1, toplam açık risk %8. Stop mesafesi ortalama fiyatın %8'i olduğu için kaldıraç genelde 1-2x.
6. **Koruma:** günlük zarar %6, zirveden %35 düşüşte durur. Ardışık kayıp molası **yok** (kazanma
   oranı ~%36 olan bir stratejide kayıp serileri normaldir).
7. **Haber filtresi:** kapalı (`strategies.trend.news_filter`), çünkü çok günlük pozisyonlarda test
   edilemedi. Açılabilir.

Pozisyonlar ortalama ~4.5 gün açık kalır. Kâr az sayıda büyük trendden gelir; işlemlerin çoğu
küçük kayıpla kapanır. Test sonuçları: [RESEARCH.md](RESEARCH.md).

Aşağıdaki bölümler SMC stratejisini anlatır.

Tüm hesaplamalar **ileriye bakmaz** (look-ahead yok): bir swing noktası ancak sağında
`length` mum kapandıktan sonra bilinir, yapı kırılımları mum kapanışıyla değerlendirilir,
üst zaman dilimi değerleri sadece kapanmış mumlardan alınır. Bu `tests/test_smc.py >
test_setups_have_no_lookahead` ile test edilir: veri kesildiğinde, kesimden önceki
setup'lar birebir aynı kalmalı.

Long ve short aynı kodla bulunur: short için fiyatlar negatife çevrilir (high ↔ low),
böylece "boğa" mantığı ayı setup'larını da bulur (`strategy.find_setups`).

## 1. Zaman dilimleri

| TF | Görev |
|---|---|
| 4H | Ana yön (swing yapısı, length 3). Varsayılan: sadece 4H trend yönünde işlem. |
| 1H | Likidite seviyeleri, dealing range (premium/discount), 1H trend, ATR(1H) → stop mesafesi |
| 15m | Sweep, MSS, displacement, FVG/OB |
| 5m | Tarama zamanlaması, fiyat takibi, MFE/MAE, optimizasyon |

## 2. Likidite seviyeleri (`smc/liquidity.py`)

| Seviye | Ağırlık | Ne zaman bilinir | Geçerlilik |
|---|---|---|---|
| Önceki hafta high/low (PWH/PWL) | 15 | Pazartesi 00:00 UTC | 7 gün |
| Önceki gün high/low (PDH/PDL) | 15 | 00:00 UTC | 2 gün |
| Equal high/low 1H (EQH/EQL) | 12 | 2. tepenin onayı | 5 gün |
| 1H swing high/low | 10 | pivot onayı (+3 mum) | 5 gün |
| Equal high/low 15m | 9 | 2. tepenin onayı | 2 gün |
| Asya seansı high/low (00-07 UTC) | 8 | 07:00 UTC | gün sonu |
| 15m swing high/low | 5 | pivot onayı (+5 mum) | 2 gün |

Equal high/low: aynı taraftaki iki swing 0.1×ATR toleransla eşit ve aradaki fiyat bu seviyeyi geçmemiş.

## 3. Setup tespiti (long; short simetrik)

1. **Sweep**: 15m mum sell-side seviyenin altına iğne atar (önceki mum seviyenin üstünde kapanmış
   olmalı) ve en fazla 2 mum içinde seviyenin **üstünde kapanır**. Seviyenin 2×ATR'den derin
   kırılması sweep sayılmaz (gerçek kırılım). Aynı mumda süpürülen seviyeler gruplanır.
2. **MSS (market structure shift)**: sweep'teki en dip noktayı oluşturan iç swing high'ın
   (length 2) üstünde kapanış, en fazla 16 mum (4 saat) içinde; bu sürede yeni dip yapılmamalı.
3. **Displacement**: sweep dibinden MSS'e kadar en az bir yükseliş mumunun gövdesi ≥ 1×ATR(15m).
4. **POI**: bu bacaktaki doldurulmamış bullish FVG'ler (≥0.1 ATR) ve order block (displacement'tan
   önceki son düşüş mumu, [low, open]). FVG ile OB çakışıyorsa `FVG+OB`. OTE (%70.5 geri çekilme)
   seviyesine en yakın POI seçilir. FVG yoksa OB kullanılır.
5. **Giriş** (`strategy.entry_mode`):
   - `market` (varsayılan): MSS onay mumunun kapanışında piyasa emri. POI, displacement'ın
     dengesizlik bıraktığının kanıtı olarak şart.
   - `limit`: POI'nin CE'sine (%50) limit emir, 2 saat geçerli; fiyat dolmadan TP'ye giderse iptal.
     Backtestte dolan emirler ters seçilim yüzünden daha kötü sonuç verdi (bkz. RESEARCH.md).
6. **Stop**: sweep dibi − 0.25×ATR(15m). En az `max(1.0×ATR(1H), %0.8)` → dar stopları genişletir
   (işlem süresi uzar, komisyonun R içindeki payı düşer). 4×ATR(1H)'den genişse setup atlanır.
7. **Hedef**: 2R. Skor ≥ 80 **ve** karşı likidite ≥ 3R uzaktaysa 3R.
8. **Sert filtreler** (varsayılan açık): 4H trend uyumlu · süpürülen seviye ağırlığı ≥ 10 ·
   sweep veya MSS London (07-10 UTC) / New York (12-15 UTC) killzone'unda.

## 4. Confluence skoru (0-100)

| Faktör | Puan |
|---|---|
| 4H trend uyumlu / nötr | 20 / 8 |
| 1H trend uyumlu / nötr | 10 / 4 |
| Süpürülen seviyenin ağırlığı (+3 birden fazla seviye) | 5-18 |
| Tek mumda geri alım (turtle soup) | 5 |
| Displacement ≥1.5 ATR / ≥1 ATR | 10 / 6 |
| Displacement hacmi ≥1.5× ortalama | 5 |
| FVG + OB çakışması | 5 |
| 1H dealing range'in discount (long) / premium (short) yarısında | 10 |
| OTE (%62-79 geri çekilme) | 5 |
| Killzone | 5 |
| SMT divergence (BTC için ETH, diğerleri için BTC; referans yeni dip yapmadı) | 8 |
| Karşı likiditeye ≥2R / ≥3R alan | 4 / 8 |

Skor `risk.tiers` ile riski belirleyebilir. Backtestte skor, sonuçla anlamlı ilişki göstermediği
için varsayılan risk sabit %2. Skor ve tüm özellikler PostgreSQL'e yazılır; canlı veriyle
yeniden değerlendirin (`v_by_score`, `v_by_feature`).

## 5. Risk

- Miktar = (bakiye × risk%) / (|giriş − stop| + giriş × (maker + taker komisyon)).
- Kaldıraç: marjin bütçesine (serbest bakiye / kalan slot) sığan en düşük kaldıraç; izole
  marjinde tasfiye mesafesi stop mesafesinin en az 1/0.7 katı olacak şekilde sınırlanır (en fazla 20x).
- Coin başına 1, toplam 3 pozisyon; açık pozisyonların toplam riski en fazla %10.
- Devre kesiciler: günlük zarar %6 → gün sonuna kadar yeni işlem yok · 3 ardışık zarar → 6 saat mola ·
  zirveden %25 düşüş → durur, `/resume` gerekir · kapanan coinde 60 dk bekleme.
- Sert sınırlar kodda: risk ≤ %4, hedef ≤ 3R.

## 6. Pozisyon optimizasyonu (`optimizer.py`)

Her 5 dakikada tarama bittikten sonra, her açık pozisyon için. Stop **sadece sıkılaşır**,
asla gevşemez. Yeni stop fiyatın gerisinde kalırsa pozisyon kapatılır.

| Kural | Varsayılan | Açıklama |
|---|---|---|
| Break-even | ✅ +1R | stop → giriş + 0.05R |
| Kâr kilidi | ✅ | 1.5R görülünce +0.5R, 2R → +1R, 2.5R → +1.5R |
| Yapısal trailing | ✅ ≥1.5R | stop → girişten sonra oluşan son 15m swing'in 0.1 ATR ötesi |
| Maksimum süre | ✅ 24 saat | günlük işlem kuralı |
| Haber koruması | ✅ | yüksek etkili habere ≤30 dk ve pozisyon ≥+0.5R → stop girişe |
| İlk 1 saat | ✅ | erken çıkış kuralları çalışmaz (SL/TP çalışır) |
| TP uzatma | ❌ | ≥1.6R + 15m BOS + 1H aleyhe değil → karşı likiditenin önüne / en fazla 3R |
| 15m yapı çıkışı | ❌ | yapı aleyhe kırılır ve < +0.5R ise çık |
| 1H dönüş çıkışı | ❌ | 1H BOS/CHoCH aleyhe ve < +1R ise çık |
| Zaman stopu | ❌ | X saatte +0.5R görmemiş ve zarardaysa çık |

❌ olanlar kodda hazır, `config.yaml`'dan açılabilir; iki yıllık testte performansı düşürdükleri
için kapalı (RESEARCH.md). Her uygulanan kural Telegram'a sebebiyle bildirilir ve
`trade_events` tablosuna yazılır.

## 7. Emir akışı ve güvenlik

1. Setup → doğrulama (fiyat kaçmadı mı, SL/TP arası mı, haber/limit engeli yok mu)
2. Kaldıraç ve izole marjin ayarlanır → giriş emri (market veya limit)
3. Dolum anında (market girişte hemen, limitte 15 sn içinde) **SL (STOP_MARKET, mark price)** ve
   **TP (TAKE_PROFIT_MARKET)** reduceOnly olarak borsaya konur (Binance Algo Order API).
   SL konulamazsa pozisyon anında kapatılır.
4. SL/TP değiştirilirken önce yeni emir konur, sonra eski iptal edilir (hiç korumasız an yok).
5. Her döngüde borsadaki SL/TP emirleri kontrol edilir; eksikse yeniden konur.
6. Pozisyon kapanınca kalan emirler iptal edilir, sonuç (`realizedPnl` − komisyon) kaydedilir.
7. Bot durursa SL/TP borsada kalır; yeniden başlayınca durum Redis'ten yüklenir.
