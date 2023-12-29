# クラスター導入メモ(2023/12/29)
kubeadmによるインストールメモ

## 前提
### ノードのIP
- master1: 192.168.1.32
- master2: 192.168.1.33
- master3: 192.168.1.34
- worker1: 192.168.1.48
- worker2: 192.168.1.49
- worker3: 192.168.1.50

### 各種スペック
- OS: ubuntu22.04
- cpu:
  - master: 2core
  - worker: 4core
- memory:
  - master: 8G
  - worker: 16G

### その他
ufw: 無効化済み
swap: 無効化済み

## 初期設定
containerdインストール
```
sudo apt install containerd
```

kubectl, kubeadm, kubeletインストール
```
curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.29/deb/Release.key | sudo gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
# This overwrites any existing configuration in /etc/apt/sources.list.d/kubernetes.list
echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.29/deb/ /' | sudo tee /etc/apt/sources.list.d/kubernetes.list

sudo apt-get update
sudo apt-get install -y kubelet kubeadm kubectl
sudo apt-mark hold kubelet kubeadm kubectl
```

コンテナランタイムの設定周り
https://kubernetes.io/docs/setup/production-environment/container-runtimes/
```
cat <<EOF | sudo tee /etc/modules-load.d/k8s.conf
overlay
br_netfilter
EOF

sudo modprobe overlay
sudo modprobe br_netfilter

# sysctl params required by setup, params persist across reboots
cat <<EOF | sudo tee /etc/sysctl.d/k8s.conf
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF

# Apply sysctl params without reboot
sudo sysctl --system
```

コントロールプレーン1台目
```
sudo kubeadm init --control-plane-endpoint 192.168.1.32:6443 --skip-phases=addon/kube-proxy
```


コントロールプレーンjoin
```
sudo kubeadm join 192.168.1.32:6443 --token wld9hi.eeuf88oakuufa2mk \
	--discovery-token-ca-cert-hash sha256:f9b01b88b87c787b136c27841bf584621d0f707d87f7936d8b61bccf49f133d6 \
	--control-plane --certificate-key a4dbd5d901ae0d0c2ab89f9c36a0586aff8546fc52c99d8829503446e0ba9e02
```

workerノードjoin
```
sudo kubeadm join 192.168.1.32:6443 --token wld9hi.eeuf88oakuufa2mk \
	--discovery-token-ca-cert-hash sha256:f9b01b88b87c787b136c27841bf584621d0f707d87f7936d8b61bccf49f133d6 \
```
