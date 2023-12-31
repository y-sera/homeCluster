# ciilum instlalのメモ

## インストール方針
helmを利用してインストール
```
helm install -n kube-system cilium cilium/cilium --values values.yaml
```

values.yamlは, cilium-cliより生成されたものをベースに書き換え
cilium-cli自体は、バイナリインストールとする.

参考
https://github.com/cilium/cilium-cli
```
cilium install --dry-run-helm-values
```

## 変更したもの
```
bpf:
  masquerade: true
operator:
  replicas: 2
```

コントロールプレーンのIPはいずれはvipに集約か, BGP化する.

