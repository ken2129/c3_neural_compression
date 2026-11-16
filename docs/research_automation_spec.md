# 研究自動化システム 詳細仕様

本書は、同じLinuxマシン上の管理用コンテナと実験用コンテナで、GitHub Issueから
Codexによる研究・実験・結果PRまでを処理するシステムの仕様である。
構成だけでなく、受付条件、設定、状態、権限、資源監視、永続化、障害時の扱いを定義する。

| 項目 | 本書の対象 |
| --- | --- |
| 仕様の基準 | PR #108の公開commit `21af4c2d651bffe00883c3a2e3458de4d299a5bf` |
| 実装の状態 | PR #108は未merge。mainに全機能が存在するという意味ではない |
| 現行経路 | `codex-direct-pr-v1` + `execution_container` |
| 動作環境 | 1台のLinuxホスト、ローカルDocker Engine、登録済みの実験コンテナ |
| 配置の記述 | 2026-11-15の確認記録。現在値の確認方法は第15節 |
| 読者 | 研究依頼者、運用者、実装の変更・レビューを行う人 |

「実装」はコードが受理・拒否する動作、「確認済み」は試験または実機記録がある範囲、
「未対応」は現在のコードが提供しない機能を指す。将来案や書式例は実行承認にならない。
概要は[研究自動化システム](research_automation.md)、過去の実行証拠は
[実GPU E2E記録](research/2026-11-15_native_container_e2e.md)を参照する。

**[詳細図（全7ページ）](research_automation_diagrams.md)**：配置・保存領域、受付/承認、実験/回収、
状態遷移、権限/認証、結果PR、GPU/資源を図で確認できる。
[編集用draw.ioファイル](diagrams/research_automation/research_automation.drawio)とSVGプレビューを同梱する。

<details>
<summary>目次（全20節）</summary>

- [1. 目的と用語](#1-目的と用語)
- [2. 全体構成](#2-全体構成)
- [3. 役割・権限・信頼境界](#3-役割権限信頼境界)
- [4. Issueから完了までの処理](#4-issueから完了までの処理)
- [5. Issueと承認の契約](#5-issueと承認の契約)
- [6. 実行planの契約](#6-実行planの契約)
- [7. 共通設定と実行先の登録](#7-共通設定と実行先の登録)
- [8. 受付・ON/OFF・状態機械](#8-受付onoff状態機械)
- [9. Codexの起動と実験中の状態](#9-codexの起動と実験中の状態)
- [10. GPUと資源制限](#10-gpuと資源制限)
- [11. 管理側と実験側の通信](#11-管理側と実験側の通信)
- [12. 認証と回収](#12-認証と回収)
- [13. 結果とPR公開](#13-結果とpr公開)
- [14. 永続データと診断](#14-永続データと診断)
- [15. 日常運用と障害対応](#15-日常運用と障害対応)
- [16. 導入・更新・ロールバック](#16-導入更新ロールバック)
- [17. 配置と確認済み範囲（2026-11-15記録）](#17-配置と確認済み範囲2026-11-15記録)
- [18. 独立した補助機能と未対応範囲](#18-独立した補助機能と未対応範囲)
- [19. 実装・試験への対応](#19-実装試験への対応)
- [20. 文書の更新規則](#20-文書の更新規則)

</details>

## 1. 目的と用語

依頼者が目的・完了条件・実行先・予算・公開範囲をIssueに定める。
固定プログラムが承認と実行環境を確認し、実験コンテナ内のCodexへ1件の仕事を渡す。
Codexはコマンドを選び、実験を待ち、結果を解析し、同じセッションで結果PRを作成する。

| 用語 | 意味 |
| --- | --- |
| 管理用コンテナ / controller | runner、manager、固定brokerを収める常駐コンテナ |
| Actions runner | GitHub Actionsの通知配送jobを実行する受付プロセス。別コンテナではない |
| manager | 現行Issue・owner承認・設定・実行台帳を確認する固定プログラム |
| broker | 管理側でDocker操作と実験側との通信を担当する信頼されたプログラム |
| 実験用コンテナ | Issueの登録名から選択する、研究環境を準備済みのコンテナ |
| supervisor | 実験側でjobの子孫・資源・認証コピーを監督するrootプロセス |
| worker | 非root UIDで動くCodexと、そのshell/file tools・子プロセス |
| ハーネス | 承認、準備、起動、監視、回収を行う固定コードの総称。Codexのプロンプトとは別 |
| claim | そのIssueの実行権を一度だけ消費したことを示す永続記録 |
| receipt | Actionsイベントの配送記録。実行承認ではない |
| binding | Issue・承認内容・解決済みplan・runを結ぶ識別情報 |

## 2. 全体構成

```text
GitHub：Issue本文 + owner承認コメント
                 |
          GitHub Actions
                 |
+----------------|-----------------------------------------------+
| Linuxホスト    v                                               |
|                                                               |
| 管理用コンテナ                                                |
| +-----------------------------------------------------------+ |
| | runner / UID10003                                          | |
| |    -> 固定submit client -> Unix socket                     | |
| |    -> manager / UID10001 -> inbox / 承認 / 実行台帳         | |
| |    -> root broker -> 固定したローカルDocker操作             | |
| +----------------------------|------------------------------+ |
|        |                     |                                |
|        v                     v                                |
| 専用永続volume       登録済み実験用コンテナ（再利用）           |
| /control, /runner    +--------------------------------------+ |
|                      | root supervisor                     | |
|                      |    -> Codex / 研究用UID             | |
|                      |       編集 -> GPU/CPU実験 -> 待機   | |
|                      |            -> 解析 -> 結果PR         | |
|                      | workspace / 実験側の私有実行記録     | |
|                      +------------------|-------------------+ |
+-----------------------------------------|---------------------+
                                          v
                                   GitHub：通常の結果PR
```

管理と実験の間は、同じDocker Engineへの固定操作と`docker exec -i`の標準入出力を使う。
SSH、別ホストの制御サーバー、実験操作用の独自MCPは使わない。
GitHubとモデルサービスへの外向き通信は必要であり、「ネットワーク不要」という仕様ではない。

実験コンテナは毎job作り直さない。登録に従って既存コンテナを使うか、
準備済みの固定imageから作成して後続jobにも使う。Codexは実験開始前から存在する固定レシピの
解析役ではなく、実験の実装・起動・待機も担当する。

`execution_container`を持たないCPU依頼は、互換経路として管理コンテナ内のCPU workerを使う。
本書の主経路は実験コンテナ指定の経路であり、旧canaryや隔離再起動型goalの状態機械を混同しない。

## 3. 役割・権限・信頼境界

| 主体 | 所持する権限・状態 | 渡さないもの・制約 |
| --- | --- | --- |
| ホストの運用者 | Docker、実inspect、volumeの専有性、配置と認証の準備 | ホスト管理者は信頼する前提 |
| 管理側PID1 / broker | root、固定runtime、ローカルDocker socket | Issueから任意Docker argvやホストパスを受け取らない |
| manager / UID10001 | `/control`、管理用GitHub読取認証、承認検証、台帳 | モデルの研究コードをroot/managerとして実行しない |
| runner / UID10003 | `/runner`の登録情報、固定イベントclient | `/control`の私有設定・認証・台帳を読ませない |
| 実験側supervisor | UID切替、所有権設定、job子孫の監視・停止 | 既存PID1や無関係な研究プロセスを停止対象にしない |
| Codex / 研究用UID | workspace、通常tools、job用モデル認証、承認済みPR用認証 | 管理volume・管理用GitHub認証・Docker socketを渡さない |

管理側のsocketは`/run/c3-docker.sock`。root所有socketと固定Dockerバイナリを照合し、
UID10001/10002/10003やその他のユーザーへsocketアクセスを許す配置を拒否する。
管理用コンテナにDockerの強い操作権限があること自体は信頼境界に含まれる。
固定brokerのバグやホスト管理者からの隔離を、プロンプトで保証するものではない。

実験側はhost bind、管理volume、Docker socket、host PID/IPC/network、privileged、
検査対象の危険なcapability等を拒否する。新規作成時の設定はハーネスが組み立てる。
既存コンテナの検査を、あらゆる追加設定・ホスト侵害を排除する完全なsandbox証明とは扱わない。

Codexは非root・空の追加group・`no-new-privileges`で起動する。
同じUIDが既に読める秘密や研究ファイルを自動で隠す機能はない。
再利用先は信頼された研究専用環境とし、必要な入力だけを読めるよう運用者が準備する。
`write_scope`は承認・公開ファイルの範囲であり、任意コードの全書込みを閉じ込めるOS境界ではない。

## 4. Issueから完了までの処理

1. 依頼者が目的・完了条件・入力・公開先・実行先・予算を確定する。
2. Issue本文末尾へ正式requestを置き、その内容に結び付いたowner承認コメントを残す。
3. Actionsが固定clientでイベントのreceiptを送る。managerが永続化してからACKする。
4. 実行ONならmanagerが待機イベントを取り出し、現在のIssueと承認をGitHubから再取得する。
5. planのモデルを共通設定から解決し、導入ID・入力source・scope・登録先・資源上限を照合する。
6. brokerが実コンテナを照合し、公開runtime/inputを準備する。実験側supervisorのreadyを確認する。
7. 実行前チェックを通過した依頼だけを管理台帳で一度claimする。
8. bindingとjob用認証を実験側へ渡す。実験側にも排他的なclaimを保存する。
9. 許可GPUを待機・再確認し、認証homeを準備してCodexを起動する。
10. Codexが通常toolsで研究・実験・待機・解析・結果PRを行う。管理側は承認を再確認する。
11. supervisorがjobの子孫を回収し、必要なら同一accountの認証更新を管理側へ返す。
12. 一時認証homeを削除し、回収済みの結果または失敗を返す。管理側が結果を保存する。
13. 管理側のjob回収ACK後にreceiptを処理済みにし、次の待機イベントへ進む。

**配送成功、実行終了、PRの実在、科学的な成功、merge、Slack通知は別の事実である。**
管理側は結果URLの形式とbindingを検査するが、モデルの研究結論を自動で独立検証する審査役ではない。

## 5. Issueと承認の契約

### 5.1 実行候補

対象repositoryは`ken2129/c3_neural_compression`に固定されている。
実行候補はopenのIssueで、タイトルが`[実行承認済み]`で始まり、本文末尾に
`c3-execution-request`のJSONブロックがちょうど1つ存在するものとする。
通常Issue、PR、タイトルだけの候補、期限切れ、取消済み、不正JSONは実行依頼として受理しない。

| requestのフィールド | 契約 |
| --- | --- |
| `schema_version` | `1` |
| `plan_version` | 正の整数。owner記録と一致 |
| `issuer` | owner検証に対応する`ken2129` |
| `approval_ref` | 依頼固有の識別子。owner記録の`request_id`と一致 |
| `expires_at` | UTCの`Z`終端時刻。owner記録と一致し、実行中も有効であること |
| `cancelled` | boolean。`true`は停止側へ分類 |
| `plan` | 第6節の閉じた実行計画 |

Issueのhashは、`number`、`author_id`、`title`、`body`のcanonical JSONに対するSHA256。
`state`と`updated_at`はhashに入れず、別途観測する。
タイトル・本文の変更も承認対象の変更であり、元へ戻せば自動復帰する仕様ではない。

### 5.2 owner承認

信頼するownerはGitHub login `ken2129`、数値ID`55587773`。
本文の自己申告ではなく、GitHubの作者・編集情報を照合する。

| コメントのフィールド | 必要な一致 |
| --- | --- |
| `schema` | `c3-owner-approval-v1` |
| `request_id` | Issueの`approval_ref` |
| `repository` / `issue` | 固定repository / 対象Issue番号 |
| `plan_version` | requestの版 |
| `content_sha256` | 確定したIssue内容のhash |
| `expires_at` | requestの期限 |
| `decision` | 実行は`approve-execution`、取消は`cancel-execution` |

GraphQLの`author`、`editor`、`lastEditedAt`等を使い、未編集かつ非表示になっていないことを確認する。
編集情報欠損を「未編集」と解釈しない。複数承認の曖昧さ、取消、既存承認との差し替え、
読取前後のIssue変更も拒否する。一度固定した承認・失効・変更の観測は台帳に保持する。
既存コメントの編集・削除で履歴を解除せず、再実行のための台帳resetも行わない。

実行中の承認は短いsnapshotで再確認する。再取得周期の基準は5秒で、取得遅延・期限・
時計異常も検査する。取消から停止完了までを厳密に5秒以内と保証するものではない。

## 6. 実行planの契約

### 6.1 必須フィールド

| フィールド | 内容・検査 |
| --- | --- |
| `kind` | `codex-direct-pr-v1` |
| `deployment_id` | 導入profileと一致する32桁hexの識別子 |
| `source_sha` | 運用者が固定した入力commit。profileの`input_source_sha`と一致 |
| `model` | 新規Issueでは省略し共通設定で解決可。明記する場合も共通設定と一致 |
| `execution_container` | 登録先のalias。省略時は互換CPU経路 |
| `goal` | 目的。非空UTF-8、最大4096 bytes |
| `completion_criteria` | 1～16件の重複しない完了条件 |
| `inputs` | 固定sourceから供給する公開ファイル。最大32件 |
| `write_scope` | 変更・公開の承認範囲。1～8件 |
| `resource_budget` | 第10節の5項目。全項目が必要 |
| `max_seconds` | job予算。実験コンテナ経路は30～3600秒、かつ登録上限以下 |
| `sandbox_mode` | `externalSandbox` |
| `external_network` | `enabled` |
| `publish_result_pr` | 実行承認されたplanでは`true` |
| `model_usage_approved` | 実行承認されたplanでは`true` |
| `direct_pr` | 固定repository、同じIssue番号、`main`、固定base SHA、`[Codex] `付きtitle、公開承認 |

planはcanonical JSONで最大16KiB。未知のフィールド、パスの`..`、隠しpath要素、glob、
重複path等を拒否する。モデル解決後のplan全体からdigestを作り、実行bindingへ含める。
入力pathと公開scopeは、導入時の`direct_pr_scope`に**完全一致**する必要がある。
Issueで別のscopeを指定するだけでは、導入済みの入力・公開範囲を拡張できない。

### 6.2 実行しない書式例

以下は構造を示すだけの例。期限切れ・公開/モデル利用未承認・架空の導入/sourceであり、
実行可能な依頼ではない。実行時は具体的条件を確定し、その内容に対するowner記録を別途必要とする。

````markdown
[人向けの目的・条件・停止条件]

```c3-execution-request
{
  "schema_version": 1,
  "plan_version": 1,
  "issuer": "ken2129",
  "approval_ref": "documentation-example-only",
  "expires_at": "2000-01-01T00:00:00Z",
  "cancelled": false,
  "plan": {
    "kind": "codex-direct-pr-v1",
    "deployment_id": "00000000000000000000000000000000",
    "source_sha": "0000000000000000000000000000000000000000",
    "execution_container": "c3-work",
    "publish_result_pr": false,
    "model_usage_approved": false,
    "sandbox_mode": "externalSandbox",
    "external_network": "enabled",
    "goal": "書式例。実行しない",
    "completion_criteria": ["具体的な検証と報告条件を承認前に確定する"],
    "inputs": ["LICENSE", "README.md"],
    "write_scope": ["docs/research/generated/example"],
    "max_seconds": 600,
    "resource_budget": {
      "cpu_seconds_per_process": 120,
      "memory_bytes": 4294967296,
      "max_processes": 128,
      "max_work_bytes": 268435456,
      "max_output_bytes": 1048576
    },
    "direct_pr": {
      "repository": "ken2129/c3_neural_compression",
      "issue_number": 123,
      "base_branch": "main",
      "base_sha": "0000000000000000000000000000000000000000",
      "title": "[Codex] 書式例",
      "public_results_approved": false
    }
  }
}
```
````

### 6.3 実行binding

| フィールド | 対応 |
| --- | --- |
| `issue_number` | 実行対象Issue |
| `issue_content_sha256` | ownerが承認したIssue hash |
| `request_id` | 承認参照 |
| `run_id` | この実行の識別子 |
| `plan_digest` | モデルを含む解決済みplanのSHA256 |
| `source_sha` | 入力source |

実験側のclaim、結果、認証更新、管理側の私有結果は同じbindingへ結び付ける。
別Issue・別plan・別モデルの応答を受け入れない。

## 7. 共通設定と実行先の登録

### 7.1 設定の所在

| 場所 | 役割 | 適用時期 |
| --- | --- | --- |
| `/control/goal-runtime.json` | 実行スイッチ、モデル、source、導入ID、予算、登録先 | `enabled`だけ動的。他は確認と再起動が必要 |
| `/control/resident-intake.json` | 受付ON/OFF、信頼するworkflow SHA | 起動時読込。日常の実行スイッチではない |
| `/control/goal-deployment.json` | 実inspect、volumeの専有性、初期化intent | 配置時に信頼する運用者が作る |
| `/control/state-initialized.json` | 初期化済み状態と管理コンテナIDの対応 | 再配置時に台帳とともに検証 |
| `/control/worker-pr.json` | 公開先、専用actor、認証使用に関する設定 | 準備時。実験・merge承認を代替しない |
| `/control/codex-auth.json` | モデル用の私有認証原本 | 準備・本人による更新、同一accountのrefresh |
| `/control/gh/hosts.yml` | managerのGitHub認証 | 管理側にのみ保持 |
| `/control/worker-github-token` | 承認済みworker用PR認証原本 | jobごとに私有コピーを渡す |

設定・認証はmanager所有の保護されたファイルとして読み、設定例やIssueから上書きしない。
初期配置のprofileは`schema: c3-goal-runtime-v1`、`enabled`、2つのsource SHA、導入ID、
モデル、資源上限、明示した残余リスクの確認を持つ。installerが検証後に
`installed_plan_digest`と、direct-PR用の`direct_pr_scope`を付ける。

`runtime_source_sha`は管理・監督コードの版、`input_source_sha`は研究入力の版。
両者を同じものとみなさない。image IDはさらに別で、実inspectと配置manifestで照合する。

### 7.2 モデルと共通プロンプト

モデル名の設定元は`goal-runtime.json`の`model`1か所。
新規Issueはモデルを省略でき、解決済みplan・worker・結果のモデルはその値と一致させる。
実験コンテナ内へ、手編集する第2のモデル設定は作らない。
推論設定やtool機能をすべてruntime JSONで変更できるという意味ではない。
共通プロンプトとapp-server起動の固定configはコードで規定し、変更時はruntime更新として扱う。
採用モデルは2026-11-15の配置で`gpt-6.1-sol`、CLI固定版は`0.160.0`。
これは本システムの選択であり、Codex製品全体の既定値を述べるものではない。

共通プロンプトは`goal_runner.goal_prompt()`が、目的・完了条件・scope・予算を含むplanから生成する。
日本語の通常PR、再現手順、実施した検証、失敗・制約、私有情報の非公開を求める。
manager認証の利用、承認コメント作成、merge、mainへのpush、権限設定変更等を禁止する指示を含む。
これらの指示だけをOS権限や認証scopeの強制機構とは扱わない。
同じ生成promptをthreadの`developerInstructions`と承認済みturnの入力へ渡す。
RPCで値を複数回渡すことと、手編集する設定元が複数存在することは区別する。

### 7.3 `execution_containers`の登録内容

`docker_control_approved: true`と、1～8件の登録先が必要。
installer側の`--approve-local-docker-control`も必要であり、JSONのフラグだけで権限を設置しない。

| 登録フィールド | 内容 |
| --- | --- |
| alias（mapのkey） | 小文字英字開始、英小文字・数字・`_`・`-`、最大64文字 |
| `name` | 照合するDocker名。登録内で一意 |
| `container_id` | 既存コンテナの64桁hex実ID、または`null` |
| `create_image` | 作成方式では準備済み`sha256:` image ID。既存ID方式では`null` |
| `python` | supervisor用のsystem Python。`/usr/local/bin/python`または`/usr/bin/python3` |
| `codex` | root所有で研究UIDが書き換えられないCodex実行ファイル |
| `worker_uid` / `workspace` | 準備済み研究UIDと専用workspace |
| `cpu_cores` | CPU affinity、新規作成時のCPU割当 |
| `resource_limits` | 指定先の6項目の上限。`max_seconds`とbudget5項目 |
| `gpu_uuids` | 許可GPU。空listはGPUなし。作成方式は初期版で最大1枚 |
| `max_wait_seconds` | GPU空き待ちの上限。job全体予算も超えない |
| `disk_reserve_bytes` | 任意。最低空き容量。省略値0 |
| `sampled_limits_accepted` | 既存コンテナでは`true`が必須 |
| `environment` | 任意の研究環境変数。認証、HOME、Docker、CUDA割当等の保護されたkeyは拒否 |

既存ID方式は名前と実IDの両方を照合し、同名の別コンテナへ追従しない。
作成方式は同名コンテナが存在すれば、導入ID・aliasのラベル、固定image、割当を照合して再利用する。
存在しなければローカルにあるimmutable imageから作成する。pullや任意Dockerfileのbuildはしない。

## 8. 受付・ON/OFF・状態機械

### 8.1 イベント配送

workflowはownerのIssueイベントとIssueコメントイベントを対象とする。
repository名/ID、ownerの数値ID、triggering actor、default branch、契約用repository変数を確認する。
runnerのlabelは`self-hosted`、`linux`、`c3-issue-intake-v1`。labelだけで信頼境界を作るものではない。
jobは固定clientを実行し、任意のIssue本文をshellへ展開しない。

| 項目 | 契約 |
| --- | --- |
| receipt schema | `c3-issue-event-v1` |
| 識別情報 | repository名/ID、Issue番号、event/action、actor ID、comment ID、run ID/attempt、workflow SHA |
| `receipt_id` | `actions:<repository_id>:<run_id>` |
| 同一配送 | `run_attempt`を除いた内容が同じならduplicate。異なるpayloadは拒否 |
| Unix socket | `/control-ipc/events.sock`、Linuxの`SO_PEERCRED`でrunner UIDを照合 |
| ACK | SQLiteの`synchronous=FULL` commit後に`enqueued`または`duplicate` |
| 待機上限 | `PENDING` + `ACTIVE`の合計で既定1024件 |
| フレーム | receipt最大8192 bytes、ACK最大4096 bytes |

Actionsの成功は配送のACKであり、その後の実験成功を示さない。
Issue一覧を巡回・自動再走査するdaemonはない。後からONにしても、未配送イベントを発見する機能はない。
承認コメント等の新しいイベントは別のreceiptになり、同一Issueの実行重複はclaimで防ぐ。

### 8.2 OFFの意味

日常のスイッチは`goal-runtime.json`の`enabled`。

| 状態 | 新規仕事 | 受付・実行中の仕事 |
| --- | --- | --- |
| `false` | 開始しない | 受付設定がONならreceiptは保存する。開始済みjobは継続 |
| `true` | 待機イベントの承認を再確認して開始 | 同時に研究jobを増やさず、直列に処理 |
| 不正・欠損・読取不可 | 開始しない | 設定拒否を記録する |
| `enabled`以外のruntime変更 | 動的変更として受理しない | 進行中の準備でも不一致を拒否。運用確認と再起動が必要 |

OFFの反映は待機中おおむね1秒周期で観測する。
ONからOFFへの切替と開始が重なる場合、実行前チェックで導入・workspace等の検証を通過した
jobは開始済みとして継続する。単なるOFFは取消ではない。
境界の前なら一時領域を回収し、receiptを`PENDING`へ戻してclaimを作らない。
進行中も取消・期限・設定/source不一致・資源・接続の検査は続く。

### 8.3 台帳ごとの状態

```text
配送台帳（inbox）
  PENDING -> ACTIVE -> FINISHED
               |
               +-> PENDING  （開始前のOFF、確認済み起動時の配送lease回復）
  gateあり -> 新規受付・取出しを停止

実行台帳（direct-PR主経路）
  未claim -> CLAIMED -> WORKER_FINISHED
                  |
                  +-> BLOCKED
  既存の実行記録あり -> duplicateとして報告、再実行しない
  起動時に未確定CLAIMEDあり -> inboxをblock、運用者が調査
```

実行の一意性はIssue単位。同じIssueの新イベントや欠けたPRを理由に、もう一度Codexを起動しない。
変更・失効を台帳で固定した依頼は、本文やコメントを元へ戻すだけでは再承認できない。
失敗後の再検証は具体的条件を改めて承認し、新しいIssueを用いる。claimを消して再試行しない。

`WORKER_FINISHED`はworkerの構造化結果を保存した状態であり、`outcome: incomplete`もあり得る。
`pr_verified`と`research_success_verified`をmanagerが自動でtrueへする仕様ではない。
`PUBLISHED`等の別状態は旧経路にも存在するため、全経路共通の単純な成功フラグとみなさない。

## 9. Codexの起動と実験中の状態

supervisorは、root所有の固定CLI版とfreshな認証homeを準備してapp-serverをstdioで起動する。
バージョン確認は別の一時homeで行い、本番認証homeと共有しない。
コンテナやjobの起動時にCodex・研究ライブラリを自動install/updateしない。

`initialize`、`thread/start`、`turn/start`の応答と設定を照合する。
thread作成時はread-only、承認済みturnは`externalSandbox`、`approvalPolicy: never`で開始する。
この`never`は未承認の任意jobを許すフラグではなく、外側のowner承認とハーネスで許可された
1回のturnが、途中の対話的承認を前提にしない設定である。

標準起動ではplugin/app/host skill等の自動導入や発見を抑制し、repository文書の自動命令読込も無効化する。
研究ファイルやtool出力は、実行権限を広げる新しい承認ではない。
共通プロンプト、明示されたplan、固定app-server契約を用いる。

| 設定の層 | 現行の指定 |
| --- | --- |
| CLI起動 | `app-server --listen stdio:// --strict-config`。追加の固定configは`fixed_app_server_request()` |
| 認証方式 | `forced_login_method="chatgpt"`、`cli_auth_credentials_store="file"` |
| tool/命令の固定config | web search、hooks、multi-agent、memories、goals等を無効化。環境の暗黙継承を抑制 |
| thread | モデル・cwd照合、`ephemeral: true`、`project_doc_max_bytes: 0`、read-only |
| turn | 同じモデル・cwd・thread、`externalSandbox`、`networkAccess: enabled`、構造化最終出力 |

`externalSandbox`という指定だけで、Codex側が別の隔離環境やfirewallを構築する仕様ではない。
外側のコンテナ・UID・監視・認証の扱いをハーネスと運用者が用意する。
shell用の固定PATHは`/usr/bin:/bin`であり、研究用Pythonやvenvが自動で選ばれるとは限らない。
依頼の再現手順では、準備済みの科学環境に対応する実行ファイルを明示する。

実験中も同じCodexセッションを保持する。shell commandの完了待ち、ログ確認、修正、解析、
PR作成を同じthread/turnで行い、GPU待ちを理由に解析用の別Codexを呼び直さない。
shell/file toolsを通常利用でき、研究コマンドを1つずつ固定allowlistへ追加する方式ではない。
モデルが実際にどのタイミングでtoolを呼ぶかやトークン量は固定せず、wall time監視とは区別する。
金額のhard capを提供する仕様ではない。

## 10. GPUと資源制限

### 10.1 GPU取得

1. 登録されたGPUについて`nvidia-smi`で利用率・使用メモリ・compute-process数を読む。
2. compute-processなし、利用率5%以下、使用メモリ256MiB以下を3回連続で観測する。
3. 実験側のGPU別ローカルlockを取って再観測し、1枚を選ぶ。
4. `CUDA_VISIBLE_DEVICES`を選択したUUIDに設定し、JAX事前確保を無効化してCodexを起動する。
5. 監視で選択GPUのcompute-processが1件を超えたら競合として自jobを停止する。

観測ループはおおむね1秒間隔で、job予算と`max_wait_seconds`の小さい方まで待つ。
観測・ローカルlockはホスト全体や別コンテナとの排他的なGPU予約ではない。
他者の利用許可を証明せず、他者プロセスを停止しない。
単一compute-processを想定しており、DDPや複数GPUへの自動拡張は対象外。
既存コンテナのGPU露出を環境変数だけで安全に縮小できるとも扱わない。

### 10.2 上限の種類

| 項目 | 実装 |
| --- | --- |
| CPU core | supervisorと子孫のaffinity。新規作成時はDocker CPU quotaも設定 |
| `cpu_seconds_per_process` | 各processの`RLIMIT_CPU`。job全体のCPU時間総和ではない |
| `memory_bytes` | job子孫のRSS総和を観測。新規コンテナにはDocker RAM/swap上限も設定 |
| `max_processes` | 子孫のthread数総和を観測。新規コンテナにはDocker PIDs上限も設定 |
| `max_work_bytes` | workspace開始時からの増分 + 一時homeを観測。単一ファイルには`RLIMIT_FSIZE`も設定 |
| `max_output_bytes` | モデル通信のframe/capture制限。任意ネットワーク通信の総量上限ではない |
| `disk_reserve_bytes` | workspace filesystemの利用可能量を観測 |
| `max_seconds` | GPU待機・Codex作業・解析・公開・回収を含むjob予算。内部に回収用20秒を確保 |

資源のsampleは最短0.5秒間隔。観測には実行・I/O時間もかかり、瞬間超過や絶対peakを防ぐ保証ではない。
再利用先の既存割当を無断で変更せず、`sampled_limits_accepted`を要求する。
回収・通信確認にも有限の余地を設けるため、外側の監視終了時刻をplanの秒数と完全一致するものとは扱わない。

parser上の絶対範囲は次のとおり。これらは利用承認・機器の割当上限ではなく、
実際の依頼は登録先の上限以下でなければならない。

| 項目 | parser範囲 |
| --- | --- |
| `max_seconds` | 1～3600秒。実験コンテナ経路は最低30秒 |
| processごとのCPU秒 | 1～3600秒 |
| RAM | 64MiB～16GiB |
| thread/PID予算 | 1～1024 |
| 作業量 | 1MiB～16GiB |
| 出力capture | 1 byte～16MiB。実験側transportはさらにframe最大64KiB、capture最大1MiBに制限 |

研究workspaceのsymlinkや特殊ファイルは容量走査で拒否する。
Codexの私有runtime homeにできる正常なaliasは、`lstat`でリンク自身のサイズだけを数え、
リンク先をたどらない。runtime内の特殊ファイルは引き続き拒否する。

## 11. 管理側と実験側の通信

外部公開HTTP APIは作らず、brokerと`docker exec`したsupervisorのpipeを用いる。
1行1JSON、実行用frameは最大32768 bytes。未知のkey、順序、binding不一致、EOF、
不正な応答を正常完了として扱わない。認証を含むframeをログやPRへ出さない。

| 方向 | frame | 内容 |
| --- | --- | --- |
| 管理→実験 | `prepare` | 解決済みplan、登録先を解決したpolicy。認証は含めない |
| 実験→管理 | `ready` | 観測したepoch、policy SHA256 |
| 管理→実験 | `execute` | durable claim後のbinding、私有認証コピー |
| 管理→実験 | `lease` | 同じbindingに対する短い継続許可 |
| 管理→実験 | `cancel` | 期限・承認再検証等に失敗したjobの停止 |
| 実験→管理 | `status` | `waiting_gpu`または`codex_running` |
| 実験→管理 | `auth_state` | 同一accountの更新済みモデル認証。私有channelのみ |
| 管理→実験 | `auth_state_saved` | 原本の競合検査後に保存したACK |
| 実験→管理 | `worker_result` | binding、回収済みフラグ、構造化結果 |
| 実験→管理 | `worker_failure` | binding、回収済みフラグ、固定理由`EXECUTION_FAILED` |

prepareのready待ちは30秒。leaseは管理側が約2秒ごとに送り、実験側は10秒の有効窓を確認する。
lease途絶やcancelで自jobを停止する。cancel後の回収確認にも上限があり、確認できなければblockする。
新しいleaseはowner承認・plan・job予算を延長する承認ではない。

## 12. 認証と回収

### 12.1 私有認証

実験側に渡すのはモデル用と承認済みPR用の認証だけ。
PR credentialのactorはjobごとに照合する。モデルはファイル型のChatGPT認証を前提にする。
認証はimage、公開bootstrap、Issue、PR、一般ログへ含めない。

`/work/worker-home`と`/work/codex-home`をjob用に新設し、研究UID所有・私有modeで置く。
以前のhomeが残っていれば上書きせず準備を拒否する。
Codexがモデル認証を更新した場合、記録上の同一identityを確認して管理側へ返す。
管理側原本が開始時から変更されていないことも確かめ、atomic replaceとfsync後にACKする。
identity比較は独自のJWT署名検証ではなく、認証内容からの記録上の同一性比較である。

現在のようにownerのPR credentialを使う設定では、scopeによってowner承認コメントを作成する能力も残る。
`owner_identity_risk_accepted`による明示的な扱いはあるが、技術的に承認権限を完全分離した状態ではない。
公開helperは利便機能であり、credential自身の権限を狭めるsecurity proxyではない。

### 12.2 回収の完了条件

実験側supervisorはLinux subreaperとして、そのjobの子孫を追跡する。
pidfdと開始時刻を使ってPID再利用を検査し、SIGTERMの後、必要ならSIGKILLを送る。
回収処理は約8秒の枠を持ち、最初の約3秒は穏当な終了を試みる。
double-forkやsession離脱も採用した子孫として回収し、`waitpid`のECHILDを必要とする。
元のprocess groupが消えたことだけで回収済みとしない。

子孫停止、認証更新の保存確認、一時home削除を通過して初めて`cleanup_verified: true`を返す。
失敗時は私有情報を残し、実験側`blocked`を立てる。未確定の過去runも次の準備を拒否する。
workspace・既存成果物・実験コンテナPID1・無関係な通知/研究プロセスは通常回収の対象外。

管理側も実験結果とjob回収ACKを確認する。確認不能、永久roleの異常等では
inboxのgateや`resident-blocked`等で新規仕事を止め、必要なら専用管理namespaceを終了する。
管理側の終了だけを、実験側の回収証明とみなさない。

## 13. 結果とPR公開

### 13.1 workerの結果

| フィールド | 契約 |
| --- | --- |
| `binding` | 実行と同じ識別情報 |
| `model` | 解決済みモデルと一致 |
| `outcome` | `completed`または`incomplete` |
| `summary` | 最大4096 UTF-8 bytesのworker申告 |
| `pr_url` | 固定repositoryのPR URL、または`null` |
| `elapsed_seconds` | 非負・有限の経過秒 |

workerは最終JSONで`outcome`、`summary`、`pr_url`を返す。
managerは構造とbindingを保存するが、URLが存在することや研究が正しいことをこのJSONだけで証明しない。
PR公開の照合と、科学的な再現・評価は別に行う。

### 13.2 任意で利用する公開helper

Codexはversion付きruntimeの`worker_pr.py`を利用できる。
manifestは`files`と、日本語の目的・結果・検証・制約等を書く`body`を持つ。
helperは独立した実行承認器や科学的査読者ではない。

| 項目 | 制約 |
| --- | --- |
| 対象 | 固定repository、承認済みbase `main`とbase commit |
| ファイル | 明示した通常ファイルのみ。scope外、symlink、hardlink、特殊ファイルを拒否 |
| 件数・容量 | 最大32件、1件256KiB、合計1MiB |
| description | 非空、UTF-8最大16KiB。予約したidentity markerの偽装を拒否 |
| branch | `research/codex-<issue>-<digest先頭16桁>` |
| identity | 解決済みplan、description、ソート済みpath/mode/blob SHAからdigestを作る |
| intent | 最初のremote mutation前に私有intentを排他的に作成し、fileと親directoryをfsync |
| 作成 | blob、tree、単一parent commit、branch、通常PR。mergeしない |
| 照合 | actor、title/body、head/base、親、差分、完全tree、blob/mode、draft=false等 |
| 再呼出し | 既存intent/branch/PRを読み取り照合。remote mutationを再実行しない |

GitHubがcommit本文の末尾LF1文字を除いて返す場合だけ、元の完全一致に加えて受理する。
余計なLF、空白、CR、他の本文差は拒否する。PR本文そのものの一致検査は緩和しない。
branchだけある・公開応答が不明・PRが見えない場合も、勝手に別branch/PRを作成しない。
通知失敗や公開の不確実性を理由に研究を繰り返さない。

## 14. 永続データと診断

```text
管理用の専用volume /control
  goal-runtime.json / resident-intake.json / goal-deployment.json
  state-initialized.json
  codex-auth.json / gh/hosts.yml / worker-github-token / worker-pr.json
  inbox.sqlite                 配送台帳とgate
  state/
    state.sqlite               実行・承認・epochの台帳
    initialized.json           状態領域の初期化記録
    claims/<issue>.json        排他的なclaim witness
  execution-results/<run>.json 実験結果・回収の私有記録

runner専用volume /runner
  runner本体 / 登録情報 / 私有診断

実験用コンテナ
  /research/c3                 登録済みworkspace（配置例）
  /opt/c3-execution/<runtime-sha>-<input-digest>/
                               root所有の公開bootstrap / 公開input
  /var/lib/c3-execution/
    active.lock / <gpu-uuid>.lock
    runs/<run>.json            claimと私有完了記録
    blocked                    回収等が不明なときの永続停止
  /work/worker-home / /work/codex-home
                               job限定。一時認証を含み、通常完了時に削除
```

`state.sqlite`はintake、owner receipt、実行binding/result、epoch等を保持する。
claim witnessとDBのbindingが一致しない場合や初期化済み状態が欠ける場合に、空の状態として作り直さない。
inboxの配送lease回復は実行claimの回復・消去とは別である。

入力は固定sourceとmanifestのsize/hashを照合する。
再利用workspaceに同じ入力があればbytesまで一致を要求し、異なっていれば上書きしない。
bootstrapはruntime SHAとinput-manifest digestごとに配置する。
途中のinstall、既存markerとの不一致を自動上書きして修復しない。

私有の失敗記録には固定理由・protocol段階・elapsed等だけを残す。
raw protocol、stderr、例外本文、prompt、認証を記録するdebug経路は設けない。
公開結果にも私有診断を追加しない。thread/turn、GPU UUID、内部path等の運用メタデータは
私有実行記録として扱い、必要性を確認せずPRへ転記しない。

## 15. 日常運用と障害対応

### 15.1 状態確認

次の順で見る。Dockerが`Up`、runnerが`online`というだけでは研究の正常完了を示さない。

1. 実コンテナID・image/runtime版・restart countを確認する。
2. `goal-runtime.json`の`enabled`とmanagerの最新状態を確認する。
3. inboxの待機/処理中/完了と、実行台帳のclaim/結果を区別する。
4. 実験側のactive lock、run、認証home、blocked、無関係なプロセスを確認する。
5. 結果PRの実在・head/base・公開内容を確認し、研究の検証内容を読む。

Macに`leo`のDocker contextがある場合の、値を限定した読み取り例：

```sh
docker --context leo inspect uchishiba-c3-native-20261111-controller-1 \
  --format '{{.State.Status}} restart={{.RestartCount}} image={{.Image}}'
docker --context leo exec --user 10001:10001 \
  uchishiba-c3-native-20261111-controller-1 python -I -B -c \
  'import json,pathlib; p=json.loads(pathlib.Path("/control/goal-runtime.json").read_text()); print({k:p[k] for k in ("enabled","model","runtime_source_sha","input_source_sha")})'
```

profileや認証ディレクトリ全体のcat、raw診断ログの公開を状態確認手順にしない。

### 15.2 実行スイッチの変更

manager UIDで`goal-runtime.json`を読み、**`enabled`だけ**を変更する。
同じ`/control`内に一時ファイルを作り、owner/group `10001:10001`・mode `0600`を維持し、
書込みとfsync、atomic replace、親directoryのfsyncを行う。
部分書込みや複数運用者の同時編集を避け、保存後に設定値とmanager状態を確認する。

ONにする前には、待機中の既存receipt、承認期限、未確定claim、実験先と資源を確認する。
ONは特定のIssue1件だけを指定するスイッチではなく、待機中の有効な依頼を順に処理可能にする。
通常のOFFは緊急停止ではない。実行中の取消には正式な取消経路を使い、回収完了を別途確認する。

### 15.3 障害時の判断

| 観測 | 動作・調査先 | 行わないこと |
| --- | --- | --- |
| runner offline / Actions queued | 登録、固定版、外向き接続、session競合、GitHub側配送を確認 | モデル/GPUが実行済みと決め付けない |
| Actions成功、inbox待機、実行OFF | 正常な待機。必要ならowner条件を確認してON | スイッチを無断でONにしない |
| 未配送のIssue | 正式イベントの配送を調査。受付は一覧走査しない | receiptを捏造してE2E成功としない |
| 設定拒否・未登録alias | 固定profileと実配置を確認 | IssueでDocker IDや権限を差し替えない |
| 承認変更・期限切れ・取消 | job停止側へ進み、記録を保持 | コメント削除やDB編集で解除しない |
| GPU空き待ち時間切れ | 自jobの失敗として回収・記録 | 他者job停止、GPU追加、無制限retryをしない |
| worker失敗、回収確認済み | BLOCKEDと私有の固定診断を読む | 同じclaimをリセットしない |
| EOF・lease途絶・回収不明 | 実験側状態と管理側gateを調査し、新規実行を止める | 管理コンテナ再起動だけで回収済みとしない |
| `CLAIMED`が再起動後も残る | 起動時gateで停止。実行の有無・回収・結果を調査 | 自動でPENDINGへ戻さない |
| PR作成応答が不明 | 既存intent・branch・PRを読み取り照合 | 新しいPR作成や研究再実行をしない |
| 認証失効 | 本人の通常の認証手順で更新し、同一identity・私有modeを確認 | 別のcredentialを自動探索・流用しない |

## 16. 導入・更新・ロールバック

標準の`tools/research_resident/prepare.py`は、新しい導入ID・専用control/runner volumeで
停止状態の管理コンテナを作る。実inspectとvolumeの専有性を検査し、私有設定をコピーして照合する。
自動で旧環境を停止せず、runner登録・repository変数ON・実験開始も行わない。
実験用imageの研究依存、CLI固定版、UID、必要データは先に準備する。

Docker操作を有効にする場合だけexecution用composeと明示opt-inでsocketを追加する。
コードのmergeだけでは配置・認証移送・実行ONは起きない。
ホストへの常駐サービスや設定ファイル追加を導入要件にしない。

2026-11-15には、信頼する運用操作として同じ専用volumeを保持した管理image更新を行った。
これは一般化された更新CLIが実装済みであることを意味しない。
同じvolumeを別コンテナへ接続するだけでは、IDに結び付く配置manifest/初期化記録との整合が取れない。

更新を行う場合の確認事項：

1. 具体的な旧/新runtime・image・管理対象を固定し、OFF・実行中claimなし・回収済みを確認する。
2. 旧inspect/profile/manifest/初期化済みmarkerと、台帳・claim・runner登録の整合情報を私有保存する。
3. 専用管理の旧コンテナだけを停止し、旧imageと永続volumeを保持する。
4. 新コンテナの実inspectとvolume専有性を検証し、実IDに合わせて信頼する配置記録を更新する。
5. SQLite・claim・owner記録・登録情報を初期化せず、更新前後の一致を確認する。
6. OFFで起動し、READY、設定、履歴、runner再接続、回収状態を確認する。

失敗時は新規実行を止めたまま原因を調べる。旧imageへ戻す場合も、再作成後の実IDで配置を検証する。
旧IDのmarkerを無条件に復元したり、古いDBへ戻して最新claimを消したりしない。
rollbackで過去の実験が未実行になるわけではない。実験用workspaceはsnapshot/復元機能で自動巻戻ししない。

## 17. 配置と確認済み範囲（2026-11-15記録）

### 17.1 leoでの配置例

| 項目 | 記録された値 |
| --- | --- |
| 管理用 | `uchishiba-c3-native-20261111-controller-1` |
| 実験用 | `uchishiba-c3-experiment` |
| Issueでのalias | `c3-work`。Docker名`uchishiba-c3-work`を意味するものではない |
| 実験workspace / UID | `/research/c3` / `10002` |
| 管理runtime | `a6a19aeca812f7a50d93b37daabb81977921631b` |
| 実GPU E2E時のruntime | `d1c30c40a3e8b069ade70495d55a0edf00a70fa0` |
| 固定入力source | `6fd11016d64f88721eb08caff2bb8b0988f3d847` |
| モデル / CLI / runner | `gpt-6.1-sol` / `0.160.0` / `2.338.1` |
| 管理割当 | 1CPU・1GiB・64PID、read-only root、専用volume2個 |
| 実験割当 | 2CPU・4GiB・128PID、GPU1枚 |
| 当該job予算 | 600秒、process CPU120秒、作業増分256MiB、出力1MiB |
| 入力 / 公開scope | LICENSE・README / `docs/research/generated/native-container-e2e` |
| 実行スイッチ | `enabled: false`。受付の初期設定は有効 |

これらは配置例であり、別マシンへの既定値でも追加実験の承認でもない。
物理ID・登録名の解決は保護されたprofileと実inspectで行う。
既存の研究用コンテナや旧controllerが別に存在しても、この経路の2コンテナへ自動で組み込まれない。

### 17.2 実機記録

Issue111/112は各1回の実行が`AppServerBlocked`で停止し、回収済み・BLOCKEDを保持した。
実Codexのモデルなし診断で、正常なruntimeリンクを旧容量監視が拒否する実装バグを再現・修正した。
修正後のIssue113で、実配送・正式承認・1回のclaim・実験側Codex・GPU計算・解析・通常PR114・回収まで到達した。
同一thread/turnを1組記録し、全体185.38秒、32×32行列積のGPU計算は1回だった。

公開helperの最終照合は、この実行ではcommit末尾LF差で失敗した。
外側が実PRを読み取り照合し、追加修正を配置後、同じPR114に対する正式plan/publication digestを再構築した
修正版helperもGET9回・mutation0回で`PUBLISHED`を確認した。
追加のモデルturn・GPU実験・新規PR作成E2Eは行っていない。
認証の再ログイン・ON/OFF・配送待ち・修正配置に運用者の介入があったことも記録に含める。

| 確認済み | そこからは主張しないこと |
| --- | --- |
| 単GPUの合成入力1組と結果PR | C3学習・圧縮品質・AP・速度の評価 |
| 同じセッションでの研究処理と回収 | 長時間・多数job・完全無介入の運用実績 |
| 修正版の既存PR読み取り照合 | 追加修正後の新規PR作成E2Eを再実行した実績 |
| Linux fixtureの契約・取消・重複・回収試験 | 全GPU/ドライバ/マシンでの動作保証 |

最終コードのCPU回帰試験は通常/`-O`とも全1200件、各1191成功・9skip・失敗なし。
実UID/回収用の7件は直前の起動監視修正版で別のcapability付きCPU環境でも成功し、残る2件は既存環境条件。
詳細な版・試験条件・失敗を含む証拠は[実GPU E2E記録](research/2026-11-15_native_container_e2e.md)に保持する。

## 18. 独立した補助機能と未対応範囲

GPU空きSlack通知は、既存の作業コンテナ内の独立した補助プロセスであり、managerの機能ではない。
記録上の通知条件は60秒周期、compute-processなし・利用率5%以下・256MiB以下が3分継続で1回通知して終了。
第10節の実行時GPU待機の「3回観測」とは別の条件である。
GPU予約・実験起動・controllerのONは行わず、再起動時の自動再開も統合されていない。
Webhookの値を仕様書やIssueへ保存しない。

現在の主な未対応・運用前提：

- 複数マシンへの配送、集中scheduler、他管理系と協調したGPU予約。
- Issueからの任意image build/pull、未登録コンテナの選択、依存の自動install。
- 複数GPU/DDP、既存コンテナ全体へのjob単位hard cgroupの自動設定。
- Issueタイトル変更だけでの実行、承認と無関係な自動retry・claim reset。
- 任意のmodel/provider/CLI版への互換保証。モデル名の設定可能性と互換性を区別する。
- 科学的成功の自動独立審査、金額hard cap、結果PRの自動merge。
- 一般化された既存volume更新/移行/rollback CLI、データ全体の自動backup/restore。
- 生の外向き通信内容や同一UIDの全秘密の隔離。研究UIDには承認済みの認証・bridge egressがある。

汎用化は[Issue110](https://github.com/ken2129/c3_neural_compression/issues/110)で追跡し、
研究本体の条件と予算は[Issue24](https://github.com/ken2129/c3_neural_compression/issues/24)等で別途確定する。

## 19. 実装・試験への対応

本書と実装に差が出た場合、以下の固定コード・試験・実機記録を照合し、仕様変更なのか不具合なのかを明記する。
過去段階の文書冒頭にある「まだ実行しない」「未配置」は、当時のscopeを示す記録の場合がある。

| 仕様 | 実装・根拠 | 主な試験 |
| --- | --- | --- |
| Issue/hash/変更検出 | [issue_intake.py](../research_harness/issue_intake.py) | [intake試験](../tests/test_harness_issue_intake.py) |
| owner承認・取消 | [owner_approval.py](../research_harness/owner_approval.py)・[service.py](../research_harness/service.py) | [owner試験](../tests/test_harness_owner_approval.py) |
| Actions配送・永続inbox | [workflow](../.github/workflows/research-issue-intake.yml)・[actions_inbox.py](../research_harness/actions_inbox.py) | [inbox試験](../tests/test_harness_actions_inbox.py) |
| ON/OFF・常駐job | [resident launcher](../tools/research_resident/launcher.py) | [switch](../tests/test_harness_resident_switch.py)・[開始境界](../tests/test_harness_resident_admission_switch.py) |
| plan・モデル・scope | [goal_plan.py](../research_harness/goal_plan.py) | [plan試験](../tests/test_harness_goal_plan.py) |
| 単回claim・結果 | [direct_pr_service.py](../research_harness/direct_pr_service.py)・[service_state.py](../research_harness/service_state.py) | [direct PR試験](../tests/test_harness_direct_pr.py) |
| 登録・Docker境界 | [execution_contract.py](../research_harness/execution_contract.py)・[execution_docker.py](../research_harness/execution_docker.py) | [execution試験](../tests/test_harness_execution.py) |
| prepare/lease/認証保存 | [execution_delivery.py](../research_harness/execution_delivery.py) | [delivery試験](../tests/test_harness_execution_delivery.py) |
| GPU/監視/回収 | [execution_worker.py](../research_harness/execution_worker.py)・[execution_process.py](../research_harness/execution_process.py) | [worker試験](../tests/test_harness_execution_worker.py) |
| Codex・共通プロンプト | [goal_runner.py](../research_harness/goal_runner.py)・[app_server_adapter.py](../research_harness/app_server_adapter.py) | [worker試験](../tests/test_harness_execution_worker.py)ほか |
| 公開intent・PR照合 | [worker_pr.py](../research_harness/worker_pr.py) | [公開helper試験](../tests/test_harness_worker_pr.py) |
| 導入・配置検証 | [prepare.py](../tools/research_resident/prepare.py)・[resident_deployment.py](../research_harness/resident_deployment.py) | [setup](../tests/test_harness_execution_setup.py)・[配置試験](../tests/test_harness_resident_deployment.py) |
| 実機の成功・失敗・限界 | [2026-11-15 E2E記録](research/2026-11-15_native_container_e2e.md) | [結果PR114](https://github.com/ken2129/c3_neural_compression/pull/114) |

## 20. 文書の更新規則

契約フィールド・状態遷移・権限・予算・認証ライフサイクルを変えるPRは、対応する節と根拠を更新する。
新しい実機確認は日付・runtime/input/imageの版・実行上限・介入・失敗・未確認事項を研究記録へ保存する。
配置例を更新するときは、モデルやcontainer名だけでなく、実装と配置済みの差も確認する。
仕様書のmergeや書式例は、新しいGPU実験・権限追加・認証移送・ON操作の承認にはならない。
