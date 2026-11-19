# DGX Spark ノード追加手順

DGX Spark を GPU 専用ワーカーとして既存クラスタ (kubeadm, v1.35.4, Cilium) に参加させる手順.
GPU を使わない Pod が載らないよう, 参加時点で taint `nvidia.com/gpu=present:NoSchedule` を付ける.

## 前提

| | spark1 | spark2 |
|---|---|---|
| LAN (enP7s7, MTU 1500) | 192.168.1.98/24 | 192.168.1.99/24 |
| QSFP rail0 (enp1s0f0np0, MTU 9000) | 192.168.10.10/24 | 192.168.10.11/24 |
| QSFP rail1 (enP2p1s0f0np0, MTU 9000) | 192.168.11.10/24 | 192.168.11.11/24 |
| RoCE HCA | rocep1s0f0, roceP2p1s0f0 | 同左 |

- ノード名は `spark<N>` にする (kubelet-csr-approver の許可 regex が `spark[0-9]+`. 別の名前にする場合は先に regex を追加する)
- OS: DGX OS (Ubuntu 24.04, aarch64). NVIDIA driver と nvidia-container-toolkit はプリインストールのものを使う (入れ替えない.
  GPU Operator は driver/toolkit 無効で使う)
- containerd.io (Docker リポジトリ版) はそのまま使う. 既定で CRI 無効なので 2. で有効化する
- QSFP は NCCL 専用. k8s のノード IP は LAN 側 (kubelet `node-ip` で固定)
- Spark 上のコマンドはすべて root (`sudo -i`) で, 1台ずつ実施する

## 0. 事前確認

### ディスク空き容量
kubelet の eviction 既定値は `nodefs.available<10%`, `imagefs.available<15%`.
ルートディスク (3.7T) の空きが 15% (約 560G) を切っていると DiskPressure になり Pod が退避される.
モデルは llm-models PVC に置き, ローカル (`~/.cache/huggingface` など) に溜めない.
```
df -h /
```

### DNS
他ノードと揃えて 192.168.1.1 (EdgeRouter X) を参照させる.
LAN 側は NetworkManager 管理 (接続名は `nmcli -g NAME,DEVICE con show --active` で確認).
```
resolvectl dns
nmcli con mod "<接続名>" ipv4.dns 192.168.1.1
nmcli device reapply enP7s7
getent hosts k8sapiserver.cluster.orenet.net   # 192.168.1.2 (kube-vip VIP) が返ること
```

## 1. OS 設定

### swap 無効化
```
swapoff -a
sed -i.bak '/swap.img/s/^/#/' /etc/fstab
rm /swap.img
swapon --show   # 何も出ないこと
```

### カーネルモジュール / sysctl
```
cat <<EOF > /etc/modules-load.d/k8s.conf
overlay
br_netfilter
EOF
modprobe overlay
modprobe br_netfilter

cat <<EOF > /etc/sysctl.d/k8s.conf
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF
sysctl --system
```

### k8s 外で GPU を使うサービスの停止 (方針: GPU 利用は k8s 経由に統一)
kubelet は k8s 外のコンテナ/プロセスを把握できず, GPU やユニファイドメモリ (128GB) の取り合い,
image GC 対象外のイメージによるディスク逼迫の原因になる.

Docker は停止する. containerd は k8s が使うので止めない. DGX Dashboard は Docker に依存しない (動作継続).
DGX Dashboard の JupyterLab も k8s 外で GPU を使うので使わない.
```
docker ps -a; docker images        # 必要なものが無いこと
systemctl disable --now docker.service docker.socket
systemctl is-active docker docker.socket containerd   # inactive, inactive, active
```
docker0 ブリッジと Docker の iptables ルール (FORWARD DROP) は再起動で消える (2. の最後で再起動する).
Docker が必要になったら `systemctl enable --now docker` で戻し, 使う間は
cordon/drain する (2ノード LLM は LWS を replicas 0 にしてから).

ollama などの推論サーバーが入っていれば同様に停止/削除する:
```
systemctl list-units --type=service --state=running | grep -iE 'ollama|vllm|triton|jupyter'
nvidia-smi                          # GPU を使っているプロセスが無いこと
```

### iSCSI マルチパス (qnap-iscsi 用)
GPU Pod の PVC は qnap-iscsi (trident) を使う (Longhorn は Spark で動かない).
trident は iSCSI ボリュームのマルチパスデバイス (`/dev/mapper/mpathX`) を前提とするため,
QNAP の LUN だけをマルチパスの対象にする (worker と同じ設定. 詳細は `qnap-trident_install_memo.md`).
未設定だと Pod が `multipath device not found when it is expected` で Init のまま止まる.

DGX OS には open-iscsi / multipath-tools が入っており, iscsid / multipathd も動いている.
```
systemctl is-active iscsid multipathd   # active, active
cp /etc/multipath.conf /etc/multipath.conf.bak.$(date +%Y%m%d)
cat > /etc/multipath.conf <<'EOF'
defaults {
    user_friendly_names yes
    find_multipaths no
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
EOF
multipath -d -v2                 # QNAP 以外 (NVMe など) が出ないこと (iSCSI 未接続なら何も出ない)
systemctl reload multipathd
```
qnap-iscsi の PVC を使う Pod が載った後, `multipath -ll` で `QNAP,iSCSI Storage` の mpath が表示されること.

## 2. containerd (CRI 有効化)

Docker 用の既定設定 (`disabled_plugins = ["cri"]`) を, k8s 用の既定設定で置き換える.

`/etc/containerd/conf.d/` に古いドロップイン (以前の GPU Operator が置いた, BinaryName が `/usr/local/nvidia/toolkit/...` のもの等) が
あれば退避する. imports のドロップインは本体設定を上書きするため, runc の `SystemdCgroup = false` などが残ると戻ってしまう.
```
ls /etc/containerd/conf.d/          # 空であること (あれば mv で退避)
```

```
cp -a /etc/containerd/config.toml /etc/containerd/config.toml.bak
containerd config default > /etc/containerd/config.toml
```

既定設定には `imports = ['/etc/containerd/conf.d/*.toml']` (nvidia-ctk のドロップイン用) が
最初から入っているので, 変更は runc の `SystemdCgroup` だけ
(`[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.runc.options]` 配下). 既存ワーカーも同じ設定のみ.
```
sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
grep -nE "^imports|SystemdCgroup|disabled_plugins" /etc/containerd/config.toml
#  imports = ['/etc/containerd/conf.d/*.toml'], SystemdCgroup = true, disabled_plugins = []
```

NVIDIA ランタイムを登録 (`/etc/containerd/conf.d/99-nvidia.toml` が作られる. 既定ランタイムにはしない.
GPU Pod は RuntimeClass/CDI で使い分ける):
```
nvidia-ctk runtime configure --runtime=containerd
systemctl restart containerd
# ドロップインとマージ後の実効設定: SystemdCgroup はすべて true, nvidia の BinaryName は /usr/bin/nvidia-container-runtime,
# default_runtime_name = runc, enable_cdi = true
containerd config dump | grep -E 'SystemdCgroup|BinaryName|default_runtime_name|enable_cdi'
```

### 再起動して確認
```
reboot
# 再起動後
swapon --show                          # 何も出ない
lsmod | grep -E 'br_netfilter|overlay'
sysctl net.ipv4.ip_forward net.bridge.bridge-nf-call-iptables   # = 1
systemctl is-active docker containerd  # inactive, active
ip link show docker0                   # 存在しない
```

## 3. kubeadm / kubelet / kubectl (クラスタと同じバージョン, arm64)

```
curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.35/deb/Release.key | gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.35/deb/ /' > /etc/apt/sources.list.d/kubernetes.list
apt-get update
apt-get install -y kubelet=1.35.4-1.1 kubeadm=1.35.4-1.1 kubectl=1.35.4-1.1
apt-mark hold kubelet kubeadm kubectl
```
join 前の kubelet は `/var/lib/kubelet/config.yaml` が無いため再起動を繰り返すが正常.

## 4. join

いずれかのコントロールプレーン (master1 など) で join 情報を発行 (token の有効期限は 24 時間):
```
kubeadm token create --print-join-command
```

Spark に `/root/join.yaml` を作成 (name, node-ip, token, hash を置き換え):
```yaml
apiVersion: kubeadm.k8s.io/v1beta4
kind: JoinConfiguration
discovery:
  bootstrapToken:
    apiServerEndpoint: k8sapiserver.cluster.orenet.net:6443
    token: <token>
    caCertHashes:
      - sha256:<hash>
nodeRegistration:
  name: spark<N>
  criSocket: unix:///run/containerd/containerd.sock
  kubeletExtraArgs:
    - name: node-ip
      value: <LAN IP>
  taints:
    - key: nvidia.com/gpu
      value: present
      effect: NoSchedule
```

```
chmod 600 /root/join.yaml
kubeadm join --config /root/join.yaml
rm /root/join.yaml        # token を含むため
```

join に失敗した場合は **その Spark 上で** `kubeadm reset -f` してからやり直す.
`kubeadm reset` をコントロールプレーンで実行すると, そのノードが etcd から外れ証明書・static pod が消える
(復旧には `kubeadm join --control-plane` での再参加が必要).

## 5. 参加後 (作業端末から)

```
kubectl label node spark<N> node-role.kubernetes.io/gpu=""
kubectl get node spark<N> -o wide            # InternalIP が LAN IP, arm64, taint 付き
kubectl get csr | grep spark<N>              # kubelet-serving が Approved,Issued (kubelet-csr-approver)
kubectl get pods -A -o wide --field-selector spec.nodeName=spark<N>
```

Spark 上で動くべきもの: cilium, cilium-envoy, trident-node-linux, prometheus-node-exporter.
それ以外の Pod (既存アプリ) が載っていないこと.
trident-node-linux は cilium の準備前に起動すると API (10.96.0.1) に繋がらず 1 回再起動することがある (問題なし).

Longhorn は taint を許容しないため Spark では動かない (意図通り).
GPU Pod の PVC は qnap-iscsi / qnap-smb (trident) を使う.

cilium の確認:
```
kubectl -n kube-system exec <spark<N> 上の cilium pod> -c cilium-agent -- cilium-dbg status --brief    # OK
kubectl -n kube-system exec <spark<N> 上の cilium pod> -c cilium-agent -- cilium-dbg status | grep 'Cluster health'   # 全ノード reachable
```
