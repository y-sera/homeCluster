#!/usr/bin/env bash
# HF キャッシュ (~/.cache/huggingface/hub) のスナップショットを,
# SMB マウントした llm-models へシンボリックリンクを解決した実体ファイルとしてコピーする.
#
#   models--<org>--<name>/snapshots/<rev>/   -> $DEST/<org>/<name>/
#   datasets--<org>--<name>/snapshots/<rev>/ -> $DEST/datasets/<org>/<name>/
#   スナップショットが複数ある場合, 最新 (mtime) 以外は <name>@<rev先頭8桁>/
#   各ディレクトリに .hf-revision (repo, revision, コピー元ホスト) を書く.
#
# 使い方 (一般ユーザーで実行):
#   ./migrate_models.sh --dry-run      # 対象一覧と容量だけ表示
#   ./migrate_models.sh                # コピー実行 (再実行で続きから)
set -uo pipefail

HUB="${HUB:-$HOME/.cache/huggingface/hub}"
DEST="${DEST:-/mnt/llm-models}"
LOGDIR="${LOGDIR:-$HOME/model-migration/logs}"
DRY=0
[[ "${1:-}" == "--dry-run" ]] && DRY=1
HOST="$(hostname)"

# 移行しないもの (ホストごと). 理由はコメント参照.
case "$HOST" in
  spark1) SKIP=(
    "models--deepseek-ai--DeepSeek-V4-Flash"               # 不完全 (90/160GB). spark2 側の完全版を使う
    "models--Mia-AiLab--GLM-5.3-Flash-EXL3-4bpw-TensorFold" # spark2 と同一 revision. spark2 側からコピー
    "models--incoai--GLM-5.3-Flash-DFlash2"                # spark2 と同一 revision. spark2 側からコピー
    "datasets--secemp9--arxiv-complete"                    # science-systems/arxiv-datasets に全量あり
  ) ;;
  spark2) SKIP=(
    "models--Mia-AiLab--GLM-5.3-EXL3-2.75bpw-TensorFold"   # 不完全 (15/293GB)
    "models--drowzeys--keys-DeepSeekV4Flash-Vision-EXP-ablit" # 実体なし (0/168GB)
  ) ;;
  *) echo "unknown host $HOST" >&2; exit 1 ;;
esac

if (( ! DRY )); then
  mountpoint -q "$DEST" || { echo "$DEST がマウントされていません" >&2; exit 1; }
  mkdir -p "$LOGDIR"
fi

is_skipped() { local x; for x in "${SKIP[@]}"; do [[ "$1" == "$x" ]] && return 0; done; return 1; }

total=0; fail=0
for repo_dir in "$HUB"/models--* "$HUB"/datasets--*; do
  [[ -d "$repo_dir" ]] || continue
  base="$(basename "$repo_dir")"
  if is_skipped "$base"; then echo "SKIP  $base"; continue; fi
  kind="${base%%--*}"                 # models | datasets
  rest="${base#*--}"; org="${rest%%--*}"; name="${rest#*--}"
  prefix=""; [[ "$kind" == "datasets" ]] && prefix="datasets/"
  mapfile -t snaps < <(ls -1t "$repo_dir/snapshots" 2>/dev/null)
  for i in "${!snaps[@]}"; do
    rev="${snaps[$i]}"
    src="$repo_dir/snapshots/$rev/"
    dst="$DEST/$prefix$org/$name"
    (( i > 0 )) && dst="$dst@${rev:0:8}"
    size=$(du -sLb "$src" 2>/dev/null | cut -f1)
    total=$((total + size))
    printf '%-6s %8.1fG  %s/%s@%s -> %s\n' COPY "$(echo "$size/1000000000" | bc -l)" "$org" "$name" "${rev:0:8}" "${dst#$DEST/}"
    (( DRY )) && continue
    log="$LOGDIR/${org}--${name}@${rev:0:8}.log"
    if rsync -rtL --no-perms --partial --mkpath --info=progress2,stats1 "$src" "$dst/" >"$log" 2>&1; then
      printf 'repo: %s/%s\nkind: %s\nrevision: %s\nsource: %s:%s\n' \
        "$org" "$name" "$kind" "$rev" "$HOST" "$src" > "$dst/.hf-revision"
      echo "  done"
    else
      echo "  FAILED (see $log)"; fail=$((fail + 1))
    fi
  done
done
printf 'TOTAL %.2f TB, failed=%d\n' "$(echo "$total/1000000000000" | bc -l)" "$fail"
exit $(( fail > 0 ))
