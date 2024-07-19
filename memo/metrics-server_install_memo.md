# metrics-serverインストールメモ
namespace: kube-system
name: metrics-server

kubernetes-dashboardやその他chartでも参照されるので, 
管理を楽にするために個別のchartでインストールする.

## 設定変更点
なし

## その他注意点
--kubelet-insecure-tlsオプションを使用しないために, kubeletのサーバ証明書をCA署名する.
参考リンク
https://github.com/kubernetes-sigs/metrics-server/issues/576#issuecomment-1820504816


## インストールコマンド
```
helm upgrade -i -n kube-system metrics-server metrics-server/metrics-server --values values.yaml
```

