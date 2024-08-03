# external-dnsのバックエンド用etcdインストール

## 方針
インストール先namespace: external-dns
name: etcd

etcdは公式チャートは存在していないため、bitnami提供のチャートを使用する.
https://etcd.io/docs/v3.5/install/#installation-on-kubernetes-using-a-statefulset-or-helm-chart

ストレージクラスはsynology-csi-deleteで設定. 
いずれは冗長化/tls化を目指したいが, 正常に稼働しないため暫定的に1pod, 非tlsとする.
https://github.com/bitnami/charts/issues/26398
https://github.com/bitnami/charts/issues/27872
https://github.com/bitnami/charts/issues/27109
TLSを有効化した場合, liveness, readness probeが壊れているらしい. また, レプリカ数を増やした場合も同様. 
このためしばらく様子見.(2024/8/4)

## 設定
- ストレージクラスを明記(synology-csi-delete)

## 備考
TLS認証用に, etcd-secretというhelmチャートを作成.
tls通信の証明書を作成する際, secretに埋め込みmountする.
サーバ証明書, ピアリング用証明書, クライアント証明書を作成する.

以下を参考に作成する.
https://etcd.io/docs/v3.2/op-guide/security/
https://etcd.io/docs/v3.2/op-guide/configuration/

## インストール/アップグレードコマンド
```
helm upgrade -i -n external-dns etcd bitnami/etcd --values values.yaml
```

### インストール時の出力結果
```
Release "etcd" does not exist. Installing it now.
NAME: etcd
LAST DEPLOYED: Sun Aug  4 03:46:47 2024
NAMESPACE: external-dns
STATUS: deployed
REVISION: 1
TEST SUITE: None
NOTES:
CHART NAME: etcd
CHART VERSION: 10.2.11
APP VERSION: 3.5.15

** Please be patient while the chart is being deployed **

etcd can be accessed via port 2379 on the following DNS name from within your cluster:

    etcd.external-dns.svc.cluster.local

To create a pod that you can use as a etcd client run the following command:

    kubectl run etcd-client --restart='Never' --image docker.io/bitnami/etcd:3.5.15-debian-12-r5 --env ETCDCTL_ENDPOINTS="etcd.external-dns.svc.cluster.local:2379" --namespace external-dns --command -- sleep infinity

Then, you can set/get a key using the commands below:

    kubectl exec --namespace external-dns -it etcd-client -- bash
    etcdctl  put /message Hello
    etcdctl  get /message

To connect to your etcd server from outside the cluster execute the following commands:

    kubectl port-forward --namespace external-dns svc/etcd 2379:2379 &
    echo "etcd URL: http://127.0.0.1:2379"

WARNING: There are "resources" sections in the chart not set. Using "resourcesPreset" is not recommended for production. For production installations, please set the following values according to your workload needs:
  - disasterRecovery.cronjob.resources
  - resources
+info https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/

```
