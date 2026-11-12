# external-dnsのバックエンド用etcdインストールメモ

namespace: external-dns
名前: etcd (StatefulSet `etcd` 3 メンバー, Service `etcd` / `etcd-headless`, PDB `etcd`)
管理: ArgoCD Application `applications/external-dns/etcd.yaml` (git の `deploy/external-dns/etcd/manifests` を kustomize で参照)
全体構成: memo/external-dns_install_memo.md

## 方針
1. 公式イメージ `gcr.io/etcd-development/etcd` を使い, StatefulSet を自作マニフェストで管理する.
   - 以前は bitnami chart を利用していたが, bitnami の無償イメージ配布終了 (bitnamilegacy は更新停止) のため 2026-11 に置き換えた.
   - bitnami chart は bitnami 独自の起動スクリプトに依存しており, 公式イメージへの差し替えはできない.
   - CoreDNS の k8s_gateway プラグインへの置き換え (etcd / external-dns 不要) も検討したが, 配布元が Codeberg のみ (取得時に 403 "under load"), ghcr.io は 1.8.2 で更新停止, 保守が少人数の有志のため見送った.
2. 3 メンバー構成 (1 台停止まで読み書き可能). TLS・認証なし. クラスター内からのみ利用する.
   - 1 レプリカでは etcd の再起動のたびに `*.cluster.orenet.net` が引けなくなり, external-dns も CrashLoopBackOff になっていたため 2026-11-13 に 3 メンバー化した.
   - さらに coredns の `serve_stale` で, etcd が全停止しても直近に引かれた名前は解決できるようにしている (memo/external-dns-coredns_install_memo.md).
3. ストレージは longhorn (各メンバー 1Gi). etcd 自身が複製するため longhorn のレプリカ (3) は冗長だが, 容量が小さいためデフォルトの StorageClass のままにしている.
4. **バックアップは取らない.** データ (`/skydns` 配下の DNS レコード) はすべて external-dns が Ingress などから再生成できる.

## 設定 (`deploy/external-dns/etcd/manifests/`)
| ファイル | 内容 |
|---|---|
| statefulset.yaml | etcd v3.7.3, 3 レプリカ (`podManagementPolicy: Parallel`), 必須の podAntiAffinity でノードを分散 (worker1-3. control-plane と qnap-vm は taint で除外), uid 1001 の非 root 実行, ルートファイルシステム読み取り専用, PVC `data` (longhorn 1Gi) を `/var/lib/etcd` にマウント (データは `/var/lib/etcd/data`) |
| service.yaml | `etcd` (2379, external-dns と coredns の接続先 `http://etcd:2379`. Ready なメンバーに振り分け), `etcd-headless` (2379/2380, `publishNotReadyAddresses`. メンバー名 `etcd-N.etcd-headless` の解決用) |
| pdb.yaml | `maxUnavailable: 1` (ドレインで同時に止まるのは1台まで) |
| kustomization.yaml | namespace と共通ラベル |

主な起動オプション:
- `--name=$(POD_NAME)` (Pod 名を環境変数で渡す), `--initial-cluster=etcd-0=...,etcd-1=...,etcd-2=...`, `--initial-cluster-state=new`: `--initial-cluster*` はデータディレクトリが空のときだけ使われ, 再起動時は無視される.
- `Parallel` にしているのは, 全メンバーが揃うまで quorum が取れず Ready にならないため (OrderedReady だと etcd-0 が Ready にならず etcd-1 以降が作られない).
- `--auto-compaction-mode=periodic --auto-compaction-retention=1h`: 履歴を1時間で compaction し, DB/WAL の肥大化を防ぐ (bitnami 時代は DB 119kB に対しデータディレクトリが 428MB あった).
- `--listen-metrics-urls=http://0.0.0.0:2381`: probe は `/livez` (liveness) と `/readyz` (startup / readiness) を参照する.
- 起動時に "Running http and grpc server on single port" と "simple token is not cryptographically signed" の警告が出るが, TLS・認証なしの構成のため想定どおり.

## 運用メモ
### 状態の確認
```
kubectl -n external-dns exec etcd-0 -- etcdctl member list -w table
kubectl -n external-dns exec etcd-0 -- etcdctl endpoint status --cluster -w table
kubectl -n external-dns exec etcd-0 -- etcdctl endpoint health --cluster
kubectl -n external-dns exec etcd-0 -- etcdctl get --prefix --keys-only /skydns | grep -c .
```

### バージョンの更新
1. https://github.com/etcd-io/etcd/releases と CHANGELOG (`CHANGELOG/CHANGELOG-3.x.md`) で Breaking Changes を確認する. 削除されたフラグを使っていないか確認する.
2. `statefulset.yaml` の image タグを変更し, commit して `gitlab` と `origin` に push, ArgoCD で sync する.
3. StatefulSet のローリング更新で 1 台ずつ再起動されるため, 名前解決は止まらない.
4. マイナーバージョンを上げると storage version も上がり, そのままでは戻せない. 戻す場合は「作り直し」の手順で旧バージョンを空から作る.

### 1 台の停止・ノード障害
- 2 台が動いていれば読み書きできる. Pod が戻れば自動でクラスターに再参加する (2026-11-13 にリーダーの Pod を削除して確認. 名前解決の失敗なし, external-dns の再起動も不要).
- worker が 3 台しかなく podAntiAffinity が必須のため, ノードが 1 台止まっている間はそのメンバーの Pod は Pending のままになる (2 台で動作は継続).

### 作り直し (メンバーの PVC を失った時・データ破損時・ダウングレード時・メンバー数変更時)
1 メンバーだけ作り直すには `etcdctl member remove/add` と `--initial-cluster-state=existing` での起動が必要で, StatefulSet の共通の引数では扱いにくい. データは再生成できるため, クラスターごと作り直す.
```
# 事前に coredns のキャッシュを温めておく (memo/external-dns-coredns_install_memo.md)
kubectl -n external-dns get pvc -o custom-columns=PVC:.metadata.name,PV:.spec.volumeName   # PV 名を控える
kubectl -n external-dns delete sts etcd
kubectl -n external-dns delete pvc data-etcd-0 data-etcd-1 data-etcd-2
kubectl delete pv <PV 名 x3>                         # longhorn は Retain のため PV も削除する
# ArgoCD で external-dns-etcd を sync (StatefulSet と PVC が新規作成される)
kubectl -n external-dns rollout restart deploy/external-dns   # レコードが再生成される
```
注意: 2026-11 の作業時, StatefulSet を削除した直後の ArgoCD の sync で「configured」と表示されたのに StatefulSet が作成されないことがあった (原因不明). sync 後に `kubectl -n external-dns get sts etcd` で存在を確認し, 無い場合は `kubectl apply -k deploy/external-dns/etcd/manifests` で作成してから ArgoCD で sync し直す (管理用アノテーションが付与される).

## 履歴
- 2026-11-13: 3 メンバー構成へ変更 (作り直し. podAntiAffinity, PDB を追加). coredns の serve_stale により作り直し中も名前解決は継続した.
- 2026-11-13: PV を synology-csi-delete (8Gi) から longhorn (1Gi) へ移行 (データは引き継がず再生成). bitnami chart 12.0.18 (bitnamilegacy/etcd 3.6.4) から公式イメージの自作マニフェストへ置き換え (v3.6.16), 同日 v3.7.3 へ更新.
- 2024-08-04: bitnami chart で構築. TLS 化・冗長化を試みたが, chart の不具合 (TLS 有効時やレプリカ増加時に probe が壊れる) のため 1 Pod・非 TLS で暫定運用していた.
