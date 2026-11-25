# Trino 導入メモ

arXiv 研究検索基盤 (namespace `science-systems`) の SQL エンジン.
Polaris (Iceberg REST カタログ, カタログ `arxiv`) の目録を使い, データは RustFS (`s3://arxiv-warehouse`) を直接読み書きする.

## 構成
| 項目 | 値 |
|---|---|
| Trino | 484 (chart `trino` 1.45.0, https://trinodb.github.io/charts), ArgoCD `science-trino`, release `trino` |
| 構成 | coordinator 1 台が worker を兼ねる (`server.workers: 0`, `include-coordinator`). ヒープ 6G, メモリ 6Gi〜8Gi, クエリのメモリ上限 3GB |
| エンドポイント | `https://trino.cluster.orenet.net` (Ingress cilium, Let's Encrypt の Certificate `trino` → Secret `trino-cert`). クラスタ内のクライアントもここを使う |
| カタログ | `iceberg` (REST: `http://polaris.science-systems.svc:8181/api/catalog`, warehouse `arxiv`, OAuth2). Polaris の名前空間 `arxiv` = Trino のスキーマ `iceberg.arxiv` |
| Polaris の権限 | principal `trino` → principal role `trino` → catalog role `trino_rw` (CATALOG_MANAGE_CONTENT) |
| RustFS のユーザー | `trino` (ポリシー `arxiv-warehouse-rw`. Polaris と共用. arxiv-raw は不可) |
| 認証 | パスワードファイル. ユーザー `admin` (すべて), `mcp_reader` (読み取りのみ) |
| mcp_reader の上限 | リソースグループ `root.mcp` (同時 3 件, 待ち 20 件, メモリ 35%), 1 件あたり実行 60 秒 / 待ちを含め 120 秒 / 読み込み 8GB / メモリ 800MB. SET SESSION は admin のみ |
| メトリクス | ServiceMonitor `trino` (release: prometheus) が coordinator の `/metrics` (OpenMetrics) を 30 秒ごとに認証なしで収集. `management.user=prometheus` で管理用のエンドポイントのみ認証なしの要求をユーザー prometheus として扱い, prometheus には system_information の read のみ許す. JVM (java.lang の Memory / GarbageCollector / Threading) も出す |

ファイル
```
applications/science-trino.yaml
deploy/sciense-systems/trino/values/values.yaml       # カタログ, アクセス制御 (rules.json), リソースグループ, セッションの既定値, Ingress, Certificate (extraManifests)
deploy/sciense-systems/trino/manifests/
  trino-external-secrets.yaml   # trino-password (password.db), trino-internal (共有鍵), trino-storage (RustFS), trino-clients (平文のパスワード)
  rustfs-trino-user-job.yaml    # RustFS のユーザー trino
  polaris-principal-job.yaml    # Polaris の principal trino と権限. 認証情報を Secret trino-polaris に書く
  smoke-test-job.yaml           # 動作確認 (導入時のみ. 本番のテーブル名 arxiv.latex を作るため手順 5b 以降は実行しない)
  limits-test-job.yaml          # mcp_reader の上限の確認 (読み取りのみ. いつでも実行できる)
```

Vault
| パス | キー |
|---|---|
| `eso/trino` | `admin-password`, `mcp-reader-password`, `shared-secret` |
| `eso/science-rustfs-trino` | `access-key` (`trino`), `secret-key` |

Polaris の principal `trino` の認証情報 (Secret `trino-polaris`, キー `credential` = `<client_id>:<client_secret>`) は Vault にない.
client_id を Polaris が払い出すため, `polaris-principal-job.yaml` が作成時に Secret へ書く.

## 導入手順
```bash
M=deploy/sciense-systems/trino/manifests
# 1. Secret (4 つとも SecretSynced)
kubectl apply -f $M/trino-external-secrets.yaml
kubectl -n science-systems get es trino-password trino-internal trino-storage trino-clients
# 2. RustFS のユーザー (ポリシー arxiv-warehouse-rw は Polaris の導入時に作成済み)
kubectl apply -f $M/rustfs-trino-user-job.yaml
kubectl -n science-systems logs job/rustfs-trino-user        # OK: arxiv-raw is denied
# 3. Polaris の principal (Secret trino-polaris ができる)
kubectl apply -f $M/polaris-principal-job.yaml
kubectl -n science-systems logs job/polaris-principal-trino  # GET .../arxiv/namespaces as trino -> 200
# 4. Trino (ArgoCD)
kubectl apply -f applications/science-trino.yaml             # 登録後に同期する
# 5. 動作確認 (手順 5b でテーブル iceberg.arxiv.latex を作った後は実行しない)
kubectl apply -f $M/smoke-test-job.yaml
kubectl -n science-systems logs job/trino-smoke-test         # ALL OK
# 6. mcp_reader の上限の確認 (読み取りのみ. 約 3 分)
kubectl apply -f $M/limits-test-job.yaml
kubectl -n science-systems logs -f job/trino-limits-test      # ALL OK
```
5 の確認用テーブルのファイルが RustFS に残るため, RustFS の root で削除する.
**`arxiv/latex/` (コピーしたデータ) ではなく `arxiv/latex-<uuid>/` のみ消す**:
`rc object remove --recursive --force <alias>/arxiv-warehouse/smoke_test/` と `<alias>/arxiv-warehouse/arxiv/latex-<uuid>/`.

## 使い方
```bash
# CLI (パスワードは Secret trino-clients)
TRINO_PASSWORD=... trino --server https://trino.cluster.orenet.net --user mcp_reader --password
```
Python (`trino` パッケージ): `trino.dbapi.connect(host="trino.cluster.orenet.net", port=443, http_scheme="https", user="mcp_reader", auth=trino.auth.BasicAuthentication("mcp_reader", pw), catalog="iceberg", schema="arxiv")`

## 注意点
- **Trino のクライアントはパスワード認証に HTTPS を必須とする.** Ingress で TLS を終端し, `http-server.process-forwarded=true` で
  X-Forwarded-Proto を信頼している (Cilium の Ingress は X-Forwarded-Proto: https を付ける). Service (`trino:8080`) に HTTP で直接来た
  要求ではパスワード認証を受け付けない (動作確認で確認済み). ただしクラスタ内から X-Forwarded-Proto を偽装すれば平文で認証できる
- **Polaris の purge が無効のため, Trino の `DROP TABLE` は失敗する** ("Failed to drop table". Trino は purgeRequested=true で削除を要求し,
  Polaris が 403 を返す). purge を有効にすると, add_files で登録したコピー済みの parquet (約 15TB) まで DROP TABLE で消えるため, 意図的に無効のままにしている.
  テーブルは `CALL iceberg.system.unregister_table(schema_name => '...', table_name => '...')` で目録から外し, 不要なファイルは RustFS の root で消す
- ファイルベースのアクセス制御では, プロシージャ (`unregister_table`, `ALTER TABLE ... EXECUTE add_files` など) は `procedures` の規則で
  明示的に許可しないと admin でも拒否される
- mcp_reader は pdf / source / ps / latex の `content` 列を読めない. `SELECT *` も拒否されるため, 列名を指定する.
  規則は名前空間 `arxiv` のテーブル名 (`pdf|source|ps|latex`) に一致させている. 本体の列を持つテーブルを追加したら rules.json を更新する
- **mcp_reader の上限** (MCP サーバー経由の LLM エージェントの問い合わせが Trino を占有しないため):
  - リソースグループ (`resourceGroups`): ユーザーで `root.admin` / `root.mcp` / `root.other` に振り分ける. `root.mcp` は同時 3 件, 待ち 20 件,
    メモリはクラスタ (3GB) の 35%. 振り分けは `system.runtime.queries` の `resource_group_id` で確認できる
  - セッションの既定値 (`sessionProperties`, ユーザー `mcp_reader` に一致): `query_max_execution_time` 60s, `query_max_run_time` 120s,
    `query_max_scan_physical_bytes` 8GB (paper_text の本文 約 66GB の全件走査を防ぐ. metadata 1.6GB の全件走査はできる), `query_max_memory` 800MB
    (`query_max_memory` は SHOW SESSION に表示されないが効いている)
  - rules.json の `system_session_properties` / `catalog_session_properties` で admin 以外の SET SESSION (`--session` を含む) を拒否する.
    この節を書くと一致しないユーザーはすべて拒否される (書かない場合は全員が変更できる)
  - エラー: `Query exceeded the maximum execution time limit of 60.00s`, `Exceeded scan limit of 8GB`, `Query exceeded distributed user memory limit of 800MB`.
    MCP サーバーはこれらを利用者 (LLM) に伝え, 条件を絞った問い合わせに直させる
  - リソースグループとセッションの既定値の変更は coordinator の再起動で反映される (values の変更 → ArgoCD の同期で Pod が作り直される)
- アクセス制御の規則 (ConfigMap) は Pod を再起動せずに反映される (ConfigMap の反映 1 分程度 + Trino の読み直し 60 秒)
- パスワードファイルは ESO のテンプレート (sprig `htpasswd "user" pass "bcrypt"`) で作る. Trino は `$2y$` しか受け付けないため `$2a$` を置き換える.
  ソルトが毎回変わるため `refreshPolicy: CreatedOnce`. パスワードを変えた場合は Secret `trino-password` を削除して作り直す
- `fs.native-s3.enabled` は Trino 484 で `fs.s3.enabled` に改名された
- Trino のイメージには grep / sed がない (Job のスクリプトは bash の組み込み機能のみ使う)
- 起動時のログの設定値は秘密情報が `[REDACTED]` になる (共有鍵, S3 の secret key, OAuth2 の credential. 出現 0 件を確認済み)
- メトリクス: Prometheus は Pod に HTTP で直接取りに来るが, パスワード認証は HTTPS が必須. そのため収集用のパスワードは作らず
  `management.user=prometheus` で認証なしにした (クラスタ内から Pod の 8080 番に届けば, 誰でも /metrics などの管理用の読み取りができる.
  SQL (/v1/statement) は 403). chart の serviceMonitor.openMetrics は空のパスワードでも Basic 認証を付けるため, endpoints を自分で書く
  (chart が作る Secret trino-metrics-auth-coordinator は使われない).
  メトリクス名: Trino 自身は `trino_execution_name_QueryManager_RunningQueries`, `trino_execution_resourcegroups_...` など,
  JVM は `JMX_java_lang_TYPE_Memory_ATTRIBUTE_HeapMemoryUsage_used` の形. jmx-object-names の区切りは `|`
