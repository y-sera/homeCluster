# 証明書系インストールメモ

kubernetes-dashboard用の証明書を作成した際のメモを残しておく.

## 方針
1. 証明書発行にはRoute53を介したDNS認証を用いる.(参考: https://cert-manager.io/docs/configuration/acme/dns01/route53/)
2. cluster-Issuerの利用も考えたが, 証明書や格納されるシークレットのnamespace依存周りがよくわからなかったため,
   一旦は各アプリに対して個別に証明書を発行する. (External-Secretを導入してnamespace間でsecret共有が可能になれば, Cluster-Issuerで発行したワイルドカード証明書に寄せるかも.)
3. 後述する証明書発行自動化については実装しない.

## 作成リソース
- issuer
- certificate

参考
https://zenn.dev/masaaania/articles/e54119948bbaa2

## issuer
参考
https://cert-manager.io/docs/tutorials/acme/dns-validation/#issuing-an-acme-certificate-using-dns-validation



```
apiVersion: cert-manager.io/v1
kind: Issuer
metadata:
  name: <Issuerリソース名> 
  namespace: <namespace名>
spec:
  acme:
    # You must replace this email address with your own.
    # Let's Encrypt will use this to contact you about expiring
    # certificates, and issues related to your account.
    email: yasuaki.sera15@gmail.com
    ## certificate for staging
    # server: https://acme-staging-v02.api.letsencrypt.org/directory
    server: https://acme-v02.api.letsencrypt.org/directory
    privateKeySecretRef:
      name: <プライベートキーを格納するためのsecret> 
    solvers:
    - selector:
        dnsZones:
          - <Route53で使用するホストゾーン名> 
      dns01:
        route53:
          region: ap-northeast-1
          hostedZoneID: XXXXXXXXXXXXXX # optional, see policy above
          accessKeyIDSecretRef:
            name: route53-credentials-secret
            key: access-key-id
          secretAccessKeySecretRef:
            name: route53-credentials-secret
            key: secret-access-key
```


## certificate
```
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: <リソース名>
  namespace: <namespace名> 
spec:
  commonName: <登録ドメイン名> 
  dnsNames:
  - <登録ドメイン名>
  duration: 2160h0m0s
  issuerRef:
    group: cert-manager.io
    kind: Issuer
    name: <Issuerリソース名> 
  privateKey:
    rotationPolicy: Always
    algorithm: RSA
    encoding: PKCS1
    size: 2048
  renewBefore: 360h0m0s
  secretName: <証明書格納先シークレット名> 
  subject:
    organizations:
    - <任意の組織名> 
  usages:
  - digital signature # 任意の文章
```

## 躓いた点
cert-managerのトラブルシューティングリンク
https://cert-manager.io/docs/troubleshooting/acme/

### 証明書発行用のURL
 証明書発行用のlet's encryptのリンクはステージング用と本番用がある.
ステージング用の証明書は発行時のレート制限が一部緩和されており, 検証時はこちらを使用するべき.
ただし, ステージング用は通常のブラウザにルート証明書は登録されていなので、本番では使用しない.

参考リンク
https://letsencrypt.org/ja/docs/staging-environment/


証明書発行用のリンク(ステージング用)
https://acme-staging-v02.api.letsencrypt.org/directory

証明書発行用のリンク(本番用)
https://acme-v02.api.letsencrypt.org/directory


### 証明書発行自動化について
kube-legoのような仕組みを導入し, ingressリソースに`kubernetes.io/tls-acme = "true"`というアノテーションを付与すると
証明書発行の自動化が出来るらしい. 
だが, kube-lego自体はpublic archve化しているため, 今回は使用しない.
kube-legoリンク
https://github.com/jetstack/kube-lego


### DNS設定(Route53)
親ドメインとサブドメインのホストゾーンを別に作成する場合,
親ドメインのホストゾーンにサブドメインのnsレコードを作成し, 
サブドメインのNSサーバをコピーする必要がある.

参考
https://docs.aws.amazon.com/ja_jp/Route53/latest/DeveloperGuide/dns-routing-traffic-for-subdomains.html

### 証明書発行用の参照DNS変更
プライベートネットワーク内のDNSを参照しないため, 諸々設定を行う. 
このあたりが参考になる.
https://cert-manager.io/docs/configuration/acme/dns01/#setting-nameservers-for-dns01-self-check
https://qiita.com/SNAMGN/items/ce6e95cf81afbcb9ceac
