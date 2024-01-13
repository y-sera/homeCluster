# prometheusインストールメモ
namespace: prometheus
helm名: prometheus
chart名: prometheus-community/kube-prometheus-stack


## 方針
prometheus, grafana, alertmanagerはとりあえず有効化.
ingress経由で通信する.
一旦とりあえずデプロイし, 細かい設定は後日検討.(Thanosなど)
prometheus用のhelmチャートはいくつかあるが, 
communityで運用されているprometheus-community/kube-prometheus-stackが
最も標準的っぽいのでこれを採用.
本チャートの前身はprometheus-operatorだったが, 現在はkube-prometheus-stackの中に組み込まれている.

## 設定
- prometheus, grafana, alertmanagerのingressを有効化/TLS化



## インストールコマンド
```
helm install -n prometheus prometheus-community/kube-prometheus-stack --values values.yaml
```
