# tektonインストールメモ

## 方針
productionとしての活用はoperatorを使ってくれとのことらしいので、operatorがオペレータと一緒にインストールする.
https://tekton.dev/docs/installation/pipelines/
https://github.com/tektoncd/operator/blob/main/docs/install.md
とりあえずallでインストールする.
tektonのdashboard用にingress, certificateリソースを別途作成する.

## インストールコマンド
```
kubectl apply -f https://storage.googleapis.com/tekton-releases/operator/latest/release.yaml
```
