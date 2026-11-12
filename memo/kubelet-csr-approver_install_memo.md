# kubelet-csr-approver インストールメモ

## 背景
全ノードの kubelet は `serverTLSBootstrap: true` で動いており, kubelet の serving 証明書 (10250 番ポート) を
`kubernetes.io/kubelet-serving` の CSR で要求する. この CSR は kube-controller-manager が自動承認しないため,
承認されないと以下が失敗する.

- `kubectl logs` / `exec` (`remote error: tls: internal error`)
- metrics-server (`kubectl top node` が `nodemetrics ... not found`)

2026-11 に qnap-vm を追加した際, CSR が Pending のまま 6 日間放置され上記の状態になっていた (手動承認で解消).
証明書は約 1 年ごとにローテーションされ, そのたびに承認が必要になるため kubelet-csr-approver で自動化する.

## 構成
```
applications/kubelet-csr-approver.yaml          # ArgoCD Application (chart 1.2.15, ns: kube-system)
deploy/kubelet-csr-approver/values/values.yaml  # values
```

承認条件 (values.yaml):
- `providerRegex: ^(master[0-9]+|worker[0-9]+|qnap-vm)$` : ノード名 (DNS SAN)
- `providerIpPrefixes: [192.168.1.0/24]` : IP SAN
- `bypassDnsResolution: true` : ノードのショート名はクラスタ内 DNS で解決できないため無効化
- `skipDenyStep: true` : 条件外の CSR は deny せず無視 (手動承認できるように残す)

**ノードを追加するときは, 命名規則に合わなければ providerRegex を更新すること.**

## インストール
```bash
kubectl apply -f applications/kubelet-csr-approver.yaml
```

## 確認
```bash
kubectl -n kube-system get pods -l app.kubernetes.io/name=kubelet-csr-approver
kubectl -n kube-system logs deploy/kubelet-csr-approver
# Pending の kubelet-serving CSR が残っていないこと
kubectl get csr | grep Pending
```

## 手動承認 (approver が使えない場合)
```bash
kubectl get csr --no-headers | awk '$4=="system:node:<node>" && $NF=="Pending"{print $1}' | xargs kubectl certificate approve
```
