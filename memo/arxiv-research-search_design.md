# arXiv 研究用検索システム 設計メモ

本ドキュメントはブランチ `feature/arxiv-research-search` で管理する. 作業 (マニフェストなど) は main にコミットする.

## 1. 概要
science-systems に置いた arXiv データセット (Hugging Face `secemp9/arxiv-complete`) を元に,
LLM のエージェントが MCP 経由で論文を検索・抽出・表示・解説し, 関連論文を辿れる研究用の検索システムを作る.

- データの正本は Apache Iceberg (S3 互換ストレージ RustFS 上) に置き, カタログは Apache Polaris を使う
- エージェントからの検索・閲覧は, 検索インデックス (OpenSearch), 引用グラフ (PostgreSQL), 本文の節チャンクで高速に応答する
- arXiv の S3 バケット (本文) と OAI-PMH (メタデータ), OpenAlex (引用) から定期的に最新化する
- MCP サーバーを自宅ネットワーク内に公開し, ローカル LLM と Claude / OpenAI のどちらからでも使えるようにする

## 2. ユースケース
| エージェントの動作 | 提供する機能 |
|---|---|
| 質問 (日本語可) から論文を探す | ハイブリッド検索 (BM25 + ベクトル, 多言語埋め込み), カテゴリ・期間・著者での絞り込み |
| 該当部分を抜き出す | 本文の節チャンクの検索 |
| 表示する | 書誌情報, arXiv の abs / PDF へのリンク (arXiv の規約上リンクは必須), 必要に応じて PDF |
| 解説する | LLM が目次と必要な節だけを読む (解説自体は LLM 側) |
| 関連論文を辿る | 参照文献 / 被引用 (自前抽出 + OpenAlex), 内容の近い論文 (埋め込みの近傍), 同じ著者・カテゴリ |
| 傾向を分析する | Trino への読み取り専用 SQL |

## 3. 現状のデータ (2026-11-18 時点)
- PVC `science-systems/arxiv-datasets` (qnap-smb, RWX, 17Ti) に HF のリポジトリ構成のまま約 15TB
  (取得 Job: `deploy/sciense-systems/manifests/hf-downloader-job.yaml`)
- スナップショット: メタデータ 2026-08-30 (OAI-PMH arXivRaw), ファイル 2026-09-05. データセット側の自動更新はない

| config | 1 行 | 行数 | parquet | 主な列 |
|---|---|---|---|---|
| metadata | 論文 | 3,148,796 | 1.6G | paper_id, title, authors, abstract, categories, primary_category, license, doi, ... |
| versions | 論文 × 版 | 5,030,612 | 257M | paper_id, version, version_date, has_pdf, pdf_sha256, is_latest_version, ... |
| files | ファイル (中身なし) | 54,597,326 | 2.3G | paper_id, version, kind, source, path, size, sha256, stored_as, ... |
| paper_text | 論文の TeX 本文 | 2,856,227 | 66G | paper_id, text (large_string), main_file, resolution, ... |
| latex | TeX 関連ファイル | 12,348,082 | 150G | paper_id, path, ext, size, sha256, content (large_binary) |
| source / pdf / ps | ファイルの中身 | 4,737万 / 497万 / 224万 | 6.0T / 7.9T / 634G | paper_id, version, kind, source, path, size, sha256, content |

- ZSTD 圧縮 (arrow 25). 中身の列は large_binary で, 行グループは 200 行程度
- 各シャードは `paper_id` の辞書順 (新形式 ID が先, `hep-th/...` などの旧形式が後) に並んでいる
- Iceberg のフィールド ID は持たない (登録時は名前で対応付ける)

## 4. 構成
```
 LLM (ローカル: vLLM / クラウド: Claude, OpenAI)
   ↑ OpenAI 互換 API
 LiteLLM ─── Open WebUI (MCP ホスト, 自宅 NW 内)
                 │ MCP (Streamable HTTP)
                 ▼
 arxiv-mcp ── arxiv-api (FastAPI) ─┬─ OpenSearch : 論文 / 節チャンクのハイブリッド検索
                                   ├─ PostgreSQL : 引用グラフ, 書誌キャッシュ
                                   ├─ 埋め込み推論 (KServe, CPU) : 質問のベクトル化
                                   └─ Trino ── Polaris (+PostgreSQL) ── Iceberg on RustFS (qnap-vm)
 取り込み (CronJob / KFP)
   日次  : OAI-PMH → metadata/versions を MERGE → 新規論文の埋め込み・索引
   月次  : arXiv S3 (pdf/src) → 中身 → latex → 本文の組み立て → 節チャンク → 引用抽出 → 索引
   四半期: OpenAlex スナップショット → 引用の補完
   初回  : HF parquet → Iceberg へ移行, 全件の埋め込み (Spark GPU, KFP)
```

### 構成要素
| 要素 | 配置 | 内容 |
|---|---|---|
| RustFS | qnap-vm (taint 許容, nodeSelector) | 単ノード単ディスク (standalone). データは qnap-iscsi の PVC (18Ti, シン). 冗長化は QNAP の RAID |
| Polaris + PostgreSQL (CNPG) | worker | Iceberg REST カタログ. catalog `arxiv`, `stsUnavailable: true`, `pathStyleAccess: true` |
| Trino | worker | Iceberg コネクタ (REST カタログ). 分析 SQL と API の裏側 |
| OpenSearch | worker | 論文の索引 (314 万件, 埋め込み付き), 節チャンクの索引 (BM25) |
| PostgreSQL (CNPG) | worker | 引用グラフ (参照 / 被引用), 書誌キャッシュ |
| 埋め込み | 一括: Spark GPU (KFP) / 質問: KServe (CPU) | 多言語モデル (例: Qwen3-Embedding-0.6B, bge-m3) |
| arxiv-api / arxiv-mcp | science-systems | FastAPI と MCP サーバー (公式 Python SDK, Streamable HTTP) |
| Open WebUI + LiteLLM | worker | MCP ホストと, ローカル / クラウド LLM の統合 API |

### 選定理由
- **Iceberg**: 日次更新の MERGE (upsert), スナップショットによる再現性 (ML 学習の入力として固定できる), Trino / DuckDB / PyIceberg / Spark から共通に使える. 既存 parquet を `add_files` で書き換えずに登録できる
- **Polaris (Gravitino ではなく)**: 用途が Iceberg のみ, Helm chart と PostgreSQL 永続化, RustFS 用の公式ガイド, `stsUnavailable` で STS なしの S3 互換ストレージに対応, Keycloak 連携のガイドあり.
  Gravitino は Hive / JDBC / fileset などの横断管理が必要になった時に Polaris の上に追加できる
- **OpenSearch (Elasticsearch ではなく)**: Apache 2.0 でライセンス階層がない. BM25 / kNN / hybrid クエリ / 絞り込み / 集計を一つで扱える
- **Open WebUI + LiteLLM**: Open WebUI は Streamable HTTP の MCP に標準対応した Web チャット. LiteLLM でローカル LLM と Claude / OpenAI を一つの OpenAI 互換 API にまとめる.
  MCP の呼び出しは自宅内の Open WebUI から行うため, MCP サーバーは外部に公開しない
- **RustFS の S3 Tables (内蔵 Iceberg カタログ)** は Trino からの書き込みを保証しておらず Iceberg v3 にも未対応のため不採用

## 5. 仕様
### 5.1 データ移行 (案 B: すべて Iceberg on RustFS へ)
- 容量: QNAP の空き 22TB に RustFS (約 15TB) を置き, 検証後に SMB の PVC (約 15TB) を削除する. Synology (約 10TB) は作業領域として使う
- 索引系 (metadata, versions, files): 書き直して Iceberg テーブルにする. 識別キー (paper_id / (paper_id, version)) を設け MERGE 可能にし, 投稿月やカテゴリで分割する
- テキスト・中身系 (paper_text, latex, source, pdf, ps): parquet を RustFS にコピーし `add_files` で登録する (書き直さない). paper_id 順のため統計で読み飛ばせる
- コピーは qnap-vm 上の Pod で行い, QNAP の近くで処理する

### 5.2 最新化 (必須)
| 対象 | 取得元 | 頻度 | 処理 |
|---|---|---|---|
| メタデータ・版 | OAI-PMH arXivRaw (export.arxiv.org) | 日次 | 前回の datestamp 以降を取得し MERGE. 状態テーブルに記録 |
| PDF・ソース | `s3://arxiv/{pdf,src}/` (requester-pays, us-east-1) | 月次 | manifest を処理済みチャンクと比較 (md5sum, timestamp) → 新規・変更チャンクを取得・展開 → pdf / source / files に追記 → latex を拡張子で抽出 → 本文の組み立て → 節チャンク → 引用抽出 → 版フラグ再計算 |
| 引用 | OpenAlex スナップショット (`s3://openalex`, 認証不要, 無料) | 四半期 | 必要な列だけ抽出し, DOI (arXiv の `10.48550/arXiv.*` を含む) で対応付け |

- 初回の追い付き: yymm が 2608 以降のチャンクと, manifest の timestamp が 2026-09-05 より新しいチャンク. 重複は sha256 と (paper_id, version, path) で除去
- 費用: requester-pays の転送料 約 $0.09/GB. 月 約 100GB で $10/月程度, 初回の追い付き 約 $30. AWS の認証情報は Vault + ESO
- arXiv の規約: 全件クロールはせず S3 で取得する. 全文を使うサービスでは arXiv へのリンクを表示する

### 5.3 関連論文
- 自前抽出: latex の `bbl` / `bib` から arXiv ID と DOI を抽出して arXiv 内の引用を作る (月次で更新)
- OpenAlex: arXiv 外の文献と被引用を補う
- 内容の近い論文: 論文の埋め込みの近傍
- 引用は Iceberg に記録し, 問い合わせは PostgreSQL で返す (1〜2 段の辿りを想定)

### 5.4 本文 (MVP に含む)
- paper_text (TeX) からコメントを除き `\section` 単位で分割する. LLM は TeX をそのまま読めるため Markdown 化はしない
- 新規論文の本文は, HF の paper_text と同等の組み立て処理を月次の取り込みで実装する
- 節チャンクは MVP では BM25 のみ (ベクトルは件数が 1 億規模のため後回し)

### 5.5 MCP ツール (arxiv-mcp)
| ツール | 内容 |
|---|---|
| `search_papers(query, categories?, date_from?, date_to?, authors?, k?)` | ハイブリッド検索. 要約カード (ID, タイトル, 著者, 年, 要旨の抜粋, スコア) を返す |
| `search_passages(query, paper_ids?, k?)` | 節チャンクの検索 |
| `get_paper(paper_id)` | 書誌, 版の一覧, arXiv の abs / PDF リンク |
| `get_outline(paper_id)` / `read_section(paper_id, section_id)` | 目次を見てから必要な節だけ読む (ローカル LLM のコンテキスト節約) |
| `get_references` / `get_citations` / `find_similar(paper_id)` | 関連論文を辿る |
| `citation_graph(paper_id, depth<=2, limit)` | 引用の近傍をまとめて返す |
| `run_sql(query)` | Trino への読み取り専用 SQL (タイムアウト, 行数上限) |
| `get_pdf(paper_id, version?)` (任意) | PDF 本体 |

- 公開範囲: 自宅ネットワーク内のみ. Keycloak の OAuth は MVP 後に対応する
- ツールは arxiv-api を薄く包む (REST API と処理を共通化する)

### 5.6 LLM と GPU の運用
| モード | spark1 | spark2 | LLM |
|---|---|---|---|
| A: LLM 1 台 | Qwen3.8-Flash-Next / Gemma4 (26B or 31B) | Kubeflow (学習, GPU Workspace, 埋め込み) | ローカル |
| B: LLM 2 台 | GLM5.3-Flash-Next | (同じモデルの一部) | ローカル |
| C: Kubeflow 占有 | Kubeflow | Kubeflow | Claude / OpenAI |

- LLM の Deployment / LWS の replicas を 0 ⇄ 1 で切り替える (まずは手順書とスクリプト. 必要なら Kueue)
- 質問のベクトル化は CPU のため, どのモードでも検索は止まらない
- 確認事項: 各モデルの vLLM のツール呼び出し (`--tool-call-parser`) 対応, GLM5.3-Flash-Next の 2 台構成 (vLLM / TensorFold)

## 6. 後続タスク (進め方)
| 手順 | 内容 | 状態 |
|---|---|---|
| 1 | RustFS を qnap-vm に構築 | ファイル作成済み (レビュー待ち) |
| 2 | Polaris + PostgreSQL (CNPG), RustFS のサービス用ユーザー | 未着手 |
| 3 | Trino | 未着手 |
| 4 | OpenSearch | 未着手 |
| 5 | データ移行 (案 B) | 未着手 |
| 6 | 埋め込み (Spark GPU), 引用抽出 (自前 + OpenAlex), 本文の節分割, 索引 | 未着手 |
| 7 | arxiv-api と arxiv-mcp | 未着手 |
| 8 | 最新化 (日次 OAI, 月次 S3, 四半期 OpenAlex) | 未着手 |
| 9 | Open WebUI + LiteLLM (まず Claude / OpenAI で確認) | 未着手 |
| 別系統 | ローカル LLM の提供 (旧 Phase 3 の再開) | 未着手 |

見積もり: 作業 約 3〜4 週間 (レビューしながら進めた場合), 機械の処理 約 3〜5 日.

その間にできなくなること:
- コピー中 (1〜2 日): QNAP のディスクとネットワークが混む (Harbor, qnap-iscsi, llm-models が遅くなる). arxiv の PVC への書き込みは止める
- 埋め込みの作成中 (数時間): Spark の GPU 2 つが埋まる (GPU Workspace, Trainer, ローカル LLM が使えない)
- 節チャンクの索引作成中 (0.5〜2 日): worker の CPU / メモリ / ディスクが混む
- 最新化の仕組みができるまで: データは 2026-09 時点のまま

## 7. TODO
### 手順 1: RustFS
- [ ] qnap-vm に open-iscsi / multipath-tools を導入し, QNAP 用の `/etc/multipath.conf` を設定する (`qnap-trident_install_memo.md`. sudo のため手動)
- [ ] Vault に `eso/science-rustfs` (access-key, secret-key) を登録する
- [ ] ExternalSecret と PVC を apply し, 18Ti の LUN が作成できることを確認する
- [ ] Application `science-rustfs` を登録・同期し, Pod が qnap-vm で起動することを確認する
- [ ] バケット作成 Job (arxiv-warehouse, arxiv-raw) を実行する
- [ ] 確認: `aws s3 ls`, コンソール (port-forward), qnap-vm で `multipath -ll`

### 全体
- [ ] `deploy/sciense-systems` のディレクトリ名の綴り (science) を直すか決める
- [ ] science-systems の ExternalSecret `hf-token` の `refreshInterval` (15s) を 1h にする
- [ ] AWS の認証情報 (requester-pays 用) を Vault に登録する
- [ ] 埋め込みモデルを決める (多言語)
- [ ] MCP ホスト (Open WebUI) から使うローカル LLM の提供方法を決める (vLLM のツール呼び出し対応の確認)

### MVP 後
- [ ] Keycloak の OAuth (MCP サーバー, Open WebUI)
- [ ] 節チャンクへのベクトル付与
- [ ] Open Notebook (選んだ論文を深く読むノート. `add_to_notebook` で連携)
- [ ] Hermes Agent (定期実行の自動化. 例: 新着論文の要約通知)
- [ ] GPU の割り当て調整 (Kueue)

## 8. 決定事項の記録 (2026-11-18)
- データ移行は案 B. QNAP の空き 22TB, Synology 約 10TB を作業領域に使う
- AWS アカウントは用意する (arXiv S3 の requester-pays)
- 本文の読み取りを MVP に含める
- MCP サーバーは自宅 NW 内に公開. Keycloak の OAuth は MVP 後
- OpenAlex を使う
- ローカル LLM: Gemma4 (26B / 31B), Qwen3.8-Flash-Next (Spark 1 台), GLM5.3-Flash-Next (Spark 2 台). 必要に応じて GPU を LLM と Kubeflow で切り替え, Claude / OpenAI でも使えるようにする
- MCP ホストは Open WebUI + LiteLLM. Open Notebook と Hermes Agent は MVP 後の候補
- RustFS のディスクは Kubernetes の PVC (qnap-iscsi)

## 参考
- arXiv bulk data on S3: https://info.arxiv.org/help/bulk_data_s3.html
- Apache Polaris: https://polaris.apache.org/ (RustFS ガイド: https://polaris.apache.org/guides/rustfs/)
- Apache Gravitino: https://gravitino.apache.org/
- RustFS S3 Tables: https://docs.rustfs.com/en/administration/data/s3-tables
- OpenAlex snapshot: https://help.openalex.org/access/snapshot/
- Open WebUI MCP: https://docs.openwebui.com/features/extensibility/mcp/
- Hermes Agent: https://hermes-agent.nousresearch.com/docs
- Open Notebook: https://github.com/lfnovo/open-notebook
