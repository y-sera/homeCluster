# harborインストールメモ

namespace: harbor
リリース名: harbor
Chart: harbor/harbor (https://helm.goharbor.io) v1.19.4 / Harbor 2.15.4
管理: ArgoCD Application `applications/harbor.yaml` (自動syncなし。手動でsyncする)
URL: https://harbor.cluster.orenet.net

## 方針
1. 公式チャートを利用し, ArgoCDで管理する. values はチャートのデフォルトからの差分のみを `deploy/harbor/values/values.yaml` に記載する(イメージタグはチャートのデフォルトに任せる).
2. ストレージは QNAP (trident) の `qnap-iscsi` StorageClass を利用する. PVC はチャートに作らせず, `deploy/harbor/manifests/pvc.yaml` で事前に作成して `existingClaim` で参照する.
3. DB・Redis(Valkey) はチャート内蔵のものを利用する.
4. 証明書は cert-manager で別途作成する (`manifests/certificate/harbor_certificate.yaml` → Secret `harbor-cert`). Ingress は cilium.
5. 秘密情報は Vault (`eso/harbor-core`) に置き, ESO (ClusterSecretStore `vault`) で Secret を作成する.
6. メトリクスは kube-prometheus-stack で収集し, ServiceMonitor とダッシュボードはチャート外で管理する.

## ファイル構成
```
applications/harbor.yaml                               # ArgoCD Application (kubectl apply で手動適用)
deploy/harbor/
├── values/values.yaml                                 # チャートの values (差分のみ)
├── manifests/
│   ├── pvc.yaml                                       # PVC (qnap-iscsi)
│   ├── core-external-secret.yaml                      # Secret harbor-core-token (トークン署名鍵/証明書, CSRF_KEY)
│   ├── core-secrets-external-secret.yaml              # Secret harbor-core-secrets (secretKey, secret, HARBOR_ADMIN_PASSWORD)
│   └── monitors.yaml                                  # ServiceMonitor (release: prometheus)
└── dashboards/                                        # Grafana ダッシュボード (kustomize ConfigMap)
manifests/namespace/harbor.yaml
manifests/certificate/harbor_certificate.yaml
```

## 設定
### ストレージ
| PVC | 容量 | 用途 |
|---|---|---|
| harbor-registry-data | 100Gi | イメージ本体 (filesystem) |
| harbor-database-data | 2Gi | PostgreSQL (`pgdata/pg18`) |
| harbor-redis-data | 1Gi | Valkey (キャッシュ/ジョブキュー) |
| harbor-trivy-data | 5Gi | trivy の脆弱性DBキャッシュ |
| harbor-jobservice-data | 1Gi | ジョブログ |

`qnap-iscsi` は reclaimPolicy: Retain. PVC を削除しても PV と QNAP 側の LUN は残る.

### 秘密情報 (Vault `eso/harbor-core`)
| property | Secret / key | 用途 |
|---|---|---|
| secretKey | harbor-core-secrets / secretKey | DB 内の暗号化データの鍵. **絶対に変更しない** (変えるとレプリケーション先の認証情報などが復号できなくなる) |
| secret | harbor-core-secrets / secret | core / jobservice / registry と core の通信用 (16文字) |
| HARBOR_ADMIN_PASSWORD | harbor-core-secrets / HARBOR_ADMIN_PASSWORD | 初回起動時の admin パスワード |
| tls.key / tls.crt | harbor-core-token | トークン署名用の鍵と自己署名証明書 (RSA 4096, 有効期限 2036-11-09) |
| CSRF_KEY | harbor-core-token | CSRF キー (32文字) |

チャートに任せると tls.key / tls.crt / CSRF_KEY は helm template のたびにランダム生成され, ArgoCD が常に OutOfSync になる(sync のたびに再ログインが必要になる)ため, Vault で固定している.

values に平文で残っているもの: `jobservice.secret`, `registry.secret`, `registry.credentials` (チャートは existingSecret に対応しているため Vault 化は可能), 内部DBのパスワード `database.internal.password` (チャートが existingSecret に未対応).

### メトリクス
- values の `metrics.enabled: true` で harbor-exporter が作成され, core / jobservice / registry / exporter の `http-metrics` (8001) が有効になる.
- チャートの ServiceMonitor (`metrics.serviceMonitor`) は `release: harbor` ラベルが固定で付き, `release: prometheus` を追加するとキーが重複するため使わない. `deploy/harbor/manifests/monitors.yaml` で定義する.
- ダッシュボードは公式 (goharbor/harbor の `contrib/grafana-dashboard/metrics-example.json`) を変更なしで取り込み (uid `iRBnfkZhQ`).

## インストール手順 (クラスター再構築時)
```
# 1. namespace と証明書
kubectl apply -f manifests/namespace/harbor.yaml
kubectl apply -f manifests/certificate/harbor_certificate.yaml

# 2. 秘密情報 (Vault の eso/harbor-core に上記 property が登録済みであること)
kubectl apply -f deploy/harbor/manifests/core-external-secret.yaml
kubectl apply -f deploy/harbor/manifests/core-secrets-external-secret.yaml
kubectl -n harbor get externalsecret   # SecretSynced / True を確認

# 3. PVC
kubectl apply -f deploy/harbor/manifests/pvc.yaml
kubectl -n harbor get pvc               # Bound を確認

# 4. Harbor 本体 (ArgoCD)
kubectl apply -f applications/harbor.yaml
# ArgoCD の UI か CLI で harbor を sync する

# 5. メトリクスとダッシュボード
kubectl apply -f deploy/harbor/manifests/monitors.yaml
kubectl apply -k deploy/harbor/dashboards --server-side
```

## 運用メモ
### チャートの更新
1. `helm pull harbor/harbor --version <新バージョン> --untar` で新旧のチャートを展開し, values.yaml とテンプレートの差分を確認する.
2. `helm template harbor <chart> -n harbor -f deploy/harbor/values/values.yaml` を2回実行して差分がないこと(ランダム値がないこと)を確認する.
3. `applications/harbor.yaml` の `targetRevision` を変更し, commit して `gitlab` リモートへ push する (ArgoCD の参照先. GitHub の `origin` にも push する).
4. `kubectl apply -f applications/harbor.yaml` で Application を更新してから sync する.
5. 更新前に `pg_dumpall` で DB をバックアップする:
   `kubectl -n harbor exec harbor-database-0 -c database -- pg_dumpall -U postgres > harbor.sql`

### DB の PostgreSQL メジャーバージョン
- harbor-db イメージは Harbor のバージョンによって PostgreSQL のメジャーバージョンが変わる (2.15.2 以降は PG18).
- 変わる場合, 初回起動時にエントリポイントが自動で `pg_upgrade` を実行し, 成功すると旧バージョンのデータディレクトリ (`pgdata/pg15` など) を削除する. 戻す手段はバックアップのみなので, 事前に必ず `pg_dumpall` を取る.
- `pg_upgrade` はコピーモードのため, 一時的にデータの2倍の容量が必要.
- アップグレード後は統計情報が引き継がれないため `ANALYZE` を実行する:
  `kubectl -n harbor exec harbor-database-0 -c database -- psql -U postgres -d registry -c "ANALYZE"`

### トークン署名用証明書の差し替え
- 有効期限: 2036-11-09.
- **鍵は PKCS#1 形式 (`-----BEGIN RSA PRIVATE KEY-----`) でなければならない.** OpenSSL 3 のデフォルト (PKCS#8, `BEGIN PRIVATE KEY`) だと core がトークンを発行できなくなる (`unable to get PrivateKey from PEM type: PRIVATE KEY`).
```
openssl req -x509 -newkey rsa:4096 -nodes -keyout tls.key -out tls.crt -days 3650 -subj "/CN=harbor-token-ca"
openssl rsa -in tls.key -traditional -out tls-pkcs1.key
# Vault の eso/harbor-core の tls.key (tls-pkcs1.key の中身) と tls.crt を vault kv patch で更新する (CSRF_KEY などは残す)
kubectl -n harbor annotate externalsecret harbor-core-token force-sync=$(date +%s) --overwrite
kubectl -n harbor rollout restart deploy/harbor-core
```
差し替え後はユーザーの再ログインが必要.

### ArgoCD の sync
- 自動 sync は無効. 変更を push したら手動で sync する.
- StatefulSet (database / redis / trivy) の `volumeClaimTemplates` は変更できないため, PVC 関連の設定を変えるときは StatefulSet を削除してから sync する.

## 履歴
- 2026-11-12〜13: Harbor 2.14.3 → 2.15.4 (chart 1.18.1 → 1.19.4), PV を synology-csi から qnap-iscsi へ移行, PostgreSQL 15 → 18, values を差分のみに整理 (`helm/harbor/` → `deploy/harbor/`), 秘密情報の Vault 化, メトリクス追加.
- それ以前: topolvm, synology-csi を利用していた.
