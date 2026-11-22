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

ファイル
```
applications/science-trino.yaml
deploy/sciense-systems/trino/values/values.yaml       # カタログ, アクセス制御 (rules.json), Ingress, Certificate (extraManifests)
deploy/sciense-systems/trino/manifests/
  trino-external-secrets.yaml   # trino-password (password.db), trino-internal (共有鍵), trino-storage (RustFS), trino-clients (平文のパスワード)
  rustfs-trino-user-job.yaml    # RustFS のユーザー trino
  polaris-principal-job.yaml    # Polaris の principal trino と権限. 認証情報を Secret trino-polaris に書く
  smoke-test-job.yaml           # 動作確認
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
- アクセス制御の規則 (ConfigMap) は Pod を再起動せずに反映される (ConfigMap の反映 1 分程度 + Trino の読み直し 60 秒)
- パスワードファイルは ESO のテンプレート (sprig `htpasswd "user" pass "bcrypt"`) で作る. Trino は `$2y$` しか受け付けないため `$2a$` を置き換える.
  ソルトが毎回変わるため `refreshPolicy: CreatedOnce`. パスワードを変えた場合は Secret `trino-password` を削除して作り直す
- `fs.native-s3.enabled` は Trino 484 で `fs.s3.enabled` に改名された
- Trino のイメージには grep / sed がない (Job のスクリプトは bash の組み込み機能のみ使う)
- 起動時のログの設定値は秘密情報が `[REDACTED]` になる (共有鍵, S3 の secret key, OAuth2 の credential. 出現 0 件を確認済み)
- メトリクス (ServiceMonitor) は未設定. `/metrics` もパスワード認証が必要なため, 収集用のユーザーと system_information の規則が要る
