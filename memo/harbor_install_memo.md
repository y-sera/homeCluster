# harborインストールメモ
namespace: harbor
helm名: harbor
Chart: bitnami/harbor


## 方針
- 公式Chartはpostgresplでinitdbを実行する際, /var/lib/postgresql/data配下のディレクトリ作成にてPermissionエラーが発生するため,
  使用を断念. 代わりにbitnami/harborを使用する.
- ストレージはNASのブロックストレージを利用する.

## 現状
- NASのLUN数が上限に達するため, 現時点ではデプロイを断念.
  TopoLVMを整備後にリトライする.

## インストールコマンド
```
helm install -n harbor harbor bitnami/harbor --values values.yaml
```
