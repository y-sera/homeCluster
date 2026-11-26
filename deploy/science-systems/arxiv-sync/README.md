# arXiv の最新化ジョブ (arxiv-sync)

HF のデータセット (secemp9/arxiv-complete, メタデータ 2026-08-30 / ファイル 2026-09-05 時点) から作った
`iceberg.arxiv` の生データのテーブルを最新に保つ CronJob. 設計は `memo/arxiv-research-search_design.md` の 5.2.

| CronJob | 頻度 | 取得元 | 更新するテーブル | 費用 |
|---|---|---|---|---|
| `arxiv-oai` | 毎日 06:00 JST | OAI-PMH arXivRaw (`https://oaipmh.arxiv.org/oai`) | metadata, versions (MERGE) | なし |
| `arxiv-pdf` | 毎週日曜 09:00 JST | GCS の公開ミラー `gs://arxiv-dataset` (匿名) | pdf, paper_files (add_files), versions (has_pdf など) | なし |
| `arxiv-source` | 毎月 15 日 09:00 JST | arXiv の S3 `s3://arxiv/src/` (requester-pays) | source, latex, paper_files (DELETE + add_files) | 約 $0.09/GB |
| `arxiv-text` | 毎月 16 日 09:00 JST | latex (上のソースの取り込みの結果) | paper_text (DELETE + add_files) | なし |

```bash
kubectl apply -k deploy/science-systems/arxiv-sync           # コード (src/) は ConfigMap になる
kubectl -n science-systems create job arxiv-oai-manual --from=cronjob/arxiv-oai
kubectl -n science-systems logs -f job/arxiv-oai-manual        # 最後に OK
```
引数を変えて 1 回だけ動かす場合は CronJob の jobTemplate を Job にして `exec python /app/<script>.py` に引数を足す
(例: `oai.py --from 2026-08-27`, `pdf.py --limit 50`, `source.py --dry-run`).

## 方式
- **書き込み**: 新しい行は parquet (ZSTD) にして RustFS に置き, Trino の `add_files` で登録する. pdf / source などの大きな中身を
  Trino に通さず, 既存の parquet (HF からコピーした約 15TB) も書き換えない. 更新・削除は Trino の MERGE / DELETE (merge-on-read).
  置き場所: `s3://arxiv-warehouse/arxiv/<テーブル>/data/sync/<run>/`
- **一時テーブル**: OAI の差分は `iceberg.arxiv_staging.oai_{m,v}_<run>` に置いて MERGE し, 終わったら登録を外してファイルを消す
- **状態**: `iceberg.arxiv_sync.src_chunks` (処理済みの S3 の tar. ファイル名と md5), `iceberg.arxiv_sync.src_payloads`
  (論文ごとに取り込んだソースの tar と時点). OAI は metadata の `oai_datestamp` の最大値の前日から取る (重なりは MERGE で吸収)
- **生データの保存**: OAI の応答 `s3://arxiv-raw/oai/arXivRaw/<run>/`, S3 の manifest `s3://arxiv-raw/s3/manifests/<run>/`
- **途中で止まった場合**: そのまま再実行してよい. pdf は開始時に paper_files から versions を合わせ直す.
  source は tar ごとに「処理済み」を記録するため, 未処理の tar から続ける (書きかけの論文は置き換え直す)

## OAI-PMH (oai.py)
- 1 ページ約 1,300 件, ページの間は 5 秒. 2026-08-27 からの初回の追い付き (2026-11-25) は 136 ページ 176,728 件, 約 23 分
- metadata は全列を置き換える. versions は OAI 由来の列 (日付, 大きさ, license, is_latest_version など) を更新し,
  ファイル由来の列 (has_pdf, pdf_sha256, has_ps など) は残す. 新しい版はファイル由来の列を「なし」で追加する
- HF のデータセットとの違い: abstract などの前後の空白を除く (HF と同じ). `acm_class` は OAI の値をそのまま使う
  (HF は msc_class と同じ値になっていたが, 現在の OAI は正しい ACM の分類を返す. 更新された論文から置き換わる)

## PDF (pdf.py)
- 対象: PDF のない版 (大きさ 0kb の版を除く) で, 版の日付が 400 日以内のもの. それより古い欠落は arXiv にもないことが多い (HF の README)
- ミラーのパス: `arxiv/arxiv/pdf/2508/2508.00010v2.pdf`, 旧形式は `arxiv/hep-th/pdf/9901/9901001v1.pdf`.
  HF のデータセットの PDF と同じ出所 (同じ版の sha256 が一致することを確認). GCS の MD5 と照合する
- ミラーは arXiv より数日〜数週間遅れる. 404 の版は次回に再び試す
- source 列は `gcs` (HF と同じ). is_latest_pdf は PDF のある最大の版

## ソース (source.py)
S3 のソースは論文ごとに 1 つ (tar を作った時点の最新版. 版の番号はない. HF と同じく version は NULL).
arXiv は新しい月の tar に加えて過去の月の tar も作り直す (2026-09〜11 には 2409〜2411, 2509〜2511, 2606〜2607 も).
作り直された tar を丸ごと取ると初回 約 1.9TB ($170) かかるため, 必要な論文だけ取る:
1. 必要な論文: ソースがない新しい論文 (`--new-since` 2026-08-01 以降の投稿, または ID が 2608 以降) と,
   前回の取り込み (HF の論文は `--baseline` 2026-09-05) より後に新しい版が出た論文
2. manifest で変わった tar のうち, 必要な論文を含むもの (`first_item` / `last_item` で判定) だけを読む.
   ソースのない論文は処理済みの tar からも探す. tar の範囲に入るのに中身がない論文は src_payloads に `n_files = 0` で記録し,
   新しい版が出るまで探さない
3. 必要な論文が tar の 30% 以上なら tar ごと (16MB ずつ範囲指定で並列に取る. 全体で 64 接続. manifest の md5sum で照合), それ未満なら tar の見出し (512 バイト) を範囲指定で順に読んで目次を作り,
   必要な論文だけ範囲指定で取る (見出しの読み取りは 1 件 1 回の GET. 約 0.2 秒/件)
4. 既存の行 (source / latex / paper_files) を DELETE してから add_files で追加する
- 展開は HF のデータセットと同じ (25 論文で path と sha256 が一致することを確認): `<ID>.gz` が tar ならその中の通常ファイル
  (先頭の `./` を除く. UTF-8 でない名前は percent エンコード), tar でなければ `<ID>.tex` (空は `<ID>.bin`),
  `<ID>.pdf` は PDF だけの投稿. 旧形式の ID は `hep-th/9901001` → `hep-th9901001`
- latex は source のうち拡張子 (最後の英数字の部分, 小文字) が tex / bib / bbl / sty / cls / bst / clo / cfg / ltx / tikz / def / ins / dtx のもの
- 1 回の取得量の上限 `--max-gb` (既定 200GB). 超えたら残りは次回
- **AWS (us-east-1) までの速度**: 1 接続あたり 0.1〜1MB/s と遅く, 時間帯で大きく変わる (GCS は 1 接続 5MB/s 以上).
  tar を 1 接続で流すと全体で 1〜6MB/s だったため, 範囲指定の並列取得にして約 9.5MB/s (2026-11-26)
- manifest の `size` は実際の tar より少し小さい (tar の末尾の分). 大きさは HEAD で取る. `md5sum` は ETag と一致する
- paper_text はここでは作らない (翌日の `arxiv-text` が作る)
- 初回のみ: `source.py --init-baseline` で, manifest の tar のうち 2026-09-05 以前のものを処理済みとして記録する (2026-11-25 実施. 12,312 個)
- メモリ: 4 並列で約 5.5GiB (上限 12Gi). 書き込みは 1GB ごと (`--flush-gb`)

## 初回の追い付きの結果 (2026-11-25〜26)
| | 件数 | 時間 |
|---|---|---|
| OAI | 176,728 件 (論文 3,148,796 → 3,268,184) | 23 分 |
| PDF | 180,243 版, 629.9GB (404: 25,107 版. ミラーにまだない) | 4 時間 38 分 |
| ソース | 約 11.7 万論文 (2606〜2610 のほぼ全部と新しい版の論文), 約 190 万ファイル. 転送 約 390GB (約 $35) | 約 13 時間 |
| paper_text | 117,411 論文を組み立て (single 73,144 / resolved 38,370 / fallback 890. TeX なし 5,007 は行なし). 本文 11.6GB.
  paper_text は 2,856,227 → 2,965,036 行. 2606〜2610 の TeX のある論文は全部 | 35 分 |
- 2611 の論文 (約 3.3 万) は arXiv がまだ tar にしていないため次回 (12-15) に取る
- 途中の失敗: PDF は 1,000 行分を溜めて OOM (6Gi) → 50 行ずつ書く. ソースは 2GB 分の中身を一度に arrow にして OOM (8Gi) →
  2,000 行ずつ arrow にする. いずれも再実行で続きから再開できた

## 本文の組み立て (text.py, assemble.py)
HF のデータセットの paper_text と同じ方式で, latex の TeX から論文ごとに 1 つの文字列を作る. HF は組み立てのコードを公開していないため,
README の説明と実際の行から方式を割り出し, 2,230 論文 (single 877, resolved 1,259, fallback 94) で HF の行と比べた:
**text_sha256, main_file, resolution, n_tex_files, n_files_used, n_unused_files, text_encoding のすべてが 99.8% (2,226 件) で一致**
- TeX ファイルは ext が tex / ltx のもの. 1 つなら `single`
- 複数なら主ファイルを選ぶ: `\documentclass` か `\documentstyle` を含むファイル (コメントの中も数える. HF と同じ) のうち,
  階層が最も浅く, その中で最も大きいもの. そこから `\input` / `\include` / `\subfile` を深さ優先でたどる (コメントは除く.
  拡張子の省略, 親のディレクトリからの相対, マクロを含むパスは名前が一意に一致すれば採る)
- 展開したファイルの合計が TeX の総バイトの半分以上なら `resolved`: **ファイルをその位置に展開せず, たどった順に丸ごと連結する**
  (そのため取り込んだファイルは主ファイルの `\end{document}` の後ろに来る). 半分未満または主ファイルがなければ
  `fallback`: 全 TeX ファイルを path 順に連結
- 最後に .bbl をすべて path 順に付ける (`\input` されていても展開には含めず, 最後に付ける)
- 連結は改行 1 つ. 全体を UTF-8 で解釈し, 失敗したら latin-1 (`text_encoding`)
- title / abstract / primary_category / license は metadata の現在の値
- 対象: ソースを取り込んだ論文のうち, 前回の組み立て (`iceberg.arxiv_sync.text_builds`) より新しいものと, TeX があるのに
  paper_text のない論文. 新しいソースに TeX がない論文 (PDF だけの版になったなど) は行を消す (HF も PDF だけの論文の行はない)
- 一致しない 4 件: 主ファイルの候補が 2 つあり HF だけ fallback にした 2 件, 細かな展開の違い 2 件

## 注意
- 3 つのジョブは同じテーブル (versions, paper_files) に書くため同時に動かさない (Iceberg のコミットが衝突する). 時刻をずらしてある
- add_files で追加した小さなファイルと MERGE / DELETE の削除ファイルが増えるため, metadata / versions / paper_files は
  ときどき `ALTER TABLE ... EXECUTE optimize` する (CTAS で作ったテーブル). **pdf / source / latex には OPTIMIZE + expire_snapshots をしない**
  (HF からコピーした parquet が消える. `memo/arxiv_iceberg_tables_memo.md`)
- 認証: Trino は admin, RustFS は root (arxiv-raw と arxiv-warehouse の両方に書くため), AWS は arxiv-aws (読み取り専用)
- 依存パッケージは起動時に pip で入れる (src/requirements.txt で版を固定)
