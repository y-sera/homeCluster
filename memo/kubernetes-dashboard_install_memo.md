# kubernetes-dashboardインストールメモ
namespace名: kubernetes-dashboard
helm名: kubernetes-dashboard

## 方針
- TLS通信を標準とする(TLS化されていないとログインを強制される)
- 今の所自分しか使用しないため, サービスロールに強権を与え, ログイン画面はスキップする.
- 依存関係を鑑み, cert-managerは別途インストールを行う.
- ingressリソースはアプリ固有のため, helmにて導入を行う. 
 

## デフォルトからの設定変更
- ログイン画面のスキップ(extraArgs)
- ingressの有効化
- 証明書指定のためのannotations追加
- ホスト名登録
- デフォルトのサービスロール有効化
- サービスロールにクラスターadmin権限相当のアクションを許可

```
< extraArgs:
<   - --enable-skip-login
<   - --disable-settings-authorizer
---
> # extraArgs:
> #   - --enable-skip-login
174c173
<   enabled: true 
---
>   enabled: false
181,183c180,181
<   annotations:
<     kubernetes.io/ingress.class: nginx
<     cert-manager.io/issuer: kubernetes-dashboard 
---
>   # annotations:
>   #   kubernetes.io/ingress.class: nginx
222,223c220,221
<   hosts:
<   - dashboard.XXX.XXXX.XXX
---
>   # hosts:
>   #   - kubernetes-dashboard.domain.com
227,230c225,228
<   tls:
<   - secretName: kubernetes-dashboard-cert
<     hosts:
<     - dashboard.XXX.XXXX.XXX
---
>   # tls:
>   #   - secretName: kubernetes-dashboard-tls
>   #     hosts:
>   #       - kubernetes-dashboard.domain.com
312c310
<   clusterReadOnlyRole: true 
---
>   clusterReadOnlyRole: false
315,322c313,314
<   clusterReadOnlyRoleAdditionalRules:
<   - apiGroups:
<     - '*'
<     resources:
<     - '*'
<     verbs:
<     - '*'
<  
---
>   # clusterReadOnlyRoleAdditionalRules: []
> 
324,325c316,317
<   roleAdditionalRules: []
<   
---
>   # roleAdditionalRules: []
> 

```



## インストールコマンド
```
helm install -n kubernetes-dashboard kubernetes-dashboard kubernetes-dashboard/kubernetes-dashboard --values values.yaml
```
 
