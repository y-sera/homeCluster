# external-dns用corednsインストールメモ
namespace名: external-dns-coredns
name: external-dns-coredns


## 変更した設定
- バックエンド: etcd(skydns)
- サービスタイプ: LoadBalancer
- LoadBalancerIP: 192.168.1.64
- dnsフォワード先: 192.168.1.161

## DNS構成
メイン: 192.168.1.64(k8s上のDNS)
apiserver用: 192.168.1.161

## TODO
DNS構成は要検討
転送ゾーン, プライマリ, セカンダリを理解して構成見直し.

# インストールコマンド
```
helm install -n external-dns-coredns external-dns-coredns coredns/coredns
```


