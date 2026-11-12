# external-dns用corednsインストールメモ

namespace: external-dns
リリース名: coredns
Chart: coredns/coredns (https://coredns.github.io/helm) 1.50.5 / CoreDNS 1.14.7
管理: ArgoCD Application `applications/external-dns/coredns.yaml`
設定: `deploy/external-dns/coredns/values/values.yaml` (チャートのデフォルトからの差分のみ)
全体構成: memo/external-dns_install_memo.md

## 方針
- クラスター外向けの DNS として構築する. `cluster.orenet.net` のレコードは etcd (external-dns が書き込む) から配信する.
- Service は LoadBalancer. IP (192.168.2.64) は cilium の `CiliumLoadBalancerIPPool` (`manifests/cilium/lbpool-dns.yaml`) から割り当てるため, `lbpool: external-dns` ラベルを付与する.
- レプリカ3. 設定変更時もローリング更新で名前解決は止まらない.
- chart 1.50 から非 root (uid 65532) で実行し, 専用の ServiceAccount `coredns` を使う (53番ポートは `NET_BIND_SERVICE` で開く).

## サーバーブロック
| ゾーン | 内容 |
|---|---|
| `cluster.orenet.net` | etcd プラグイン (`/skydns`, `http://etcd:2379`) でレコードを配信. cache 30 (serve_stale 1h verify) |
| `k8sapiserver.cluster.orenet.net` | Synology の DNS (192.168.1.161) へ転送. cache 30 |
| `.` | 192.168.1.1 へ転送. cache 30, loop |

- prometheus (`:9153`) は全ブロックに入れる (ブロック単位で有効になるため).
- `health` / `ready` は1か所 (`cluster.orenet.net` ブロック) にあればよい.

### serve_stale について
- `cache 30 { serve_stale 1h verify }`: まず etcd に問い合わせ, 失敗したときだけ期限切れのキャッシュ (最大1時間) を返す.
- etcd の作り直しやメンバー全停止の間も, 直近1時間に引かれた名前は解決できる. 一度も引かれていない名前は解決できない.
- キャッシュは coredns の Pod ごと. etcd を止める作業の前に, 全 Pod にレコードを引かせておくと確実 (下記「キャッシュを温める」).
- 2026-11-13 にテスト用の etcd / coredns で確認済み (etcd 停止 40 秒後もキャッシュ済みの名前は応答, 未キャッシュの名前は SERVFAIL).

### k8sapiserver.cluster.orenet.net について
- Kubernetes API サーバーの接続先 (kubeadm の `--control-plane-endpoint`, kubeconfig, cilium の `k8sServiceHost`). 実体は kube-vip の VIP 192.168.1.2 で, レコードは Synology の DNS が持つ.
- `cluster.orenet.net` ブロック内に `forward k8sapiserver... 192.168.1.161` を書いても効かない. CoreDNS のプラグインの実行順は Corefile の記述順ではなく固定 (plugin.cfg) で, etcd が forward より先に実行され, ゾーン全体に NXDOMAIN を返すため. CoreDNS は最も細かく一致するゾーンのブロックを使うので, 専用ブロックで転送している.
- Synology 側にレコードがあるため, 192.168.2.64 ⇔ Synology の間でループはしない.

## 運用メモ
### 設定変更の確認
本番に反映する前に, 同じイメージと Corefile でテスト用の Pod を立てて確認する.
```
helm template coredns <chart> -n external-dns -f deploy/external-dns/coredns/values/values.yaml   # ConfigMap の Corefile を取り出す
kubectl -n external-dns create configmap coredns-test --from-file=Corefile=<file>
# coredns-test Pod (args: -conf /etc/coredns/Corefile) を作成し, busybox の nslookup で Pod IP に問い合わせる
# 確認後, Pod と ConfigMap を削除する
```

### キャッシュを温める (etcd を止める作業の前)
```
IPS=$(kubectl -n external-dns get pods -l app.kubernetes.io/name=coredns -o jsonpath='{.items[*].status.podIP}')
NAMES=$(kubectl get ingress -A -o jsonpath='{.items[*].spec.rules[*].host}')
kubectl -n external-dns run dnswarm --rm -i --restart=Never --image=busybox:1.37 --env="IPS=$IPS" --env="NAMES=$NAMES" \
  -- sh -c 'for ip in $IPS; do for n in $NAMES; do nslookup $n $ip >/dev/null 2>&1 || echo NG $n $ip; done; done'
```

### 動作確認
```
dig +short @192.168.2.64 k8sapiserver.cluster.orenet.net   # 192.168.1.2
dig +short @192.168.2.64 harbor.cluster.orenet.net         # etcd のレコード
dig @192.168.2.64 www.google.com                           # 外部への転送
```

## 履歴
- 2026-11-13: `cluster.orenet.net` ブロックから使われていない `kubernetes` プラグインを削除, `loop` を `.` ブロックへ移動, cache に `serve_stale 1h verify` を追加, prometheus を全ブロックで有効化.
- 2026-11-13: values を差分のみに整理し `deploy/external-dns/coredns/` へ移動. k8sapiserver の転送を専用ブロックに変更, `.` ブロックの cache 設定の誤記 (`parameter` → `parameters`) を修正, 使われていない flightctl 用の rewrite を削除. chart 1.45.2 (1.13.1) → 1.50.5 (1.14.7).
- それ以前: Helm CLI でインストールしていた (`helm upgrade -i -n external-dns coredns coredns/coredns --values values.yaml`).
