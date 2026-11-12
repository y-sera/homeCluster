# external-dnsインストールメモ

namespace: external-dns
管理: ArgoCD (自動syncなし. 変更を push したら手動で sync する)

external-dns 関連は3つのコンポーネントで構成する. このファイルは全体構成と external-dns 本体について記載する.
- etcd: memo/external-dns-backend-etcd_install_memo.md
- coredns: memo/external-dns-coredns_install_memo.md

## 全体構成
```
Ingress / Service ──(external-dns)──> etcd (/skydns) ──> coredns (etcd plugin) ──> 192.168.2.64:53
```
| コンポーネント | ArgoCD Application | Chart / イメージ | 設定 |
|---|---|---|---|
| external-dns | `applications/external-dns/external-dns.yaml` | kubernetes-sigs/external-dns chart 1.23.0 (v0.23.0) | `deploy/external-dns/external-dns/values/values.yaml` |
| coredns | `applications/external-dns/coredns.yaml` | coredns/coredns chart 1.50.5 (1.14.7) | `deploy/external-dns/coredns/values/values.yaml` |
| etcd | `applications/external-dns/etcd.yaml` | 公式イメージ `gcr.io/etcd-development/etcd:v3.7.3` (自作マニフェスト, 3 メンバー) | `deploy/external-dns/etcd/manifests/` |

- values はチャートのデフォルトからの差分のみを記載する (イメージタグはチャートのデフォルトに任せる).
- `cluster.orenet.net` のレコードは external-dns が Ingress などから etcd に書き込み, coredns (192.168.2.64) が配信する.
- kube-system の CoreDNS は `cluster.orenet.net` を 192.168.2.64 に転送している (`manifests/coredns_kube-system/coredns.yaml`). このため Pod から `*.cluster.orenet.net` を引く場合もこの構成に依存する.
- **etcd が全停止すると `*.cluster.orenet.net` の新規の名前解決ができなくなる.** ArgoCD → GitLab (`gitlab.cluster.orenet.net`) や Harbor からのイメージ pull にも影響する.
  - 対策: etcd は 3 メンバー (1 台停止まで影響なし), coredns は `serve_stale` で直近 1 時間に引かれた名前を返し続ける.
  - etcd を全停止する作業 (作り直しなど) の前には, 変更を push して ArgoCD に refresh させ, coredns のキャッシュを温めておく.

## external-dns の設定
- provider: coredns (`ETCD_URLS=http://etcd:2379`)
- registry: txt (所有権を `a-<name>` の TXT レコードで管理)
- policy: sync (先に導入したコンポーネントのレコードも更新できるようにするため. 0.22 以降は指定必須)
- sources: service, ingress, gateway-httproute, gateway-grpcroute, gateway-tcproute, gateway-udproute
  - `gateway-tlsroute` は外している. external-dns 0.22 以降は TLSRoute v1 (Gateway API v1.5+) 前提だが, クラスタの CRD は v1alpha2/v1alpha3 のみ (Gateway API v1.4.1 experimental) のため. Gateway API を v1.5 以上に上げたら戻す.
- アノテーションの接頭辞は `external-dns.kubernetes.io/` のみ (0.22 以降. 旧 `external-dns.alpha.kubernetes.io/` は読まれない).
- chart 1.23 から CRD `dnsrecords.externaldns.k8s.io` (crd registry 用) が作成されるが, txt registry のため未使用.

## インストール手順 (クラスター再構築時)
```
kubectl apply -f manifests/namespace/external-dns.yaml
kubectl apply -f manifests/cilium/lbpool-dns.yaml          # coredns の LoadBalancer IP (192.168.2.64)

# etcd → coredns → external-dns の順に Application を作成し, ArgoCD で sync する
kubectl apply -f applications/external-dns/etcd.yaml
kubectl apply -f applications/external-dns/coredns.yaml
kubectl apply -f applications/external-dns/external-dns.yaml

# kube-system の CoreDNS から cluster.orenet.net を転送する
kubectl apply -f manifests/coredns_kube-system/coredns.yaml
```
GitLab が名前解決できない状態 (etcd 未構築など) では ArgoCD がリポジトリを取得できないため, 初回構築時は `kubectl kustomize` / `helm template` で生成したマニフェストを直接 apply してから ArgoCD の管理下に戻す.

## 運用メモ
### external-dns が CrashLoopBackOff になる
external-dns は etcd に接続できないと fatal で終了する. etcd の作り直しなどで全メンバーが止まった後に CrashLoopBackOff になっていたら再起動する.
```
kubectl -n external-dns rollout restart deploy/external-dns
kubectl -n external-dns logs deploy/external-dns --tail=3   # "All records are already up to date" を確認
```

### レコードの確認
```
dig +short @192.168.2.64 harbor.cluster.orenet.net
kubectl -n external-dns exec etcd-0 -- etcdctl get --prefix --keys-only /skydns
```

### チャートの更新
1. 新旧のチャートを `helm pull --untar` し, `helm template ... -f values.yaml` の結果を比較する.
2. external-dns はリリースノートの Breaking Changes を確認し, 新バージョンを `--dry-run --once` 付きの Pod で1回実行して, レコードの変更予定がないことを確認する (`--log-level` は chart の args に含まれるため重複させない).
3. `applications/external-dns/*.yaml` の `targetRevision` を変更し, commit して `gitlab` と `origin` に push, `kubectl apply` で Application を更新してから sync する.

## 履歴
- 2026-11-13: etcd を 3 メンバー構成に変更. coredns に serve_stale を追加し, 使われていない kubernetes プラグインを削除.
- 2026-11-13: values を `helm/external-dns*/` から `deploy/external-dns/` へ移動し差分のみに整理. etcd の PV を synology-csi から longhorn へ移行, bitnami chart から公式イメージの自作マニフェストへ置き換え (v3.6.16 → v3.7.3). external-dns chart 1.21.1 → 1.23.0, coredns chart 1.45.2 → 1.50.5. coredns の k8sapiserver 転送・cache 設定を修正. Helm CLI 時代のリリース情報 (Secret) を削除.
- それ以前: Helm CLI でインストールしていた (`helm upgrade -i -n external-dns external-dns external-dns/external-dns --values values.yaml`).
