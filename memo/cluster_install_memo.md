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
192.168.1.1 (EdgeRouter X の dnsmasq. `k8sapiserver.cluster.orenet.net` は自身で応答し, `cluster.orenet.net` は 192.168.2.64 へ転送)
ER-X の DNS 設定: memo/edgerouter-x_dns_memo.md

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

全ノード共通の設定 (2026-11-24 の worker3 NotReady の再発防止. 2026-11-26 に全 8 ノードへ適用済み)
- inotify の上限: 既定の max_user_instances 128 では, containerd を再起動したときに CRI プラグインが
  起動できず (inotify の確保に失敗), kubelet が CRI に接続できずノードが NotReady になる
- needrestart: apt でライブラリを更新すると containerd / kubelet を自動で再起動するため, 対象から外す
  (更新後の再起動は, ノードを drain してから手作業で行う)
- hold: apt upgrade のついでに Kubernetes / containerd が上がらないようにする.
  バージョンアップのときは `sudo apt-mark unhold <パッケージ>` で外してから上げ, 終わったら hold し直す
```
sudo bash <<'SCRIPT'
cat <<'EOF' > /etc/sysctl.d/99-kubernetes-inotify.conf
fs.inotify.max_user_instances = 8192
fs.inotify.max_user_watches = 524288
EOF
sysctl --system

cat <<'EOF' > /etc/needrestart/conf.d/90-kubernetes.conf
$nrconf{override_rc}{qr(^containerd\.service$)} = 0;
$nrconf{override_rc}{qr(^kubelet\.service$)} = 0;
EOF
for p in kubeadm kubelet kubectl containerd.io; do
  dpkg -s "$p" >/dev/null 2>&1 && apt-mark hold "$p"
done
SCRIPT

# 確認
sysctl fs.inotify.max_user_instances fs.inotify.max_user_watches
apt-mark showhold
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
