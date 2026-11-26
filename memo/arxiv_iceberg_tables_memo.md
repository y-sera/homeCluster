# arXiv の Iceberg テーブル (手順 5b) メモ

HF のデータセット (secemp9/arxiv-complete) を Iceberg テーブルにする手順と結果.
カタログは Polaris の `arxiv` (Trino では `iceberg`), 名前空間 `arxiv`. 前提: 手順 5a のコピーと確認 (`arxiv-migrate-verify`) が済んでいること.

## テーブル
| テーブル | 作り方 | 場所 | 分割 / 並び | 行数 |
|---|---|---|---|---|
| paper_text | add_files (書き直さない) | `arxiv/paper_text/iceberg` (データは `arxiv/paper_text/data/hf/`) | なし / paper_id | 2,856,227 |
| latex | add_files | `arxiv/latex/iceberg` | なし / paper_id | 12,348,082 |
| ps | add_files | `arxiv/ps/iceberg` | なし / paper_id | 2,237,839 |
| source | add_files | `arxiv/source/iceberg` | なし / paper_id | 47,373,604 |
| pdf | add_files | `arxiv/pdf/iceberg` | なし / paper_id | 4,973,956 |
| metadata | CTAS (書き直し) | `arxiv/metadata-<uuid>` | year(first_version_date) / paper_id | 3,148,796 |
| versions | CTAS | `arxiv/versions-<uuid>` | なし / paper_id, version | 5,030,612 |
| paper_files (元の files) | CTAS | `arxiv/paper_files-<uuid>` | なし / paper_id, version, path | 54,597,326 |

場所は `s3://arxiv-warehouse/` からの相対パス. 列は元の parquet と同じ (設計メモ 3 章).
中身系の並び (sorted_by) は今後の書き込み用の設定. 既存の parquet はもともと paper_id の辞書順 (新形式 ID が先, 旧形式が後) のため,
`WHERE paper_id = ...` はファイルの統計 (最小・最大) で読み飛ばせる (paper_text で 1 件 6 秒程度. 確認時は RustFS が混雑していた).

## 手順
```bash
M=deploy/science-systems/manifests
# 1. 中身系 (add_files. フッターを読むだけで 30〜45 分程度. 確認の Job と並行してよい)
kubectl apply -f $M/arxiv-iceberg-content-job.yaml
kubectl -n science-systems logs -f job/arxiv-iceberg-content            # ALL OK
# 2. 索引系 (書き込みを伴うため確認の Job の完了後)
kubectl apply -f $M/arxiv-iceberg-index-job.yaml
kubectl -n science-systems logs -f job/arxiv-iceberg-index -c stage
kubectl -n science-systems logs -f job/arxiv-iceberg-index -c build     # ALL OK
# 3. 作業用のテーブルとファイルの削除 (2 の確認後)
#    Trino (admin): CALL iceberg.system.unregister_table(schema_name => 'arxiv_staging', table_name => 'metadata') など 3 つ (metadata / versions / paper_files)
#                   DROP SCHEMA iceberg.arxiv_staging
#    RustFS (root): s3://arxiv-warehouse/arxiv_staging/ を削除 (元の parquet は arxiv-raw に残る)
```

## 注意点
- **add_files で登録したテーブルに OPTIMIZE と expire_snapshots を続けて実行しない.** OPTIMIZE で書き直された元の parquet
  (コピーしたデータ, 約 15TB) は, 古いスナップショットを消した時点で Trino が削除する (Polaris の purge の無効は DROP TABLE にしか効かない).
  DELETE / UPDATE / MERGE は削除ファイルを追加する方式 (merge-on-read) のため元の parquet は書き換えない
- `remove_orphan_files` はテーブルの場所 (`.../iceberg`) の下だけを見るため, `data/hf/` のコピー済みの parquet は対象外
- Trino の `add_files` はカタログの設定 `iceberg.add-files-procedure.enabled=true` が必要 (既定は無効). `add_files_from_table` は Hive のテーブルが元のため使わない
- Trino は既にファイルがある場所にテーブルを作れない ("Cannot create a table on a non-empty location").
  そのためコピー済みのデータ (`arxiv/<テーブル>/data/hf/`) の隣の空の場所 `arxiv/<テーブル>/iceberg` をテーブルの場所にした.
  データファイルはテーブルの場所の外でもよい
- Polaris はテーブルの場所を名前空間のパス (`s3://arxiv-warehouse/<名前空間>/`) の下に制限する
- 元の parquet は Iceberg のフィールド ID を持たない. Trino の add_files が `schema.name-mapping.default` (列名 → フィールド ID) を
  テーブルに設定するため, 列名で読める. 列を改名する場合は name mapping も直す必要がある (改名しないこと)
- add_files はファイルのスキーマを確認しない. 列の型と名前は元の parquet に合わせる (全シャードで同じスキーマであることを確認済み)
- 元の timestamp[ms] (タイムゾーンなし) は Iceberg の timestamp (マイクロ秒) としてそのまま読める
- iSCSI が混雑している間 (確認の Job の実行中など) は RustFS の書き込みが 1 件数秒かかり, 並べ替えて書く CTAS が
  `HIVE_WRITER_CLOSE_ERROR` で失敗することがあった (2 回目は成功. versions の CTAS に 232 秒).
  並べ替えの作業ファイルは `<テーブルの場所>/data/trino-tmp-files/` に書かれ, 失敗すると残る
- **テーブル名 `files` は使えない.** Trino は `files` / `history` / `snapshots` / `manifests` / `partitions` / `refs` など
  Iceberg のメタデータテーブルの種類と同じ名前を「テーブル <名前空間> の `$files`」と解釈する (Polaris に `namespaces//tables/<名前空間>` を問い合わせる).
  作成は "already exists" / IF NOT EXISTS は何もせず成功, 参照は "Metadata not found in metadata location" になる. そのため元の files は `paper_files` にした
- mcp_reader は名前空間 `arxiv` のみ読める (`arxiv_staging` は不可). pdf / source / ps / latex の content 列は拒否 (trino/values/values.yaml)
- Trino の動作確認の Job (`trino/manifests/smoke-test-job.yaml`) は iceberg.arxiv.latex を作って消すため, 今後は実行しない

## 最新化 (2026-11-26 から)
- `deploy/science-systems/arxiv-sync/` の CronJob 3 つで生データのテーブルを最新に保つ (詳細は同ディレクトリの README):
  OAI-PMH → metadata / versions (日次), GCS の公開ミラー → pdf (週次), arXiv の S3 → source / latex (月次)
- 追加の行は `arxiv/<テーブル>/data/sync/<run>/` に置いて add_files. 置き換えは DELETE (merge-on-read の削除ファイル).
  HF からコピーした `data/hf/` の parquet は書き換えない
- 状態のテーブル: 名前空間 `arxiv_sync` (src_chunks, src_payloads). OAI 用の一時テーブルは `arxiv_staging` に作って消す
- 初回の追い付き (2026-11-25〜26) で, pdf に `data/sync/` の parquet 約 180 個 (630GB), source / latex / paper_files に
  約 320 回分の add_files と DELETE が入った. 小さなファイルと削除ファイルが増えるため, metadata / versions / paper_files は
  ときどき OPTIMIZE する. **pdf / source / latex は OPTIMIZE + expire_snapshots をしない** (上記)
- paper_text は作り直していない (HF のデータセットにない論文と, ソースを置き換えた論文の本文は古いかない). 後続の加工で作る
