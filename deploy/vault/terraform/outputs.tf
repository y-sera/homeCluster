output "kv_mount_path" {
  description = "Path of the KV v2 secrets engine."
  value       = vault_mount.kv.path
}

output "kubernetes_auth_backend_path" {
  description = "Mount path of the Kubernetes auth backend."
  value       = vault_auth_backend.kubernetes.path
}

output "kubernetes_auth_role_name" {
  description = "Kubernetes auth role to set in the (Cluster)SecretStore."
  value       = vault_kubernetes_auth_backend_role.eso.role_name
}

output "example_secret_path" {
  description = "KV v2 path of the example secret (null when disabled)."
  value       = var.enable_example_secret ? "${vault_mount.kv.path}/${var.example_secret_name}" : null
}
