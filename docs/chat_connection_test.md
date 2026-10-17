# チャットとGitHubの接続試験

## 今回確かめること

「GitHubに置いた計画をdotが読み、このチャットであなたに確認し、承認をGitHubに記録して、受信側が読み取れるか」を試します。

**実験は動かしません。研究室マシンも操作しません。** 受信側が返すのは「試験用の記録を受け取りました」という確認だけです。

## 流れ

1. 接続試験だけの計画を、専用のPRに置く。
2. dotが計画を読み、このチャットに試験内容・PR番号・コードの版を示す。
3. あなたが、その試験を明示的に承認する。
4. dotが返信を確認し、公開してよい最小限の試験承認記録を、そのPRのコメントに保存する。
5. 受信プログラムがGitHubから計画とコメントを読み直す。対象・版・期限が一致すれば、受信確認を保存する。
6. dotが、このチャットに結果を伝える。

途中で計画が変わった場合、期限切れ、PRが閉じられた場合は受け付けません。同じ記録を再度読んでも確認結果は増えません。同じ試験IDに別の記録が来た場合は止まります。

## 今できることと、まだできないこと

- このプログラムは実際の公開GitHub APIを読む機能を持ちます。トークンは使いません。
- チャットでの本人確認・返信の読み取り・GitHubへの投稿はdotが行います。プログラム自体はチャットの認証を証明しません。
- コメントを書いたGitHubアカウントだけでは、あなたがチャットで承認したことの証明になりません。dotが実際の返信を確認し、その記録のハッシュを別経路で受信側へ渡す必要があります。
- 最初は手動の受け渡しです。自動通知でdotが起きること、研究室マシンの常駐監視、署名付きの実行許可は、まだ確認対象外です。
- 接続試験に成功しても、実験開始の許可にはなりません。試験専用の形式は既存の実行承認形式と分かれています。
- 受信後に取り消された記録を監視し続ける機能はありません。受信確認は過去の観測であり、現在有効な実行許可ではありません。試験を取り消す場合は、受信前にPRを閉じるかコメントを削除します。すでに返った受信確認から何かが実行されることはありません。

## 試験計画の形式

専用PRに `docs/connection-tests/plan.json` を置きます。以下は形式の例で、実際の承認済み計画ではありません。有効期限は試験実施時に決めます。

```json
{
  "schema": "connection-probe-plan-v1",
  "purpose": "chat-github-receipt-only",
  "request_id": "connection-test-example",
  "revision": 1,
  "repository": "ken2129/c3_neural_compression",
  "expires_at": 1,
  "execute": false
}
```

`expires_at` はUTCのUnix秒です。`1` は意図的に期限切れの例です。新しい試験には新しい `request_id` を使います。PRのhead SHAはファイル内に書き込みません。ファイルを追加した後のSHAを承認記録に固定することで、自己参照を避けます。

## 試験承認記録

ユーザーの具体的な試験承認をdotが確認した後にだけ、以下のJSONをコメント本文として投稿します。Markdownのコード枠や説明文は付けません。チャット本文・内部のメッセージID・個人情報は投稿しません。

```json
{
  "schema": "connection-probe-receipt-v1",
  "purpose": "chat-github-receipt-only",
  "request_id": "connection-test-example",
  "revision": 1,
  "repository": "ken2129/c3_neural_compression",
  "pr_number": 1,
  "head_sha": "0000000000000000000000000000000000000000",
  "plan_digest": "0000000000000000000000000000000000000000000000000000000000000000",
  "decision": "approve-test-only",
  "execute": false
}
```

PR番号、head、計画のハッシュは実物から取得します。`plan_digest` と記録全体のハッシュは `research_harness.connection_probe.digest` で計算します。JSONの重複キーや不明な項目は拒否します。上の例はそのままでは使えません。

## 受信側を動かす

リポジトリ直下、Python 3.10以上。GPUや科学計算の依存パッケージは不要です。

```sh
python -m research_harness.connection_probe \
  --pr <試験PR番号> \
  --comment <試験承認コメントID> \
  --confirmed-receipt-digest <dotが返信を確認して固定した記録全体のSHA256> \
  --receipt-db <この試験専用のSQLiteファイル>
```

このコマンドは公開GitHubへのGET通信とローカルの受信確認保存だけを行います。実験のプロセス起動、GitHubの変更、コメント投稿、マージは行いません。ネットワーク障害やAPI制限は失敗として扱い、成功を装いません。必要な通信先は `api.github.com:443` です。

出力 `RECEIVED_TEST_ONLY` は「指定された試験記録の読み戻しに成功」の意味です。本人確認基盤や研究室の安全対策が完成したという意味ではありません。試験の成功を報告するときは、実際のPR・コメント・head・受信確認と、チャットでの確認が揃ったことを別々に確認します。

## 検証

```sh
python -m unittest discover -s tests -p 'test_harness_connection_probe.py' -v
python -m unittest discover -s tests -p 'test_harness_*.py' -v
```

単体テストは通信を置き換えた検証です。テストが通っただけでは、実際のGitHubとチャットを一周したことにはなりません。
