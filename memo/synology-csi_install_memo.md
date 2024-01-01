# synology-csiインストールメモ

インストール先namespace: synology-csi
シークレット情報は、helm-secretsプラグインで暗号化する.(gpgキーを使用)
helm-secrets: https://github.com/jkroepke/helm-secrets
Zendesk氏のhelm-secretsはpublic archve化しているので注意.

## インストールコマンド
```
helm install synology-csi synology-csi-chart/synology-csi -n synology-csi --values values.yaml --values secrets_dec.yaml
```

gpgキーの設定
ひとまずhelm/synology-csi/配下に配置
.sops.yaml
```
creation_rules:
  - pgp: "XXXXXXXXXXXXXXXXXx"
```

## アップデートコマンド
helm secretsのサブコマンドを使用.
-f オプションを指定すると勝手に復号化して適用してくれる.
```
helm secrets upgrade synology-csi synology-sci-chart/synology-csi -n synology-csi -f values.yaml -f secrets.yaml
```

