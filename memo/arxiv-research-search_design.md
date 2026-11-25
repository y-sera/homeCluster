# arXiv 研究用検索システム 設計メモ

本ドキュメントはブランチ `feature/arxiv-research-search` で管理する. 作業 (マニフェストなど) は main にコミットする.

## 1. 概要
science-systems に置いた arXiv データセット (Hugging Face `secemp9/arxiv-complete`) を元に,
LLM のエージェントが MCP 経由で論文を検索・抽出・表示・解説し, 関連論文を辿れる研究用の検索システムを作る.

- データの正本は Apache Iceberg (S3 互換ストレージ RustFS 上) に置き, カタログは Apache Polaris を使う
- 本文の加工結果 (正規化した節, チャンク, 埋め込み) も Iceberg の派生テーブルに置き, 検索インデックスはそこから作り直せるようにする
- エージェントからの検索・閲覧は, 検索インデックス (OpenSearch), 引用グラフ (PostgreSQL) で高速に応答する
- OpenSearch は専用の検索ノード (Intel NUC) に置き, データはノードのローカル NVMe 上の PV に置く
- arXiv の S3 バケット (本文) と OAI-PMH (メタデータ), OpenAlex (引用) から定期的に最新化する
- MCP サーバーを自宅ネットワーク内に公開し, ローカル LLM と Claude / OpenAI のどちらからでも使えるようにする

## 2. ユースケース
| エージェントの動作 | 提供する機能 |
|---|---|
| 質問 (日本語可) から論文を探す | ハイブリッド検索 (BM25 + ベクトル, 多言語埋め込み, RRF で統合), カテゴリ・期間・著者での絞り込み |
| 該当部分を抜き出す | 本文チャンクの検索 (BM25), 節の種類 (手法・実験など) での絞り込み |
| 表示する | 書誌情報, arXiv の abs / PDF へのリンク (arXiv の規約上リンクは必須), 必要に応じて PDF |
| 解説する | LLM が目次と必要な節だけを読む (解説自体は LLM 側) |
| 関連論文を辿る | 参照文献 / 被引用 (自前抽出 + OpenAlex), 内容の近い論文 (埋め込みの近傍), 同じ著者・カテゴリ |
| 傾向を分析する | Trino への読み取り専用 SQL |

- 検索の対象は各論文の最新版とする. 過去の版は閲覧 (版を指定した `read_section`) と SQL で扱う

## 3. 現状のデータ (2026-11-18 時点)
- PVC `science-systems/arxiv-datasets` (qnap-smb, RWX, 17Ti) に HF のリポジトリ構成のまま約 15TB (RustFS への移行後, 2026-11-25 に削除)
  (取得 Job: `deploy/science-systems/manifests/hf-downloader-job.yaml`. PVC の削除に合わせて 2026-11-26 に削除)
- スナップショット: メタデータ 2026-08-30 (OAI-PMH arXivRaw), ファイル 2026-09-05. データセット側の自動更新はない

| config | 1 行 | 行数 | parquet | 主な列 |
|---|---|---|---|---|
| metadata | 論文 | 3,148,796 | 1.6G | paper_id, title, authors, abstract, categories, primary_category, license, doi, ... |
| versions | 論文 × 版 | 5,030,612 | 257M | paper_id, version, version_date, has_pdf, pdf_sha256, is_latest_version, ... |
| files (Iceberg では paper_files) | ファイル (中身なし) | 54,597,326 | 2.3G | paper_id, version, kind, source, path, size, sha256, stored_as, ... |
| paper_text | 論文の TeX 本文 | 2,856,227 | 66G | paper_id, text (large_string), main_file, resolution, ... |
| latex | TeX 関連ファイル | 12,348,082 | 150G | paper_id, path, ext, size, sha256, content (large_binary) |
| source / pdf / ps | ファイルの中身 | 4,737万 / 497万 / 224万 | 6.0T / 7.9T / 634G | paper_id, version, kind, source, path, size, sha256, content |

- ZSTD 圧縮 (arrow 25). 中身の列は large_binary で, 行グループは 200 行程度
- 各シャードは `paper_id` の辞書順 (新形式 ID が先, `hep-th/...` などの旧形式が後) に並んでいる
- Iceberg のフィールド ID は持たない (登録時は名前で対応付ける)
- paper_text は論文ごとに 1 行で版の列を持たない (1 つの版の本文のみ). 過去の版の本文が必要な場合は source から組み立てる

## 4. 構成
```
 LLM (ローカル: vLLM / クラウド: Claude, OpenAI)
   ↑ OpenAI 互換 API
 LiteLLM ─── Open WebUI (MCP ホスト, 自宅 NW 内)
                 │ MCP (Streamable HTTP)
                 ▼
 arxiv-mcp ── arxiv-api (FastAPI) ─┬─ OpenSearch [search1] : 論文 (hybrid) / 本文チャンク (BM25)
                                   ├─ 質問のベクトル化 [search1, CPU]
                                   ├─ PostgreSQL : 引用グラフ, 書誌キャッシュ
                                   └─ Trino ── Polaris (+PostgreSQL) ── Iceberg on RustFS (worker)
                                                                         ├─ 正本 : metadata / versions / files / paper_text / latex / source / pdf / ps
                                                                         └─ 派生 : paper_sections / paper_chunks / paper_embeddings / citations
 取り込み・加工 (CronJob / KFP)
   日次  : OAI-PMH → metadata/versions を MERGE → 新規論文の埋め込み → 論文索引を更新
   月次  : arXiv S3 (pdf/src) → 中身 → latex → 本文の組み立て → 正規化・節分割 → チャンク → 引用抽出 → 索引を更新
   四半期: OpenAlex スナップショット → 引用の補完
   初回  : HF parquet → Iceberg へ移行, 全件の加工, 論文の埋め込み (DGX Spark の GPU, KFP), 索引作成
 OpenSearch のスナップショット → RustFS (バックアップ)
```

### 構成要素
| 要素 | 配置 | 内容 |
|---|---|---|
| RustFS | worker (ノードは固定しない) | 単ノード単ディスク (standalone). データは qnap-iscsi の PVC (18Ti, シン). 冗長化は QNAP の RAID |
| Polaris + PostgreSQL (CNPG) | worker | Iceberg REST カタログ. catalog `arxiv`, `stsUnavailable: true`, `pathStyleAccess: true` |
| Trino | worker | Iceberg コネクタ (REST カタログ). 分析 SQL と API の裏側 |
| OpenSearch | search1 (NUC) | 単一ノード. データはローカル NVMe の PV. 論文の索引 (314 万件, 埋め込み付き), 本文チャンクの索引 (BM25). スナップショットを RustFS に取る |
| PostgreSQL (CNPG) | worker | 引用グラフ (参照 / 被引用), 書誌キャッシュ |
| 埋め込み | 一括: DGX Spark の GPU (KFP) / 質問: search1 (CPU, KServe) | Qwen3-Embedding-0.6B (初期. 5.5 参照) |
| arxiv-api / arxiv-mcp | science-systems (worker) | FastAPI と MCP サーバー (公式 Python SDK, Streamable HTTP) |
| Open WebUI + LiteLLM | worker | MCP ホストと, ローカル / クラウド LLM の統合 API |
| 検索ノード search1 | Intel NUC 11PAHi5 (i5-1135G7, 64GB, NVMe 500GB, SATA SSD を追加予定) | OpenSearch, 質問のベクトル化, エージェントのサンドボックス (agent-sandbox, 別設計) を同居. 5.10 参照 |

### 選定理由
- **Iceberg**: 日次更新の MERGE (upsert), スナップショットによる再現性 (ML 学習の入力として固定できる), Trino / DuckDB / PyIceberg / Spark から共通に使える. 既存 parquet を `add_files` で書き換えずに登録できる
- **Polaris (Gravitino ではなく)**: 用途が Iceberg のみ, Helm chart と PostgreSQL 永続化, RustFS 用の公式ガイド, `stsUnavailable` で STS なしの S3 互換ストレージに対応, Keycloak 連携のガイドあり.
  Gravitino は Hive / JDBC / fileset などの横断管理が必要になった時に Polaris の上に追加できる
- **OpenSearch (Elasticsearch ではなく)**: Apache 2.0 でライセンス階層がない. BM25 / kNN / hybrid クエリ (RRF) / 絞り込み / 集計を一つで扱える
- **加工結果を Iceberg に置く**: 埋め込みモデルやチャンクの作り方を変えても, OpenSearch を Iceberg から作り直せる. OpenSearch は派生物とし, 単一ノードで運用する
- **OpenSearch を NUC に置く**: 他の候補は次の理由で不採用
  - QNAP (TS-473A) 上の VM: CPU (Ryzen V1500B) が弱く, メモリ 32GB を NAS と分け合う. ディスクが HDD のみで, ランダム読み込みが遅い
  - worker + Longhorn: Longhorn の空きが約 110G (レプリカ 3) で本文の索引が入らない. worker の CPU の空きが 2 コア程度
  - worker + qnap-iscsi: 本文の検索が HDD を iSCSI 越しに読むことになり遅い
  - DGX Spark: 容量と性能は十分だが, メモリが GPU と共用で LLM / 学習と取り合う
- **Open WebUI + LiteLLM**: Open WebUI は Streamable HTTP の MCP に標準対応した Web チャット. LiteLLM でローカル LLM と Claude / OpenAI を一つの OpenAI 互換 API にまとめる.
  MCP の呼び出しは自宅内の Open WebUI から行うため, MCP サーバーは外部に公開しない
- **RustFS の S3 Tables (内蔵 Iceberg カタログ)** は Trino からの書き込みを保証しておらず Iceberg v3 にも未対応のため不採用

## 5. 仕様
### 5.1 データ移行 (案 B: すべて Iceberg on RustFS へ)
- 容量: QNAP の空き 22TB に RustFS (約 15TB) を置き, 検証後に SMB の PVC (約 15TB) を削除する. Synology (約 10TB) は作業領域として使う
- 索引系 (metadata, versions, files): 書き直して Iceberg テーブルにする. 識別キー (paper_id / (paper_id, version)) を設け MERGE 可能にし, 投稿月やカテゴリで分割する
- テキスト・中身系 (paper_text, latex, source, pdf, ps): parquet を RustFS にコピーし `add_files` で登録する (書き直さない). paper_id 順のため統計で読み飛ばせる
- コピー先: 中身系は `s3://arxiv-warehouse/arxiv/<テーブル>/data/hf/<元のファイル名>` (catalog arxiv の既定の場所の下), 索引系と README / SHA256SUMS は `s3://arxiv-raw/hf/arxiv-complete/<元のパス>`
- コピーは rclone の Job (`deploy/science-systems/manifests/arxiv-migrate-copy-job.yaml`) で, RustFS と同じ worker で行う. 確認は SHA256SUMS との照合 (`arxiv-migrate-verify-job.yaml`)
- qnap-vm (QNAP 上の VM) での実行は VM の CPU が上限になり約 49MB/s だったため不採用. worker は 1GbE の送受信を両方使い約 100MB/s (2026-11-21 に測定)
- rclone のマルチスレッドコピーは cifs の先読みと重なり読み込みが約 3 倍に増える (約 30MiB/s) ため無効にする (約 90MiB/s)

### 5.2 最新化 (必須)
| 対象 | 取得元 | 頻度 | 処理 |
|---|---|---|---|
| メタデータ・版 | OAI-PMH arXivRaw (`oaipmh.arxiv.org`) | 日次 | metadata の datestamp の最大値の前日以降を取得し MERGE (metadata は全列, versions は OAI 由来の列のみ. 新しい版はファイル由来の列を「なし」で追加). 新規・更新論文の埋め込みを作り論文索引を更新 |
| PDF | GCS の公開ミラー `gs://arxiv-dataset` (匿名, 無料. HF と同じ出所. ファイル名に版あり) | 週次 | PDF のない版を名前で直接取得 → pdf / paper_files に add_files → versions の has_pdf / pdf_sha256 / is_latest_pdf を更新 |
| ソース | `s3://arxiv/src/` (requester-pays, us-east-1) | 月次 | manifest で変わった tar のうち必要な論文 (ソースのない新しい論文, 新しい版が出た論文) を含むものだけ読む. 必要な論文が多い tar は丸ごと, 少ない tar は見出しを範囲指定で読んで目次を作り必要な分だけ取る → 既存の行を DELETE して source / latex / paper_files に add_files → 本文の組み立て → 正規化・節分割 (5.4) → チャンク → 引用抽出 → 本文索引を更新 |
| 引用 | OpenAlex スナップショット (`s3://openalex`, 認証不要, 無料) | 四半期 | 必要な列だけ抽出し, DOI (arXiv の `10.48550/arXiv.*` を含む) で対応付け |

- 実装: `deploy/science-systems/arxiv-sync/` (CronJob 3 つ. 詳細は README). 本文の組み立て以降は後続の加工 (5.4) で行う
- S3 の tar の性質 (2026-11-25 に確認): tar の中の名前に版がない (`2610/2610.42099.gz`. tar を作った時点の最新版). arXiv は新しい月に加えて
  過去の月の tar も作り直す (2026-09〜11 に 2409〜2411, 2509〜2511, 2606〜2607). 変わった tar を丸ごと取る当初の案は初回 約 1.9TB ($170),
  毎月 $50 前後になるため, 必要な論文だけ取る方式にした. PDF は版つきの GCS のミラーから取る (S3 の PDF は使わない)
- 初回の追い付き (2026-11-25〜): OAI 176,728 件 (論文 +119,388). PDF 約 20 万版 (GCS, 無料). ソース 約 18 万論文, 転送 約 400GB (約 $36)
- 費用: ソースの転送料 約 $0.09/GB. 毎月は新規投稿の分 (約 130GB) と新しい版で $10〜15/月程度の見込み. AWS の認証情報は Vault + ESO
- arXiv の規約: 全件クロールはせず S3 で取得する. 全文を使うサービスでは arXiv へのリンクを表示する
- 新しい版が出た論文は, 本文索引の該当 paper_id のチャンクを削除して入れ直す (索引は常に最新版のみ)

### 5.3 関連論文
- 自前抽出: latex の `bbl` / `bib` から arXiv ID と DOI を抽出して arXiv 内の引用を作る (月次で更新)
- OpenAlex: arXiv 外の文献と被引用を補う
- 内容の近い論文: 論文の埋め込みの近傍
- 引用は Iceberg に記録し, 問い合わせは PostgreSQL で返す (1〜2 段の辿りを想定)

### 5.4 本文の加工 (MVP に含む)
原本の TeX (paper_text, latex) はそのまま残し, 加工結果を派生テーブルに置く. 加工処理はバージョン (`normalizer_version`, `chunker_version`) を持ち, 変えた時は作り直す.

**正規化** (検索と LLM の閲覧に使う. 軽い処理にとどめる)
- コメントとプリアンブルを除く. プリアンブルの単純な `\newcommand` / `\def` (引数なし, 固定引数) は展開する
- 書式コマンドは中身だけ残す (`\textbf{x}` → `x`), `\cite{k}` → `[cite]` (キーは引用抽出用に別に保持), `\ref` / `\eqref` → `[ref]`
- 数式は LaTeX のまま残す. BM25 の対象からは解析器 (char_filter) で外す
- 図表はキャプションを残し, 表は `content_type = table` とする
- `\ref` を番号 (「Equation 3」など) に解決する処理はしない (実質 LaTeX のコンパイルが必要で, 件数に対して割に合わない)

**節とチャンク** (読む単位と検索単位を分ける)
| | 節 (`paper_sections`) | チャンク (`paper_chunks`) |
|---|---|---|
| 用途 | `get_outline` / `read_section` で LLM が読む | `search_passages` で検索する |
| 単位 | `\section` / `\subsection` (階層を保持) | 節を段落で分け, 長い段落は文で分ける. 短い段落は隣と結合 |
| 大きさ | 制限なし (長い節はページ送りで返す) | 目標 500〜800 トークン, 最大 1,000, 最小 100. オーバーラップなし (BM25 のみのため) |
| 主な列 | paper_id, version, section_id, parent_id, level, ordinal, title, section_type, text, n_tokens, normalizer_version | chunk_id, paper_id, version, section_id, section_path, chunk_index, content_type, text, n_tokens, content_hash, chunker_version |

- トークン数は埋め込みモデル (Qwen3-Embedding) のトークナイザで数える (将来チャンクにベクトルを付ける時と揃える)
- `section_type`: 見出しから規則で分類する (intro / related / method / experiment / result / discussion / conclusion / appendix / other)
- `chunk_id` = `{paper_id}:{section_id}:{chunk_index}`. `content_hash` で変更のないチャンクを判定し, 再埋め込みを省く
- チャンクはオーバーラップなしのため, 同じ節のチャンクを順に連結すると節の本文になる
- 新規論文の本文は, HF の paper_text と同等の組み立て処理を月次の取り込みで実装する
- HF の paper_text の resolved は `\input` のファイルを `\end{document}` の後ろに連結している. 正規化では後ろの本文も含める (検証で判明)
- 全件の処理の前にサンプルで検証する (5.7)

### 5.5 埋め込み
| 項目 | 内容 |
|---|---|
| モデル | Qwen3-Embedding-0.6B (初期). BGE-M3 (dense) を比較用. 評価 (5.7) で確定する |
| 対象 (MVP) | 論文 (314 万件). 本文チャンクのベクトルは MVP 後 |
| 文書側の入力 | `Title: ...\n\nAbstract: ...` (インストラクションなし). カテゴリを含める版も評価で比べる |
| 質問側の入力 | 固定のインストラクション付き (例: `Instruct: Given a research question, retrieve relevant arXiv papers\nQuery: {q}`) |
| 次元・正規化 | 1024 次元, L2 正規化 (内積で検索). 次元の削減 (MRL) は評価で判断 |
| 記録する情報 | model, model_revision (固定), input_template_version, dim, normalize. `paper_embeddings` と索引のメタデータに記録 |
| 一括処理 | DGX Spark の GPU, KFP のパイプライン. 結果は `paper_embeddings` に書き, そこから索引に入れる |
| 質問のベクトル化 | search1 の CPU (KServe). 目標は p95 300ms 以下. 1 件と同時 5〜10 件の遅延, CPU, メモリ, 起動時間を測る |

- 文書側と質問側で同じモデル・revision・トークナイザ・正規化を使う. モデルを変える時は全件を作り直し, 新しい索引に切り替える (エイリアスで切り替え)

### 5.6 OpenSearch の索引
| 索引 | 1 文書 | 主なフィールド | 検索 | 設定 |
|---|---|---|---|---|
| `papers` | 論文 (最新版) | paper_id, title, abstract, authors, categories, primary_category, published_at, updated_at, version, embedding | BM25 (title, abstract) + kNN → RRF | `index.knn: true`, Faiss HNSW, fp16 (sq), innerproduct. codec は default (LZ4) |
| `paper_chunks` | チャンク (最新版) | chunk_id, paper_id, version, section_id, section_path, section_type, chunk_index, content_type, text, title, categories, published_at, chunker_version | BM25 (text, title) | `index.knn: true` (MVP 後にベクトルのフィールドを追加できるように), codec は zstd (レベル 3) |

- 索引には最新版だけを入れる. 文書 ID は `papers` = paper_id, `paper_chunks` = chunk_id
- 実際の索引名に版を付け (`papers_v1` など), エイリアス (`papers`) で参照する. 作り直しは新しい索引を作ってから切り替える
- `paper_chunks` の text は正規化した本文 (数式を含む) を保存し, 解析器の char_filter で数式を除いてから BM25 の索引を作る
- ハイブリッド検索は search pipeline (RRF) で統合する. 候補は BM25・kNN それぞれ上位 50〜100 件
- 容量の見積もり (推定, 5.7 の検証で実測する): `papers` 約 25GB, `paper_chunks` 80〜150GB (zstd, オーバーラップなし)
- `index.codec` の比較 (default / zstd / zstd_no_dict) を 5.7 の検証で行う. zstd は保存フィールド (_source) だけを圧縮し, 索引作成とマージで CPU を使う
- 将来: 本文チャンクのベクトル (数千万件) は on_disk モード (二値量子化 + 再スコアリング) を候補とする. その時は SATA SSD を追加する

### 5.7 検証と評価
**チャンク作成の検証** (全件の処理の前. HF の parquet を Kubeflow の Workspace から直接読むため, RustFS / Iceberg の構築と並行してできる)
- 1,000 件で処理を確認し, 10,000 件 (cs.AI, cs.LG, math, physics, hep など代表的なカテゴリから) で統計を取る
- 取る値: TeX の正規化の成功率, 節の抽出の成功率, 空の本文の件数, 節数 / 論文, チャンク数 / 論文 (平均・中央値・P95), トークン数 / チャンク (平均・中央値・P95), 表の件数, 重複チャンクの率
- 索引の容量と作成時間 (codec 別) を測り, 全件に外挿する

**検索の評価**
- 評価データ (300〜1,000 件の質問):
  - 引用の文脈: 本文中の引用を含む文を質問, 引用先の論文を正解とする (自前抽出の引用を使う)
  - LLM で要旨から質問を作る (日本語の質問を含む)
  - 手作りの質問 50〜100 件
- 指標: Recall@10, Recall@50, MRR, nDCG@10
- 比べるもの: 埋め込みモデル (Qwen3-Embedding-0.6B / BGE-M3), 文書側の入力 (カテゴリの有無), BM25 のみ / kNN のみ / hybrid (RRF)

### 5.8 MCP ツール (arxiv-mcp)
| ツール | 内容 |
|---|---|
| `search_papers(query, categories?, date_from?, date_to?, authors?, k?)` | ハイブリッド検索 (固定). 要約カード (ID, タイトル, 著者, 年, 要旨の抜粋, スコア) を返す |
| `search_passages(query, paper_ids?, section_types?, k?)` | 本文チャンクの検索. 節の種類 (method / experiment / result など) で絞り込める. チャンクの section_id を返す |
| `get_paper(paper_id)` | 書誌, 版の一覧, arXiv の abs / PDF リンク |
| `get_outline(paper_id, version?)` | 目次 (section_id, 見出し, 階層, 節の種類, トークン数) |
| `read_section(paper_id, section_id, version?, offset?)` | 節の本文 (正規化済み). 長い節はページ送り. 最新版は OpenSearch のチャンクを連結して返し, 過去の版は Iceberg から読む |
| `get_references` / `get_citations` / `find_similar(paper_id)` | 関連論文を辿る |
| `citation_graph(paper_id, depth<=2, limit)` | 引用の近傍をまとめて返す |
| `run_sql(query)` | Trino への読み取り専用 SQL |
| `get_pdf(paper_id, version?)` (任意) | PDF 本体 |

- 検索方式 (keyword / semantic / hybrid) は MCP に出さない (LLM の引数の選び間違いを避ける). サーバー側で, arXiv ID は直接取得, 引用符付きの語句はフレーズ一致にする. 方式の切り替えは arxiv-api (開発・評価用) にだけ持たせる
- `run_sql` の制限は Trino 側で強制する
  - 専用ユーザー (`mcp_reader`) とアクセス制御ファイル: arxiv のスキーマの読み取りのみ, pdf / source / ps / latex の content 列は拒否
  - resource groups (同時実行数, メモリ), `query_max_execution_time`, `query_max_scan_physical_bytes`
  - arxiv-api 側: 返す行数の上限, 監査ログ
- 公開範囲: 自宅ネットワーク内のみ. Keycloak の OAuth は MVP 後に対応する
- ツールは arxiv-api を薄く包む (REST API と処理を共通化する)
- 将来: `compare_papers(paper_ids, focus)` (複数論文の該当節をまとめて取得)

### 5.9 LLM と GPU の運用
| モード | spark1 | spark2 | LLM |
|---|---|---|---|
| A: LLM 1 台 | Qwen3.8-Flash-Next / Gemma4 (26B or 31B) | Kubeflow (学習, GPU Workspace, 埋め込み) | ローカル |
| B: LLM 2 台 | GLM5.3-Flash-Next | (同じモデルの一部) | ローカル |
| C: Kubeflow 占有 | Kubeflow | Kubeflow | Claude / OpenAI |

- LLM の Deployment / LWS の replicas を 0 ⇄ 1 で切り替える (まずは手順書とスクリプト. 必要なら Kueue)
- 質問のベクトル化は search1 の CPU のため, どのモードでも検索は止まらない
- 確認事項: 各モデルの vLLM のツール呼び出し (`--tool-call-parser`) 対応, GLM5.3-Flash-Next の 2 台構成 (vLLM / TensorFold)

### 5.10 検索ノード (search1)
Intel NUC 11PAHi5 を k8s のノードとして追加し, OpenSearch とエージェントのサンドボックスを同居させる.

| 用途 | メモリ | CPU (4 コア 8 スレッド) |
|---|---|---|
| OS, kubelet, DaemonSet | 約 2GB | |
| OpenSearch (ヒープ 6〜8GB + ベクトル fp16 約 6.5GB + ページキャッシュ) | 約 24GB | 3 スレッド以上を残す |
| 質問のベクトル化 | 約 2GB | |
| エージェントのサンドボックス (agent-sandbox, 同時 6〜8 個) | 約 36GB | 上限の合計 4〜5 スレッド |

| ディスク | 用途 |
|---|---|
| NVMe 500GB | OS, OpenSearch (`papers`, `paper_chunks`) |
| SATA SSD (追加予定, 1〜2TB) | サンドボックスの作業領域. 将来の本文チャンクのベクトル |

- taint と nodeSelector で, このノードには OpenSearch, 質問のベクトル化, サンドボックスだけを置く
- OpenSearch は Guaranteed QoS (requests = limits) と高い PriorityClass. メモリが足りない時はサンドボックスが先に退避される
- サンドボックス用の namespace に ResourceQuota (CPU, メモリ, ephemeral-storage) を設ける
- サンドボックスの通信は Cilium の NetworkPolicy で制限する (OpenSearch への直接接続, k8s の API, 自宅 LAN は拒否. arxiv-mcp と LLM の API などは許可)
- OpenSearch のデータはローカル PV. Longhorn はこのノードにレプリカを置かない
- ホストの設定: `vm.max_map_count=262144`
- バックアップ: OpenSearch のスナップショットを RustFS (バケット `opensearch-snapshots`) に定期的に取る. 障害時はスナップショットから戻すか, Iceberg から作り直す
- 本文の一括索引作成の間 (数日) はサンドボックスを止めるか同時 1 個にする
- サンドボックスの詳細 (DeepSeek Harness, OpenCode, Pi, Codex, Claude Code を agent-sandbox で動かす. 隔離は gVisor から) は別の設計メモにする

## 6. 後続タスク (進め方)
| 手順 | 内容 | 状態 |
|---|---|---|
| 1 | RustFS を構築 | 完了 (2026-11-21) |
| 5a | データ移行のうちコピー (parquet をそのまま RustFS へ) と確認 | 完了 (2026-11-25. 3,745 ファイルの sha256 が一致. SMB の PVC は削除) |
| 2 | Polaris + PostgreSQL (CNPG), RustFS のサービス用ユーザー | 完了 (2026-11-22. `memo/polaris_install_memo.md`) |
| 3 | Trino (`mcp_reader` の制限を含む) | 完了 (2026-11-22. `memo/trino_install_memo.md`) |
| 並行 | チャンク作成の検証, 評価データの作成 (HF の parquet を直接使う. 5.7) | チャンク作成の検証は完了 (2026-11-22. `deploy/science-systems/chunking-poc/README.md`). 評価データは未着手 |
| 4 | 検索ノード search1 (NUC のメモリ増設, ノード追加) と OpenSearch | 未着手 |
| 5b | データ移行のうち Iceberg テーブルの作成 (索引系の書き直し, 中身系の add_files) | 完了 (2026-11-25. 8 テーブル. `memo/arxiv_iceberg_tables_memo.md`) |
| 6 | 埋め込みモデルの比較 (評価データで) | 未着手 |
| 7 | 全件の加工 (正規化, 節, チャンク), 論文の埋め込み (DGX Spark), 引用抽出 (自前 + OpenAlex), 索引作成 | 未着手 |
| 8 | arxiv-api と arxiv-mcp | 未着手 |
| 9 | 最新化 (日次 OAI, 月次 S3, 四半期 OpenAlex) | OAI / PDF / ソースの取り込みを作成 (2026-11-26. `deploy/science-systems/arxiv-sync/`). 初回の追い付き中. OpenAlex と本文の組み立て以降は未着手 |
| 10 | Open WebUI + LiteLLM (まず Claude / OpenAI で確認) | 未着手 |
| 11 | 検索の評価と改善 (Reranker, 本文チャンクのベクトルなど) | 未着手 |
| 別系統 | ローカル LLM の提供 (旧 Phase 3 の再開), エージェントのサンドボックス (search1) | 未着手 |

見積もり: 作業 約 4〜5 週間 (レビューしながら進めた場合), 機械の処理 約 4〜7 日.

その間にできなくなること:
- コピー中 (1〜2 日): QNAP のディスクとネットワークが混む (Harbor, qnap-iscsi, llm-models が遅くなる). arxiv の PVC への書き込みは止める
- 論文の埋め込みの作成中 (数時間): DGX Spark の GPU が埋まる (GPU Workspace, Trainer, ローカル LLM が使えない)
- 全件の加工中 (1〜2 日): 加工を動かすノードの CPU と RustFS が混む
- 本文の索引作成中 (数日): search1 の CPU が埋まる (サンドボックスは止めるか同時 1 個)
- 最新化の仕組みができるまで: データは 2026-09 時点のまま

## 7. TODO
### 手順 1: RustFS
- [x] qnap-vm に open-iscsi / multipath-tools / cifs-utils を導入し, QNAP 用の `/etc/multipath.conf` を設定する (`qnap-trident_install_memo.md`. virtio ディスクのため devnode `^vd[a-z]+` も除外)
- [x] Vault に `eso/science-rustfs` (access-key, secret-key) を登録する
- [x] ExternalSecret と PVC を apply し, 18Ti の LUN が作成できることを確認する
- [x] Application `science-rustfs` を登録・同期し, Pod が qnap-vm で起動することを確認する
- [x] バケット作成 Job (arxiv-warehouse, arxiv-raw) を実行する
- [x] 確認: `aws s3 ls`, qnap-vm で `/dev/mapper/mpatha` (QNAP iSCSI Storage 18T) が /data にマウントされていること
- [x] ~~確認: qnap-vm で `sudo multipath -ll` が `features='1 queue_if_no_path'` であること~~ (qnap-vm を廃止したため不要. RustFS は worker2 で動く), コンソール (port-forward)

### 手順 5a: コピーと確認
- [x] コピーの Job (arxiv-migrate-copy) を完了させる (2026-11-23. 約 45 時間, エラーなし. arxiv-raw 13 個 4.07GiB, arxiv-warehouse 3,736 個 14.6TiB)
- [x] 確認の Job (arxiv-migrate-verify) で 3,745 ファイルの sha256 が一致することを確認する (2026-11-23 12:49 〜 2026-11-25 03:23 UTC, 約 38 時間. OK: all 3745 files match)
- [x] SMB の PVC (arxiv-datasets) を削除する (2026-11-25. PV の回収ポリシーを Delete に変えてから削除し, QNAP の共有フォルダも Trident で削除)

### 手順 5b: Iceberg テーブル
- [x] Trino で add_files を有効にする (`iceberg.add-files-procedure.enabled=true`)
- [x] add_files の動作確認 (フィールド ID のない parquet は name mapping で読める, 統計で読み飛ばせる, timestamp[ms] を読める)
- [x] 中身系 5 テーブルを add_files で登録する (Job arxiv-iceberg-content. 確認の Job と並行. 2026-11-23 完了, 62 分, 全テーブル OK)
- [x] 索引系 3 テーブルを CTAS で書き直す (Job arxiv-iceberg-index. 2026-11-25 完了, 全テーブルの行数が一致. files は paper_files に改名)
- [x] 作業用の名前空間 arxiv_staging のテーブルとファイルを削除する (2026-11-25. unregister_table, DROP SCHEMA, s3://arxiv-warehouse/arxiv_staging/ を削除)

### qnap-vm の廃止 (2026-11-22 完了)
- [x] RustFS の values から nodeSelector / tolerations を削除して同期し, worker で起動することを確認する
- [x] kubelet-csr-approver の `providerRegex` から qnap-vm を削除する. external-dns の etcd のコメントとメモを直す
- [x] `kubectl drain qnap-vm --ignore-daemonsets` → `kubectl delete node qnap-vm` → qnap-vm で `kubeadm reset` → QNAP で VM を削除
- [x] QNAP の iSCSI 設定から qnap-vm の initiator を削除する

### 手順 2: Polaris
- [x] CNPG の PostgreSQL (polaris-postgres, 18.7), RustFS のユーザー polaris (arxiv-warehouse のみ)
- [x] realm POLARIS の初期化, Polaris 1.9.0 (ArgoCD science-polaris)
- [x] カタログ arxiv (stsUnavailable: クライアントは各自の RustFS の認証情報を使う), 名前空間 arxiv, テーブル作成の確認
- [x] PostgreSQL のバックアップ (2026-11-26. 毎日 03:30 JST の pg_dump を RustFS の science-backups へ, 30 日保持. 復元を確認済み. `memo/polaris_install_memo.md`)

### 手順 3: Trino
- [x] Trino 484 (ArgoCD science-trino, coordinator 1 台が worker を兼ねる), カタログ iceberg (Polaris の principal trino, RustFS のユーザー trino)
- [x] パスワード認証 (admin / mcp_reader) を Ingress https://trino.cluster.orenet.net の TLS で受ける
- [x] mcp_reader: iceberg.arxiv の読み取りのみ, pdf / source / ps / latex の content 列は拒否 (動作確認の Job で確認)
- [x] Polaris の purge は無効のまま (DROP TABLE は失敗する. add_files で登録するコピー済みの parquet を守るため). 削除は unregister_table
- [x] mcp_reader の上限: resource groups (同時実行数, メモリ), `query_max_execution_time`, `query_max_scan_physical_bytes` (2026-11-24. root.mcp 同時 3 件, 1 件あたり 60 秒 / 8GB / 800MB, SET SESSION は admin のみ. `trino_install_memo.md`)
- [x] メトリクス (ServiceMonitor. 2026-11-26. 収集用のユーザー prometheus は management.user で認証なし, system_information の read のみ)

### 手順 4: 検索ノード search1
- [ ] NUC のメモリを 64GB にする
- [ ] OS を入れ, k8s のノードとして追加する (`kubeadm join`. kubelet-csr-approver の `providerRegex` にノード名を追加)
- [ ] taint, ラベル, Longhorn のスケジューリング対象外, `vm.max_map_count` を設定する
- [ ] ローカル PV (NVMe) を用意する
- [ ] OpenSearch を入れ, 索引のテンプレート (5.6) とスナップショットのリポジトリ (RustFS) を設定する
- [ ] 質問のベクトル化 (KServe, CPU) を入れて遅延を測る
- [ ] SATA SSD を追加する (サンドボックスを動かす前, または本文索引の実測で必要になった時)

### 検証と評価
- [x] チャンク作成の検証 (1,000 件 → 10,000 件) と統計 (2026-11-22. 正規化 99.4%, 節の抽出 99.9%, 全件で約 9,100 万チャンク・480 億トークン・本文 155GB・83 CPU 時間. `deploy/science-systems/chunking-poc/README.md`)
- [x] 短い節 (100 トークン未満. 全チャンクの 5.4%) のチャンクはそのまま残す (節をまたがない. 2026-11-22 決定)
- [x] section_type の other (28%) は今のまま (規則の分類で十分. 2026-11-22 決定)
- [ ] 索引の容量と作成時間 (codec 別) を測る (検証の 10,000 件のチャンクを使う. OpenSearch ができてから)
- [ ] 評価データを作る (引用の文脈, LLM 生成, 手作り)

### 全体
- [x] ディレクトリ名の綴りを直す (2026-11-26. `deploy/sciense-systems` → `deploy/science-systems`. Application 3 つの valueFiles も変更)
- [x] ~~science-systems の ExternalSecret `hf-token` の `refreshInterval` (15s) を 1h にする~~ (2026-11-26. 使う Job がなくなったため hf-downloader-job と一緒に削除. Vault のキーは llm-system が使う)
- [x] AWS の認証情報 (requester-pays 用) を Vault に登録する (2026-11-26. IAM ユーザー arxiv-requester-pays, arxiv の pdf/ と src/ の一覧と読み取りのみ. Vault eso/arxiv-aws → Secret arxiv-aws. 拒否も確認済み)
- [ ] 埋め込みモデルを決める (評価で)
- [ ] MCP ホスト (Open WebUI) から使うローカル LLM の提供方法を決める (vLLM のツール呼び出し対応の確認)
- [ ] エージェントのサンドボックスの設計メモを書く

### MVP 後
- [ ] Keycloak の OAuth (MCP サーバー, Open WebUI)
- [ ] Reranker (Qwen3-Reranker. GPU が前提)
- [ ] 本文チャンクへのベクトル付与 (on_disk モード, SATA SSD)
- [ ] sparse 検索 (BGE-M3 など) の評価
- [ ] `compare_papers` ツール
- [ ] Open Notebook (選んだ論文を深く読むノート. `add_to_notebook` で連携)
- [ ] Hermes Agent (定期実行の自動化. 例: 新着論文の要約通知)
- [ ] GPU の割り当て調整 (Kueue)

## 8. 決定事項の記録
### 2026-11-18
- データ移行は案 B. QNAP の空き 22TB, Synology 約 10TB を作業領域に使う
- AWS アカウントは用意する (arXiv S3 の requester-pays)
- 本文の読み取りを MVP に含める
- MCP サーバーは自宅 NW 内に公開. Keycloak の OAuth は MVP 後
- OpenAlex を使う
- ローカル LLM: Gemma4 (26B / 31B), Qwen3.8-Flash-Next (Spark 1 台), GLM5.3-Flash-Next (Spark 2 台). 必要に応じて GPU を LLM と Kubeflow で切り替え, Claude / OpenAI でも使えるようにする
- MCP ホストは Open WebUI + LiteLLM. Open Notebook と Hermes Agent は MVP 後の候補
- RustFS のディスクは Kubernetes の PVC (qnap-iscsi)

### 2026-11-18 (外部レビューの反映)
- 加工結果 (節, チャンク, 埋め込み) を Iceberg の派生テーブルに置き, OpenSearch はそこから作り直せる派生物とする
- 読む単位 (節) と検索単位 (チャンク) を分ける. チャンクは 500〜800 トークン, オーバーラップなし
- 検索用の本文は軽く正規化する. `\ref` の番号の解決はしない
- OpenSearch の索引は最新版のみ. 過去の版は Iceberg で扱う
- 埋め込みは Qwen3-Embedding-0.6B から始め, 評価データで BGE-M3 と比べて決める. MVP は論文のみ, 本文チャンクは BM25 のみ
- 全件の処理の前にチャンク作成の検証と評価データの作成を行う (インフラ構築と並行)
- MCP に検索方式の引数は出さない. `run_sql` の制限は Trino 側で強制する
- OpenSearch は Intel NUC (64GB に増設) を検索ノード search1 とし, ローカル NVMe の PV に置く. エージェントのサンドボックスと同居させる
- `paper_chunks` の codec は zstd, `papers` は default

### 2026-11-23 (手順 5b)
- 中身系 (paper_text, latex, ps, source, pdf) は add_files で登録し書き直さない. テーブルの場所は `arxiv/<テーブル>/iceberg`, データはコピー済みの `arxiv/<テーブル>/data/hf/` のまま (Trino は既にファイルがある場所にテーブルを作れないため)
- 索引系は CTAS で書き直す. metadata は投稿年 (`year(first_version_date)`) で分割, versions / files は分割なし (小さいため). すべて paper_id で並べる
- 識別キーは Iceberg の identifier fields として設定しない (Trino で設定できない). MERGE の ON 句で指定する
- add_files で登録したテーブルは OPTIMIZE + expire_snapshots をしない (コピーした parquet が消えるため)

### 2026-11-25 (手順 5a, 5b の完了)
- 索引系の files は Iceberg では `paper_files` とする. Trino は Iceberg のメタデータテーブルの種類 (files / history / snapshots など) と
  同じ名前のテーブルを「名前空間の $files」と解釈し, 作成も参照もできないため. 列名と中身は元の files と同じ
- 正本のテーブルは metadata / versions / paper_files / paper_text / latex / source / pdf / ps の 8 つ (名前空間 arxiv)

### 2026-11-26 (最新化)
- PDF は arXiv の S3 ではなく GCS の公開ミラー (`gs://arxiv-dataset`) から取る. 版つきの名前で直接取れ, 無料. HF のデータセットと同じ sha256
- ソースは S3 から, 必要な論文だけを取る (tar の見出しを範囲指定で読んで目次を作る). 版は付けない (HF と同じく version は NULL)
- OAI の取り込みが先 (どの論文のどの版が新しいかを確定する). その後に PDF とソースを取る
- 書き込みは parquet + add_files (中身を Trino に通さない, 既存の parquet を書き換えない). 置き換えは Trino の DELETE (merge-on-read)

### 外部レビューで採らなかった案と理由
| 案 | 理由 |
|---|---|
| OpenSearch で過去の版も検索する | paper_text に過去の版の本文がない. 索引が約 1.6 倍になり, 検索結果が同じ論文の版で埋まる. 版の比較は閲覧の操作で, `read_section(version=)` で足りる |
| `search_papers` に検索方式 (keyword / semantic / hybrid) の引数を付ける | ローカル LLM が引数を選び間違えやすい. hybrid (RRF) でほぼ両方を拾える. サーバー側の自動判定で補う |
| `\ref` を番号に解決した検索用テキストを作る | 正しい番号を出すには実質 LaTeX のコンパイルが必要. 「Equation 3」で検索する需要もない |
| `run_sql` を SQL パーサーで検証する | 抜け道が多く, 正しい SELECT でも大量のスキャンを防げない. Trino の権限と上限で強制する |

## 参考
- arXiv bulk data on S3: https://info.arxiv.org/help/bulk_data_s3.html
- Apache Polaris: https://polaris.apache.org/ (RustFS ガイド: https://polaris.apache.org/guides/rustfs/)
- Apache Gravitino: https://gravitino.apache.org/
- RustFS S3 Tables: https://docs.rustfs.com/en/administration/data/s3-tables
- OpenAlex snapshot: https://help.openalex.org/access/snapshot/
- Open WebUI MCP: https://docs.openwebui.com/features/extensibility/mcp/
- Hermes Agent: https://hermes-agent.nousresearch.com/docs
- Open Notebook: https://github.com/lfnovo/open-notebook
- Agent Sandbox (kubernetes-sigs): https://agent-sandbox.sigs.k8s.io/
