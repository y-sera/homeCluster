# tektonインストールメモ

## 方針
productionとしての活用はoperatorを使ってくれとのことらしいので、operatorがオペレータと一緒にインストールする.
https://tekton.dev/docs/installation/pipelines/
https://github.com/tektoncd/operator/blob/main/docs/install.md
とりあえずallでインストールする.

## 


## インストールコマンド
```
kubectl apply -f https://storage.googleapis.com/tekton-releases/operator/latest/release.yaml
kubectl apply -f https://raw.githubusercontent.com/tektoncd/operator/main/config/crs/kubernetes/config/all/operator_v1alpha1_config_cr.yaml
```

2つ目のコマンドで以下warningが表示されるが、ひとまずリソースは作成されているので良しとする。
```
arning: resource tektonconfigs/config is missing the kubectl.kubernetes.io/last-applied-configuration annotation which is required by kubectl apply. kubectl apply should only be used on resources created declaratively by either kubectl create --save-config or kubectl apply. The missing annotation will be patched automatically.
tektonconfig.operator.tekton.dev/config configured
```
