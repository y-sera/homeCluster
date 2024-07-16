export VIP=192.168.1.2
export INTERFACE=ens18

KVVERSION=$(curl -sL https://api.github.com/repos/kube-vip/kube-vip/releases | jq -r ".[0].name")
alias kube-vip="docker run --network host --rm ghcr.io/kube-vip/kube-vip:$KVVERSION"

kube-vip manifest daemonset \
    --interface $INTERFACE \
    --address $VIP \
    --inCluster \
    --taint \
    --controlplane \
    --arp > daemonset.yaml 

curl -o rbac.yaml https://kube-vip.io/manifests/rbac.yaml
