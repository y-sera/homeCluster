# EdgeRouter X DNS 設定メモ

EdgeRouter X (192.168.1.1, EdgeOS v3.0.1) の DNS フォワーダー (dnsmasq 2.85) の設定.
クラスターのノードの nameserver. 設定は Git 管理外なので, ルーターの交換・初期化時はこのメモから復旧する.

## 方針
- `k8sapiserver.cluster.orenet.net` (kube-vip の VIP 192.168.1.2) はルーター自身が応答する.
  Kubernetes API サーバーの接続先なので, クラスター上の DNS (192.168.2.64) が止まっていても引けるようにする.
- それ以外の `cluster.orenet.net` は external-dns 用 coredns (192.168.2.64) へ転送する (memo/external-dns-coredns_install_memo.md).
- その他の名前は上流 (192.168.200.1, YAMAHA RTX1220) へ転送する.

## 設定
```
configure
set service dns forwarding cache-size 150
set service dns forwarding listen-on switch0     # 192.168.1.0/24 (192.168.2.0/24 も switch0 経由で届く)
set service dns forwarding listen-on switch0.1   # 192.168.101.0/24 (Proxmox 管理)
set service dns forwarding listen-on wg0         # WireGuard
set service dns forwarding listen-on eth0        # 192.168.200.0/24 と, その先の 192.168.100.0/24
set service dns forwarding options "server=/cluster.orenet.net/192.168.2.64"
set service dns forwarding options "host-record=k8sapiserver.cluster.orenet.net,192.168.1.2"
set service dns forwarding options "local=/k8sapiserver.cluster.orenet.net/"
set system name-server 192.168.200.1
commit
save
exit
```
- 反映後の dnsmasq の設定は `/etc/dnsmasq.conf` で確認できる.
- キャッシュのクリアは `clear dns forwarding cache`.

## 注意
- `cluster.orenet.net` は `address=` ではなく `server=` (転送) にする. 192.168.2.64 は Ingress ではなく DNS サーバーで, Ingress ごとに IP が異なる (例: argocd → 192.168.2.13).
- k8sapiserver に `address=/k8sapiserver.../192.168.1.2` は使わないこと. dnsmasq 2.85 では IPv4 だけの `address=` は AAAA を上流 (= `server=` の 192.168.2.64) へ転送する. 192.168.2.64 は k8sapiserver を 192.168.1.1 へ転送するため, AAAA がループしてタイムアウトする.
  `host-record` + `local=` なら AAAA は NODATA で即答し, 上流へは転送しない.
- `host-record` / `local=` は `server=/cluster.orenet.net/` より細かい一致なので優先される.
- ER-X の DHCP サーバーは無効. クライアントの DNS は各端末 (または別の DHCP サーバー) で 192.168.1.1 を指定する. WireGuard クライアントは `[Interface]` の `DNS = 192.168.1.1`.
- eth0 は `WAN_LOCAL` (default accept) のため, 192.168.200.1 配下の端末はすべて問い合わせできる. eth0 はプライベートアドレスでインターネットからは直接届かない.

## 関連: YAMAHA RTX1220 (192.168.100.1)
192.168.100.0/24 の DNS. `cluster.orenet.net` だけ ER-X へ転送し, それ以外はプロバイダの DNS へ問い合わせる.
192.168.2.64 が止まっても, 192.168.100.0/24 の一般の名前解決には影響しない.
```
dns server 8.8.8.8
dns server pp 1
dns server select 10 192.168.1.1 any cluster.orenet.net 192.168.100.0-192.168.100.255
dns server select 500001 pp 1 any . restrict pp 1
```
- `dns server` に 192.168.2.64 を入れないこと. PPPoE 切断時 (`restrict pp 1` で select 500001 が無効) に RTX → 192.168.2.64 → 192.168.1.1 → 192.168.200.1 (RTX) とループする.

## 確認
```
dig @192.168.1.1 k8sapiserver.cluster.orenet.net        # 192.168.1.2
dig @192.168.1.1 k8sapiserver.cluster.orenet.net AAAA   # NOERROR, ANSWER 0 (即答)
dig @192.168.1.1 argocd.cluster.orenet.net              # 192.168.2.13 (192.168.2.64 から)
dig @192.168.1.1 www.google.com
dig @192.168.100.1 argocd.cluster.orenet.net            # RTX 経由
```
