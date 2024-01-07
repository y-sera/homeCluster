# hashicorp vaultインストールメモ

namespace: vault
helm名: vault

## 方針
Vault Operatorを使用する前提とする. 
以下の理由により, Secret CSI driverは利用しない.
- シークレットのローテーションができない

参考: 
- https://www.hashicorp.com/blog/kubernetes-vault-integration-via-sidecar-agent-injector-vs-csi-provider
- 
手順: 
https://developer.hashicorp.com/vault/tutorials/kubernetes/vault-secrets-operator

External Secret Operatorとの比較は一旦見送る. 
Hashicorp Vaultを導入した後, 不便さを感じたら検討する.


## 設定
agent, csi driverは使用しないので無効化する.
injector.enabled: false
csi.enabled: false

## インストール
```
helm install -n vault vault hashicorp/vault --values values.yaml
```

