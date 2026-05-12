#!/usr/bin/env bash
# W1 bench harness: sweep tiny -> base -> small on the user's real episode.
# Logs append to benchmarks.jsonl; each model gets its own transcript file.
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"

EP="${1:-samples/文系AI部　第1回　素材.m4a}"
OUT_DIR="samples/bench_w1"
mkdir -p "$OUT_DIR"

echo "=== W1 bench start: $(date -Is) ==="
echo "Episode: $EP"
ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$EP"

for M in tiny base small; do
  echo
  echo "=== $(date -Is)  model=$M  begin ==="
  uv run podedit transcribe "$EP" \
    --model "$M" \
    --device auto \
    -o "$OUT_DIR/ep1.${M}.transcript.json"
  echo "=== $(date -Is)  model=$M  end ==="
done

echo
echo "=== W1 bench complete: $(date -Is) ==="
