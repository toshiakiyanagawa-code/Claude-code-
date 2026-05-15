注: この環境では `git show` / `pytest` が sandbox の `bwrap` 制約で実行できなかったため、添付 diff と実装サマリに基づく静的レビューです。

**必修事項 (Must Fix Before Ship)**

- `src/cms_entry_assistant/web/app.py / _fetch_candidates`
  問題点: LLM query を先頭に積み、`collect_until` 到達で break するため、LLM が「失敗はしていないが broad / title 汚染された query」を返すと legacy の `s.query_ja` / `query_plan` が実行されません。これは過去の「全スロット同質化」再発リスクが高いです。
  推奨修正: `llm` / `llm_rerank` でも必ず primary legacy query は実行する。例: LLM pool と legacy pool を別々に一定数集めて merge/dedupe する、または `s.query_ja` だけは early break の対象外にする。

- `src/cms_entry_assistant/web/app.py / _fetch_candidates`
  問題点: `llm_rerank` 経路では `rank_hits(..., history=UsageHistory())` を通らず、既存の履歴・既存 ranking signal が落ちています。`prefs` は reranker に渡っていますが、`UsageHistory` が無効化されるのは既存挙動からの回帰です。
  推奨修正: rerank 前に `rank_hits(collected, preferences=prefs, history=history, limit=30)` を通す、または reranker 側に base rank / history penalty を特徴量として入れる。最終的な tie-break も既存 rank を使うべきです。

- `src/cms_entry_assistant/llm_query_generator.py / generate_query_plan` + `src/cms_entry_assistant/web/app.py / _maybe_generate_llm_plan`
  問題点: 空または低品質な `search_queries` の扱いが曖昧です。`result.ok` が「plan がある」だけで true になる設計なら、`llm_rerank` は legacy 候補を空プランで再順位付けしてしまいます。また、Anthropic 呼び出しの timeout / retry / rate-limit 方針が diff 上明示されておらず、fallback が遅延する可能性があります。
  推奨修正: `ok` は「正規化後の `search_queries` が非空」「rerank 用 lexical terms が非空」「confidence が閾値以上」まで含める。API 呼び出しには明示 timeout、短い retry、429/timeout 時の即 legacy fallback を入れる。

**推奨事項 (Should Consider)**

- `src/cms_entry_assistant/llm_query_generator.py / LlmQueryPlan` + `src/cms_entry_assistant/candidate_reranker.py / rerank_candidates` + `web/app.py / _ReRankPlanView`
  問題点: `LlmQueryPlan` は `intent/search_queries/keywords/negative_keywords`、reranker は `intent_terms/query_terms/avoid_terms` を要求しており、現在は web 層の private adapter が契約を隠しています。
  推奨修正: Phase 2 の意図として adapter 自体は妥当です。ただし置き場所は `web/app.py` ではなく、`LlmQueryPlan` の computed property か `candidate_reranker.RerankPlan.from_llm_query_plan()` に寄せるべきです。web 層は「生成して渡す」だけにするのがよいです。

- `src/cms_entry_assistant/llm_query_generator.py / compute_slot_hash`
  問題点: 現在の slot payload は概ね必要情報を含んでいますが、cache correctness は「prompt に入る全情報」と一致している必要があります。`*-latest` 系モデル名は実体が変わっても cache key が変わらない点も注意です。
  推奨修正: `prompt_version` に加えて prompt/schema checksum を cache namespace に入れる、または prompt 変更時の version bump を必須化する。評価時は pinned model を使う。cache write は atomic にし、壊れた cache は無視して再生成する。

- `src/cms_entry_assistant/llm_query_generator.py / prompt` + `web/app.py / _maybe_generate_llm_plan`
  問題点: `article_title` は全 slot 共通なので、prompt 上で重く扱うと全 slot の query が同質化します。
  推奨修正: prompt で「article_title は弱い文脈。slot_label/type/primary_query/rationale を優先」と明文化する。hero/h4 間で normalized query が過度に一致したら fallback または警告するテストを追加する。

- `src/cms_entry_assistant/candidate_reranker.py / rerank_candidates`
  問題点: `0.55 / 0.25 / 0.15` は初期値としては理解できますが、根拠はまだ評価セット待ちです。人物・国旗・ランドマークなど特定型では Jaccard より exact entity / type constraint が効く場面があります。
  推奨修正: weight を定数化・設定化し、type_code ごとの補正を入れる。特定型では entity exact match、generic image では intent coverage を重くするなど、評価で調整してください。

- `src/cms_entry_assistant/web/app.py / _maybe_generate_llm_plan`
  問題点: import failure や missing key が完全に無音だと、`llm` にしたつもりで常に legacy になっていても気づきにくいです。
  推奨修正: missing key / SDK missing / API failure / parse failure を区別して debug または warning に出す。UI には出さなくても、運用ログには残すべきです。

**任意事項 (Nice-To-Have)**

- `src/cms_entry_assistant/llm_query_generator.py / file cache`
  問題点: cache に記事由来の intent/query/rationale が残るため、ローカルとはいえ運用上の扱いを明確にしたいです。
  推奨修正: cache TTL、clear コマンド、cache dir の明示ドキュメントを追加する。

- `src/cms_entry_assistant/web/app.py / _fetch_candidates`
  問題点: 1 案件あたり slot 数分の API call になり、通常 4-7 requests です。
  推奨修正: Phase 4 以降で「全 slot を 1 request で plan 生成」する batch mode を検討する。title 汚染の検出や slot 間 diversity 制約も入れやすくなります。

- `tests`
  問題点: unit test 14 件は良いですが、ランキング品質の保証には不足です。
  推奨修正: fixture 化した plan/candidate で、empty plan、broad LLM query、history penalty、adapter contract、title pollution を追加する。

**Phase 4 で Codex がやるべき作業**

- `llm` / `llm_rerank` でも legacy primary query を必ず候補 pool に入れる実装へ修正。
- `llm_rerank` に `rank_hits` / `UsageHistory` の既存 signal を戻す。
- `QueryPlanResult.ok` の validation を強化し、空 query・低 confidence は legacy fallback にする。
- Anthropic 呼び出しに timeout / retry / rate-limit fallback を明示実装。
- `_ReRankPlanView` を web 層から generator/reranker 層へ移す。
- 25-30 docx の評価 harness を作り、`legacy / llm / llm_rerank` を top1/top3 relevance、slot diversity、latency、API cost、cache hit で比較する。
- 評価結果で prompt と rerank weight を調整し、default は評価が通るまで `legacy` のまま維持する。
