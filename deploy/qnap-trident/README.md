# qnap-trident (QNAP CSI Driver)

[QNAP-CSI-PlugIn](https://github.com/qnap-dev/QNAP-CSI-PlugIn) を部分取り込み(ベンダリング)したディレクトリ。

> `git submodule` / `git subtree`(全体)ではなく部分取り込みにしている理由:
> 上流リポジトリの `bin/` 配下に tridentctl バイナリ(linux-amd64 + linux-arm64 で約176MB)が含まれており、
> リポジトリ履歴の肥大化・クローン時間の悪化を避けるため。必要な Helm チャートとマニフェストのみをコピーしている。

## 出所

- リポジトリ: https://github.com/qnap-dev/QNAP-CSI-PlugIn
- 適用タグ: **v1.6.2** (commit `8e88467ecf612fb84bbcd4caf300b624fca41de2`, 2026-07-31)
- 取り込み対象:
  - 上流 `Helm/trident/` → 本ディレクトリの `helm/trident/`(Helm チャート, chart version 2.0.0 / appVersion 1.6.2)
  - 上流 `VolumeSnapshot/` → 本ディレクトリの `volumesnapshot/`(snapshot-controller デプロイ + VolumeSnapshot CRD。kustomization.yaml 付き)

## 構成

```
deploy/qnap-trident/
├── README.md             # このファイル(出所・タグ・更新手順)
├── manifests/            # 自作マニフェスト(backend secret など)
├── helm/trident/         # vendored Helm chart (v1.6.2)
└── volumesnapshot/       # vendored manifests (v1.6.2)
```

## インストール例

```bash
# snapshot-controller + VolumeSnapshot CRD
kubectl apply -k deploy/qnap-trident/volumesnapshot

# QNAP CSI (trident operator) — namespace は manifests/namespace.yaml で作成済み前提
helm install qnap-trident deploy/qnap-trident/helm/trident -n trident \
  -f <values ファイル>
```

## 更新手順(新しいタグへの追従)

```bash
NEW_TAG=vX.Y.Z  # 更新先のタグ

# 1. 部分クローン(blob フィルタ付き。バイナリはダウンロードされない)
rm -rf /tmp/qnap-csi-$NEW_TAG
git clone --filter=blob:none --no-checkout --depth 1 --branch $NEW_TAG \
  https://github.com/qnap-dev/QNAP-CSI-PlugIn.git /tmp/qnap-csi-$NEW_TAG
cd /tmp/qnap-csi-$NEW_TAG
git sparse-checkout set Helm/trident VolumeSnapshot
git checkout

# 2. 差し替え
cd /home/yasuaki/works/homeCluster
rm -rf deploy/qnap-trident/helm/trident deploy/qnap-trident/volumesnapshot
cp -a /tmp/qnap-csi-$NEW_TAG/Helm/trident deploy/qnap-trident/helm/trident
cp -a /tmp/qnap-csi-$NEW_TAG/VolumeSnapshot deploy/qnap-trident/volumesnapshot
```

3. 本ファイルの「適用タグ」を新しいタグと commit hash に更新してコミットする。
   (commit hash は `git -C /tmp/qnap-csi-$NEW_TAG rev-parse HEAD` で確認)

## 注意

- 上流の `bin/`(tridentctl バイナリ)は意図的に取り込んでいない。tridentctl が必要な場合は上流から別途入手すること。
- チャートの `kubeVersion: ">= 1.24.0 <= 1.35"`。
- チャートのデフォルト values.yaml は `helm/trident/values.yaml`。独自設定は `-f` で上書きする。
