# external-dnsのバックエンド用etcdインストール

## 方針
インストール先namespace: external-dns
name: etcd

etcdは公式チャートは存在していないため、bitnami提供のチャートを使用する.
https://etcd.io/docs/v3.5/install/#installation-on-kubernetes-using-a-statefulset-or-helm-chart

ストレージクラスはsynology-csi-deleteで設定. 
冗長化のため, レプリカ数は3とする
パスワード認証はセキュアではないため, tls認証を追加する. この際, corednsに対応するために証明書をsecretにて保管する.
証明書保管用のsecretは, 自作のhelm chartであるetcd-secretにて生成する.

## 設定
- ストレージクラスを明記(synology-csi-delete)
- 外部secretによる証明書利用設定(client, secret)
- replicas 3

## etcd-secretインストール
```
helm install -n external-dns etcd-secret
```

## インストール/アップグレードコマンド
```
helm upgrade -i -n external-dns etcd bitnami/etcd --values values.yaml
```

インストール時の出力結果
```
** Please be patient while the chart is being deployed **

etcd can be accessed via port 2379 on the following DNS name from within your cluster:

    etcd.external-dns.svc.cluster.local

To create a pod that you can use as a etcd client run the following command:

    kubectl run etcd-client --restart='Never' --image docker.io/bitnami/etcd:3.5.14-debian-12-r4 --env ROOT_PASSWORD=$(kubectl get secret --namespace external-dns etcd -o jsonpath="{.data.etcd-root-password}" | base64 -d) --env ETCDCTL_ENDPOINTS="etcd.external-dns.svc.cluster.local:2379" --namespace external-dns --command -- sleep infinity

Then, you can set/get a key using the commands below:

    kubectl exec --namespace external-dns -it etcd-client -- bash
    etcdctl --user root:$ROOT_PASSWORD --cert /bitnami/etcd/data/fixtures/client/cert.pem --key /bitnami/etcd/data/fixtures/client/key.pem put /message Hello
    etcdctl --user root:$ROOT_PASSWORD --cert /bitnami/etcd/data/fixtures/client/cert.pem --key /bitnami/etcd/data/fixtures/client/key.pem get /message

To connect to your etcd server from outside the cluster execute the following commands:

    kubectl port-forward --namespace external-dns svc/etcd 2379:2379 &
    echo "etcd URL: http://127.0.0.1:2379"

 * As rbac is enabled you should add the flag `--user root:$ETCD_ROOT_PASSWORD` to the etcdctl commands. Use the command below to export the password:

    export ETCD_ROOT_PASSWORD=$(kubectl get secret --namespace external-dns etcd -o jsonpath="{.data.etcd-root-password}" | base64 -d)

 * As TLS is enabled you should add the flag `--cert-file /bitnami/etcd/data/fixtures/client/cert.pem --key-file /bitnami/etcd/data/fixtures/client/key.pem` to the etcdctl commands.

 * You should also export a proper etcdctl endpoint using the https schema. Eg.

    export ETCDCTL_ENDPOINTS=https://etcd-0:2379

 * As TLS host authentication is enabled you should add the flag `--ca-file /opt/bitnami/etcd/certs/client/ca.crt` to the etcdctl commands.

WARNING: There are "resources" sections in the chart not set. Using "resourcesPreset" is not recommended for production. For production installations, please set the following values according to your workload needs:
  - disasterRecovery.cronjob.resources
  - resources
+info https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/

```
