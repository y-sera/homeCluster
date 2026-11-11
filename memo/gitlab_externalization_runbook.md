# GitLab 外部化 切替手順書(Phase 4)

chart 9.11.16(GitLab 18.11.14)のまま, 同梱 PostgreSQL / Redis / MinIO を外部サービスへ切り替え, Gitaly の PV を qnap-iscsi へ移す.
切替後に 10.2.10(19.2) → 10.5.4(19.5)へアップグレードする(本手順の対象外).

**実施記録: 2026-11-12 に本手順で切替完了**(helm rev 44 → 45, 停止から全 Pod Ready まで約 10 分).
各手順の結果は該当箇所に記載. 旧 PV / 旧 Secret / `helm/gitlab/values.yaml` は削除済み.

| 対象 | 切替前 | 切替後 | データ移行 |
|---|---|---|---|
| PostgreSQL | 同梱 bitnami 16.6(synology-csi-nfs) | CNPG `gitlab-postgres` 17(qnap-iscsi 10Gi) | CNPG の initdb.import(pg_dump / pg_restore) |
| Redis | 同梱 bitnami(synology-csi) | Valkey `gitlab-valkey`(qnap-iscsi) | 移行しない(キュー空を確認して切替) |
| オブジェクトストレージ | 同梱 MinIO(synology-csi 10Gi) | RustFS `gitlab-rustfs`(qnap-iscsi 20Gi) | rclone copy |
| Gitaly | synology-csi 50Gi | qnap-iscsi 10Gi | rsync |

関連ファイル
- GitLab values: `deploy/gitlab/gitlab/values/values.yaml`(旧: `helm/gitlab/values.yaml`)
- 接続用 Secret: `deploy/gitlab/gitlab/manifests/`, `deploy/gitlab/postgres/manifests/postgres-app-external-secret.yaml`
- CNPG Cluster: `deploy/gitlab/postgres/manifests/cluster.yaml`(**停止後に apply する**)
- 移行用(一回限り): `deploy/gitlab/gitlab/migration/`(移行完了後に削除. 内容はコミット 8995e31 を参照)

切替前の調査結果(2026-11-11): DB 191MB(gitlabhq_production のみ), MinIO 14.7MB, Gitaly 11MB.

---

## 0. 事前準備(GitLab 稼働中に実施)

### 0-1. 前提の確認
```bash
# 外部サービスが稼働していること
kubectl -n gitlab get deploy gitlab-valkey gitlab-rustfs
# 接続用 Secret が全て SecretSynced であること
kubectl -n gitlab get externalsecret
# RustFS にバケットが作成済みであること(13 個)
kubectl -n gitlab logs job/gitlab-rustfs-create-buckets   # Job が消えていれば create-buckets-job.yaml を再 apply
```

### 0-2. CNPG 取込のリハーサル
本番と別名の Cluster で取込を試す. GitLab 稼働中のため整合性は取れないが, 取込自体が通るか(拡張・権限)を確認する.
```bash
sed 's/^  name: gitlab-postgres$/  name: gitlab-postgres-rehearsal/' deploy/gitlab/postgres/manifests/cluster.yaml \
  | kubectl apply -f -
kubectl -n gitlab get cluster gitlab-postgres-rehearsal -w     # "Cluster in healthy state" まで待つ
kubectl -n gitlab logs -l cnpg.io/cluster=gitlab-postgres-rehearsal,cnpg.io/jobRole=import --tail=-1 | grep -iE 'error|warn'

# 旧 DB と比較
# Q: テーブル数(全スキーマ), 拡張, schema_migrations / users / projects / p_ci_builds / issues / notes / events の件数
# S: スキーマごとのオブジェクト数(パーティション用スキーマ gitlab_partitions_* を含む)
# ※ ci_builds はパーティション化されて p_ci_builds になっている
Q="select (select count(*) from pg_tables where schemaname not in ('pg_catalog','information_schema')), (select string_agg(extname, ',' order by extname) from pg_extension), (select count(*) from schema_migrations), (select count(*) from users), (select count(*) from projects), (select count(*) from p_ci_builds), (select count(*) from issues), (select count(*) from notes), (select count(*) from events)"
S="select string_agg(nspname||':'||c, ',' order by nspname) from (select n.nspname, count(*) c from pg_class r join pg_namespace n on n.oid=r.relnamespace where n.nspname not in ('pg_catalog','information_schema','pg_toast') group by 1) x"
for q in "$Q" "$S"; do
  kubectl -n gitlab exec gitlab-postgresql-0 -c postgresql -- sh -c "PGPASSWORD=\$POSTGRES_POSTGRES_PASSWORD psql -U postgres -d gitlabhq_production -Atc \"$q\""
  kubectl -n gitlab exec gitlab-postgres-rehearsal-1 -c postgres -- psql -d gitlabhq_production -Atc "$q"
done
# 旧 / 新 の各行が一致すること

# gitlab ユーザー(gitlab-postgres-app のパスワード)で Service 経由で接続できること
kubectl -n gitlab get secret gitlab-postgres-app -o jsonpath='{.data.password}' | base64 -d \
  | kubectl -n gitlab exec -i gitlab-postgres-rehearsal-1 -c postgres -- sh -c \
    'read -r PGPASSWORD; export PGPASSWORD; psql "host=gitlab-postgres-rehearsal-rw.gitlab.svc user=gitlab dbname=gitlabhq_production sslmode=require" -Atc "select current_user, count(*) from projects"'
```
リハーサル結果(2026-11-12): 取込は約 2 分でエラーなし. Q / S とも一致
(テーブル 2427, 拡張 btree_gist / pg_trgm / plpgsql, schema_migrations 7702, スキーマ別 public 6505 / static 1968 / dynamic 4470).
public の全テーブルの owner が gitlab であること, gitlab ユーザーでの接続も確認済み.
import ログの "error" / "warning" の一致はテーブル名(error_tracking_*)と initdb の定型警告のみで問題なし.
後片付け(qnap-iscsi は Retain のため, PV を Delete に変えてから消すと QNAP 側のボリュームも削除される)
```bash
PV=$(kubectl -n gitlab get pvc gitlab-postgres-rehearsal-1 -o jsonpath='{.spec.volumeName}')
kubectl patch pv $PV -p '{"spec":{"persistentVolumeReclaimPolicy":"Delete"}}'
kubectl -n gitlab delete cluster gitlab-postgres-rehearsal
kubectl get pv $PV        # 消えていること
```

### 0-3. レンダリング結果の確認
```bash
helm repo update gitlab
helm template gitlab gitlab/gitlab -n gitlab --version 9.11.16 -f deploy/gitlab/gitlab/values/values.yaml > /tmp/gitlab-new.yaml
```
想定される差分(旧 values との比較)
- 削除: MinIO(Deployment / PVC `gitlab-minio` / Service / Ingress / create-buckets Job), PostgreSQL・Redis の StatefulSet / Service
- 変更: DB 接続先 `gitlab-postgres-rw`, Redis 接続先 `gitlab-valkey`, オブジェクトストレージ接続 Secret, registry storage, toolbox の backup 設定, runner の cache 設定, Gitaly の volumeClaimTemplates(qnap-iscsi 10Gi)

### 0-4. 旧 PV を Retain に変更
誤操作で旧 PVC を削除してもデータが残るよう, **必ず切替前に実施する**.
(当初は helm upgrade で MinIO の PVC が削除されると想定していたが, 実際は `helm.sh/resource-policy: keep` により残った)
```bash
for pv in $(kubectl get pv -o jsonpath='{range .items[?(@.spec.claimRef.namespace=="gitlab")]}{.metadata.name} {.spec.claimRef.name}{"\n"}{end}' \
            | awk '$2 ~ /^(data-gitlab-postgresql-0|redis-data-gitlab-redis-master-0|gitlab-minio|repo-data-gitlab-gitaly-0)$/ {print $1}'); do
  kubectl patch pv $pv -p '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}'
done
kubectl get pv -o custom-columns=NAME:.metadata.name,CLAIM:.spec.claimRef.name,RECLAIM:.spec.persistentVolumeReclaimPolicy | grep -E 'gitlab-(postgresql|redis|minio|gitaly)'
```

---

## 1. バックアップ
```bash
mkdir -p ~/gitlab-backup && cd ~/gitlab-backup
# rails secret(バックアップのリストアに必須. 安全な場所に保管する)
kubectl -n gitlab get secret gitlab-rails-secret -o yaml > gitlab-rails-secret.yaml

# フルバックアップ(MinIO の gitlab-backups バケットに保存される)
kubectl -n gitlab exec deploy/gitlab-toolbox -- backup-utility
kubectl -n gitlab exec deploy/gitlab-toolbox -- s3cmd ls s3://gitlab-backups/
# 手元にもコピー
F=<上で表示された xxx_gitlab_backup.tar>
kubectl -n gitlab exec deploy/gitlab-toolbox -- s3cmd get s3://gitlab-backups/$F /tmp/$F
kubectl -n gitlab cp $(kubectl -n gitlab get pod -l app=toolbox -o name | head -1 | cut -d/ -f2):/tmp/$F ./$F
kubectl -n gitlab exec deploy/gitlab-toolbox -- rm /tmp/$F
```
現在の helm リビジョンを控える(切り戻し用)
```bash
helm -n gitlab history gitlab | tail -1
```

---

## 2. 停止
```bash
# 入口と書き込み元を止める(HPA は replicas 0 の間は動作しない)
kubectl -n gitlab scale deploy gitlab-webservice-default gitlab-gitlab-shell gitlab-kas gitlab-registry \
  gitlab-gitlab-runner gitlab-gitlab-exporter --replicas=0

# Sidekiq のキューが空になるのを待つ(全て 0 になるまで繰り返す)
kubectl -n gitlab exec deploy/gitlab-toolbox -- gitlab-rails runner \
  'p Sidekiq::Queue.all.map { |q| [q.name, q.size] }.reject { |_, s| s.zero? }; p busy: Sidekiq::Workers.new.size, retry: Sidekiq::RetrySet.new.size, scheduled: Sidekiq::ScheduledSet.new.size'
# busy と各キューが 0 になれば止めてよい. retry / scheduled に残るジョブは Redis 切替で失われる(個人利用のため許容)

kubectl -n gitlab scale deploy gitlab-sidekiq-all-in-1-v2 gitlab-toolbox --replicas=0
kubectl -n gitlab get pod     # gitaly / postgresql / redis / minio / valkey / rustfs 以外が無いこと
```

---

## 3. PostgreSQL の移行(CNPG 取込)
```bash
kubectl apply -f deploy/gitlab/postgres/manifests/cluster.yaml
kubectl -n gitlab get cluster gitlab-postgres -w      # "Cluster in healthy state" まで待つ
kubectl -n gitlab logs -l cnpg.io/cluster=gitlab-postgres,cnpg.io/jobRole=import --tail=-1 | grep -iE 'error|warn'

# 0-2 と同じクエリ(Q, S)で旧 DB と一致することを確認(Q, S は 0-2 で定義したものを使う)
for q in "$Q" "$S"; do
  kubectl -n gitlab exec gitlab-postgresql-0 -c postgresql -- sh -c "PGPASSWORD=\$POSTGRES_POSTGRES_PASSWORD psql -U postgres -d gitlabhq_production -Atc \"$q\""
  kubectl -n gitlab exec gitlab-postgres-1 -c postgres -- psql -d gitlabhq_production -Atc "$q"
done

# gitlab ユーザーで Service 経由で接続できること
kubectl -n gitlab get secret gitlab-postgres-app -o jsonpath='{.data.password}' | base64 -d \
  | kubectl -n gitlab exec -i gitlab-postgres-1 -c postgres -- sh -c \
    'read -r PGPASSWORD; export PGPASSWORD; psql "host=gitlab-postgres-rw.gitlab.svc user=gitlab dbname=gitlabhq_production sslmode=require" -Atc "select current_user, count(*) from projects"'
```

---

## 4. オブジェクトストレージの移行(MinIO → RustFS)
```bash
kubectl apply -f deploy/gitlab/gitlab/migration/object-storage-copy-job.yaml
kubectl -n gitlab wait --for=condition=complete job/gitlab-object-storage-copy --timeout=30m
kubectl -n gitlab logs job/gitlab-object-storage-copy
# 各バケットの check が "0 differences found" で, 最後の size が old / new で一致すること
kubectl -n gitlab delete job gitlab-object-storage-copy
```

---

## 5. Gitaly の移行(synology-csi → qnap-iscsi)
StatefulSet の volumeClaimTemplates は変更できないため, StatefulSet を一旦削除し, 新しい PVC を同名で用意してから helm upgrade で再作成させる.
```bash
OLD_PV=$(kubectl -n gitlab get pvc repo-data-gitlab-gitaly-0 -o jsonpath='{.spec.volumeName}')
echo $OLD_PV      # pvc-75466582-09a8-4278-b65d-8dc73a1d7f65(migration/gitaly-pvcs.yaml の volumeName と一致すること)

kubectl -n gitlab scale sts gitlab-gitaly --replicas=0
kubectl -n gitlab wait --for=delete pod/gitlab-gitaly-0 --timeout=5m
kubectl -n gitlab delete sts gitlab-gitaly --cascade=orphan
kubectl -n gitlab delete pvc repo-data-gitlab-gitaly-0
kubectl get pv $OLD_PV            # Released / Retain であること

# 旧 PV を再バインドできるようにする
kubectl patch pv $OLD_PV --type json -p '[{"op":"remove","path":"/spec/claimRef"}]'

kubectl apply -f deploy/gitlab/gitlab/migration/gitaly-pvcs.yaml
kubectl -n gitlab get pvc gitaly-old repo-data-gitlab-gitaly-0     # 両方 Bound

kubectl apply -f deploy/gitlab/gitlab/migration/gitaly-copy-job.yaml
kubectl -n gitlab wait --for=condition=complete job/gitlab-gitaly-copy --timeout=30m
kubectl -n gitlab logs job/gitlab-gitaly-copy
# "--- verify" の後に何も出力されず, du が概ね一致し, 所有者が 1000 であること

kubectl -n gitlab delete job gitlab-gitaly-copy
kubectl -n gitlab delete pvc gitaly-old        # 旧 PV は Released(Retain)で残る
```

---

## 6. helm upgrade
`--reuse-values` は付けない(旧 values の全量を引き継がないため).
```bash
helm upgrade gitlab gitlab/gitlab -n gitlab --version 9.11.16 -f deploy/gitlab/gitlab/values/values.yaml --timeout 20m
```
helm upgrade で削除されるもの: MinIO の Deployment / Service / Ingress, PostgreSQL / Redis の StatefulSet.
PVC(`gitlab-minio`(resource-policy: keep), `data-gitlab-postgresql-0`, `redis-data-gitlab-redis-master-0`)は残るため 8 で削除する.

replicas を chart が指定しない Deployment は 0 のままのため戻す(HPA の min 値)
```bash
kubectl -n gitlab scale deploy gitlab-webservice-default gitlab-gitlab-shell gitlab-kas gitlab-registry --replicas=2
kubectl -n gitlab scale deploy gitlab-sidekiq-all-in-1-v2 gitlab-gitlab-runner --replicas=1
kubectl -n gitlab get pod -w        # 全て Running / Completed になること
```

---

## 7. 動作確認
```bash
TB="kubectl -n gitlab exec deploy/gitlab-toolbox --"
$TB gitlab-rake gitlab:check SANITIZE=true
$TB gitlab-rake gitlab:doctor:secrets           # 復号できないデータが無いこと
$TB gitlab-rake gitlab:artifacts:check gitlab:lfs:check gitlab:uploads:check
# Gitaly
$TB gitlab-rake gitlab:gitaly:check
# Registry が起動していること(s3_v2 の checksum_disabled が 18.11 で受け付けられない場合はここで起動に失敗する)
kubectl -n gitlab logs deploy/gitlab-registry --tail=50
# 新しいバックアップが RustFS に保存できること
$TB backup-utility --skip registry,uploads,artifacts,lfs,packages
$TB s3cmd ls s3://gitlab-backups/
```
ブラウザ / クライアントでの確認
- [ ] ログイン(Redis 切替のためセッションは切れる)
- [ ] リポジトリの閲覧, `git clone` / `git push`(HTTPS と SSH)
- [ ] LFS のファイル, アップロード済み画像, 過去のジョブログ・artifacts の表示
- [ ] `docker pull` / `docker push`(Container Registry)
- [ ] パイプラインの実行(Runner が登録されたままであること, cache の保存と復元)

---

## 8. 旧リソースの削除(動作確認後すぐ)
旧 PV を Delete に戻してから PVC を削除すると, PV と synology 側のボリュームも削除される.
```bash
# 旧 PV が使われていないこと(attach されていないこと)を確認
kubectl get pv -o custom-columns=NAME:.metadata.name,CLAIM:.spec.claimRef.name,STATUS:.status.phase,RECLAIM:.spec.persistentVolumeReclaimPolicy \
  | grep -E 'data-gitlab-postgresql-0|redis-data-gitlab-redis-master-0|gitlab-minio|gitaly-old'
kubectl get volumeattachment -o custom-columns=PV:.spec.source.persistentVolumeName,NODE:.spec.nodeName | grep <PV名>   # 何も出ないこと

# 1. 旧 PV(4 個: postgresql / redis / minio / gitaly)を Delete に戻す
#    Gitaly の旧 PV は PVC 削除済み(Released)のため, この時点で削除される
kubectl patch pv <PV名> -p '{"spec":{"persistentVolumeReclaimPolicy":"Delete"}}'   # 4 個それぞれ

# 2. 残っている旧 PVC を削除する(対応する PV も削除される)
kubectl -n gitlab delete pvc data-gitlab-postgresql-0 redis-data-gitlab-redis-master-0 gitlab-minio
kubectl get pv | grep <PV名>     # 4 個とも消えていること
```

CNPG の取込元定義を削除する(旧 Secret `gitlab-postgresql-password` を参照しているため, Secret より先に行う)
- `deploy/gitlab/postgres/manifests/cluster.yaml` から `bootstrap.initdb.import` と `externalClusters` を削除し,
  `kubectl diff` / `kubectl apply --dry-run=server` の後に apply する(Pod の再起動は発生しない).

旧 Secret(chart が生成したもの)
```bash
# helm の manifest と Pod から参照されていないことを確認してから削除
helm -n gitlab get manifest gitlab | grep -cE 'gitlab-minio-secret|gitlab-postgresql-password'   # 0
kubectl -n gitlab delete secret gitlab-minio-secret gitlab-postgresql-password
# gitlab-registry-database-password は Registry メタデータ DB の検討が終わるまで残す
```

リポジトリ
- `git rm helm/gitlab/values.yaml`
- `git rm -r deploy/gitlab/gitlab/migration/`(内容はコミット 8995e31 を参照)

---

## 切り戻し(8 の削除前のみ可能)
1. 停止(手順 2 と同じ)
2. Gitaly: `kubectl -n gitlab delete sts gitlab-gitaly --cascade=orphan`, 新 PVC `repo-data-gitlab-gitaly-0` を削除し, 旧 PV の claimRef を外して同名 PVC(`storageClassName: synology-csi-delete`, `volumeName: <旧PV>`, 50Gi)で再バインドする.
3. MinIO: 旧 PV の claimRef を外し, PVC `gitlab-minio`(`storageClassName: synology-csi-delete`, `volumeName: <旧PV>`, 10Gi)で再バインドする.
4. `helm rollback gitlab <手順1で控えたリビジョン>`(旧 PostgreSQL / Redis は残っている PVC で起動する)
5. 手順 6 と同様に replicas を戻す.
切替後に新環境へ書き込んだデータ(push, issue 等)は失われる.
