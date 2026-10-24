# ------------------------------------------------------------------------------
# Vault Kubernetes auth backend
#
# TokenReviewに使う認証情報について:
# - VaultはデフォルトでVaultポッド自身のprojected SAトークン
#   (/var/run/secrets/kubernetes.io/serviceaccount/token) とローカルCAを
#   TokenReviewの都度読み込んで使用する (disable_local_ca_jwt=false)。
#   kubeletによるトークンローテーションにも自動追従する (Vault 1.9+)。
# - TokenReviewを許可するRBAC (system:auth-delegator) は、Vault Helm chartの
#   server.authDelegator.enabled=true (deploy/vault/values/values.yaml) が
#   ClusterRoleBinding `vault-server-binding` として付与している。
#   chartからauthDelegatorを外す場合は自前のClusterRoleBindingが必要。
# ------------------------------------------------------------------------------

resource "vault_auth_backend" "kubernetes" {
  type        = "kubernetes"
  path        = var.kubernetes_auth_backend_path
  description = "Kubernetes auth backend for External Secrets Operator"
}

resource "vault_kubernetes_auth_backend_config" "kubernetes" {
  backend = vault_auth_backend.kubernetes.path

  # Vaultサーバーはクラスタ内で動作しているため、in-cluster DNSでAPIサーバーに到達できる。
  kubernetes_host = var.kubernetes_host

  # token_reviewer_jwt / kubernetes_ca_cert は未指定:
  # Vaultポッド自身のトークンとローカルCAが使用される (デフォルト動作)。

  # トークンのissuer検証は無効化 (k8sのissuer設定に依存しない)。
  disable_iss_validation = true
}

# ------------------------------------------------------------------------------
# Policy & role
# ------------------------------------------------------------------------------

# ESOから読み取れるのはこのKVマウント配下のみ。
data "vault_policy_document" "eso_read" {
  rule {
    path         = "${vault_mount.kv.path}/data/*"
    capabilities = ["read"]
  }

  rule {
    path         = "${vault_mount.kv.path}/metadata/*"
    capabilities = ["read"]
  }
}

resource "vault_policy" "eso" {
  name   = var.auth_policy_name
  policy = data.vault_policy_document.eso_read.hcl
}

resource "vault_kubernetes_auth_backend_role" "eso" {
  backend                          = vault_auth_backend.kubernetes.path
  role_name                        = var.auth_role_name
  bound_service_account_names      = [var.eso_service_account_name]
  bound_service_account_namespaces = [var.eso_namespace]

  token_ttl      = var.token_ttl
  token_max_ttl  = var.token_max_ttl
  token_policies = [vault_policy.eso.name]
}
