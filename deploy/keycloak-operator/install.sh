helm upgrade -i keycloak-operator oci://quay.io/keycloak/keycloak-operator-helm \
  --namespace keycloak-operator --create-namespace
