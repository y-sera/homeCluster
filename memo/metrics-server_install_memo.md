# metrics-serverインストールメモ
namespace: kube-system
name: metrics-server

kubernetes-dashboardやその他chartでも参照されるので、
保守性の観点から、独立してインストールする。

## 設定変更点
変数に以下を追加
```
--kubelet-insecure-tls
```

## インストールコマンド
```
helm install -n kube-system metrics-server metrics-server/metrics-server --values values.yaml
```

