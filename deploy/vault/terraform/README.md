# Vault + External Secrets Operator 連携のTerraform

External Secrets Operator (ESO) を HashiCorp Vault バックエンドで使用するために必要な、
Vault側のリソース (KV v2マウント・Kubernetes認証) を作成するTerraformコード。

## 作成されるリソース

| リソース | 名前 (デフォルト) | 用途 |
|---|---|---|
| `vault_mount` | `kv` (KV v2) | シークレットの保存先 |
| `vault_auth_backend` | `kubernetes` | Kubernetes認証の有効化 |
| `vault_kubernetes_auth_backend_config` | - | APIサーバー (`https://kubernetes.default.svc`) の設定。TokenReviewにはVaultポッド自身のSAトークン/ローカルCAが使用される (`disable_local_ca_jwt=false`) |
| `vault_policy` | `external-secrets-operator` | `kv/data/*`, `kv/metadata/*` の `read` |
| `vault_kubernetes_auth_backend_role` | `external-secrets-operator` | ESOのSA (`external-secrets-operator@external-secrets-operator`) へのバインド |

認証フロー:

```
ESO pod (external-secrets-operator ns)
  --1. JWTでVaultにlogin--> vault.cluster.orenet.net (kubernetes auth)
  --2. VaultがTokenReview APIでJWTを検証--> Kubernetes API server
  --3. policyに基づき kv/data/* を読み取り--> ESOがSecretを同期
```

## 前提条件

- Vaultサーバーが起動・unseal済みであること (`deploy/vault/`)
- ESOが導入済みで namespace `external-secrets-operator` が存在すること (`applications/external-secrets-operator.yaml`)
- TokenReview用のRBACが設定済みであること: Vault Helm chartの `server.authDelegator.enabled: true` (`deploy/vault/values/values.yaml`) が、`system:auth-delegator` をVaultのServiceAccount (`vault` @ `vault` ns) にバインドする `vault-server-binding` ClusterRoleBindingを作成する。TokenReviewにはVaultポッド自身のSAトークンを使用するため、専用のreviewer SAは不要
- ローカルPCからVault API (`https://vault.cluster.orenet.net`) に到達できること。加えて、state保存先がKubernetes Secret (`backend "kubernetes"`) のため、kubeconfigでKubernetes APIにも到達できること
- Vaultトークン: マウント・認証バックエンド・ポリシーを作成できる権限 (初回設定はrootトークンが簡単)。**環境変数 `VAULT_TOKEN` でのみ指定する** (tfvarsや変数では管理しない)

## 実行手順

```bash
cd deploy/vault/terraform

# プロバイダーのインストール
terraform init

# Vault接続情報。トークンは環境変数 VAULT_TOKEN でのみ指定する
# (アドレスは terraform.tfvars の vault_address でも指定可)
export VAULT_ADDR="https://vault.cluster.orenet.net"
export VAULT_TOKEN="<root token または admin token>"

terraform plan
terraform apply
```

`VAULT_ADDR` もtfvarsで渡す場合は `terraform.tfvars` を作成 (.gitignore 済みのためコミットしないこと)。トークンはtfvarsに記載しないこと:

```hcl
vault_address = "https://vault.cluster.orenet.net"
```

## Backend (Terraform state)

Terraform stateはKubernetes Secretに保存する (`versions.tf` の `backend "kubernetes"`)。

| 項目 | 値 |
|---|---|
| Secret名 | `tfstate-default-vault-external-secrets` (workspace `default` の場合) |
| 保存先namespace | `vault` |
| ロック | Leaseリソースによる排他制御 |

- backendブロックは変数化できないため、kubeconfigを `~/.kube/config` 以外に置く場合は `terraform init -backend-config="config_path=/path/to/kubeconfig"` または環境変数 (`KUBE_CONFIG_PATH` / `KUBE_CTX` / `KUBE_NAMESPACE`) で上書きする
- backendはnamespaceを自動作成しないため、`vault` namespaceが存在している必要がある
- 保存場所の確認: `kubectl -n vault get secret tfstate-default-vault-external-secrets`
- backend設定を変更した場合は `terraform init -reconfigure` (state未適用時) または `terraform init -migrate-state` (state引き継ぎ時) で再初期化する
- plan/applyが異常終了するとロック (Lease `lock-tfstate-default-vault-external-secrets`) が残留し、自動失効しない。`terraform plan` が `Error acquiring the state lock` で失敗する場合は、`kubectl -n vault get lease` でholderを確認し `terraform force-unlock -force <HOLDER UUID>` で解放する (ライブなterraformプロセスがいないことを確認してから実行)

## シークレットの登録方法

apply後、KV v2マウント (`kv`) 配下にシークレットを登録する:

```bash
vault kv put kv/myapp/config username="admin" password="supersecret"

# 確認
vault kv get kv/myapp/config
```

Vault UI (`https://vault.cluster.orenet.net`) からも登録可能。

- 登録したシークレットはVault上では `kv/data/myapp/config` に配置される (`vault kv` コマンドは `data/` を自動付与)
- ESOの `remoteRef.key` には `myapp/config` のように `data/` を付けないパスを指定する

## ESO側の設定例

`ClusterSecretStore` と `ExternalSecret` の例。ArgoCD等のGitOpsフローに乗せて適用する。

```yaml
apiVersion: external-secrets.io/v1
kind: ClusterSecretStore
metadata:
  name: vault
spec:
  provider:
    vault:
      server: https://vault.cluster.orenet.net
      path: kv
      version: v2
      auth:
        kubernetes:
          mountPath: kubernetes
          role: external-secrets-operator
---
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: myapp
  namespace: default
spec:
  refreshInterval: 1h
  secretStoreRef:
    kind: ClusterSecretStore
    name: vault
  target:
    name: myapp
  data:
    - secretKey: username
      remoteRef:
        key: myapp/config
        property: username
    - secretKey: password
      remoteRef:
        key: myapp/config
        property: password
```

- `serviceAccountRef` を省略した場合、ESOはコントローラpodのSA (`external-secrets-operator`) でVaultにログインする。Terraformで作成したロールはこのSAにバインド済み
- サーバー証明書はLet's Encrypt (公的CA) のため、ESO側の追加のCA設定は不要

## 動作確認用サンプルシークレット

フラグで有効化できる (`data` はterraform stateに平文で入る点に注意):

```bash
terraform apply -var="enable_example_secret=true" \
  -var='example_secret_data={username="admin", password="change-me"}'
```

`kv/example/app` が作成され、`outputs.example_secret_path` で確認できる。

## 主な変数

| 変数 | デフォルト | 説明 |
|---|---|---|
| `vault_address` | `null` (VAULT_ADDR環境変数) | Vault APIアドレス |
| `kv_mount_path` | `kv` | KV v2マウントパス |
| `kubernetes_auth_backend_path` | `kubernetes` | kubernetes認証のマウントパス |
| `kubernetes_host` | `https://kubernetes.default.svc` | Vault podから見たAPIサーバー |
| `eso_namespace` | `external-secrets-operator` | ESOが動作するnamespace |
| `eso_service_account_name` | `external-secrets-operator` | ESOのServiceAccount名 |
| `auth_role_name` / `auth_policy_name` | `external-secrets-operator` | ロール/ポリシー名 |
| `token_ttl` / `token_max_ttl` | `3600` / `14400` | 発行トークンのTTL (秒) |
| `enable_example_secret` | `false` | サンプルシークレットの作成 |

## 注意事項

- **stateは機密情報を含み得る**: 例えばサンプルシークレット (`enable_example_secret`) の data は state に平文で記録される。stateは `vault` namespaceのSecret (`tfstate-default-vault-external-secrets`) に保存されるが、Secretはbase64エンコードのみで暗号化されないため、etcdのシークレット暗号化 (k3sなら `--encrypt-secrets`) の有効化や、当該namespaceへのRBAC制限を推奨。ローカルに残る `*.tfstate*` や `*.tfvars` はコミットしない (`.gitignore` 済み)
- **TokenReviewはVault自身のSAトークンで行う**: `vault_kubernetes_auth_backend_config` は `kubernetes_host` と `disable_iss_validation` のみを設定しており、Vaultポッドのprojected SAトークンとローカルCAがTokenReviewに使用される (kubeletによるトークンローテーションにも自動追従、Vault 1.9+)。この動作には `system:auth-delegator` 権限が必要で、Vault Helm chartの `server.authDelegator.enabled: true` (`deploy/vault/values/values.yaml`) が `vault-server-binding` ClusterRoleBindingとして付与している。chartで `authDelegator` を無効化する場合は、`system:auth-delegator` をVaultのServiceAccountにバインドするClusterRoleBindingを自前で用意すること
- **policyの権限は `read` のみ**: シークレットの書き込みは `vault kv put` やUIで行う。ESOの `find` (メタデータ一覧) 等で `list` が必要になった場合は `auth.tf` の `vault_policy_document` に `capabilities = ["read", "list"]` を追加する
- `disable_iss_validation = true` としているため、Kubernetesのissuer設定に依存せず動作する

## 検証

```bash
terraform fmt -check -recursive
terraform validate
```
