# qnap-trident(QNAP CSI)インストールメモ

namespace: trident
helm名: qnap-trident
チャート/マニフェスト: `deploy/qnap-trident/`(取り込み元・更新手順は `deploy/qnap-trident/README.md` を参照)

## 方針
- backend は `backend-qts`(storageDriverName: `qnap-nas`, 仮想プール `pool1`)を1つだけ作り、プロトコルはストレージクラス側で分ける.
  - `qnap-smb`: SMB(RWX). 大容量の共有データ用(llm-models, arxiv-datasets など).
  - `qnap-iscsi`: iSCSI(RWO). DB・Gitaly・オブジェクトストレージなどのブロック用途.
- どちらも `reclaimPolicy: Retain` とし, PVC を消してもデータは残す.
- ログイン情報などのシークレットは ExternalSecret(Vault)で管理し, helm 管理対象外とする.

## backend の設定(networkInterfaces)
`networkInterfaces` は**未設定(コメントアウトのまま)**とする.
未設定の場合, iSCSI のポータルには backend の接続先(storageAddress = 192.168.1.162)が使われ,
管理用 IP と iSCSI 用 IP が同一である本環境ではこれで十分.

- 指定すると, 仮想スイッチ名の変更や再作成で backend が Failed になる要因が増えるため指定しない.
- 指定する場合の名前は QTS「iSCSI & Fibre Channel → ターゲット → ネットワークポータル」の表示名に合わせる.
  本環境では物理 NIC(Adapter 1)が Virtualization Station 用の仮想スイッチ(Virtual Switch 1)に束ねられており,
  `Virtual Switch 1` は受け付けられたが, 物理アダプタ名(`Adapter1` / `Adapter 1`)は
  `adapter (...) not found; please check the storage network interfaces setting` で backend が Failed になった.
  (参考: https://www.qnap.com/en/how-to/faq/con_show.php?cid=2030)

### 補足: 空のポータル(p1)について
iSCSI ボリュームの接続時, ノードに渡される接続情報は `iscsiTargetPortalCount: 2`, `p1: ""`, `p2: "192.168.1.162"` となり,
trident-node のログに空ポータル(`portal=":3260"`)への discovery 失敗が毎回出力される.
これは QNAP ドライバの挙動で `networkInterfaces` の設定有無では変わらず, `p2` 経由でログインできるため実害はない.
ポータルが複数あるため trident はマルチパスデバイスの作成を前提とする(下記「マルチパス設定」が必須となる理由).

## ノード側の準備(iSCSI を使う全ワーカーノード)

### 必要パッケージ
```bash
sudo apt install open-iscsi multipath-tools
sudo systemctl enable --now iscsid multipathd
```
qnap-smb (SMB) の PVC をマウントするノードには `cifs-utils` も必要.
```bash
sudo apt install cifs-utils
```

### マルチパス設定(重要)
QNAP CSI(Trident)は iSCSI ボリュームの接続時にマルチパスデバイス(`/dev/mapper/mpathX`)の作成を前提とする.
マルチパスデバイスができない場合, Pod は `Init` のまま止まり, 以下のエラーになる.

```
MountVolume.MountDevice failed ... failed to stage volume: multipath device not found when it is expected
```

一方で, Longhorn のメモ(`memo/longhorn_install.md`)では Longhorn との競合回避のため
`devnode "^sd[a-z0-9]+"` で sd デバイスを全てブラックリストにしており, この設定のままだと QNAP の LUN も対象外になってしまう.
また, devnode によるブラックリストは `blacklist_exceptions` で除外できない(devnode の一致が先に評価されるため).

そのため, ブラックリストをデバイス名(devnode)ではなくベンダー名(device)で指定し,
QNAP の LUN だけをマルチパスの対象にする. **Longhorn のメモにある devnode の設定はこの設定で置き換える.**

`/etc/multipath.conf`
```
defaults {
    user_friendly_names yes
    find_multipaths no
    no_path_retry 120
}
blacklist {
    device {
        vendor ".*"
        product ".*"
    }
}
blacklist_exceptions {
    device {
        vendor "QNAP"
        product "iSCSI Storage"
    }
}
```

`no_path_retry 120` は, QNAP の再起動(OS 更新など)で PV が read-only になるのを防ぐための設定.
- 各 LUN は1パスのみのため, QNAP が止まると全パス断になる. 既定値(fail)では dm-multipath が I/O を即エラーで返し,
  書き込み中の ext4 が journal abort して read-only で再マウントされる. パスが戻っても read-only のままで, Pod を作り直すまで直らない
  (2026-11-17 の QNAP 再起動で gitlab-valkey / harbor-redis などが実際にこうなった).
- 設定すると, 全パス断の間も I/O をエラーにせず `no_path_retry × polling_interval(既定5秒)` = 約10分キューで待たせる.
  QNAP が戻れば I/O はそのまま再開し, Pod の再起動は不要.
- 無期限の `queue` にしないのは, QNAP が戻らないときにプロセスが D state で固まり, Pod の削除や drain もできなくなるため.
- 10分を超えて止まった場合は read-only になる. ノードで `journalctl -k | grep -E "Remounting filesystem read-only|Aborting journal"` を確認し,
  該当する Pod を削除して再マウントする.

ノード上のディスクとベンダー名の対応(worker1 で確認)
| デバイス | VENDOR / MODEL | 用途 | マルチパス |
|---|---|---|---|
| sda, sdb | QEMU / QEMU HARDDISK | OS(root LVM), Longhorn 用ディスク | 対象外 |
| sdc〜sdf | IET / VIRTUAL-DISK | Longhorn ボリューム | 対象外 |
| sdg | SYNOLOGY / Storage | synology-csi(iSCSI) | 対象外 |
| sdh | QNAP / iSCSI Storage | qnap-iscsi | **対象** |

OS ディスクが virtio (`/dev/vda`) の VM (qnap-vm など) では, virtio ディスクはベンダーが `0x1af4`, モデル名が空のため,
ベンダー名の指定で確実に除外できるとは限らない. `blacklist` に devnode を追加して明示的に除外する
(QNAP の LUN は sd デバイスのため, この devnode の指定には一致しない).
```
blacklist {
    devnode "^vd[a-z]+"
    device {
        vendor ".*"
        product ".*"
    }
}
```

### 反映手順
```bash
# 1. バックアップ
sudo cp /etc/multipath.conf /etc/multipath.conf.bak.$(date +%Y%m%d)

# 2. 上記の内容に書き換え

# 3. ドライランで対象デバイスを確認(何も作成されない)
sudo multipath -d -v2
#   → QNAP 以外(QEMU / IET / SYNOLOGY)が出てきた場合は反映せず, .bak に戻す

# 4. 反映(restart ではなく reload. 稼働中デバイスの I/O を止めない)
sudo systemctl reload multipathd

# 5. 確認
sudo multipath -ll
#   → "mpathX (...) dm-N QNAP,iSCSI Storage" のみが表示されること
#   → 各 mpath の次の行が features='1 queue_if_no_path' になっていること(no_path_retry が効いている)
#      features='0' のままなら sudo multipathd reconfigure を実行して再確認する
```

iscsid / kubelet / containerd / trident-node の再起動は不要.
マウント待ちの Pod は kubelet の再試行で自動的に起動する(起動しない場合は Pod を作り直す).

## 動作確認
```bash
kubectl -n trident get tbc backend-qts        # STATUS: Success
kubectl get sc qnap-iscsi
# iSCSI の PVC を使う Pod で, マウント先が /dev/mapper/mpathX になっていること
kubectl exec <pod> -- sh -c 'mount | grep <mountPath>'
```

## 補足: PV とノードの関係
- iSCSI の PV は作成時点では特定ノードに固定されない(PV に nodeAffinity は付かない).
  Pod が配置されたノードにその都度 attach される. 同時に attach できるのは1ノードのみ(RWO).
- そのため, **Pod が載りうる全ワーカーノードで上記のマルチパス設定が必要**.
- ノードが突然停止した場合は VolumeAttachment が残り, 移動先で `Multi-Attach error` になることがある.
  その場合は停止ノードに `node.kubernetes.io/out-of-service` taint を付けて強制的に detach する.

## 補足: NIC を変更する場合(IP変更, NIC追加・削除など)
PV(TridentVolume)には IQN のみが保存され, ポータルの IP は attach のたびに backend の設定から計算される.
そのため NIC を変更してもデータに影響はないが, 接続中のセッションは切れるため以下の順で作業する.

1. qnap-iscsi を使うワークロードを replicas 0 にし, VolumeAttachment が消えたことを確認する.
2. QTS で NIC / IP を変更する. 必要に応じて iSCSI ターゲットのネットワークポータル設定も見直す.
3. 管理 IP(storageAddress)を変える場合は `backend-qts-secret` を更新する.
   iSCSI 用に別の NIC / IP を使う場合のみ `deploy/qnap-trident/manifests/backend.yaml` に `networkInterfaces` を設定して apply する.
4. ワークロードを起動し, マウントを確認する.
