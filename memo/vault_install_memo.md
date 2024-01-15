# hashicorp vaultインストールメモ

namespace: vault
helm名: vault

## 方針
Vault Operatorを使用する前提とする. 
以下の理由により, angent, Secret CSI driverは利用しない.
- シークレットのローテーションができない

参考: 
- https://www.hashicorp.com/blog/kubernetes-vault-integration-via-sidecar-agent-injector-vs-csi-provider
- 
手順: 
https://developer.hashicorp.com/vault/tutorials/kubernetes/vault-secrets-operator

今回はvault secrets operatorを導入する.
External Secret Operatorとの比較は一旦見送り, 不便さを感じたら検討する.

### seal
初期設定についてはAWS KMSを利用したauto-unsealにて行う.
https://developer.hashicorp.com/vault/docs/configuration/seal/awskms

auto-unseal用にIAMユーザを作成し, 以下の権限を付与する.
許可アクション: 
- kms:Encrypt
- kms:Decrypt
- kms:DescribeKey
許可リソース: 
"*"

アクセスキー, シークレットアクセスキー, KMSキーエイリアスARNは別途secret.yamlにて管理し,
環境変数にて値を埋め込む.

### HA
ストレージは, casulやetcd, zookeeperか,
Vaultに内部的に統合されたIntegrated Storageの二択がある.

causl, etcdを別運用するのは負荷が高いので, Integrated Storageを利用する.
自動でjoinされるように設定する.

### UI
利用する. このためのingressリソースも作成する.

## 設定
基本的な設定: https://developer.hashicorp.com/vault/docs/platform/k8s/helm/run#protecting-sensitive-vault-configurations
設定パラメータリンク: https://developer.hashicorp.com/vault/docs/configuration#cluster_addr
agent, csi driverは使用しないので無効化する.
injector.enabled: false
csi.enabled: false

raft.configにHA, unsealの設定を追加.
server.extraSecretEnvironmentVarsにAWSのKMSキー等の情報を記載.

## インストール
```
helm install -n vault vault hashicorp/vault --values values.yaml
```

インストール後に以下コマンドで初期化する.(auto-unsealにて自動でunseal化される)
```
k exec -it -n vault vault-0 -- vault operator init
```
root tokenを控えておく.
