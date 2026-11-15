#!/usr/bin/env bash
# コピー先 ($DEST) の内容をコピー元スナップショットと照合する.
#   既定: 全ファイルのサイズ一致を確認 (速い)
#   --sha <org>/<name> ...: 指定リポジトリの LFS ファイルを SMB から読み戻し,
#                            sha256 を HF の blob 名 (= LFS の sha256) と照合する (遅い. 1GbE で約110MB/s)
set -uo pipefail
HUB="${HUB:-$HOME/.cache/huggingface/hub}"
DEST="${DEST:-/mnt/llm-models}"
mountpoint -q "$DEST" || { echo "$DEST がマウントされていません" >&2; exit 1; }

ng=0
check_repo() {   # $1=.hf-revision のあるディレクトリ, $2=sha 確認するか
  local dst="$1" dosha="$2" src f rel blob want got
  src="$(sed -n 's/^source: [^:]*://p' "$dst/.hf-revision")"
  [[ -d "$src" ]] || { echo "NOSRC $dst"; return; }
  while IFS= read -r -d '' f; do
    rel="${f#$src}"
    if [[ ! -f "$dst/$rel" ]]; then echo "MISSING $dst/$rel"; ng=$((ng+1)); continue; fi
    if [[ $(stat -Lc %s "$f") != $(stat -c %s "$dst/$rel") ]]; then echo "SIZE   $dst/$rel"; ng=$((ng+1)); continue; fi
    if (( dosha )); then
      blob="$(basename "$(readlink "$f")")"
      if [[ "$blob" =~ ^[0-9a-f]{64}$ ]]; then
        got="$(sha256sum "$dst/$rel" | cut -c1-64)"
        if [[ "$got" == "$blob" ]]; then echo "SHA-OK $rel"; else echo "SHA-NG $dst/$rel"; ng=$((ng+1)); fi
      fi
    fi
  done < <(find "$src" \( -type l -o -type f \) -print0)
}

host="$(hostname)"
if [[ "${1:-}" == "--sha" ]]; then
  shift
  for repo in "$@"; do
    for d in "$DEST/$repo" "$DEST/$repo"@*; do
      [[ -f "$d/.hf-revision" ]] && grep -q "^source: $host:" "$d/.hf-revision" && check_repo "$d" 1
    done
  done
else
  while IFS= read -r meta; do
    grep -q "^source: $host:" "$meta" || continue
    check_repo "$(dirname "$meta")" 0
  done < <(find "$DEST" -maxdepth 4 -name .hf-revision)
fi
echo "NG=$ng"
exit $(( ng > 0 ))
