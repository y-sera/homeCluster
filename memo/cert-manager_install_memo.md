# cert-managerインストールメモ

使用namespace: kube-system
name: kube-system

## 変更したパラメータ
- CRDのインストールを有効化
crds.enabled=true
https://cert-manager.io/docs/installation/helm/#crd-considerations

## 注意事項
startup checkが走る関係か, インストール時のhelm upgradeコマンドの応答が遅いが, たぶん問題ないと思われる.

## インストールコマンド
```
helm upgrade -i -n kube-system cert-manager jetstack/cert-manager --values values.yaml
```
