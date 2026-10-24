# KV v2 secrets engine.
# シークレットは `vault kv put kv/<name> ...` で登録し、
# ESOの SecretStore では path = "kv", version = "v2" として参照する。
resource "vault_mount" "kv" {
  path        = var.kv_mount_path
  type        = "kv"
  description = "KV v2 secrets engine for External Secrets Operator"

  options = {
    version = "2"
  }
}

# 動作確認用のサンプルシークレット (デフォルトでは作成しない)。
# terraform apply -var="enable_example_secret=true" で有効化できる。
# 注意: data は terraform state に平文で保存される。
resource "vault_kv_secret_v2" "example" {
  count = var.enable_example_secret ? 1 : 0

  mount     = vault_mount.kv.path
  name      = var.example_secret_name
  data_json = jsonencode(var.example_secret_data)
}
