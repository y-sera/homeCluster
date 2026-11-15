# DGX Spark ノード参加メモ (spark1 / spark2)

DGX Spark 2台を GPU 専用ワーカーとして既存クラスタ (kubeadm, v1.35.4, Cilium) に参加させる手順.
GPU を使わない Pod が載らないよう, 参加時点で taint `nvidia.com/gpu=present:NoSchedule` を付ける.

## 前提

| | spark1 | spark2 |
|---|---|---|
| LAN (enP7s7, MTU 1500) | 192.168.1.98/24 | 192.168.1.99/24 |
| QSFP rail0 (enp1s0f0np0, MTU 9000) | 192.168.10.10/24 | 192.168.10.11/24 |
| QSFP rail1 (enP2p1s0f0np0, MTU 9000) | 192.168.11.10/24 | 192.168.11.11/24 |
| RoCE HCA | rocep1s0f0, roceP2p1s0f0 | 同左 |

- OS: DGX OS (Ubuntu 24.04.5, aarch64), NVIDIA driver 580.178.04, nvidia-container-toolkit 1.20.1 (プリインストール, 入れ替えない)
- containerd.io 2.2.5 (Docker リポジトリ版. 既定で CRI 無効)
- QSFP は NCCL 専用. k8s のノード IP は LAN 側 (kubelet `node-ip` で固定)
- 以降のコマンドはすべて root (`sudo -i`) で, 1台ずつ実施する

## 0. 事前確認

### ディスク空き容量 (必須)
kubelet の eviction 既定値は `nodefs.available<10%`, `imagefs.available<15%`.
ルートディスク (3.7T) の空きが 15% (約 560G) を切っていると DiskPressure になり Pod が退避される.
2026-11-14 時点で 95-97% 使用 (`~/.cache/huggingface` が 2.8-3.3T) のため, 参加前に空ける.

```
df -h /
du -xsh ~yasuaki/.cache/huggingface/hub/* | sort -h
```

### DNS
spark2 は 192.168.2.64 (クラスタ内 external-dns の CoreDNS) を参照している.
他ノードと揃えて 192.168.1.161 (Synology) にする (netplan の enP7s7 の nameservers).

```
resolvectl dns
getent hosts k8sapiserver.cluster.orenet.net   # 192.168.1.2 (kube-vip VIP) が返ること
```

## 1. OS 設定

### swap 無効化
```
swapon --show
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

## 2. containerd (CRI 有効化)

Docker 用の既定設定 (`disabled_plugins = ["cri"]`) を, k8s 用の既定設定で置き換える.
Docker もこの containerd を使うが, namespace が分かれている (moby / k8s.io) ので共存できる.

```
cp -a /etc/containerd/config.toml /etc/containerd/config.toml.bak
containerd config default > /etc/containerd/config.toml
```

`/etc/containerd/config.toml` を編集:
- runc の options に `SystemdCgroup = true` (containerd 2.x の既定設定では
  `[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.runc.options]` 配下)
- 先頭付近の `imports` に `"/etc/containerd/conf.d/*.toml"` を追加 (nvidia-ctk のドロップイン用)

NVIDIA ランタイムを登録 (既定ランタイムにはしない. GPU Operator 導入時に RuntimeClass/CDI で使い分ける):
```
nvidia-ctk runtime configure --runtime=containerd
systemctl restart containerd
crictl --runtime-endpoint unix:///run/containerd/containerd.sock info | grep -E '"SystemdCgroup"|nvidia' | head
systemctl restart docker   # docker 利用を続ける場合
```

## 3. kubeadm / kubelet / kubectl (v1.35.4, arm64)

```
curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.35/deb/Release.key | gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.35/deb/ /' > /etc/apt/sources.list.d/kubernetes.list
apt-get update
apt-get install -y kubelet=1.35.4-1.1 kubeadm=1.35.4-1.1 kubectl=1.35.4-1.1
apt-mark hold kubelet kubeadm kubectl
```

## 4. join

master1 で join 情報を発行:
```
kubeadm token create --print-join-command
```

各 Spark に `/root/join.yaml` を作成 (spark2 は name と node-ip を置き換え):
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
  name: spark1
  criSocket: unix:///run/containerd/containerd.sock
  kubeletExtraArgs:
    - name: node-ip
      value: 192.168.1.98
  taints:
    - key: nvidia.com/gpu
      value: present
      effect: NoSchedule
```

```
kubeadm join --config /root/join.yaml
```

## 5. 参加後 (作業端末から)

```
kubectl label node spark1 node-role.kubernetes.io/gpu=""
kubectl get node spark1 -o wide            # InternalIP 192.168.1.98, arm64, taint 付き
kubectl get csr | grep spark1              # kubelet-serving が Approved,Issued (kubelet-csr-approver)
kubectl get pods -A -o wide --field-selector spec.nodeName=spark1
```

spark1 上で動くべきもの: cilium, cilium-envoy, trident-node-linux, prometheus-node-exporter.
それ以外の Pod (既存アプリ) が載っていないこと.

Longhorn は taint を許容しないため Spark では動かない (意図通り).
GPU Pod の PVC は qnap-iscsi / qnap-smb (trident) を使う.

確認:
```
kubectl logs -n kube-system <spark1 上の cilium pod> | grep -i error
kubectl exec -n kube-system <spark1 上の cilium pod> -- cilium-dbg status --brief
```

## 6. 2台目

spark2 で 1〜5 を繰り返す.
