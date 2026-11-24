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

## バージョンアップ
chart (vault-helm) は Vault 本体より遅れて出るため, chart に新しい版がない場合は
`deploy/vault/values/values.yaml` の `server.image.tag` (と `injector.agentImage.tag`) だけを上げる.
StatefulSet は OnDelete のため, ArgoCD で同期しても Pod は入れ替わらない. Pod は手で 1 台ずつ削除する.

事前の確認:
- 間のマイナー版の important changes を読む (https://developer.hashicorp.com/vault/docs/updates/important-changes)
- raft のスナップショットを取り, 手元に保存する
```
kubectl -n vault exec -it vault-1 -- sh    # active の Pod
vault login
vault operator raft snapshot save /tmp/pre-upgrade.snap
rm -f ~/.vault-token
exit
kubectl -n vault cp vault-1:/tmp/pre-upgrade.snap ./vault-pre-upgrade.snap
kubectl -n vault exec vault-1 -- rm -f /tmp/pre-upgrade.snap
```
- 3 台とも raft に参加していることを確認する (`vault status` の `Raft Applied Index` がそろっている,
  active の Pod のログに `failed to heartbeat` / `failed to appendEntries` が出ていない)

入れ替え (standby → standby → active の順. step-down は使わない):
```
kubectl -n vault exec vault-N -- vault status | grep -E 'HA Mode|Version'   # active を確認
kubectl -n vault delete pod <standby>
# Ready になったら: Sealed false (自動 unseal), Version, HA Mode standby, Raft Applied Index が他と同じ
# 2 台目の standby も同様. 最後に active を削除 (新しい版の standby が active を引き継ぐ)
```
最後に ClusterSecretStore `vault` が Valid, ExternalSecret が SecretSynced であることを確認する.

## raft から外れたノードを参加し直させる
ノード障害で Pod が強制的に作り直された後などに, leader のログに
`failed to heartbeat to: peer=vault-N... remote error: tls: unrecognized name` が出続け,
そのノードの Raft Applied Index が進まない場合は, raft から外してデータを消し, 参加し直させる.
```
kubectl -n vault exec -it <active> -- sh
vault login
vault operator raft list-peers                 # 外れたノードの Node ID を確認
vault operator raft remove-peer <Node ID>
rm -f ~/.vault-token
exit
kubectl -n vault delete pvc data-vault-N --wait=false
kubectl -n vault delete pod vault-N
```
新しい PVC で起動した Pod は retry_join で参加し (自動 unseal), autopilot により約 10 秒で voter に昇格する.
