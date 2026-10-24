terraform {
  backend "kubernetes" {
    secret_suffix = "vault-external-secrets"
    namespace     = "vault"
    config_path   = "~/.kube/config"
  }

  required_version = ">= 1.7"

  required_providers {
    vault = {
      source  = "hashicorp/vault"
      version = "~> 5.13"
    }
  }
}
