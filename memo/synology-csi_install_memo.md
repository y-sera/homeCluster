# synology-csiインストールメモ

namespace: synology-csi
helm名: synology-csi

## 方針
ログイン関係のシークレット情報は, helm管理対象外とする.
ブロックストレージはhelmにて標準実装されている2種類のストレージクラスを提供する.
- synology-csi-delete 
- synology-csi-retain
ファイルストレージは, smbプロトコルを用いてsynology-csiの機能の中で提供する.
- synology-csi-smb-delete
- synology-csi-smb-retain

defaultストレージクラスは, synology-csi-deleteとする.
これは, PVCを削除すれば自動でLUNを開放してくれる.
必要に応じて, ストレージクラスは増やしていく.


## シークレット用yaml設定内容
クライアント用シークレット情報
clients-secrets.yaml
```
apiVersion: v1
kind: Secret
metadata:
  name: synology-csi-client-info
  namespace: synology-csi
type: Opaque
data:
  client-info.yaml: |
    clients:
    - host: 192.168.1.161
      https: false
      password: <password>
      port: 5000
      username: <username>
    - host: 192.168.1.161
      https: true
      password: <password>
      port: 5001
      username: <username>
```

smb用のログイン情報は, 事前にシークレットリソースとして登録しておく必要がある.
https://github.com/SynologyOpenSource/synology-csi#creating-storage-classes
cifs-secrets.yaml
```
apiVersion: v1
kind: Secret
metadata:
  name: cifs-csi-credentials
  namespace: synology-csi
type: Opaque
stringData:
  username: <username>  # DSM user account accessing the shared folder
  password: <password>  # DSM user password accessing the shared folder
```

## インストールコマンド
```
helm upgrade -i synology-csi synology-csi-chart/synology-csi -n synology-csi --values values.yaml
```
