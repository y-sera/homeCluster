# minio-tenant インストールメモ
namespace: minio-tenant
helm名: minio-tenant1
chart名: minio-operator/tenant

## 方針
1. tenantはhelmで管理する(minio-oerator/tenant).
2. ストレージクラスをsynology-csi-deleteを利用しようとしたが, うまく行かなかったため諦め.
   各ノードに導入するdirectpvを利用する.(directpvインストールメモを参照)
3. テナント名は, 今後増やしていくことを想定しtenant1とする(ただし, namespace名は一旦ナンバリングは振らないでおく). 
   名前やインストール先についても今後必要に応じて検討する.

## 設定変更
- ingress有効化. ホスト名: minio-operator.cluster.orenet.net
## インストールコマンド
```
helm install -n minio-tenant minio-tenant1 minio-operator/tenant --vaules values.yaml
```

ログイン用のトークン
helmの標準設定だと, secretsのmyminio-env-configurationから拾ってくることで取得できる.
(今後vaultに寄せる?)
