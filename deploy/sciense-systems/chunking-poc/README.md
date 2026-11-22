# チャンク作成の検証 (設計メモ 5.4 / 5.7)

HF の paper_text (組み立て済みの TeX) を正規化し, 節 (`paper_sections`) とチャンク (`paper_chunks`) に分ける処理の検証.
全件の処理の前に, 1,000 件で処理を確かめ, 10,000 件で統計を取る.

## ファイル
```
dev-pod.yaml        作業用の Pod chunking-poc と PVC chunking-poc-work (Longhorn 20Gi). 元データ arxiv-datasets を /src に読み取り専用で
src/texnorm.py      TeX の正規化と節の抽出 (normalizer_version)
src/chunker.py      チャンク分割 (chunker_version). トークン数は Qwen/Qwen3-Embedding-0.6B のトークナイザ
src/process.py      parquet → papers / sections / chunks の parquet (4 並列)
src/make_sample.py  層別のサンプル (paper_text のランダムな 300 行グループから, 13 の層ごとに約 770 件)
src/report.py       統計 (5.7 の「取る値」) と全件への外挿
results/            統計の結果 (JSON)
```

## 実行
```bash
kubectl apply -f deploy/sciense-systems/chunking-poc/dev-pod.yaml
kubectl -n science-systems exec chunking-poc -- bash -c 'python -m venv /work/venv && /work/venv/bin/pip install pyarrow==22.0.0 tokenizers==0.22.1 huggingface_hub'
for f in deploy/sciense-systems/chunking-poc/src/*.py; do kubectl -n science-systems cp $f chunking-poc:/work/src/$(basename $f); done
kubectl -n science-systems exec -it chunking-poc -- bash
cd /work
venv/bin/python src/process.py --input /src/sample/train-00000-of-00001.parquet --out out/sample    # HF の sample (991 件)
venv/bin/python src/make_sample.py --out sample10k.parquet --population population.json            # 約 4 分 (2.7GB 読む)
venv/bin/python src/process.py --input sample10k.parquet --out out/s10k                             # 約 4.5 分
venv/bin/python src/report.py --run out/s10k --population population.json
```
検証が終わったら Pod と PVC を削除する (`kubectl delete -f deploy/sciense-systems/chunking-poc/dev-pod.yaml`).

## 結果 (2026-11-22, normalizer v0.7 / chunker v0.2, 層別 9,868 件)
| 項目 | 値 |
|---|---|
| 正規化の成功率 (例外なし, 本文 200 文字以上) | 99.41% (失敗 58 件: `%auto-ignore` 38, `\includepdf` 12. いずれも元の TeX に本文がない) |
| 節の抽出の成功率 (元の TeX に `\section` 系がある論文) | 99.9%. `\section` 系がない論文は 5.3% (PRL などのレター, plain TeX) で, 本文全体を 1 節とする |
| 節 / 論文 (要旨・参考文献・前置きを除く) | 平均 17.2, 中央値 14, P95 39 |
| チャンク / 論文 | 平均 32.6, 中央値 26, P95 76 |
| トークン / チャンク | 平均 484, 中央値 534, P5 57, P95 807, P99 928 |
| チャンクの大きさ | 500〜800: 47.4%, 100〜499: 37.6%, 801〜1,000: 5.8%, 100 未満: 9.2%, 1,000 超: 19 件 (0.006%) |
| 表 | 論文あたり平均 4.4 (中央値 2). 表のチャンクは全体の 10% |
| 重複チャンク (内容のハッシュが同じ) | 0.33%. すべて論文間の定型文 ("The authors declare no competing interests." など). 論文内の重複は除いている |
| 本文の残る割合 (正規化後 / 正規化前の文字数) | 中央値 72%, P5 47% (コマンド, 図, 前付け, 参考文献を除くため) |
| 処理時間 | 0.08 秒 / 論文 (中央値), P99 0.45 秒, 最大 2.7 秒. 10,000 件で 267 秒 (4 並列) |

層ごと (13 層) の成功率は 98.6% (cs.CV) 〜 99.9% (cond-mat), 節の抽出は 99.6% 以上. チャンク数の中央値は cond-mat 16 〜 stat 37.

全件 (paper_text 2,856,227 件) への外挿 (層の大きさで重み付け)
| 項目 | 推定 |
|---|---|
| チャンク数 | 約 9,100 万 |
| トークン数 | 約 480 億 (Qwen3 のトークナイザ) |
| チャンクの本文 (UTF-8, 圧縮前) | 約 155 GB |
| 処理時間 | 約 83 CPU 時間 (16 並列で約 5 時間) |

## 検証で分かったこと (処理に反映済み)
- **HF の paper_text の resolved (18%) は, `\input` で取り込んだファイルを `\end{document}` の後ろに連結している.**
  `\end{document}` で切ると本文の大半を失う (初版で 7.6% の論文が空になった). 後ろの本文も含め, サブファイルの `\documentclass` などは除く.
  本体の `\appendix` の位置は付録の位置を表さないため, この場合は見出しで判定する
- 連結されたファイルには biblatex の .bbl, `glyphtounicode.tex`, スタイルファイル (TeX のプログラム) も含まれる. プログラムのコマンドの密度が高い段落を除く (文字数の 0.25%)
- プリアンブルで `\section` などを `\renewcommand` で定義し直す論文がある. 展開すると見出しが壊れるため, 標準のコマンドは展開しない
- マクロの自己参照の判定は名前の区切りまで見る (`\be` と `\begin` の区別. `\def\be{\begin{equation}}` が展開されていなかった)

## 未決定・残り
- **短い節のチャンク**: 100 トークン未満のチャンクの 6 割 (全チャンクの 5.4%) は節そのものが短い (設計は「節をまたがない」). トークンでは全体の 1%.
  そのままにするか, 同じ親の続く節と結合するかを決める
- **section_type の other が 28%**: 見出しが内容を表す節 ("Unit disk graphs" など) は規則で分類できない. 親の節の種類を継ぐようにしたが, 第 1 階層の内容の見出しは other になる
- 索引の容量と作成時間 (codec 別) は OpenSearch (手順 4, search1) ができてから, この 10,000 件のチャンクで測る
- `\ref` の番号は解決しない ([ref] のまま. 設計どおり)
