# ------------------------------------------------------------------------------
# terraform.tfvars - この環境 (homeCluster) 用の実値
# 値の出所:
#   - Vault外部アドレス: kubectl -n vault get ingress (vault.cluster.orenet.net)
#   - ESOのSA/namespace: helm/external-secrets-operator/values.yaml
# ------------------------------------------------------------------------------

# Vault provider -----------------------------------------------------------------
# address を設定すると VAULT_ADDR 環境変数が不要になる。
vault_address = "https://vault.cluster.orenet.net"

# Vaultトークンは tfvars では指定しない。環境変数 VAULT_TOKEN のみで指定すること。
#   export VAULT_TOKEN="hvs.xxxxxxxxxxxxxxxxxxxx"

# Vault設定 -------------------------------------------------------------------------
kv_mount_path                = "kv"
kubernetes_auth_backend_path = "kubernetes"

# Vaultポッドから見たAPIサーバーエンドポイント (in-cluster DNS)
kubernetes_host = "https://kubernetes.default.svc"

# External Secrets Operator -----------------------------------------------------------
eso_namespace            = "external-secrets-operator"
eso_service_account_name = "external-secrets-operator"
auth_role_name           = "external-secrets-operator"
auth_policy_name         = "external-secrets-operator"
token_ttl                = 3600
token_max_ttl            = 14400

# 任意のサンプルシークレット ------------------------------------------------------------
# 初回スモークテストでのみ true にする。
enable_example_secret = false

# enable_example_secret = true の場合のみ設定 (デフォルト値の例)
# example_secret_name = "example/app"
# example_secret_data = {
#   username = "admin"
#   password = "change-me"
# }
