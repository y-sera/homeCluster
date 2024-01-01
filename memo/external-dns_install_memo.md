# external-dnsインストールメモ
インストール先namespace: external-dns
name: external-dns

## 変更した設定
provider: coredns
env:
- name: ETCD_URLS
- value: coredns-backend-etcd用serviceのDNS名

インストールコマンド
```
helm install external-dns external-dns/external-dns --namespace external-dns --values values.yaml
```
