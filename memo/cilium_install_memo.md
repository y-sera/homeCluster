# ciilum instlalのメモ
namespace名: kube-system
helm名: cilium
chart名: cilium/cilium

## インストール方針
### クラスター構築時
values.yamlは, cilium-cliより生成されたものをベースに書き換え
cilium-cli自体は、バイナリインストールとする.

kube-proxyを無効化し, eBPFベースのルーティングを使用する.
CNIをインストール後, kube-vip, External-DNS, Cert-manager等を導入する.

参考
https://github.com/cilium/cilium-cli
```
cilium install --dry-run-helm-values
```

### クラスター構築後
External-DNS/証明書を作成した後にhelmを適用し, hubbleを有効化する.


## 変更したもの(クラスター構築時)
```
bpf:
  masquerade: true
operator:
  replicas: 2
```

## クラスター構築時コマンド
```
helm install -n kube-system cilium cilium/cilium --values setup_values.yaml
```

## クラスター構築後helm更新(hubble等インストール)
```
helm upgrade -n kube-system cilium cilium/cilium --values values.yaml
```


