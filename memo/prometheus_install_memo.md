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
- grafana の admin ユーザーとパスワードは Vault の固定値を ESO で渡す (`grafana.admin.existingSecret: grafana-admin`).
  未指定だと chart がパスワードをランダムに生成する. 既存の Secret を再利用する処理 (`lookup`) は ArgoCD の描画では効かないため,
  描画のたびに値が変わり, Secret `prometheus-grafana` と Deployment の `checksum/secret` が常に OutOfSync になる.
  grafana のデータは emptyDir のため, そのまま同期すると admin パスワードが変わる.



## インストールコマンド
grafana の admin 用 Secret を先に作る:
```
vault kv put eso/grafana-admin admin-user=admin admin-password=<パスワード>
kubectl apply -f deploy/prometheus/manifests/grafana-admin-external-secret.yaml
kubectl -n prometheus get externalsecret grafana-admin    # STATUS SecretSynced
```
```
helm install -n prometheus prometheus-community/kube-prometheus-stack --values values.yaml
```
