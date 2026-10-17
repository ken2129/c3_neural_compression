# Issueから計画を提出する仕組み（実験はまだ始めない）

## この変更でできること

1. 管理側が指定したIssue番号だけを読み、閉じたIssueを除く。
2. 計画担当が、目的・比較条件・評価方法・使用量の上限などをJSONで返す。
3. 未決定の条件は「判断が必要なこと」として残す。金額やGPU時間を勝手に確定しない。
4. 公開してよい内容か人が確認した後、GitHubに計画ファイルと読みやすいPR本文を提出する。
5. 同じ計画を再送してもPRを増やさない。変更するときは確認したPRの版だけを更新する。

**提出された計画は実行許可ではない。マージも行わない。**
この実装からGPU・実験・Codexを起動する経路はない。

## 何が未接続か

- Codexの安全な起動と、Issueから計画JSONを生成する実サービス。`Planner.propose` が接続口で、現段階のテストは代役を使用する。
- dotチャットの本人承認を実験実行へ結び付ける機能。
- 共有マシン上の権限分離・資源制限・常駐設定。
- 実GitHubへの投稿を含む統合試験。今回確認するのはモックとローカルテストであり、実計画は投稿しない。

PR #61の接続テスト用JSONとは別形式。そちらの承認をこの計画の承認として流用しない。

## 計画の保存場所と形式

提出先は `docs/research-plans/issue-N/plan.json` と人向けの `plan.md`。PR本文は固定の案内で、詳細は同じcommit内のMarkdownにまとめる。
固定したコードcommit、Issue本文・更新日時のSHA256、計画の版を記録する。
GitHubのコメントやIssueの自由文から操作コマンドを組み立てない。

JSONの必須項目は `version: 1`, `kind: "proposal_only"`, `issue`,
`issue_fingerprint`, `source_sha`, `revision`, `sections`, `unresolved`。
`sections` は以下の12項目をすべて含む。

| JSON項目 | 人が読む内容 |
|---|---|
| summary | 何をする計画か |
| question | 確かめたいこと |
| evidence | 根拠となる公開記録のURLとcommit |
| reuse | 再利用する既存結果 |
| new_work | 新しく行う作業 |
| conditions_and_controls | 比較条件と対照 |
| calibration_and_evaluation | 条件調整用・評価用の分離 |
| metrics | bppやAPなど評価指標の定義 |
| deliverables | 残す成果物 |
| completion | 完了条件 |
| dependencies | 先に必要な準備 |
| budget | 使用量の上限。未確定なら明記 |

`unresolved` は決める必要がある事項の配列。空でも実行許可にはならない。
文字列の形式検査だけでは、内容の科学的妥当性や秘密の不存在は証明できない。
公開担当が内容全体を確認することが必須。

## 将来、許可された管理環境で使うコマンド

既に認証済みの `gh` と `git` が必要。認証情報の作成・導入はこの機能では行わない。
まず読取りだけを行う。

```sh
python -m research_harness.plan_github inspect --issue 24
```

計画JSONを別工程で準備し、全文を確認する。公開確認済みのJSONのSHA256と、
既存PRがあればその確認済みhead SHAを渡す。

```sh
python -m research_harness.plan_github publish --issue 24 \
  --plan reviewed-plan.json --reviewed-digest EXACT_PLAN_SHA256 \
  --expected-head EXACT_EXISTING_PR_HEAD_SHA
```

初回は `--expected-head` を省略する。公開先はこのリポジトリに固定。
branchは `research-plan/issue-N`、変更ファイルは計画JSONとMarkdownだけ。
既存PRを更新するとき、既存ファイルは保持する。
新規PRは通常PR（draftではない）として作成するが、内容は計画段階と明記する。
結果は同じPRへ追加していく設計であり、この提出機能自体は結果を追加しない。

## 事故・やり直しの扱い

- Issueが変更・closeされたら古い計画を拒否。
- 他のPRが同じIssueを参照している場合も、重複の可能性があるため停止して照合する。
- PRが変更されたら古いheadの確認では更新しない。gitのexpected-head leaseで競合を拒否。
- 既存PRがclose・mergeされていたら、新しいPRを勝手に作らない。
- 投稿結果が不明なときは状態を読み直す。API失敗に対して無条件で再送しない。
- branchだけ作られてPR作成前に停止した場合、内容が同一で他の変更がないことを確認できた場合だけPR作成を続ける。
- 自動常時巡回や全Issueの実行は行わない。管理側の明示Issue番号が必要。

このCLIは保護された管理環境の操作者向け。計画担当のCodexにその認証情報や
公開コマンドの実行権限を渡してよいという意味ではない。

Issue更新とGit更新は別サービス操作なので、完全な同時変更防止は保証しない。書込直前と読戻し後に照合し、変更を検出した場合は成功と報告しない。

## このPRで確認したこと

- 計画の形式、Issue・版の対応、古い承認対象の拒否、再送、PRの閉鎖・重複、公開内容の読戻しをテスト。
- 一時フォルダ内だけのGitリポジトリで、本物のcommit/pushとhead競合拒否を確認。GitHub APIは代役、外部ネットワークは使わない。
- ハーネス関連テストは通常起動とPython最適化起動の両方で成功。
- リポジトリ全体のテストは機械学習用依存が不足しており、全件成功とはしていない。
- 実GitHubの計画PR投稿、実Codex生成、研究室マシン、実験起動は未検証。
