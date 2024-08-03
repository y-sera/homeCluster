# external-dnsインストールメモ
インストール先namespace: external-dns
name: external-dns

## 変更した設定
provider: coredns
env:
- name: ETCD_URLS
- value: coredns-backend-etcd用serviceのDNS名
policy: sync

## 備考
hubbleなど, 先に導入したコンポーネントも更新できるようにpolicyをsyncとしておく.

## インストールコマンド
```
helm upgrade -i -n external-dns external-dns external-dns/external-dns --values values.yaml
```
