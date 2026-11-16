# ciilum instlalのメモ
namespace名: kube-system
helm名: cilium
chart名: cilium/cilium

## インストール方針
- kube-proxyを無効化し, eBPFベースのルーティングを使用する.
- BGP Control Planeを使用することにより, ExternalIPをCiliumにて実装する.
- Ingress Classもciliumを使用する. ingressに使用するLoadBalancerはsharedとする.
- CNIをインストール後, External-DNS, Cert-manager等を導入する.
- hubbleを有効化する. Ingressにはcert-manager用のアノテーションも付与しておく.
- v1.15.7時点において, kube-apiserverの冗長化はkube-vipを使用する. 将来的にciliumが対応したらそちらに移行する.
- `cni.exclusive=false` とする. Istio の istio-cni (chained plugin) が `/etc/cni/net.d` の cilium の conflist に追記するため
  (true だと cilium が他の CNI 設定を `*.cilium_bak` にリネームする). この値は agent 起動時に読まれるため,
  変更時は helm upgrade 後に `kubectl -n kube-system rollout restart ds cilium` が必要.

### カスタムリソース
- CiliumBGPPeeringPolicy
- CiliumLoadBalancerIPPool
インストール後にmanifest/cilium/以下のリソースをapplyする.

### LoadBalancerIPPool
以下の4種類を用意する.
- Ingress
- apiserver
- external-dns
- any-pool

### 注意点(v1.15.7)
- BGPコントロールプレーンを使用する場合はdatapathを使用しないこと.
- CiliumBGPPeeringPolicyは各ノードに2つ以上存在してはいけない.
- VirtualRouterは各ノードに複数存在可能. ただし, LocalASNは重複してはいけない.
- VirtualRouterにはServiceSelectorの指定が必要. これがないと経路広告されない.(任意のサービスを使用したければNotInオペレータを使用する)


## インストール/アップグレードコマンド
```
helm upgrade -i  -n kube-system cilium cilium/cilium --values values.yaml
```
