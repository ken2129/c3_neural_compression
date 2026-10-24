# Job A: LICENSE の CPU 検証結果

承認済み source SHA: `6fd11016d64f88721eb08caff2bb8b0988f3d847`  
入力: `LICENSE`（ステージ済みの公開バイト列）

| 項目 | 結果 |
| --- | --- |
| バイト数 | 11358 |
| SHA256 | `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30` |

Python 標準ライブラリのみを使用し、CPU 上で計算した。ファイル全体を読み込む方法と、別の Python プロセスで 4096 バイトずつ読む方法の結果が一致した。機械可読の結果は [job-a.json](job-a.json) に記録した。

## 再現手順

リポジトリのルートで以下を実行する。入力はバイナリモードで読み、改行の変換は行わない。

```python
from pathlib import Path
import hashlib
raw = Path("LICENSE").read_bytes()
print(len(raw), hashlib.sha256(raw).hexdigest())
```

別の Python プロセスで以下を実行して照合した。

```python
import hashlib
count = 0
h = hashlib.sha256()
with open("LICENSE", "rb") as source:
    while chunk := source.read(4096):
        count += len(chunk)
        h.update(chunk)
print(count, h.hexdigest())
```

## 実施した検証と制限

- 2 通りの計算はいずれも上記のバイト数と SHA256 を返し、不一致はなかった。
- JSON の全 5 フィールドと報告書の測定値を再読込して照合した。
- 入力への書込み、依存関係のインストール、GPU の利用、モデル実験、データセットや重みのダウンロードは行っていない。
- 作業環境に `rg` がなく、Git メタデータもなかったため、ローカルの Git status による差分確認はできなかった。公開対象は指定の 2 ファイルに限定する。
- source SHA は承認済み計画の値を記録した。ステージ済み LICENSE とそのコミット内の LICENSE の一致は独立には検証していない。
- この検証は LICENSE のバイト列のみを対象とし、モデル性能や外部通知、ワーカー外部のライフサイクルを検証するものではない。

公開準備では一時領域への manifest 作成が読取り専用で失敗したため、書込み可能な管理領域へ保存して復旧した。入力と測定結果への影響はない。
