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

### nodeのnameserver
192.168.1.161(nasのdnsを使用)

### 各種スペック
- OS: ubuntu22.04
- cpu:
  - master: 2core
  - worker: 4core
- memory:
  - master: 8G
  - worker: 16G

### その他
ufw: 無効化済み(最小インストールだと元々入っていない)
swap: 無効化済み

#### swap無効化方法
```
swapon -s #swap存在確認
sudo swapoff /swap.img
sudo rm /swap.img
swapon -s #swap存在確認
```

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

ubuntuの公式リポジトリはバージョンが古いため, docker公式リポジトリのcontainerdを使う
ゴミ掃除
```
for pkg in docker.io docker-doc docker-compose docker-compose-v2 podman-docker containerd runc; do sudo apt-get remove $pkg; done
```

リポジトリ登録
```
# Add Docker's official GPG key:
sudo apt-get update
sudo apt-get install ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

# Add the repository to Apt sources:
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
```

containedのインストール(パッケージ名はcontainerd.ioであることに注意)
```
sudo apt install containerd.io
```

カーネルコンフィグモジュール/コンフィグ周りの設定
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

コントロールプレーンのみ
etcd-clientのインストール
```
sudo apt install etcd-client
```


コントロールプレーン1台目
kubeletが参照するdns設定用のパラメータ"--cluster-dns"があるようだが, 有効なパラメータとして動かなかったため指定しない. 
corednsの方でクラスター外向けのDNSへフォワードするように設定する.
https://kubernetes.io/ja/docs/setup/production-environment/tools/kubeadm/kubelet-integration/#kubelet%E3%81%AE%E8%A8%AD%E5%AE%9A%E3%83%91%E3%82%BF%E3%83%BC%E3%83%B3
```
sudo kubeadm init --control-plane-endpoint k8sapiserver.cluster.orenet.net:6443 --skip-phases=addon/kube-proxy --upload-certs
```


コントロールプレーンjoin
```
sudo kubeadm join k8sapiserver.cluster.orenet.net:6443 --token cyog0z.deod4vu663zjz321 \
	--discovery-token-ca-cert-hash sha256:cfed27d38fc34a3fa39dd627373c08cda34ca7128bf21427311b5e7095faa63c \
	--control-plane --certificate-key 19812916cc1d3c30d40ed4402f07e13c4f019068d39c23b38aeef19999c1e479
```

workerノードjoin
```
kubeadm join k8sapiserver.cluster.orenet.net:6443 --token cyog0z.deod4vu663zjz321 \
	--discovery-token-ca-cert-hash sha256:cfed27d38fc34a3fa39dd627373c08cda34ca7128bf21427311b5e7095faa63c
```
token切れの場合は, kubeadm token create --print-command-join
