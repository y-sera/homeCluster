# external-dns用corednsインストールメモ
namespace名: external-dns-coredns
name: external-dns-coredns

## 方針など
- クラスター外向けのDNSとして構築.
- バックエンドはetcdを利用する.
- 将来的には, etcdはsecretを介した証明書認証にて行う.(現時点では無効化)
- ロードバランサーのIP割当はcilium bgp control planeにて行うため, 
  lbpool: external-dnsのラベルを付与する.

## 変更した設定
- バックエンド: etcd(skydns)
- サービスタイプ: LoadBalancer
- LoadBalancerIP: 192.168.1.64
- dnsフォワード先: 192.168.1.161
- カスタムラベル: lbpool: external-dns

## DNS構成
メイン: 192.168.1.64(k8s上のDNS)
apiserver用: 192.168.1.161

## TODO
- DNS構成は要検討
- 転送ゾーン, プライマリ, セカンダリを理解して構成見直し.

# インストールコマンド
```
helm upgrade -i -n external-dns coredns coredns/coredns --values values.yaml
```


