# vault-secrets-operatorインストールメモ
namespace: vault-secrets-operator
helm名: vault-secrets-operator

## 方針
とりあえず導入して, 今後必要に応じてチューニングする

## 設定箇所
公式チュートリアルの設定に従う.(https://developer.hashicorp.com/vault/tutorials/kubernetes/vault-secrets-operator)
defaultconection: ture
address: vaultのサービスに対応して値を設定

## インストールコマンド
```
helm upgrade -n vault vault hashicorp/vault --values values.yaml
```
