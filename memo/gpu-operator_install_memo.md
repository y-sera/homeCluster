# DGX Spark GPU / RDMA 基盤 導入手順

DGX Spark (GB10, arm64) の GPU と QSFP (RoCE) を k8s から使うための構成.
ノード参加は `dgx-spark_join_memo.md` を先に実施しておく (taint `nvidia.com/gpu=present:NoSchedule`, ラベル `node-role.kubernetes.io/gpu=""`).

| コンポーネント | 方式 | 定義 |
|---|---|---|
| GPU Pod の toleration 自動付与 | apiserver admission `ExtendedResourceToleration` | kubeadm-config + 各 master の static pod manifest |
| NVIDIA GPU Operator v26.7.2 | ArgoCD (helm) | `applications/gpu-operator.yaml`, `deploy/gpu-operator/values/values.yaml` |
| Grafana ダッシュボード | kubectl apply -k | `deploy/gpu-operator/dashboards/` |
| RDMA shared device plugin v1.5.4 | ArgoCD (repo) | `applications/rdma-shared-device-plugin.yaml`, `deploy/rdma-shared-device-plugin/manifests/` |
| MPI Operator v0.8.2 | ArgoCD (upstream) | `applications/mpi-operator.yaml` |
| nccl-tests | 自前イメージ + MPIJob | `deploy/llm-system/images/nccl-tests/`, `deploy/llm-system/manifests/nccl-tests-mpijob.yaml` |

GPU は device-plugin で `nvidia.com/gpu` (1 ノード 1, 共有なし) として公開する (DRA は使わない.
DRA にすると ExtendedResourceToleration による toleration 自動付与が効かない).

## 1. apiserver: ExtendedResourceToleration

`nvidia.com/gpu` を要求する Pod に toleration `nvidia.com/gpu Exists NoSchedule` を自動で付ける.
GPU Pod 側で toleration を書かなくてよくなり, GPU を要求しない Pod は taint で Spark に載らない.

### kubeadm-config (コントロールプレーン再参加・アップグレード時に引き継ぐため)
```
kubectl -n kube-system edit cm kubeadm-config
```
ClusterConfiguration に以下があること:
```
apiServer:
  extraArgs:
  - name: enable-admission-plugins
    value: NodeRestriction,ExtendedResourceToleration
```

### 各 master の static pod manifest (1 台ずつ, root)
```
cp /etc/kubernetes/manifests/kube-apiserver.yaml /root/kube-apiserver.yaml.bak-$(date +%Y%m%d)
vi /etc/kubernetes/manifests/kube-apiserver.yaml
#   - --enable-admission-plugins=NodeRestriction,ExtendedResourceToleration
```
保存すると kubelet が apiserver を再起動する. 1〜2 分 connection refused になり, そのノード自身も一時的に NotReady になる
(コントロールプレーンの kubelet はローカルの apiserver を見ているため). Ready に戻ってから次の master を行う.
バックアップは manifests ディレクトリに置かない (static pod として読まれる).

### 確認
```
cat <<EOF | kubectl create --dry-run=server -o jsonpath='{.spec.tolerations}' -f -
apiVersion: v1
kind: Pod
metadata: {name: tol-test, namespace: default}
spec:
  containers:
  - name: c
    image: busybox
    resources: {limits: {nvidia.com/gpu: 1}}
EOF
# {"effect":"NoSchedule","key":"nvidia.com/gpu","operator":"Exists"} が含まれること
```

## 2. NVIDIA GPU Operator

- driver / toolkit は DGX OS 付属のものを使うため無効 (`driver.enabled=false`, `toolkit.enabled=false`)
- CDI 有効. CDI spec はホストの `nvidia-cdi-refresh.service` が `/var/run/cdi/nvidia.yaml` に生成する
- device-plugin, GFD, DCGM exporter (ServiceMonitor `release: prometheus`) を有効, MIG manager は無効 (GB10 は MIG 非対応)
- namespace gpu-operator は PSA privileged (Application の `managedNamespaceMetadata` で付与)

```
kubectl apply -f applications/gpu-operator.yaml
```
ArgoCD は自動同期なし. UI から Sync するか, 以下で同期する:
```
kubectl -n argocd patch application gpu-operator --type merge \
  -p '{"operation":{"initiatedBy":{"username":"kubectl"},"sync":{"syncOptions":["ServerSideApply=true"]}}}'
```
**`sync.revision` は指定しない.** 指定するとその revision が Application の targetRevision より優先される
(例: `HEAD` を指定すると mpi-operator が v0.8.2 ではなく upstream master で適用される).

### 確認
```
kubectl get clusterpolicy                       # STATUS ready
kubectl -n gpu-operator get pods -o wide
kubectl get node -l node-role.kubernetes.io/gpu \
  -o custom-columns=NAME:.metadata.name,GPU:.status.allocatable.nvidia\\.com/gpu,PRODUCT:.metadata.labels.nvidia\\.com/gpu\\.product
# GPU=1, PRODUCT=NVIDIA-GB10 (compute capability 12.1)
```
RuntimeClass `nvidia` / `nvidia-cdi` / `nvidia-legacy` が作られるが, containerd に handler があるのは `nvidia` のみ.
GPU Pod は runtimeClassName なしで動く (CDI で GPU が注入される).

動作確認用 Pod (toleration なしで Spark に載ること):
```
cat <<EOF | kubectl create -f -
apiVersion: v1
kind: Pod
metadata: {name: gpu-test, namespace: default}
spec:
  restartPolicy: Never
  containers:
  - name: c
    image: nvcr.io/nvidia/k8s/cuda-sample:vectoradd-cuda12.5.0-ubuntu22.04
    resources: {limits: {nvidia.com/gpu: 1}}
EOF
kubectl logs gpu-test        # Test PASSED
kubectl delete pod gpu-test
```

## 3. ダッシュボード

```
kubectl apply -k deploy/gpu-operator/dashboards --server-side
```
Grafana (https://grafana.cluster.orenet.net) の uid `dgx-spark-gpu`.

GB10 で DCGM exporter が出す指標は `DCGM_FI_DEV_*` の 11 個のみ:
- `DCGM_FI_DEV_FB_*` は出ない (ユニファイドメモリ. `nvidia-smi` の memory.total も N/A)
  → メモリは node-exporter の `MemTotal - MemAvailable` (`node_uname_info` で nodename と結合)
- `DCGM_FI_PROF_*` は出ない (exporter ログで "metric not enabled")
- GPU の割り当て状況は kube-state-metrics (`resource="nvidia_com_gpu"`)

このため NVIDIA 公式ダッシュボード (Grafana 12239) ではなく自作 JSON を使う.

## 4. RDMA shared device plugin

QSFP の RDMA デバイスを `rdma/qsfp` として Pod に渡す. hostNetwork / privileged なしで Pod から RoCE を使える.
- ホストの RDMA subsystem は netns shared モード (DGX OS 既定. `rdma system` で `netns shared` を確認) のため,
  Pod の netns からでも QSFP の GID が見える
- 対象 NIC は `ifNames` で QSFP rail0/rail1 (`enp1s0f0np0`, `enP2p1s0f0np0`) のみ. 未使用の f1 ポートは含めない
- `rdmaHcaMax: 16` = 1 ノードで `rdma/qsfp` を同時に使える Pod 数
- DaemonSet は `node-role.kubernetes.io/gpu` ノードのみ (toleration 付き)

```
kubectl apply -f applications/rdma-shared-device-plugin.yaml   # 以後 Sync は 2. と同じ
kubectl get node -l node-role.kubernetes.io/gpu -o custom-columns=NAME:.metadata.name,RDMA:.status.allocatable.rdma/qsfp
# RDMA=16
```

Spark を追加して NIC 名が異なる場合は `deploy/rdma-shared-device-plugin/manifests/configmap.yaml` の `ifNames` に追加する.

RDMA を使う Pod の要件:
- `resources.limits: rdma/qsfp: 1`
- `securityContext.capabilities.add: ["IPC_LOCK"]` (メモリ pin)
- NCCL 環境変数: `NCCL_IB_HCA==rocep1s0f0:1,roceP2p1s0f0:1`, `NCCL_IB_GID_INDEX=3` (RoCEv2 IPv4), `NCCL_SOCKET_IFNAME=eth0`
- `/dev/shm` を emptyDir (medium: Memory) で拡張

Cilium は QSFP NIC もデバイスとして検出するが, RoCE はカーネルのネットワークスタックを通らないため影響なし (設定変更不要).

## 5. MPI Operator

nccl-tests (MPIJob v2beta1) 用. Kubeflow Trainer v2 導入後に不要なら削除する.
```
kubectl apply -f applications/mpi-operator.yaml   # 以後 Sync は 2. と同じ (revision を指定しない)
kubectl -n mpi-operator get pods
```

## 6. nccl-tests (2 ノード RDMA 検証)

### イメージ
`harbor.cluster.orenet.net/llm-system/nccl-tests:v2.21.1-cuda13.0.3` (arm64 のみ).
- CUDA 13.0 (ホスト driver 580 に合わせる), NCCL 2.28.3 (ベースイメージ同梱), sm_121 向けビルド
- OpenMPI, rdma-core, perftest 入り. sshd は port 2222 (MPI Operator 公式 base イメージと同じ)

作り直す場合 (作業端末, buildx + qemu):
```
docker login harbor.cluster.orenet.net
docker buildx build --platform linux/arm64 \
  -t harbor.cluster.orenet.net/llm-system/nccl-tests:v2.21.1-cuda13.0.3 \
  --push deploy/llm-system/images/nccl-tests
```

### ホスト間の基準値 (Spark 上, 一般ユーザで可)
2 レール同時 (レールごとに別ポート. 1 レールだけ測る場合は片方のみ実行):
```
# spark2 (server)
ib_write_bw -d rocep1s0f0   -p 18515 -R -F --report_gbits -D 8 -s 1048576 -q 4 &
ib_write_bw -d roceP2p1s0f0 -p 18516 -R -F --report_gbits -D 8 -s 1048576 -q 4 &
# spark1 (client)
ib_write_bw -d rocep1s0f0   -p 18515 -R -F --report_gbits -D 8 -s 1048576 -q 4 192.168.10.11 &
ib_write_bw -d roceP2p1s0f0 -p 18516 -R -F --report_gbits -D 8 -s 1048576 -q 4 192.168.11.11
```
目安: 1 レール 約 112 Gb/s, 2 レール同時 約 98 + 98 ≈ 196 Gb/s.

### 実行
```
kubectl create -f deploy/llm-system/manifests/nccl-tests-mpijob.yaml
kubectl -n llm-systems logs -f -l training.kubeflow.org/job-name=nccl-tests,training.kubeflow.org/job-role=launcher
kubectl -n llm-systems delete mpijob nccl-tests
```
- worker は各 Spark に 1 つ (anti-affinity), GPU 1 + `rdma/qsfp` 1
- launcher は GPU を使わないが, イメージが arm64 のみのため toleration + nodeSelector を明示して Spark に載せる

### 期待値
- ログに `NET/IB : Using [0]rocep1s0f0:1/RoCE [1]roceP2p1s0f0:1/RoCE` (NET/Socket になっていないこと)
- `GPU Direct RDMA Disabled` と出るが問題ない (GB10 はユニファイドメモリ)
- `Out of bounds values : 0 OK`
- all_reduce_perf の busbw: 8 MiB 以上で約 20 GB/s, 1 GiB 以上で **約 23〜24 GB/s (≈ 194 Gb/s)** = ホスト間の基準値とほぼ同じ
