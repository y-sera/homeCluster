# cert-managerインストールメモ

使用namespace: cert-manager
name: cert-manager

## 変更したパラメータ
- CRDのインストールを有効化
kubectlとhelmの一長一短らしい.
https://cert-manager.io/docs/installation/helm/#crd-considerations

## インストールコマンド
```
helm install -n cert-manager cert-manager jetstack/cert-manager --values values.yaml
```
