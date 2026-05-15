# Precision analysis: eval_v3_20articles.json

## Photographer match (most precise signal)

- Articles with iStock photographer recorded: **16**
- Slots where one of top-5 candidates was the SAME photographer as the editor's choice: **0**

## Retrieval ceiling (codex review § 1-2)

- Articles analyzed (with iStock photog): **16**
- Articles where actual photog appeared in top-5 of *some* slot: **0/16** (0%)

If this number is very low, the issue is *retrieval* (LLM queries / iStock search are not surfacing the photographer at all). If it's high but per-slot photographer_match is 0, the issue is *ranker* (the photog was surfaced somewhere but not in the right slot).

## Body-image alt similarity (jaccard)

Body image pairs analyzed: **32**

- top1 jaccard mean: **0.000**
- top5_max jaccard mean: **0.000**
- top1 hit rate (≥ 0.15): **0.0%**
- top5 hit rate (≥ 0.15): **0.0%**
- near-miss rate (top5 < 0.05): **100.0%**

## By topic

| topic | n | top5_max_mean | top5_hit_rate |
|---|---|---|---|
| 健康 | 20 | 0.0 | 0.0% |
| 中国 | 4 | 0.0 | 0.0% |
| その他 | 4 | 0.0 | 0.0% |
| 食 | 4 | 0.0 | 0.0% |

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

### [健康] slot hero — actual(body): 車椅子に一人で座っているシニアの後ろ姿
  - アジアの老人カップルでお探しの眺め - elderly japanese back view memory ストックフォトと画像
  - バックスタイルをお楽しみのカップル - elderly japanese back view memory ストックフォトと画像
  - 公園で座り合い、慰め合うシニアカップル - elderly japanese back view memory ストックフォトと画像
  - 橋から紅葉を見ている男 - elderly japanese back view memory ストックフォトと画像
  - 老人の裏図 - elderly japanese back view memory ストックフォトと画像

### [健康] slot h4_1 — actual(body): 車椅子に一人で座っているシニアの後ろ姿
  - バックスタイルをお楽しみのカップル - senior back view memory japanese ストックフォトと画像
  - アジアの老人カップルでお探しの眺め - senior back view memory japanese ストックフォトと画像
  - 橋から紅葉を見ている男 - senior back view memory japanese ストックフォトと画像
  - 老人の裏図 - senior back view memory japanese ストックフォトと画像
  - 老人男性 - senior back view memory japanese ストックフォトと画像

### [健康] slot h4_2 — actual(body): 車椅子に一人で座っているシニアの後ろ姿
  - 精神医療の概念 - memory abstract silhouette ストックフォトと画像
  - 緑色の背景に花で作られた人間の脳のシルエット - memory abstract silhouette ストックフォトと画像
  - 若い男脳と思考概念の側面図 - memory abstract silhouette ストックフォトと画像
  - 若い男脳と思考概念の側面図 - memory abstract silhouette ストックフォトと画像
  - 青い背景に紙の人間の頭のシンボルと花 - memory abstract silhouette ストックフォトと画像

### [健康] slot h4_3 — actual(body): 車椅子に一人で座っているシニアの後ろ姿
  - バージニア州ハーンドンのシュガーランドランストリームバレートレイルハイキング、バージニア州のフェアファックス郡の春、舗装された道の道と希望の概念として日没に向かって歩く男� - シルエット 道 希望 
  - 軽い概念に向かって歩く男。3d レンダリング - シルエット 道 希望 ストックフォトと画像
  - あなたに戻って老人 - japanese senior back view future ストックフォトと画像
  - あなたに戻って老人 - japanese senior back view future ストックフォトと画像
  - 外を歩く先輩男性 - japanese senior back view future ストックフォトと画像

### [健康] slot hero — actual(body): シニア男性
  - アクティブなビジネスマン - 人生設計 時間 日本人 後ろ姿 ストックフォトと画像
  - アクティブなビジネスマン - 人生設計 時間 日本人 後ろ姿 ストックフォトと画像
  - スマートフォンを持つ少女 - 人生設計 時間 日本人 後ろ姿 ストックフォトと画像
  - 外で活気のある男 - 人生設計 時間 日本人 後ろ姿 ストックフォトと画像
  - 太陽を取る人 - 人生設計 時間 日本人 後ろ姿 ストックフォトと画像

### [健康] slot h4_1 — actual(body): シニア男性
  - コンピューターの仕事で立ち往生している女性 - 後ろ姿 日本人 思考 迷い ストックフォトと画像
  - 夜の街を一人で歩く女性 - 後ろ姿 日本人 思考 迷い ストックフォトと画像
  - 背面の女性 - 後ろ姿 日本人 思考 迷い ストックフォトと画像
  - 若い男性の街 - 後ろ姿 日本人 思考 迷い ストックフォトと画像
  - 若い男性の街 - 後ろ姿 日本人 思考 迷い ストックフォトと画像

### [健康] slot h4_2 — actual(body): シニア男性
  - トラムレールのある石畳の通りで日の出のサイクリストと描かれた都市生活 - silhouette crossroads decision ストックフォトと画像
  - 壁面ネオン矢印記号 - silhouette crossroads decision ストックフォトと画像
  - 方向矢印標識 - silhouette crossroads decision ストックフォトと画像
  - ビジネスマンは2方向の矢印で道路に立って、どちらに行くかを決めます - silhouette crossroads decision ストックフォトと画像
  - 壁面ネオン矢印記号 - silhouette crossroads decision ストックフォト�と画像

### [健康] slot h4_3 — actual(body): シニア男性
  - アジアのビジネスマンがオフィスの窓から遠くを眺める - 後ろ姿 窓 夕焼け 日本人 ストックフォトと画像
  - アパートの窓から夕日を眺めている少女 - 後ろ姿 窓 夕焼け 日本人 ストックフォトと画像
  - アパートの窓から夕日を眺めている少女 - 後ろ姿 窓 夕焼け 日本人 ストックフォトと画像
  - ホテルの部屋でリラックスしているアジアのビジネスウーマン - 後ろ姿 窓 夕焼け 日本人 ストックフォトと画像
  - 夕暮れ時の空港エスカレーターで歩いているアジアのビジネスマンのリアビュー - 後ろ姿 窓 夕焼け 日本人 ストックフォトと画像

### [健康] slot hero — actual(body): 車を運転している高齢者
  - シニアの運転の日 - 高齢者 運転 後ろ姿 ストックフォトと画像
  - シニア運転車設定バックミラー - 高齢者 運転 後ろ姿 ストックフォトと画像
  - 田舎道を車で運転する年配の女性 - 高齢者 運転 後ろ姿 ストックフォトと画像
  - ただ、オープンロード - 高齢者 運転 後ろ姿 ストックフォトと画像
  - 手を上げて、太陽 - 高齢者 運転 後ろ姿 ストックフォトと画像

### [健康] slot h4_1 — actual(body): 車を運転している高齢者
  - 自宅のリビングのソファに座りながらノートパソコンを使う高齢者夫婦 - 高齢者 ストックフォトと画像
  - ヘルスクラブのエクササイズ�クラスでストレッチをするアクティブな高齢者。 - 高齢者 ストックフォトと画像
  - 歩行杖で先輩を支援��する看護師 - 高齢者 ストックフォトと画像
  - 高齢患者を抱きしめる在宅医療従事者 - 高齢者 ストックフォトと画像
  - 先輩女友達トランプ - 高齢者 ストックフォトと画像

### [健康] slot h4_2 — actual(body): 車を運転している高齢者
  - シニア運転車設定バックミラー - 高齢者 運転 後ろ姿 ストックフォトと画像
  - シニアの運転の日 - 高齢者 運転 後ろ姿 ストックフォトと画像
  - 田舎道を車で運転する年配の女性 - 高齢者 運転 後ろ姿 ストックフォトと画像
  - ただ、オープンロード - 高齢者 運転 後ろ姿 ストックフォトと画像
  - 手を上げて、太陽 - 高齢者 運転 後ろ姿 ストックフォトと画像

## Big hits (top5_max > 0.30) — what works
