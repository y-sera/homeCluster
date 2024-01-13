# external-dnsのバックエンド用etcdインストール

## 方針
インストール先namespace: external-dns-backend
name: external-dns-backend-etcd

etcdは公式チャートは存在していないため、bitnami提供のチャートを使用する.
https://etcd.io/docs/v3.5/install/#installation-on-kubernetes-using-a-statefulset-or-helm-chart

1podしか立てずnode依存してしまうため, ストレージクラスは一旦synology-csi-deleteで設定. 
冗長性も含め詳細は後日検討.

## 設定
- ストレージクラスを明記(synology-csi-delete)

## インストール用コマンド
```
helm install -n external-dns-backend external-dns-backend-etcd bitnami/etcd --values values.yaml
```

インストール時の出力結果
```
** Please be patient while the chart is being deployed **

etcd can be accessed via port 2379 on the following DNS name from within your cluster:

    external-dns-backend-etcd.external-dns-backend.svc.cluster.local

To create a pod that you can use as a etcd client run the following command:

    kubectl run external-dns-backend-etcd-client --restart='Never' --image docker.io/bitnami/etcd:3.5.11-debian-11-r2 --env ROOT_PASSWORD=$(kubectl get secret --namespace external-dns-backend external-dns-backend-etcd -o jsonpath="{.data.etcd-root-password}" | base64 -d) --env ETCDCTL_ENDPOINTS="external-dns-backend-etcd.external-dns-backend.svc.cluster.local:2379" --namespace external-dns-backend --command -- sleep infinity

Then, you can set/get a key using the commands below:

    kubectl exec --namespace external-dns-backend -it external-dns-backend-etcd-client -- bash
    etcdctl --user root:$ROOT_PASSWORD put /message Hello
    etcdctl --user root:$ROOT_PASSWORD get /message

To connect to your etcd server from outside the cluster execute the following commands:

    kubectl port-forward --namespace external-dns-backend svc/external-dns-backend-etcd 2379:2379 &
    echo "etcd URL: http://127.0.0.1:2379"

 * As rbac is enabled you should add the flag `--user root:$ETCD_ROOT_PASSWORD` to the etcdctl commands. Use the command below to export the password:

    export ETCD_ROOT_PASSWORD=$(kubectl get secret --namespace external-dns-backend external-dns-backend-etcd -o jsonpath="{.data.etcd-root-password}" | base64 -d)
```
