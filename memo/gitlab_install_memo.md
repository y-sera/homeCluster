# gitlabインストールメモ

namespace名: gitlab
helm名: gitlab

## 方針
1. 複数ドメインを持つリソースを作成するため, 証明書はワイルドカードで作成する.
2. nginx-ingress, cert-managerは別途リソース構築をしているため, 本helmでは無効化する.
3. 証明書も, 他のアプリと方針を合わせるため(ワイルドカード証明書でクラスター全体で統合もありうるため), 別途IssuerとCertificateリソースを作成する.
4. DB, オブジェクトストレージ, redis等々の外部リソース化出来るものは, 現時点ではhelm内リソースを利用する.
5. certmanager-issuer.emailは指定必須のようなので値は代入するが, 利用はしない.(issuer, secretは作成される)

## 設定変更
gitlab-runnerはクラスター内のdnsを参照してgitlabを探すため, クラスター内dnsで自宅ドメインを参照出来るように変更する.
kube-systemのcoredns configmapにて, external-dns用のIPをフォワードするよう設定する.
manifest/coredns_kube-system/coredns.yaml

## インストールコマンド
```
helm install -n gitlab gitlab gitlab/gitlab --values values.yaml
``` 
