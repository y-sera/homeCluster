# Kubeflow 導入手順 (Kubeflow 26.10.0)

Kubeflow (kubeflow/community-distribution) を ArgoCD で導入する手順.
各 kustomization は upstream を remote base (`https://github.com/kubeflow/community-distribution//<path>?ref=26.10.0`) で参照し,
本環境向けの差分だけをリポジトリに置く.

URL: https://kubeflow.cluster.orenet.net (Keycloak realm `home` でログイン)

| ArgoCD app | パス | 内容 |
|---|---|---|
| kubeflow-istio | `deploy/kubeflow/istio` | Istio 1.31.3 (istio-cni, native sidecar), namespace kubeflow / kubeflow-system (NetworkPolicy 含む) |
| kubeflow-oauth2-proxy | `deploy/kubeflow/oauth2-proxy` | oauth2-proxy 7.15.5. Dex は使わず Keycloak に直結 |
| kubeflow-base | `deploy/kubeflow/base` | ClusterRole, kubeflow-gateway, 証明書, cilium Ingress |
| kubeflow-dashboard | `deploy/kubeflow/dashboard` | Dashboard 2.0.1 (Profile controller, KFAM, PodDefaults webhook) |
| kubeflow-workspaces | `deploy/kubeflow/workspaces` | Workspaces 2.0.0 (Notebooks v2) + WorkspaceKind `jupyterlab` |
| kubeflow-pipelines | `deploy/kubeflow/pipelines` | KFP 2.18.0 (multi-user, MySQL + SeaweedFS, PVC は longhorn) |
| kubeflow-kserve | `deploy/kubeflow/kserve` | KServe 0.21.0 (Standard モード, Knative なし) + KServe UI |
| kubeflow-trainer | `deploy/kubeflow/trainer` | Trainer 2.4.0 + JobSet 0.13.0 |

使わないもの: Dex, Knative, Katib, Model Registry (Hub), Spark Operator, Notebooks v1, TensorBoard (26.10.0 で削除).

## 前提
- cert-manager (kube-system, ClusterIssuer `letsencrypt`), external-dns, Keycloak (realm `home`), Vault + External Secrets (ClusterSecretStore `vault`)
- Cilium: `cni.exclusive=false` (istio-cni が cilium の conflist に chain するため), `socketLB.hostNamespaceOnly=true`
  (`cilium_install_memo.md`). これがないと istio-cni が動かず, sidecar が入った Pod が起動しない
- worker の CPU request に約 2 コアの空き (Kubeflow 全体の request 合計. sidecar は 1 つ 10m)
- argocd CLI でログインしていること (`argocd login argocd.cluster.orenet.net`)

## 1. 認証 (Keycloak + Vault)
Vault に client secret と cookie secret を登録する:
```
vault kv put eso/kubeflow-oidc client-secret="$(openssl rand -hex 32)" cookie-secret="$(openssl rand -hex 16)"
```
Keycloak の client `kubeflow` を作る (Secret より先に client を作ると 1 回エラーになるが, operator が再試行して解消する):
```
kubectl apply -f deploy/keycloak/manifests/oidc/kubeflow-external-secret.yaml
kubectl apply -f deploy/keycloak/manifests/oidc/kubeflow-client.yaml
kubectl -n keycloak get keycloakoidcclient kubeflow -o jsonpath='{.status.conditions}'   # HasErrors: False
```
Kubeflow を使うユーザーは Keycloak で **Email verified を ON** にする (未検証だと oauth2-proxy が拒否する).

issuer (`https://keycloak.cluster.orenet.net/realms/home`) は次の 3 か所で一致させる:
`deploy/kubeflow/oauth2-proxy/oauth2_proxy.cfg`, 同 kustomization の RequestAuthentication `dex-jwt` パッチ, Keycloak.

## 2. ArgoCD への登録と同期
Application は YAML (`applications/kubeflow-*.yaml`) を `kubectl apply` で登録し,
同期は argocd CLI で行う. 自動同期 (`syncPolicy.automated`) は設定していないため,
git に push しただけでは反映されない.

**KFP が CRD `applications.app.k8s.io` を入れるため, 以後 `kubectl get application` は ArgoCD を指さない.**
kubectl で ArgoCD の Application を扱うときは `applications.argoproj.io` と明示する.

### 登録と同期
順番 (前のものが Synced / Healthy になってから次へ):
kubeflow-istio → kubeflow-oauth2-proxy → kubeflow-base → kubeflow-dashboard → kubeflow-workspaces
→ kubeflow-pipelines → kubeflow-kserve → kubeflow-trainer
```
for app in istio oauth2-proxy base dashboard workspaces pipelines kserve trainer; do
  kubectl apply -f applications/kubeflow-${app}.yaml
  argocd app sync kubeflow-${app} --timeout 600 || break
  argocd app wait kubeflow-${app} --sync --health --timeout 600 || break
done
```
- 失敗したら原因を直し, 止まったアプリから再実行する (`for` のリストを途中からにする)
- syncOptions (ServerSideApply など) は各 Application の `spec.syncPolicy.syncOptions` が使われる.
  `--revision` は付けない (Application の `targetRevision` と違うものが適用される)
- `SkipDryRunOnMissingResource`: CRD と CR を同じ同期で入れるため
- kubeflow-kserve は初回, webhook の Pod が起動する前に ClusterServingRuntime を適用して失敗することがある.
  `kubectl -n kserve wait pod --all --for=condition=Ready --timeout=300s` の後, 再度同期する
- 変更を push した後に 1 つだけ反映するとき: `argocd app diff kubeflow-<app>` で差分を確認 → `argocd app sync kubeflow-<app>`

### 本環境での差分
Kubeflow のバージョンを上げるときは, upstream の該当ファイルと比較して作り直す.
- `istio/patches/istio-configmap-oauth2-proxy.yaml`: upstream の mesh 設定のコピーに `headersToDownstreamOnAllow: [set-cookie]` を追加
  (Keycloak のトークン更新後の cookie を保存するため). upstream 更新時は差分を取り直す
- `oauth2-proxy/`: `m2m-dex-only` を元に cfg を Keycloak 用に置換, upstream の固定値 Secret を削除し ExternalSecret を参照
- `base/ciliumnetworkpolicy.yaml`: Kubeflow の NetworkPolicy (istio-system) が cilium Ingress (identity `reserved:ingress`) から
  istio-ingressgateway への通信を遮断するため許可する. ないと 503
- `kserve/patches/inferenceservice-config.yaml`: `defaultDeploymentMode: Standard`, `disableIngressCreation: true`
- Application の ignoreDifferences: webhook / CRD の caBundle, 集約型 ClusterRole の rules, profiles CRD の preserveUnknownFields

## 3. ユーザーの namespace (Profile)
`REGISTRATION_FLOW=false` のため, 初回ログインで namespace は作られない. Profile を手動で作る.
owner の email を含むため本物はコミットしない (`.gitignore`: `deploy/kubeflow/profiles/*.yaml`).
```
cp deploy/kubeflow/profiles/profile.yaml.sample deploy/kubeflow/profiles/kubeflow-ws-<user>.yaml
# metadata.name と spec.owner.name (Keycloak の email) を書き換える
kubectl apply -f deploy/kubeflow/profiles/kubeflow-ws-<user>.yaml
kubectl get ns kubeflow-ws-<user> --show-labels    # istio-injection=enabled, pipelines.kubeflow.org/enabled=true
```

## 4. Workspaces (Notebooks v2)
WorkspaceKind `jupyterlab` (`deploy/kubeflow/workspaces/workspacekind-jupyterlab.yaml`):
- イメージ: `jupyter-scipy:v1.11.1`, `jupyter-pytorch-cuda-full:v1.11.1` (amd64 / arm64, CUDA 13.0)
- PodConfig: `small_cpu`, `medium_cpu` (worker), `spark_gpu` (DGX Spark, GPU 1, 8 CPU, 64Gi)
- activityRules (v1 の culling 相当): GPU は 1 時間, それ以外は 2 時間操作がなければ一時停止. 判定は 10 分ごと
- KFP: Workspace の ServiceAccount に kubeflow-edit を付与し, KFP 用 token を渡すため `kfp.Client()` を引数なしで使える

**GPU (spark_gpu) の Workspace の home ボリュームは StorageClass `qnap-iscsi` にする.**
既定の longhorn は Spark で動かないためマウントできない. Workspace は既存の PVC を名前で参照するだけで,
WorkspaceKind では StorageClass を指定できないため, PVC を作るときに指定する:
```
kubectl -n kubeflow-ws-<user> apply -f - <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: <workspace>-home
spec:
  storageClassName: qnap-iscsi
  accessModes: ["ReadWriteOnce"]
  resources:
    requests:
      storage: 20Gi
EOF
```
qnap-iscsi は reclaimPolicy `Retain` のため, PVC を消しても PV と QNAP の LUN が残る. 不要な場合は別途 PV を削除すること.
Spark 側に QNAP 用のマルチパス設定が必要 (`dgx-spark_join_memo.md`).

## 5. 動作確認
- `https://kubeflow.cluster.orenet.net/` → 「Sign in with Keycloak」→ ログイン後, namespace `kubeflow-ws-<user>` を選べる
- Workspaces / Pipelines / KServe の画面が開く. 4 分以上経っても再ログインを求められない (cookie 更新)
- `/oauth2/start` が Keycloak のログイン画面にリダイレクトされる
  (`/` で `RBAC: access denied` になる場合は `/` の VirtualService (Dashboard) が無い. ext_authz は route が無いと動かない)
- GPU: spark_gpu の Workspace で
  ```
  python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"   # True NVIDIA GB10
  ```
  目安: torch 2.12.0+cu130, bf16 matmul (8192) 約 45 TFLOPS
- KFP: Workspace から簡単な pipeline を `kfp.Client().create_run_from_pipeline_func(...)` で実行し SUCCEEDED になる

## 補足
- Dashboard 右下の build version は `26.03.1` と表示される (upstream 26.10.0 の `applications/pipeline/overlays/kubeflow-application.yaml`
  の値が更新されていない. 表示のみで実害なし. upstream の修正待ち)
- Kubeflow の NetworkPolicy により, istiod は 15012 / 15017 番ポートしか受け付けない (メトリクス 15014 は遮断される)
- upstream を上げるときは, すべての kustomization の `?ref=` を変更し, 上記「本環境での差分」と `oauth2_proxy.cfg` を見直す
