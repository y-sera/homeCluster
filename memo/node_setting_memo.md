# ワーカーノード再構築時の設定メモ

## ワーカーノード設定
### ハード
CPU: 4コア
MEMORY: 32GB
Storage:
- OS/root用(/dev/sda): 100GB
- TopoLVM用(/dev/sdb): 300GB

### ソフト
OS: Ubuntu22.04 Server(minimum Install)
Storage:
- ubuntu-lv: ubuntu-vgの最大値まで拡張(96.945G)
IP: 192.168.1.32-34


## 標準パッケージ
vim
dnsutils
traceroute
iputils-ping

## スワップ無効化
```
# check
swapon -s

sudo swapoff -a

# after check
swapon -s

# delete swap file
sudo rm /swap.img
```

## firewall
ufw未インストール


## カーネルモジュールインストール

### ストレージ設定
```
sudo fdisk /dev/sdb
```
nでボリューム作成. パーティションタイプはLinux filesystemとする.
tで変更できる.
もしかしたらこの工程は飛ばして, このままpvcreateから行っても問題ないかもしれない.


### LVM設定
TopoLVM用のボリュームグループはtopolvm-vgとする
```
sudo pvcreate /dev/sdb1
# check pv
sudo pvs

# create VG
sudo vgcreate topolvm-vg /dev/sdb1 

# check vg
sudo vgs
```

## containerdの設定
パッケージインストールしたcontainerdの設定を行う
systemdにてcgroupを動かす設定を追加する.

```
sudo mkdir /etc/containerd
sudo su -
containerd config default > /etc/containerd/config.toml
```

```
[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runc]
  ...
  [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runc.options]
    SystemdCgroup = true
```

