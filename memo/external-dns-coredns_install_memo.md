# external-dns用corednsインストールメモ
namespace名: external-dns-coredns
name: external-dns-coredns


## 変更した設定
- バックエンド: etcd(skydns)
- サービスタイプ: LoadBalancer
- LoadBalancerIP: 192.168.1.64
- dnsフォワード先: 192.168.1.161

# インストールコマンド
```
helm install -n external-dns-coredns external-dns-coredns coredns/coredns
```


