前提: こちらでは `bwrap` 制約で実ファイルパス確認ができなかったため、提示された `photo_preferences` / `candidate_reranker` 名ベースで書きます。

**1. Policy-3 設計**
Must: 候補 d。`candidate_reranker` 側で明示違反を hard-filter し、さらに `policy_score` を式に入れる。tie-break だけでは弱いです。

`candidate_reranker` の `rank_raw` を例えば以下に変更します。

```python
policy = evaluate_editorial_people_policy(c, query_context)

if policy.hard_block:
    blocked.append(c)
    continue

rank_raw = (
    0.40 * intent_score
    + 0.20 * query_score
    + 0.30 * policy.normalized_score
    + 0.10 * baseline_score
    - violation_penalty
)
```

Should: `photo_preferences._editorial_people_policy_score()` は int だけでなく、`hard_block`, `ambiguous_person`, `safe_reason`, `reasons` を返す評価関数に分ける。

```python
evaluate_editorial_people_policy(hit, query_context) -> PolicyEval
```

Nice: `PreferencesStore.score_hit()` を実装し、`rank_hits` と `candidate_reranker` が同じ policy 評価を使うようにする。今の「常に 0」はバグ扱いで直すべきです。

**2. alt 中立候補の扱い**
Must: 全除外ではなく soft-demote。人物語があるのに `日本人` / `後ろ姿` / `手元` / `シルエット` がない候補は安全ではないので、抽象・グラフ・手元候補より下げる。

例:

```python
if people_bearing and not japanese_hint and not no_face_hint:
    score -= 6
    ambiguous_person = True
```

Should: 非人物の抽象候補を明確に boost する。現状の関数は人物でも顔なしでもない候補を `0` にしているため、編集部ポリシー 3 が弱いです。

```python
if abstract_or_symbol_or_graph:
    score += 10
```

Nice: 最終表示枠は `safe` → `ambiguous_person` の順に埋める。明示違反は最後まで出さない。

**3. iStock 取得時点で捨てるべきか**
Must: API 取得時点では捨てず、取得後に `policy_eval` を付与して、最終表示前に hard-filter する。取得時点で捨てると候補不足とデバッグ不能が起きます。

Should: 取得件数を増やす。例えば各 query で 30-50 件取り、dedupe 後に rerank/filter する。安全候補が不足したら `グラフ`, `矢印`, `手元`, `後ろ姿`, `シルエット`, `日本人` 系の追加 query を走らせる。

Nice: hard-block 候補はログ・検証用には残すが、UI には出さない。理由付きで保存すると改善が速いです。

**4. テストデータ**
Must: 入れるべきです。貼られた 27 件では、明示違反は最低 4 件あります。

- `カメラを見ている...肖像画`
- `微笑む実業家`
- `黒人起業家`
- `幸せな中年のビジネスウーマン...` は happy/smiling 系として block または強 demote

`tests/test_photo_policy.py` に fixture 化して、hard-block が最終 top N に入らないことを assert します。

Should: `tests/test_candidate_reranker.py` で、同じ 27 件を rerank し、`top10_violation_count == 0` と `displayed_violation_count == 0` を見る。

Nice: CI で以下をメトリクス化する。

```text
before: explicit violations = 4 / 27
after: displayed explicit violations = 0
neutral people demoted below abstract/no-face candidates
```

最優先の実装順は、`candidate_reranker` の hard-filter、共通 `PolicyEval` 化、抽象候補 boost、fixture テスト追加です。
