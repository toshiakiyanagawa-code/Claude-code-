# Precision analysis: eval_v4_50articles.json

## Photographer match (most precise signal)

- Articles with iStock photographer recorded: **44**
- Slots where one of top-5 candidates was the SAME photographer as the editor's choice: **0**

## Retrieval ceiling (codex review § 1-2)

- Articles analyzed (with iStock photog): **44**
- Articles where actual photog appeared in top-5 of *some* slot: **0/44** (0%)

If this number is very low, the issue is *retrieval* (LLM queries / iStock search are not surfacing the photographer at all). If it's high but per-slot photographer_match is 0, the issue is *ranker* (the photog was surfaced somewhere but not in the right slot).

## Body-image alt similarity (jaccard)

Body image pairs analyzed: **124**

- top1 jaccard mean: **0.000**
- top5_max jaccard mean: **0.000**
- top1 hit rate (≥ 0.15): **0.0%**
- top5 hit rate (≥ 0.15): **0.0%**
- near-miss rate (top5 < 0.05): **100.0%**

## By topic

| topic | n | top5_max_mean | top5_hit_rate |
|---|---|---|---|
| その他 | 57 | 0.0 | 0.0% |
| 健康 | 49 | 0.0 | 0.0% |
| 食 | 8 | 0.0 | 0.0% |
| 中国 | 4 | 0.0 | 0.0% |
| ビジネス | 4 | 0.0 | 0.0% |
| 教育 | 2 | 0.0 | 0.0% |

## Near misses (top5_max < 0.05) — gap targets

### [中国] slot hero — actual(body): 北京の天安門に掲げられた毛沢東の肖像
  - 中国の経済。投資家のシルエットが描かれた中華人民共和国の旗。危機の財務チャート。投資相場。中国とのビジネスコンセプト。中国経済の衰退のチャート。中国の危機。3d画像 - china economy g
  - 素早い開発に都会の経済および財務部門 - china economy graph silhouette ストックフォトと画像
  - 財務上の成功矢印記号 - china economy graph silhouette ストックフォトと画像
  - 財務上の成功矢印記号 - china economy graph silhouette ストックフォトと画像
  - 中国の旗とストック ・ ダイヤグラム - china economy graph silhouette ストックフォトと画像

### [中国] slot h4_1 — actual(body): 北京の天安門に掲げられた毛沢東の肖像
  - 道路上の人々の影と中国の旗の概念図 - china flag silhouette ストックフォトと画像
  - グランジの金属の質感を持つ中国と日本の旗 - 中国 国旗 シルエット ストックフォトと画像
  - オフィスビルの壁にある白紙の看板 - 中国 建築 象徴 ストックフォトと画像
  - 冬の街並み - 中国 建築 象徴 ストックフォトと画像
  - 都市スケープとネットワーク接続コンセプトの上に平らにマップピン - 中国 建築 象徴 ストックフォトと画像

### [中国] slot h4_2 — actual(body): 北京の天安門に掲げられた毛沢東の肖像
  - 北京の夕暮れの街並み - 中国 建設 インフラ ストックフォトと画像
  - 近代的なオフィスビルの詳細、ガラス表面 - 中国 建設 インフラ ストックフォトと画像
  - 北京のスカイラインの都市ネットワークの航空写真 - 中国 経済成長 都市 ストックフォトと画像
  - 夜の街並みと高層ビルの上面図 - 中国 経済成長 都市 ストックフォトと画像
  - 夜の街並みと高層ビルの上面図 - 中国 経済成長 都市 ストックフォトと画像

### [中国] slot h4_3 — actual(body): 北京の天安門に掲げられた毛沢東の肖像
  - 中国のプライド - 中国 国旗 デモ silhouette ストックフォトと画像
  - 中国の旗円でブルーマン - 中国 国旗 デモ silhouette ストックフォトと画像
  - 中国人女性 - 中国 国旗 デモ silhouette ストックフォトと画像
  - 柱を背景に天安門の中国国旗 - 中国 国旗 デモ silhouette ストックフォトと画像
  - シルエットの群衆、背後に中国の旗が掲げられている - 中国 国旗 デモ silhouette ストックフォトと画像

### [健康] slot hero — actual(body): 腰痛で腰を押さえている人
  - カジュアルな服装の先輩男性の側面図 - japanese senior back view posture ストックフォトと画像
  - シニアウーマン - japanese senior back view posture ストックフォトと画像
  - 男性を - japanese senior back view posture ストックフォトと画像
  - 老人の裏図 - japanese senior back view posture ストックフォトと画像
  - 背中の痛みに苦しむ高齢者男 - japanese senior back view posture ストックフォトと画像

### [健康] slot h4_1 — actual(body): 腰痛で腰を押さえている人
  - シニアウーマン - japanese elderly back view posture ストックフォトと画像
  - カジュアルな服装の先輩男性の側面図 - japanese elderly back view posture ストックフォトと画像
  - 男性を - japanese elderly back view posture ストックフォトと画像
  - 老人の裏図 - japanese elderly back view posture ストックフォトと画像
  - 背中の痛みに苦しむ高齢者男 - japanese elderly back view posture ストックフォトと画像

### [健康] slot h4_2 — actual(body): 腰痛で腰を押さえている人
  - 男性を - japanese senior back view posture ストックフォトと画像
  - カジュアルな服装の先輩男性の側面図 - japanese senior back view posture ストックフォトと画像
  - シニアウーマン - japanese senior back view posture ストックフォトと画像
  - 老人の裏図 - japanese senior back view posture ストックフォトと画像
  - 背中の痛みに苦しむ高齢者男 - japanese senior back view posture ストックフォトと画像

### [健康] slot h4_3 — actual(body): 腰痛で腰を押さえている人
  - 老人の裏図 - japanese senior back view posture ストックフォトと画像
  - カジュアルな服装の先輩男性の側面図 - japanese senior back view posture ストックフォトと画像
  - シニアウーマン - japanese senior back view posture ストックフォトと画像
  - 男性を - japanese senior back view posture ストックフォトと画像
  - 背中の痛みに苦しむ高齢者男 - japanese senior back view posture ストックフォトと画像

### [ビジネス] slot hero — actual(body): 東京の夜景
  - 日本の東京の風景 - タワーマンション 都市景観 日本 ストックフォトと画像
  - 日本の福岡市の風景 - タワーマンション 都市景観 日本 ストックフォトと画像
  - 空のコンクリート地面と日本のスカイライン表示ウィンドウまたはモックアップを作成 - タワーマンション 都市景観 日本 ストックフォトと画像
  - ナイトシティの背景を持つ未来的な高速ライトテール - urban development japan cityscape ストックフォトと画像
  - ビジネスネットワークの概念。 - urban development japan cityscape ストックフォトと画像

### [ビジネス] slot h4_1 — actual(body): 東京の夜景
  - 飛行機から見た鳥瞰図から見た日本の海辺の町。 - tokyo urban development skyline ストックフォトと画像
  - 飛行機から見た鳥瞰図から見た日本の海辺の町。 - tokyo urban development skyline ストックフォトと画像
  - 日本の東京の風景 - 日本 タワーマンション 都市景観 ストックフォトと画像
  - 日本の福岡市の風景 - 日本 タワーマンション 都市景観 ストックフォトと画像
  - 空のコンクリート地面と日本のスカイライン表示ウィンドウまたはモックアップを作成 - 日本 タワーマンション 都市景観 ストックフォトと画像

### [ビジネス] slot h4_2 — actual(body): 東京の夜景
  - ビジネス地区の近代的な超高層ビル - 高層ビル 再開発 日本 ストックフォトと画像
  - 東京のクレーンによる建設 - 高層ビル 再開発 日本 ストックフォトと画像
  - 東京ベイエリアの航空写真 - 高層ビル 再開発 日本 ストックフォトと画像
  - 青空市の近代的な建物 - 高層ビル 再開発 日本 ストックフォトと画像
  - 東京の新宿&渋谷エリアからの近代都市のスカイライン鳥瞰航空写真 - 高層ビル 再開発 日本 ストックフォトと画像

### [ビジネス] slot h4_3 — actual(body): 東京の夜景
  - 日本の福岡市の風景 - タワーマンション 都市 日本 ストックフォトと画像
  - ナイトシティの背景を持つ未来的な高速ライトテール - urban development japan cityscape ストックフォトと画像
  - ビジネスネットワークの概念。 - urban development japan cityscape ストックフォトと画像
  - 東京の地球地図技術とチームワーク - urban development japan cityscape ストックフォトと画像
  - 東京日本高速列車トンネルモーションブラー抽象 - urban development japan cityscape ストックフォトと画像

### [健康] slot hero — actual(body): 膝が痛いシニア
  - カジュアルな服装の先輩男性の側面図 - senior back view posture japanese ストックフォトと画像
  - シニアウーマン - senior back view posture japanese ストックフォトと画像
  - 男性を - senior back view posture japanese ストックフォトと画像
  - 老人の裏図 - senior back view posture japanese ストックフォトと画像
  - 背中の痛みに苦しむ高齢者男 - senior back view posture japanese ストックフォトと画像

### [健康] slot h4_1 — actual(body): 膝が痛いシニア
  - シニアビジネスマンはホワイトボードに書く - 疲労 日本人 後ろ姿 ストックフォトと画像
  - ベッドに腰を乗せている若い女性の後ろ姿 - 疲労 日本人 後ろ姿 ストックフォトと画像
  - ベッドに腰を乗せている若い男性の後ろ姿 - 疲労 日本人 後ろ姿 ストックフォトと画像
  - 空のアパートでアジア人男性 - 疲労 日本人 後ろ姿 ストックフォトと画像
  - 肩こりに苦しむビジネスウーマン - 疲労 日本人 後ろ姿 ストックフォトと画像

### [健康] slot h4_2 — actual(body): 膝が痛いシニア
  - シニアウーマン - senior back view posture japanese ストックフォトと画像
  - カジュアルな服装の先輩男性の側面図 - senior back view posture japanese ストックフォトと画像
  - 男性を - senior back view posture japanese ストックフォトと画像
  - 老人の裏図 - senior back view posture japanese ストックフォトと画像
  - 背中の痛みに苦しむ高齢者男 - senior back view posture japanese ストックフォトと画像

## Big hits (top5_max > 0.30) — what works
