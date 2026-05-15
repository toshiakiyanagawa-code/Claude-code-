最も effort/return が良いのは **B + D** です。

A の「深掘り」は正しいクエリなら効きますが、今の例ではクエリ自体が「天安門 毛沢東 肖像」から「中国 経済 国旗 グラフ」に逸れています。まず深掘りより、**抽象化前の具体物・固有名詞クエリを guaranteed に混ぜる**のが先です。

**仮説の重み**
- **B: 高**: near-miss が典型。actual は具体物、候補は抽象テーマ。
- **D: 高**: Policy-2 が retrieval 段階で「具体物」を落としている可能性が高い。
- **A: 中**: 正しい具体クエリに限れば `limit=8` は浅い。全クエリ深掘りは費用対効果が悪い。
- **C: 中**: 政治・国際・事件系では Editorial 側が必要。ただしまず Creative/Editorial 以前に query mismatch を直すべき。

**1 ステップ実装**
やることは 1 つだけです。

`search_queries` 生成後、`guaranteed` bucket を作る箇所で、Policy-2 を通さない **concrete/literal query lane** を追加します。

変更箇所:
- `search_queries` → 上位3件 + `primary_query` を `guaranteed` に入れている retrieval module
- 具体的には `crawl_search(q, limit=8)` に渡す query list を作っている関数
- `precision_analyzer.py` は計測追加のみ。pipeline 本体の変更は上記 1 箇所

追加する query:
```text
article title / lead / slot intent から、
固有名詞 + 具体物名をそのまま残した query を 1-2 件作る

例:
中国 経済 → 天安門 毛沢東 肖像
政治家記事 → 人名 会見 / 人名 演説
健康記事 → 腰 痛み / 膝 関節 / 血糖値 測定
```

実装方針:
- 既存 LLM query は残す
- 新規 `literal_guaranteed_queries` を先頭 bucket に追加
- この lane だけ `crawl_search(q, limit=24)` にする
- Policy-2 の「日本人 / 顔なし / 抽象」は適用しない
- Policy-3 の hard safety / 明確な不適合 filter は維持する

期待値:
- photog match: **0/44 → 3〜8/44** 程度を期待
- いきなり 20/44 は期待しない。Editorial 不在や iStock 側未収録が残るため
- ただし retrieval ceiling が 0% から動くかを見るには十分

リスク:
- 抽象・顔なし方針の精度が少し落ちる
- 政治家・海外人物・報道寄り写真が混ざる
- 対策は「literal lane は候補追加だけ」に留め、最終 rerank / Policy-3 は既存のまま通すこと

**追加 metric**
photog match に加えて、以下を出すべきです。

- `raw_pool_recall@k`: actual asset / photographer が rerank 前の候補 pool にいたか
- `min_rank_by_query`: actual photographer が各 query の検索結果で何位に出たか
- `min_rank_by_lane`: LLM abstract / legacy / literal のどの lane で拾えたか
- `page_depth_hit`: @8, @24, @48 で hit するか
- `retrieval_vs_ranking_loss`: raw pool にはいるが top-5 から落ちた件数

可能なら photographer だけでなく、actual iStock asset id / image URL / caption similarity で見るべきです。photographer 一致だけだと同一撮影者の別写真で false positive になります。

**記事タイプ分岐**
分けるべきです。

判定:
- Editorial/news: 人名、国名、都市名、政府、選挙、裁判、戦争、企業名、日付、事件語が多い
- Stock: 健康、食、教育、生活、家計、介護など evergreen で固有名詞が少ない

戦略:
- Editorial/news: 固有名詞・場所・具体物 query、Editorial 検索、深め `limit=24〜48`
- Stock: 現行 Policy-2 を維持、日本人・顔なし・抽象/生活シーン寄せ

**次の 1 commit でやること**

`guaranteed` query 組み立て箇所に **Policy-2 非適用の concrete/literal query lane を 1-2 件追加**し、その lane だけ `crawl_search(..., limit=24)` にする。あわせて `precision_analyzer.py` に `raw_pool_recall@8/24` と `min_rank_by_lane` を出す計測を追加する。
