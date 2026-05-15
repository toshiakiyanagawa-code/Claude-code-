## 1. 最優先 (Must do now): A. Async candidate loading
- 効果: **POST 80s → 0.5-2s**。候補生成完了は当面 80s のままだが、Codespaces proxy timeout は回避できる。
- 難易度: M
- リスク: in-memory background job はサーバ再起動で消える。複数アップロード時に Anthropic/iStock が詰まるので job concurrency は 1-2 に制限する。
- 実装場所:
  - FastAPI の `POST /case` handler
  - `GET /case/{id}` template
  - 新規 `GET /case/{id}/candidates`
  - 既存の候補生成処理: `llm_rerank` / `rank_hits` / `candidate_reranker` を呼んでいる service 関数
- 実装手順:
  1. `POST /case` は docx parse と case 保存だけ行い、`candidate_status=queued` を保存して即 303 redirect。
  2. 既存の同期候補生成を `generate_candidates_for_case(case_id)` に切り出し、FastAPI `BackgroundTasks` または専用 `ThreadPoolExecutor` に投げる。
  3. `GET /case/{id}` は候補なしでも表示し、「読み込み中」状態を出す。
  4. `GET /case/{id}/candidates` は `{status, progress, candidates, error}` を返す。JS は 2-3 秒間隔で poll。
  5. 完了時に DOM 差し替え。失敗時は retry ボタンと error を表示。

## 2. 次点 (Should do next): B. First-paint with legacy, then upgrade
- 効果: **候補の初回表示 80s → 15-30s**。legacy が cache だけで返せるなら 0.5-2s。最終的な AI rerank 完了は 80s 前後。
- 難易度: M
- リスク: 初期候補と AI 後候補が変わるため、編集中に勝手に差し替えると混乱する。ユーザーが選択済みなら自動置換せず「AI候補を反映」ボタンにする。
- 実装場所:
  - 候補生成 service の mode 分岐
  - `query_plan` / legacy ranking 側
  - `GET /case/{id}/candidates` の response に `stage=legacy|llm_rerank` を追加
  - candidate card template / JS
- 実装手順:
  1. upload 後、まず `legacy_fast` job を走らせる。各 slot は primary 1 query、`collect_until=6-8` 程度に制限。
  2. legacy 候補が出たら `status=partial` として UI に表示。
  3. 続けて `llm_rerank` job を background で実行。
  4. 完了時に `status=ready, stage=llm_rerank` を返す。
  5. ユーザー未選択なら自動差し替え、選択済みなら明示ボタンで反映。

## 3. 後回し (Nice-to-have):
- **D. Persistent Chromium**
  - 効果: final 80s → 45-60s 目安。Chromium 起動 5s × 検索回数の無駄を削れる。
  - 難易度: M/L
  - リスク: Playwright の lifecycle、page leak、crash recovery、thread safety。`_MIN_SEARCH_INTERVAL_S=2.5s` は維持。
  - 実装場所: `crawl_search`, iStock crawler module, FastAPI startup/shutdown。

- **G. Prompt 圧縮 / max_tokens=300**
  - 効果: LLM 11s → 5-8s 程度。ただし全体 80s → 74-77s 程度で、主因の iStock には効かない。
  - 難易度: S
  - リスク: JSON 欠落、query 品質低下。schema validation と fallback 必須。
  - 実装場所: `LlmQueryPlan` generator / Anthropic call。

- **F. Slot 数削減**
  - 効果: 6 slots 80s → 3-4 slots で 45-55s 程度。iStock 呼び出し数にほぼ線形で効く。
  - 難易度: S/M
  - リスク: 編集者が必要な h4 候補を見られない。緊急モードとしては有効。
  - 実装場所: docx parse 後の slot selection / h4 extraction。

- **C/H/E**
  - Anthropic Batch API は対話用途には遅くなりがち。Streaming も JSON 完了前に iStock に進めないので効果薄。
  - HTTP/2/pool 調整は微改善止まり。
  - parse_docx 中 pre-warm は parse が短いなら効果が小さい。

- **I. iStock を JSON-RPC/RSS に変更**
  - 効果は大きい可能性があるが、規約・壊れやすさ・調査コストが高い。今の緊急対応には不向き。

## 補足: ユーザーが今すぐ使えるようにする緊急回避策

設定変更だけなら、まず一時的に **`CMS_ENTRY_ASSISTANT_SEARCH_MODE=legacy`** に落とすのが現実的です。これで LLM 11s と LLM query 分の iStock 呼び出しを外せます。

ただし fresh iStock が残るなら、legacy 単体でも <30s を保証できない可能性があります。確実に <30s を狙うなら、併せて **hero + h4 上位 2-3 件に制限**、`collect_until` を 6-8 に下げる緊急モードが必要です。`_MIN_SEARCH_INTERVAL_S=2.5s` は下げない方がよいです。
