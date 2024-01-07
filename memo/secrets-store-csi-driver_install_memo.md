# secret store csi driverインストールメモ
namespace: kube-system
helm名: csi-secrets-store

## 方針
ひとまず標準構成のままインストール
プロバイダーはvaultを選択する. (後から別途インストール)

## インストールコマンド
```
helm install -n kube-system csi-secrets-store secrets-store-csi-driver/secrets-store-csi-driver --values values.yaml
``` 
