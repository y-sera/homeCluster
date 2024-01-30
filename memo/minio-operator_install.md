# minio-operator インストールメモ
namespace: minio-operator
helm名: minio-operator
chart名: minio-operator/operator

## 方針
1. operatorはhelmで導入する. tenantもhelmで管理する(minio-oerator/tenant).
2. helmチャートは, minio-operator/operatorを使用する. minio-operator/minio-operatorは古いチャートであるため使用しない.

https://min.io/docs/minio/kubernetes/upstream/operations/install-deploy-manage/deploy-operator-helm.html
```
The minio-operator/minio-operator is a legacy chart and should not be installed under normal circumstances.
```


## 設定変更
- ingress有効化. ホスト名: minio-operator.cluster.orenet.net
## インストールコマンド
```
helm install -n minio-operator minio-operator minio-operator/operator --vaules values.yaml
```

ログイン用のトークン取得
```
kubectl get secret/console-sa-secret -n minio-operator -o json | jq -r ".data.token" | base64 -d
```
