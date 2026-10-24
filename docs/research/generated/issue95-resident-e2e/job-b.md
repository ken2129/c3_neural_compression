# Job B: README.md のバイト数と SHA256

## 目的と結果

承認されたソース SHA `6fd11016d64f88721eb08caff2bb8b0988f3d847` に対応する、ステージ済みの公開 `README.md` の生バイト列を Python 標準ライブラリで測定した。

- 入力: `README.md`
- バイト数: **14941**
- SHA256: `95aa24566d5f182ae0dfbac61fec60b3db0451f0e0eac39c6862ee017295eb6e`
- 機械可読の測定結果: [job-b.json](job-b.json)

## 実施した検証

`Path.read_bytes()` と `hashlib.sha256()` による一括計算を実施した。独立した2回目の読み取りでは、4096 バイトずつ読み、バイト数の加算と `hashlib.sha256().update()` による逐次計算を実施した。バイト数と SHA256 が両方一致することを assert で確認し、成功した。入力の変更は行っていない。

以下をリポジトリのルートで実行すると、公開 JSON と照合できる。

```python
from pathlib import Path
import hashlib
import json

expected = json.loads(Path(
    "docs/research/generated/issue95-resident-e2e/job-b.json"
).read_text(encoding="utf-8"))
data = Path("README.md").read_bytes()
first = (len(data), hashlib.sha256(data).hexdigest())
second_hash = hashlib.sha256()
second_count = 0
with open("README.md", "rb") as stream:
    while chunk := stream.read(4096):
        second_count += len(chunk)
        second_hash.update(chunk)
second = (second_count, second_hash.hexdigest())
assert first == second == (expected["byte_count"], expected["sha256"])
print("PASS", first)
```

## 制約と失敗した確認

作業ディレクトリには Git メタデータがなく、`git status` と `git rev-parse HEAD` は失敗した。このためソース SHA は承認値を記録しており、ローカルの Git 履歴による照合はできていない。`rg` は未導入だったためファイル確認に使用できず、標準のファイル操作で続行した。ハッシュ検証に不一致はなかった。

CPU と Python 標準ライブラリのみを使用した。モデル実験、GPU、追加依存関係の導入、データセットや重みのダウンロードは実施していない。検証対象はステージ済み README のバイト列のみであり、モデル性能や外部通知、ライフサイクルの観測は検証していない。
