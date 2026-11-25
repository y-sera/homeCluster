# Apache Polaris (Iceberg REST カタログ) 導入メモ

arXiv 研究検索基盤 (namespace `science-systems`) の Iceberg カタログ.
テーブルの中身と構造 (データ, manifest, metadata.json) は RustFS (`s3://arxiv-warehouse`) に Iceberg の形式で置き,
Polaris は目録 (カタログ, 名前空間, テーブルと最新の metadata.json の場所, 権限) を PostgreSQL (CNPG) に持つ.

## 構成
| 項目 | 値 |
|---|---|
| Polaris | chart `polaris` 1.9.0 (https://downloads.apache.org/polaris/helm-chart), ArgoCD `science-polaris`, release `polaris` |
| エンドポイント | `http://polaris.science-systems.svc:8181` (REST カタログ `/api/catalog`, 管理 API `/api/management/v1`), 管理用 (health / metrics) `polaris-mgmt:8182`. Ingress なし |
| realm / root | realm `POLARIS`, root の client_id `root` (client_secret は Vault `eso/polaris-root`) |
| カタログ | `arxiv` (保存先 `s3://arxiv-warehouse`, `stsUnavailable: true`, `pathStyleAccess: true`), 名前空間 `arxiv` |
| PostgreSQL | CNPG `polaris-postgres` (PostgreSQL 18.7, 1 インスタンス, qnap-iscsi 5Gi), DB `polaris`, ユーザー `polaris`, スキーマ `polaris_schema` |
| RustFS のユーザー | `polaris` (ポリシー `arxiv-warehouse-rw`: arxiv-warehouse の読み書き・削除のみ) |

ファイル
```
applications/science-polaris.yaml
deploy/science-systems/polaris/values/values.yaml
deploy/science-systems/polaris/manifests/
  postgres-external-secrets.yaml   # polaris-postgres-app (CNPG 用, basic-auth), polaris-persistence (username / password / jdbcUrl)
  polaris-external-secrets.yaml    # polaris-token-keys (RSA), polaris-root, polaris-storage
  postgres-cluster.yaml            # CNPG Cluster + PodMonitor
  postgres-backup-cronjob.yaml     # 目録の毎日のダンプ (RustFS の science-backups へ)
  rustfs-polaris-user-job.yaml     # RustFS のユーザー polaris とポリシー (rustfs/rc)
  bootstrap-job.yaml               # realm の初期化 (admin tool)
  catalog-setup-job.yaml           # カタログ arxiv, 名前空間 arxiv, テーブル作成の確認
```

Vault
| パス | キー |
|---|---|
| `eso/polaris-postgres` | `password` |
| `eso/polaris-token-keys` | `private-key`, `public-key` (PEM. `openssl genrsa -out private.pem 2048 && openssl rsa -in private.pem -pubout -out public.pem`) |
| `eso/polaris-root` | `client-secret` |
| `eso/science-rustfs-polaris` | `access-key` (`polaris`), `secret-key` |

## 導入手順
```bash
M=deploy/science-systems/polaris/manifests
# 1. Secret (5 つとも SecretSynced になること)
kubectl apply -f $M/postgres-external-secrets.yaml -f $M/polaris-external-secrets.yaml
kubectl -n science-systems get es
# 2. PostgreSQL (スキーマ polaris_schema は postInitApplicationSQL で作られる)
kubectl apply -f $M/postgres-cluster.yaml
kubectl -n science-systems get cluster polaris-postgres     # Cluster in healthy state
kubectl apply -f $M/postgres-backup-cronjob.yaml           # 毎日 03:30 JST にダンプ
# 3. RustFS のユーザー
kubectl apply -f $M/rustfs-polaris-user-job.yaml
kubectl -n science-systems logs job/rustfs-polaris-user     # OK: arxiv-raw is denied
# 4. realm の初期化 (サーバーより先に行う)
kubectl apply -f $M/bootstrap-job.yaml
kubectl -n science-systems logs job/polaris-bootstrap       # Realm 'POLARIS' successfully bootstrapped.
# 5. Polaris (ArgoCD)
kubectl apply -f applications/science-polaris.yaml          # 登録後に同期する
# 6. カタログ
kubectl apply -f $M/catalog-setup-job.yaml
kubectl -n science-systems logs job/polaris-catalog-setup   # done
```
6 の確認用テーブルの metadata.json が `s3://arxiv-warehouse/smoke_test/` に残るため, RustFS の root で削除する
(`rc object remove --recursive --force <alias>/arxiv-warehouse/smoke_test/`).

## 注意点
- **admin tool のイメージ既定の起動スクリプト (run-java.sh) は, 実行するコマンドを引数ごとログに出力する.**
  `bootstrap --credential=POLARIS,root,<secret>` の secret がそのままログに出るため, Job では `java -jar /deployments/polaris-admin-tool.jar` を直接実行している.
  2026-11-21 の初回導入時はこれで secret がログに出たため, Vault の値を作り直し, realm を purge してから bootstrap し直した.
- bootstrap は冪等で, 初期化済みの realm の root の認証情報は作り直さない. 作り直す場合は `purge --realm=POLARIS` (カタログ・権限を含めすべて消える) の後に bootstrap する.
  運用中に root の secret を変える場合は管理 API の principal の rotate を使う.
- **`stsUnavailable: true` のカタログでは, Polaris は chart の `storage.secret` (`polaris.storage.aws.*`) ではなく AWS SDK の既定の探索で認証情報を探す.**
  環境変数 `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` を `extraEnv` で渡す (未設定だとテーブル作成が 500 "Unable to load credentials from any of the providers").
- `stsUnavailable: true` ではクライアントへの認証情報の払い出し (vended credentials) が行われない. Trino / PyIceberg / Spark は各自の RustFS の認証情報を使う.
- テーブル削除の purge (ファイルの削除) は既定で無効 (403). テーブルを消しても RustFS のファイルは残る.
- スキーマは JDBC の URL (`?currentSchema=polaris_schema`) で指定し, サーバーと admin tool で揃えている. Polaris はスキーマを作らないため CNPG で作る.
- PostgreSQL のバックアップは毎日 03:30 JST の pg_dump (下記). 最悪でも, RustFS の metadata.json から `register_table` で登録し直せる (権限の設定は失われる).

## 目録のバックアップと戻し方
- CronJob `polaris-postgres-backup` が `pg_dump --format=custom --no-owner --no-privileges` を
  `s3://science-backups/polaris-postgres/polaris-<UTC 時刻>.dump` に置く (2026-11 時点で約 27KB). 30 日より古いものは削除する.
  Barman Cloud Plugin (WAL を含む継続的なバックアップ) は目録が小さく, 失っても登録し直せるため使わない
- すぐに取る: `kubectl -n science-systems create job polaris-postgres-backup-<名前> --from=cronjob/polaris-postgres-backup`
- 戻し方 (2026-11-26 に一時 DB への復元で確認済み. 全テーブルの行数が一致):
  ```bash
  # ダンプを取り出す (RustFS の root で. rclone の場合)
  rclone copy dst:science-backups/polaris-postgres/polaris-<時刻>.dump .
  # Polaris を止める (書き込みを止めるため)
  kubectl -n science-systems scale deploy polaris --replicas=0
  # 既存の中身を消して戻す (Cluster を作り直した場合はスキーマが空なのでそのまま pg_restore)
  P="kubectl -n science-systems exec -i polaris-postgres-1 -c postgres --"
  $P psql -U postgres -d polaris -c "DROP SCHEMA polaris_schema CASCADE; CREATE SCHEMA polaris_schema AUTHORIZATION polaris"
  $P pg_restore -U postgres -d polaris --no-owner --role=polaris < polaris-<時刻>.dump
  # Polaris を戻す (ArgoCD の同期, または scale --replicas=1)
  ```
- ダンプにはロール (polaris) が含まれない. Cluster を作り直した場合, ロールは bootstrap (initdb の owner) で作られる

## 動作確認
```bash
# トークン (client_credentials)
TOKEN=$(curl -s http://polaris.science-systems.svc:8181/api/catalog/v1/oauth/tokens \
  -d grant_type=client_credentials -d client_id=root -d client_secret=<secret> -d scope=PRINCIPAL_ROLE:ALL | jq -r .access_token)
curl -s -H "Authorization: Bearer $TOKEN" http://polaris.science-systems.svc:8181/api/management/v1/catalogs/arxiv
curl -s -H "Authorization: Bearer $TOKEN" http://polaris.science-systems.svc:8181/api/catalog/v1/arxiv/namespaces
```
