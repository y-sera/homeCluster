# Vault provider settings ------------------------------------------------------

variable "vault_address" {
  description = "Vault API address (e.g. https://vault.cluster.orenet.net). Falls back to the VAULT_ADDR environment variable when null."
  type        = string
  default     = null
}

# Vault configuration ------------------------------------------------------------

variable "kv_mount_path" {
  description = "Path where the KV v2 secrets engine is mounted."
  type        = string
  default     = "kv"
}

variable "kubernetes_auth_backend_path" {
  description = "Path where the Kubernetes auth backend is mounted."
  type        = string
  default     = "kubernetes"
}

variable "kubernetes_host" {
  description = "Kubernetes API server endpoint as seen from the Vault pods. The default works when Vault runs inside the cluster."
  type        = string
  default     = "https://kubernetes.default.svc"
}

# External Secrets Operator ---------------------------------------------------------

variable "eso_namespace" {
  description = "Namespace where the External Secrets Operator runs."
  type        = string
  default     = "external-secrets-operator"
}

variable "eso_service_account_name" {
  description = "Service account name of the External Secrets Operator pods used to authenticate against Vault."
  type        = string
  default     = "external-secrets-operator"
}

variable "auth_role_name" {
  description = "Name of the Kubernetes auth role created for the External Secrets Operator."
  type        = string
  default     = "external-secrets-operator"
}

variable "auth_policy_name" {
  description = "Name of the Vault policy granting read access to the KV mount."
  type        = string
  default     = "external-secrets-operator"
}

variable "token_ttl" {
  description = "TTL (seconds) of the tokens issued by the auth role."
  type        = number
  default     = 3600
}

variable "token_max_ttl" {
  description = "Maximum TTL (seconds) of the tokens issued by the auth role."
  type        = number
  default     = 14400
}

# Optional example secret ---------------------------------------------------------

variable "enable_example_secret" {
  description = "If true, store an example secret in the KV v2 mount (handy for a first smoke test)."
  type        = bool
  default     = false
}

variable "example_secret_name" {
  description = "Name of the example secret, relative to the KV mount."
  type        = string
  default     = "example/app"
}

variable "example_secret_data" {
  description = "Key/value data stored in the example secret."
  type        = map(string)
  default = {
    username = "admin"
    password = "change-me"
  }
  sensitive = true
}
