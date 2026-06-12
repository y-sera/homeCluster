# longhornのインストール関連メモ

minioベースのdirectpvから, 分散ストレージのLonghornに移行する.
PV用の領域として, 各workerノードにて`/dev/sdb`に300GBのストレージを確保していたため, そちらを流用する.

## 準備

各ノードにて, 以下の作業を実施.

```bash
# format filesystem
sudo mkfs.ext4 /dev/sdb

# create mountpoint
sudo mkdir -p /mnt/longhorn-disk

# check UUID
sudo blkid /dev/sdb

# add below to /etc/fstab
# UUID=<your disk uuid> /mnt/longhorn-disk ext4 defaults 0 0
vi /etc/fstab
```

また, このままではマルチパスがLonghornボリュームデバイスに対してもマルチパスデバイスを作成してしまい, ストレージがマウントできなくなってしまうため, 以下を実施する.
参考: [Troubleshooting: `MountVolume.SetUp failed for volume` due to multipathd on the node](https://longhorn.io/kb/troubleshooting-volume-with-multipath/)

以下を`/etc/multipath.conf`に追記
```
blacklist {
    devnode "^sd[a-z0-9]+"
}
```

```
sudo systemctl restart multipathd.service
```

## ArgoCD用の設定

Helmインストールと異なり, ArgoCDによる初回インストールにおいては, 以下のパラメータをfalseとしておく.
こうしておかないと, preUpdateCheckのjobが最初に走ろうとしてしまうが, サービスアカウントが存在しないことによりハングしてしまう.
初回インストール後はtrueに戻しても問題ない.

```values.yaml
preUpgradeChecker:
  # -- Setting that allows Longhorn to perform pre-upgrade checks. Disable this setting when installing Longhorn using Argo CD or other GitOps solutions.
  jobEnabled: false
```
