# TODO

## Kubeflow (DGX Spark)
DGX Spark (GPU ノード 2 台) と Kubeflow 26.10.0 の導入後に残っている作業.
Kubeflow は機械学習を継続的に行う仕組み (学習 → パイプライン → 公開) として使う.

### 機械学習を 1 サイクル回す
- [ ] 回したい学習 (データ, モデルの規模, 更新頻度) を決める
- [ ] Trainer (TrainJob) で学習する. GPU は Spark 2 台で計 2 つ. 2 台にまたがる場合は NCCL を QSFP 経由で使う
      (`deploy/llm-system/manifests/nccl-tests-mpijob.yaml` の設定 (`rdma/qsfp`, `IPC_LOCK`, `NCCL_IB_HCA` など) を参考にする.
      NCCL を使う Pod は Istio sidecar を入れない)
- [ ] KFP でパイプライン化する (データ準備 → 学習 → 評価 → 公開). Workspace から `kfp.Client()` で実行できる
- [ ] KServe (Standard モード) で公開する. 外部公開の方法 (cilium Ingress など) を決める
- [ ] 学習結果の保存先 (PVC / オブジェクトストレージ) を決める

### 監視とメモの整理
- [ ] Kubeflow の監視. istiod のメトリクス (15014) は Kubeflow の NetworkPolicy で遮断されているため, scrape するなら NetworkPolicy を追加する
- [ ] GPU の使用状況のダッシュボード (Grafana `dgx-spark-gpu`) の見直し (Kubeflow のジョブとの対応など)
- [ ] メモの整理

### 任意
- [ ] Kueue: GPU (計 2 つ) を学習ジョブ / GPU Workspace / 推論で取り合わないよう割り当てを調整する
- [ ] Katib (ハイパーパラメータ探索)
- [ ] Model Registry (Hub)
- [ ] GPU 用 WorkspaceKind の改良

### 保留: 普段使いの LLM (旧 Phase 3)
使うモデルの候補が決まってから再開する.
- 目的: Spark 2 台 (128GB × 2) にまたがる 1 つの LLM を QSFP 経由で動かし, API として使う
- 想定構成: LWS + vLLM (TensorFold は任意), namespace `llm-systems` (Istio mesh 外), hostNetwork なし
  (`rdma/qsfp` + `IPC_LOCK` + NCCL の環境変数. nccl-tests で hostNetwork なしでもホストと同等の帯域を確認済み),
  leader spark1 / worker spark2, replicas 0 ⇄ 1 で使うときだけ起動
- モデル: llm-models の PVC を元に, 使うモデルを Spark のローカル NVMe にコピーする
- GPU を 2 つとも使うため, 起動中は Kubeflow の学習や GPU Workspace が動かせない (Kueue で調整する場合は上記)

## その他
### prometheus の OutOfSync
- [x] Grafana の admin パスワードを `admin.existingSecret` (Vault + ExternalSecret) に移す.
      `adminPassword` 未設定のため描画のたびにランダムなパスワードが作られ, Secret / Deployment が常に OutOfSync になっている.
      このまま同期すると admin パスワードが変わるので注意

### prometheus のデータの永続化
現状は Prometheus / Alertmanager / Grafana のどれも PVC がなく (emptyDir), Pod が作り直されるとデータが消える.
- [ ] Prometheus: `prometheus.prometheusSpec.storageSpec` で PVC を使う (現在 retention 10d). StorageClass と容量を決める
- [ ] Alertmanager: `alertmanager.alertmanagerSpec.storage` で PVC を使う (silence などの状態が消えないように)
- [ ] Grafana: `grafana.persistence` で PVC を使う (UI で作ったダッシュボードや設定が消えないように).
      ダッシュボードを ConfigMap (sidecar) で管理する方針なら不要

### ArgoCD
- [ ] 3.6 へ上げるとき: `argocd-cmd-params-cm` の `server.repo.server.strict.tls` (3.5 で非推奨) を見直す
