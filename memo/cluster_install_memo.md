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
sudo kubeadm init --control-plane-endpoint k8sapiserver.cluster.orenet.net:6443 --skip-phases=addon/kube-proxy --upload-certs
```


コントロールプレーンjoin
```
sudo  kubeadm join k8sapiserver.cluster.orenet.net:6443 --token sbesef.ke8kbt0jo72zqw60 \
	--discovery-token-ca-cert-hash sha256:d48db821676017be4a9df5ea5e88ee68ab38b68b168d53e18be7dc1dbd7a04cc \
	--control-plane --certificate-key c671fe05cab01374a166b0622ab52ad3b47e425b352884a991015ac98ada46e6
```

workerノードjoin
```
sudo kubeadm join k8sapiserver.cluster.orenet.net:6443 --token sbesef.ke8kbt0jo72zqw60 \
	--discovery-token-ca-cert-hash sha256:d48db821676017be4a9df5ea5e88ee68ab38b68b168d53e18be7dc1dbd7a04cc 
```

