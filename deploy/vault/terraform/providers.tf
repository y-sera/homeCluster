# Vault provider.
# address が null の場合は環境変数 VAULT_ADDR にフォールバックする。
# Vaultトークンは環境変数 VAULT_TOKEN でのみ指定する (providerにtokenを渡さない)。
provider "vault" {
  address = var.vault_address
}
