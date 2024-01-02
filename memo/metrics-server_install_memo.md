# metrics-serverインストールメモ
namespace: kube-system
name: metrics-server

kubernetes-dashboardやその他chartでも参照されるので、
保守性の観点から、独立してインストールする。

## 設定変更点
helm Chart パラメータ説明
https://github.com/kubernetes-sigs/metrics-server/tree/master/charts/metrics-server


変数に以下を追加
```
--kubelet-insecure-tls
```

認証無しで/metricsにアクセス出来る.(なんとなくtrueに.多分いらない.)
```
metrics:
  enabled: true
```

## インストールコマンド
```
helm install -n kube-system metrics-server metrics-server/metrics-server --values values.yaml
```

