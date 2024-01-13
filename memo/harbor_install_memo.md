# harborインストールメモ
namespace: harbor
helm名: harbor
Chart: harbor/harbor


## 方針
- topolvmにてfsGroup機能を利用可能であり, ストレージのパーミッションエラーが解消されたため公式のチャートを利用する.
- ストレージはtopolvmを利用.

## 設定


## インストールコマンド
```
helm install -n harbor harbor harbor/harbor --values values.yaml
```
