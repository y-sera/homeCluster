# kube-vip導入メモ
## 方針
ciliumにてapiserverのHAがサポートされるまではkube-vipを利用する.
kube-vipで利用するのはあくまでapiserverの冗長化のみ
BGPはciliumと競合するのでarpを用いる.
kube-vipはstatic podとdaemonsetが存在するが, k8sの標準的なリソースであるdaemonsetを利用する.

## 設定方法
https://kube-vip.io/docs/installation/daemonset/
上記URLに従いマニフェストを生成しapplyする.
kube-vip.shにコマンドをまとめてある.

```
source kube-vip.sh
```

