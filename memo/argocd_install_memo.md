## ArgoCDインストールメモ
helm repo url: https://artifacthub.io/packages/helm/argo/argo-cd
namespace名: argocd
helm名: argocd

## 方針
1. 証明書もhelm内で作成出来るが以下の2点のためhelm管轄外とする.
- kubernetes-dashboradと構成を同じにするため(今後ワイルドカード証明書に統一の可能性を見据えて)
- let's encryptのレート制限回避のため(初期構築時helmによるインストール/アンインストールを繰り返すことで引っかかる可能性があるため)
2. NetworkPoilcyは一旦設定しない(将来的に見直しの可能性あり)
3. Ingressリソースはserver用, applicastionSetのwebhook用の2用途あり, どちらも有効化する.
4. ロマン重視でHA構成&autoscaleを有効化(redis-ha有効化など) 将来的に見直しの可能性あり.(参考: https://github.com/argoproj/argo-helm/blob/main/charts/argo-cd/README.md)

## 設定変更
- crds.keep = false: helmアンインストール時crdを削除するように変更
- ingress周りの設定. sslパススルーするannotation追加.(https://argo-cd.readthedocs.io/en/stable/operator-manual/ingress/#ssl-passthrough-with-cert-manager-and-lets-encrypt)


## インストールコマンド
```
helm install -n argocd argocd argo/argo-cd --values values.yaml
```
