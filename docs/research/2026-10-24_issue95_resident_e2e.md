# Issue95：新常駐方式の実E2E結果（2026-10-24）

[計画Issue #95](https://github.com/ken2129/c3_neural_compression/issues/95)に沿い、承認済みの小さなCPUタスク2件を実GitHubイベント・実runner・実Codexで完走した。通常PRの作成、結果保存、dotによる元チャットへの報告、仕事後の子孫回収と次の仕事の受付を確認した。同一イベントの実再配送は重複として応答し、追加実行・追加PRを生じなかった。

これは自動化経路の検証であり、圧縮性能・GPU研究実験ではない。PRのmergeは別判断で、今回行っていない。

## 固定した実装と入力

| 対象 | 版・状態 |
| --- | --- |
| 再利用したActions受付 | [PR #93](https://github.com/ken2129/c3_neural_compression/pull/93)、head `9f99f59a5b08b4c7139582b774d17e8c3e2fc753` |
| 再利用した常駐方式 | [PR #94](https://github.com/ken2129/c3_neural_compression/pull/94)、head `f9e6b9a6beb95cbe34396b078a8cd5ba92ab6083` |
| 配置ランタイム | ローカルcommit `13d200cf2c019a1dcad84c1cbe74e850e4e82ca2` |
| 配置イメージ | `sha256:eda225e7cf757f093a84d8cc61ac48fceddc1613140c4e35896a7c655272a7d4` |
| Codex / モデル | Codex 0.160.0 / gpt-6.1-sol |
| 公式Actions runner | 2.338.1、登録runner ID 2 |
| mainの承認済みworkflow・タスク入力・PR基点 | [`6fd11016d64f88721eb08caff2bb8b0988f3d847`](https://github.com/ken2129/c3_neural_compression/commit/6fd11016d64f88721eb08caff2bb8b0988f3d847) |

PR93・94は検証時点で未マージ。mainへ導入したのはレビュー済みworkflowの1ファイルだけである。配置版には、既存CLAIMEDの確認・復旧判断を受付listener公開より先に行う修正と、実測したprivate cgroup v1用のresident専用アダプターを含む。以前のDocker診断はモデルなしの根拠として再利用し、今回の実GitHub・実モデルの証明とは区別した。

上記ローカルcommitと後述の修正commitは、この報告PRには実装として含めていない。GitHubで取得できる公開実装と、検証環境に配置したローカル修正版を同一視しない。

## 実経路での2件の結果

| 確認項目 | A | B |
| --- | --- | --- |
| 承認済み実行Issue | [#96](https://github.com/ken2129/c3_neural_compression/issues/96) | [#97](https://github.com/ken2129/c3_neural_compression/issues/97) |
| owner承認コメントID | 6382647051 | 6382710600 |
| 実Actions配送 | [run 40082781323](https://github.com/ken2129/c3_neural_compression/actions/runs/40082781323)、初回attempt 1 | [run 40083267630](https://github.com/ken2129/c3_neural_compression/actions/runs/40083267630)、attempt 1 |
| 配送先 | runner ID 2 | 同じrunner ID 2 |
| 実Codexの処理時間 | 166.122秒 | 143.247秒 |
| 保存結果 | WORKER_FINISHED / completed | WORKER_FINISHED / completed |
| 通常PR | [#98](https://github.com/ken2129/c3_neural_compression/pull/98)、未マージ | [#99](https://github.com/ken2129/c3_neural_compression/pull/99)、未マージ |
| 計測入力 | LICENSE | README.md |
| サイズ | 11,358 bytes | 14,941 bytes |
| 追加ファイル | job-a.json / job-a.md | job-b.json / job-b.md |
| dot報告 | 利用者が到着を確認 | 利用者が報告全文を転送 |

managerが実GitHubからowner承認・期限・入力source・許可範囲を確認した後、各仕事でUID10002の実Codexと子プロセスが動いた。観測者がモデル仕事を代行したり、GitHub配送・モデル応答をfixtureへ置き換えたりしていない。

変更は両PRとも `docs/research/generated/issue95-resident-e2e` 配下の指定2ファイルだけ。JSONと日本語レポートをGitHubから読み戻し、期待サイズ・SHA256・変更ファイル集合と一致することを確認した。BはA完了と外側の報告到着確認後に承認して開始した。

workerはローカルGitメタデータがなく、承認commitと入力を独立照合できなかった旨を報告した。仕事完了後、観測者が実イメージ内の入力を承認commitのGit blobとbyte-for-byte照合し、一致を確認した。worker自身がGit照合したという意味ではない。

| 入力 | Git blob SHA | SHA256 |
| --- | --- | --- |
| LICENSE | `d645695673349e3947e8e5ae42332d0ac3164cd7` | `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30` |
| README.md | `4fca4007622e482e6941d2fd071e3a3e6d9dcda7` | `95aa24566d5f182ae0dfbac61fec60b3db0451f0e0eac39c6862ee017295eb6e` |

## 自動起動・連続処理・後処理

専用コンテナの起動からPID1 broker・runner・managerが自動起動した。測定区間のStartedAtは `2026-10-24T09:34:03.003555664Z`。A開始前からA完了、B完了、再配送後まで、コンテナID・StartedAt・RestartCount=0と、3常駐プロセスのPID/start_ticksが一致した。

各仕事の終了ではjob subreaperがECHILDを確認し、子孫0、一時認証除去、空の `/work` を確認した。cleanup記録は計2回。正常終了でコンテナ全体を再起動する旧方式に戻していない。専用コンテナは次の別途承認された仕事を待つ状態を維持した。既存bashコンテナ・元リポジトリ・ホスト設定は保護した。

## 同一イベントの実再配送

Aの元Actions job 120839578629をGitHubで1回rerunした。同じrun 40082781323のattempt 2、job 120842139060が同じrunner ID 2で成功し、実ログに次を記録した。

```text
2026-10-24T09:52:26.2584131Z ISSUE_EVENT_DUPLICATE
```

再配送前後で変化したのはAのlast_attemptが1→2になった点だけ。receipt identity/payload/outcome、2件のclaim witness、保存結果のバイトハッシュは不変だった。cleanupは2回のまま、追加worker完走0、作業領域は空、blockedなし。GitHubでも成果PRは98・99の2件のみでhead不変だった。

dot側の確認は利用者の外側の観測として扱った。両件の報告到着を確認し、再配送による余分な報告は利用者返信時点では未着と記録した。将来にわたる通知不在を保証するものではない。異なる新規イベントから同じIssueを再承認する別試験は実施していない。

## セットアップで人手を要した点と修正

最初の公式runnerアーカイブ展開が `/runner` を0755へ戻し、厳格な0700検査が起動を拒否した。初期失敗には自動restartとdurable blockがあり、初回セットアップが完全無介入だったという結果ではない。

仕事・claim・状態初期化がまだないことを確認し、0700へ修復した。block記録は削除せず保存し、新しい専用コンテナのセットアップ再起動で復旧した。受付policyのON反映にもセットアップ再起動を行った。これらはA開始前であり、測定区間内の再起動ゼロとは分けて扱う。

再発防止のローカルcommit `6f25e2e7557c61fd5a2c913b6d1bc54494d4bd9d` は、Dockerfileの公式tar展開・chown後にchmod0700を追加する。実GNU tarでトップ「.」が0755/0777のアーカイブを展開し、実bootstrapを通す回帰テストは修正前に失敗、修正後に成功した。稼働イメージは13d200cのままで、修復済みvolumeを使用している。この修正を再ビルド・再配置済みとは扱わない。

## テストと資源・費用の境界

- 起動時受付順序の5回帰は未修正PR94で失敗、修正後normal/-Oで成功。
- resident専用v1の17回帰と、実private namespaceでのnative resource probeが成功。v2/disposable経路やホスト設定は変更していない。
- runner権限修正前の全harnessはnormal/-O各1109件、1108成功・既存PID1環境条件1skip。
- runner権限修正後は対象6件がnormal/-Oで成功。resident関連69件は両モードとも68成功・同1skip。修正後の全harness再実行とは主張しない。
- 各仕事は600秒・1 turn、120 CPU秒/プロセス。コンテナ全体は1CPU・1GiB・swap0・PID64、work256MiB、出力1MiB。実モデル仕事は2件、各通常PR1件、再配送で追加turnなし。
- ChatGPT認証と既存プラン/クレジット利用は承認済み。新規購入・API課金への切替は行わず、実使用トークン数・金額は未計測。時間制限は金額上限ではない。
- Codexレビューの「指摘なし」とCI成功は別。成果PRのCI成功は確認していない。
- GPU研究実験、データセット・重み取得、他者のジョブ操作、Slack送信、PR mergeは行っていない。

## 公開証跡の確認方法

[観測結果JSON](2026-10-24_issue95_resident_e2e_evidence.json)に、再配送前後と最終状態、claim/result/receiptのハッシュ、cleanup、実Actions ACK、PR・入力の照合結果、利用者による報告確認を保存した。プロセスの同一性はPIDとstart_ticksの組のSHA256へ変換した。接続先・ホスト絶対パス・認証情報・私有ログは公開していない。

これは観測者が採取した記録であり、第三者による署名付き証明ではない。local_witness_sha256は非公開原本との後日の照合用で、公開変換後JSONそのもののハッシュではない。公開Actions/PRはリンク先から別途検証できる。

リポジトリのrootで次を実行すると、保存した証跡の内部整合性をモデル・ネットワークなしで検査できる。新しいE2Eを実行するコマンドではない。

```sh
python3 -B - <<'PY'
import copy
import json
from pathlib import Path

p = Path("docs/research/2026-10-24_issue95_resident_e2e_evidence.json")
d = json.loads(p.read_text())
before = copy.deepcopy(d["before_redelivery"])
after = d["after_redelivery"]
event = next(e for e in before["events"]
             if e["receipt"] == "actions:1312915009:40082781323")
assert event["last_attempt"] == 1
event["last_attempt"] = 2
assert before == after == d["final"]
assert [r["issue"] for r in after["runs"]] == [96, 97]
assert all(r["phase"] == "WORKER_FINISHED" and r["outcome"] == "completed"
           for r in after["runs"])
assert after["restart_count"] == 0 and after["job_cleaned_count"] == 2
assert after["work_empty"] and not after["blocked"]
assert all(c["worker"] == 0 and c["descendants_echild"]
           and c["temporary_credentials_removed"] for c in after["cleanup"])
assert len(after["claims"]) == len(after["events"]) == 2
assert [p["number"] for p in d["pr_verification"]] == [98, 99]
assert d["redelivery_github"]["job"]["run_attempt"] == 2
assert d["redelivery_github"]["job"]["conclusion"] == "success"
assert any("ISSUE_EVENT_DUPLICATE" in s
           for s in d["redelivery_github"]["ack_lines"])
assert all(x["exact_bytes_match"] for x in d["input_provenance"]["inputs"])
assert all(d["dot_reports"][job]["confirmed"] for job in ("a", "b"))
assert d["dot_reports"]["redelivery"]["extra_report_received"] is False
print("PASS: recorded two-job completion, cleanup and same-event deduplication")
PY
```

この報告PRは文書と観測記録だけを追加する。新しいモデル実行・受付設定変更・修正コードの配置は含まない。追加の仕事、広い互換性、汎用セットアップ、GPU実験の承認には流用しない。
