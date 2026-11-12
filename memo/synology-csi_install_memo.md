# synology-csiインストールメモ

namespace: synology-csi
helm名: synology-csi

> **2026-11-13 にアンインストール済み.** 以下は導入当時の記録として残している.
> - 利用していた PV はすべて longhorn / qnap-trident (qnap-iscsi) へ移行済み (Harbor, external-dns etcd など).
> - `helm uninstall -n synology-csi synology-csi` で StorageClass 6種, CSIDriver `csi.san.synology.com`, controller / node, RBAC を削除し,
>   Secret (`synology-csi-client-info`, `cifs-csi-credentials`) と namespace を削除した.
>   削除前に, synology-csi を使う PV / PVC / VolumeAttachment がないこと, 各ノードに Synology (192.168.1.161) への iSCSI セッション・マウントがないことを確認した.
> - default StorageClass は longhorn のみになった (以前は synology-csi-nfs-delete と重複していた).
> - values (`helm/synology-csi/values.yaml`) と namespace マニフェスト (`manifests/synology-csi/`) はリポジトリから削除した (git の履歴には残る).

## 方針
ログイン関係のシークレット情報は, helm管理対象外とする.
ブロックストレージはhelmにて標準実装されている2種類のストレージクラスを提供する.
ボリュームはvolume2を使用する.
- synology-csi-delete 
- synology-csi-retain
ファイルストレージは, nfsプロトコルを用いてsynology-csiの機能の中で提供する.
ボリュームはvolume3を使用する.
- synology-csi-nfs-delete
- synology-csi-nfs-retain

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
