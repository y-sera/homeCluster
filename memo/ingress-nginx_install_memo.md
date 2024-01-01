# ingress-nginxインストールメモ
インストール先namespace: ingress-nginx


ひとまず、デフォルト設定でインストール
```
helm install ingress-nginx ingress-nginx/ingress-nginx -n ingress-nginx 
```

インストール後、ingressリソースを追加するためのyaml設定
```
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: example
  namespace: foo
spec:
  ingressClassName: nginx
  rules:
  - host: www.example.com
    http:
      paths:
      - pathType: Prefix
        backend:
          service:
            name: exampleService
            port:
              number: 80
        path: /
  # This section is only required if TLS is to be enabled for the Ingress
  tls:
  - hosts:
    - www.example.com
    secretName: example-tls
```

TLS通信を行う場合には以下のシークレットを追加
```
apiVersion: v1
kind: Secret
metadata:
  name: example-tls
  namespace: foo
data:
  tls.crt: <base64 encoded cert>
  tls.key: <base64 encoded key>
type: kubernetes.io/tls
```
