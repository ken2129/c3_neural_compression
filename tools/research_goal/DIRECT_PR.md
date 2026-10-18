# Codex が実装から通常 PR まで担当する CPU 経路

新しく準備する goal 実行では、この **opt-in の direct PR 経路**を推奨します。
既定は OFF です。既存 deployment を切り替えたり、既存の認証を自動流用したりする変更ではありません。

Codex が承認された目的の範囲で、実装・CPU 実験・テスト・結果の記録・branch の push・通常 PR 作成を担当します。
manager は本人承認の確認、永続的な単回 claim、資源上限の監督、所有を確認した自分のジョブの停止に集中します。
コマンドを一つずつ固定 allowlist に登録する方式や、manager が候補の内容を別途承認して公開する方式ではありません。
公開してよい入力と成果物の範囲を、実行前に本人が明示して承認する必要があります。

**検証範囲:** この変更のローカル試験は、モデルなしの fixture と契約・拒否条件の検証です。
新経路の credential 設定、GitHub の保護設定変更、deployment、実物 Codex／実モデルによる実行、実 PR 作成、
webhook の疎通は実施していません。GPU はこの経路の対象外です。
旧経路の実機診断や PR の結果を、新経路の動作実証には使いません。

## 1. 役割と、残す境界

1. 本人が公開してよい目的・完了条件・入力・書込範囲・予算・PR の対象を承認する。
2. manager が GitHub 上の owner receipt と最新の依頼を確認し、installation の許可範囲と照合する。
3. manager が永続的に一度だけ claim してから、worker に実行を渡す。
4. Codex が許可範囲内で方法を選び、実装・実験・テスト・報告を進め、専用 branch と通常 PR を作る。
5. manager は実行中の承認再確認、資源制限、停止を担当する。結果は実際の GitHub PR と照合して確認する。
6. 本人が PR をレビューする。実行承認も PR 作成承認も、merge 承認にはならない。

ユーザー専用 container の manager は UID `10001`、worker は UID `10002` で分離します。
管理台帳・manager 用認証・保護設定を worker に渡しません。別 tmux や同一 UID だけを分離とは扱いません。
worker に渡すのは、この新しい用途のために別途承認・用意した認証です。
ホストの他者ジョブを停止する権限は与えず、共有 GPU の空き表示だけを使用許可と解釈しません。

PID namespace の終了と再起動は、子孫プロセスを残さないための hard cleanup です。
**direct PR では、再起動を成果物の公開ゲートにしません。** PR 作成は worker の実行中に起こり得ます。
失敗・取消・通信断のあとで、すでに行われた push や PR 作成が取り消されたとは扱いません。
単回 claim は失敗しても消去せず、同じ承認を使って勝手に再実行しません。
外部 write の成否が不明な場合は、実際の branch／PR を確認してから復旧方針を決めます。

### 既存 PR #71 系の経路は残す

[`README.md`](README.md) の `codex-goal-v1` は、従来どおり private 候補を保存し、
新しい namespace を確認してから別の内容承認を受け、manager が公開する経路です。
その private copy、内容承認、publication journal を direct PR の設定で無効化しません。
既存 canary の container・volume・履歴・認証も変更しません。
既存 installation を新経路に読み替えず、用途に合う方を明示して準備してください。

## 2. GitHub の名義と、受け入れるリスク

本人の方針により、worker は同じ `ken2129`（固定ユーザー ID `55587773`）の名義でも使えます。
新しい account や GitHub App は必須ではありません。
設定の `owner_identity_risk_accepted` は厳密な boolean で、既定は `false` です。
同じ名義を有効にするには `actor_login: "ken2129"`、`actor_id: 55587773` を指定し、
`owner_identity_risk_accepted` と既存の 3 consent flags をすべて `true` にして、別途 `enabled: true` にします。

現在の owner-comment 承認は GitHub の名義を信頼します。同じ名義の token を worker が使うと、
**本人が書いた承認コメントと worker が作った承認コメントを区別できず、worker 自身が owner 承認を作成できます。**
`owner_identity_risk_accepted` はこのリスクを明示して受け入れる設定であり、承認の偽造防止を証明するものではありません。
OS の UID 分離や manager の保護を残しても、この GitHub identity の問題は解消しません。

同じ名義でも、対象 repository を絞った worker 専用 token を別途承認・用意します。
既存 manager token を暗黙にコピー・流用しません。この変更では account 作成・ログイン・token 発行をしていません。
新しい persistent credential access を明示的に承認し、安全な経路で用意してください。
token を Issue、PR、チャット、plan、profile、ログへ貼らないでください。
protected config は `/control/worker-pr.json`、専用 token は `/control/worker-github-token` に分けます。
worker が自身の token を読めることと、外向き通信があることも、明示的に受け入れる残余リスクです。

### main と merge の制限は実際の GitHub 設定で確認する

repository と権限を絞った token、worker helper、「merge しない」という prompt だけでは、
main への直接 write や merge を技術的に禁止したことにはなりません。
有効化前に branch protection／rulesets と token の権限を確認し、直接 push・force push・削除、
bypass、merge、保護設定の変更のうち、実際に何が禁止され、何が可能かを記録してください。

同じ `ken2129` が作成した PR を `ken2129` 自身が approve することはできません。
したがって「同じ名義で PR を作り、同じ owner の required review を必須にする」という条件は使いません。
本人が内容を確認して手動 merge する運用は選べますが、これは運用上の約束です。
GitHub に同じ identity として見える操作を、本人と worker に暗号学的に区別させることはできません。
本人には merge を許しながら同じ名義の worker だけを技術的に禁止できる、と説明しないでください。

`protected_branches_confirmed` は実際の保護設定と残る権限を確認した記録です。
すべての write 経路を安全に制限した証明ではありません。
必要とする技術的制限が設けられない場合は、その制約と残余リスクを本人へ示し、具体的な判断を受けてください。
権限・credential・保護設定の追加や変更は、この文書の存在だけでは承認されません。

## 3. goal と installation の範囲

新しい plan kind は `codex-direct-pr-v1` です。既存 goal plan の各項目に `direct_pr` を追加します。
必須の共通項目は次のとおりです。

- `deployment_id`: 新しい installation を示す公開 ID
- `source_sha`: installation が許可する、40 桁の固定 source commit
- `publish_result_pr: true`
- `model`: 明示的な固定モデル ID。`-latest` は使わない
- `sandbox_mode: "externalSandbox"` と `external_network: "enabled"`
- `model_usage_approved: true`
- `max_seconds` と `resource_budget`
- `goal`, `completion_criteria`, `inputs`, `write_scope`

`direct_pr` は次の値を、本人承認を受けた実値で指定します。
以下は項目の説明であり、実行や公開の承認ではありません。

| 項目 | 制約・意味 |
| --- | --- |
| `repository` | `ken2129/c3_neural_compression` |
| `issue_number` | この実行依頼の正の Issue 番号 |
| `base_branch` | `main` |
| `base_sha` | 承認時に確認した 40 桁の base commit。`source_sha` と同じ値 |
| `title` | `[Codex]` から始まる通常 PR のタイトル |
| `public_results_approved` | 公開する成果物の範囲を本人が承認した場合のみ `true` |

研究入力と PR の起点は同じ revision に固定し、`base_sha == source_sha` を必須にします。
main が進んでも、承認した base を勝手に置換したり、自動 rebase したりしません。
公開前の base 変更は停止・再確認し、PR 作成後の更新や競合はレビューで扱います。

書込範囲や入力 path の文字列検査だけでは、OS の書込境界や公開内容の安全性は保証できません。
公開 repository に出せない入力、秘密、個人情報を含む task はこの公開許可へ混ぜないでください。
実行後の candidate-content 承認を待たずに Codex が公開できる点が、旧経路からの重要な変更です。

### 毎回 container を作り直す必要のない範囲

installation は固定の source・入力・書込 scope・モデル・資源上限・repository／actor の許可範囲を持ちます。
入力 path と `write_scope` のリストは installation と一致させます。
その範囲内なら、異なる目的や完了条件を持つ複数の goal を、それぞれの本人承認と単回 claim で受け付けられます。
一つの goal 全体の digest を installation の唯一の task として固定する旧方式とは区別します。

これは自由な権限拡大ではありません。別 source、未導入の入力、許可外の書込先、資源上限超過、
別 repository／actor を使う場合は、許可範囲と必要な設定の見直し・新しい承認が必要です。
失敗した claim の再利用や、期限切れ receipt の復活も認めません。

## 4. 準備と本人承認

### 無効な template の生成だけを先に行う

次の三つのコマンドは設定例を出力するだけです。認証取得、Docker 操作、GitHub 操作、モデル実行は行いません。
未使用の作業ディレクトリで、既存ファイルを上書きしないように実行してください。

```sh
python3 -B tools/research_goal/prepare.py --template > goal-profile.json
python3 -B tools/research_goal/prepare.py --worker-pr-template > worker-pr.json
python3 -B tools/research_goal/prepare.py --direct-plan-template > goal-plan.json
```

profile と worker 設定は `enabled: false`、plan はモデル利用・公開の承認が false です。worker 設定の `actor_login` と `actor_id` は未設定で、
以下の 4 項目も `false` になっています。

- `credential_access_approved`: 専用 worker credential の継続利用を本人が承認した
- `repository_scope_confirmed`: 対象 repository と必要な access scope を確認した
- `protected_branches_confirmed`: worker に適用される GitHub 側の保護設定と残る権限を確認した
- `owner_identity_risk_accepted`: 同じ owner 名義では worker が owner 承認を作成できるリスクを受け入れた

`repository` は `ken2129/c3_neural_compression` に固定します。
`actor_login` と `actor_id` は実際の専用 credential の identity に一致させます。
installer と起動時の staging は、明示された token による読取専用の `/user` 照会で identity を確認します。
同じ名義を選んでも `/user` と設定の login／ID の一致を省略しません。
別 actor を設定したまま owner token を渡すことはできません。

従来の goal profile にある 5 項目の `risk_acknowledgements` も必要です。
任意 CPU コマンド、worker が自身のモデル認証を読めること、制限のない bridge egress、
runtime home と private IPC への書込、利用料金のハード上限がないことを確認します。
これに加えて worker の GitHub write と事前承認済み成果物の公開が可能になります。
設定の `true` は本人確認を省くための値ではなく、GitHub の権限検証の証明でもありません。

`goal-profile.json` には、新しい `deployment_id`、確認済みの入力／runtime commit、固定モデル、
本人が承認した wall／CPU／memory／PID／work／output の上限を入れます。
初版の aggregate allocation の上限は 1 GiB memory、64 PID、256 MiB work です。
plan の aggregate 予算は実際に作る allocation と一致させます。
source と runtime は別に指定できますが、`plan.source_sha` と `direct_pr.base_sha` は同じ入力 commit です。
初回 `goal-plan.json` は、この文書の `codex-direct-pr-v1` plan オブジェクトそのものです。

### 必要な承認を得てから新 installation を作る

本番の設定・起動を代理で行う場合は、新 container／volume／network と credential の永続利用を含む必要な承認を先に得ます。
本人が安全な経路で準備した private regular file（mode `0600`、hardlink なし）を指定します。
`auth.json` は worker のモデル用、`hosts.yml` は manager の GitHub 読取用、
`worker-token` は承認した actor の専用 GitHub token です。同じ owner 名義も前述の明示 opt-in で選べます。
既存 manager token を自動コピーせず、worker 用に別途承認・用意した token を指定します。

以下は承認後に本人が実行する形式です。placeholder を実値に置き換えたこと自体は承認ではありません。
clean checkout の runtime commit を確認し、新しい project 名と template から生成した deployment ID を使います。

```sh
python3 -B tools/research_goal/prepare.py \
  --approve-new-goal-deployment \
  --project c3-direct-pr-review-001 \
  --profile goal-profile.json --plan goal-plan.json \
  --codex-auth-file /PRIVATE/PATH/auth.json \
  --github-hosts-file /PRIVATE/PATH/hosts.yml \
  --approve-worker-github-access \
  --worker-pr-config worker-pr.json \
  --worker-github-token-file /PRIVATE/PATH/worker-token
```

モードは plan kind で選びます。direct PR では worker 用の追加 3 flags がすべて必要です。
旧 plan へ worker credential を添付しても受け付けません。
新しい volume に `/control/worker-pr.json` と `/control/worker-github-token` を manager-owned で保存し、
起動時の検証後、worker 用の保護されたファイルとして staging します。
ambient HOME、環境変数、既存 container から token を探しません。
既存 deployment への自動 fallback や、既存 volume の初期化は行いません。

goal は、[Issue 受付の形式](../../docs/research_issue_intake.md)の `c3-execution-request` に含めます。
[本人承認コメントの手順](../../docs/research_owner_approval.md)で、確定した依頼内容と期限に結び付いた承認を用意します。
これらの文書には初期実装当時の「実行不可」という検証記録も含まれます。
新経路で実行可能になるのは、新しい opt-in 設定・runtime admission・最新の本人承認がすべて満たされた場合だけです。
タイトル接頭辞、Issue の作者、本文の自己申告だけでは実行しません。

まず公開入力だけを使う小さな CPU task で、受付、承認、単回 claim、UID／資源境界、
実モデル接続、テスト、通常 PR、停止、通知を個別に確認してください。
この変更のモデルなしテストが通っても、その実機確認を済ませたことにはなりません。

### 任意の worker-side PR helper

Codex は固定の helper `/opt/service/worker_pr.py` を使うこともできます。
これは worker が実行する公開用の補助ツールで、manager の publication gate ではありません。
承認の真正性を独自に証明せず、worker が token を別の方法で使うことを制限する security boundary でもありません。

manifest は `{"files": ["承認済みscope内の相対path", "同じscope内の報告書path"], "body": "日本語のPR説明"}` です。
実装・テスト・結果報告を含め、公開する exact files と本文を worker が選びます。
`body` は空でない UTF-8 テキスト（最大 16 KiB）で、目的、変更・結果、実際のテスト、未検証事項、
公開する報告書・テスト成果物へのリンクを日本語でまとめます。見出しや文言の固定 schema はありません。
承認された公開情報だけを使い、秘密や非公開ログを転記しません。型・サイズ等の検証は、秘密がないことや
内容の正しさを証明するものではありません。Issue URL と helper の識別 marker は末尾に別途付加されます。
古い `files` だけの manifest は拒否します。
承認済み worker 内での形式は次のとおりです。

```sh
python3 -B /opt/service/worker_pr.py publish \
  --approved-plan /work/worker-home/approved-plan.json \
  --manifest /work/worker-home/pr-files.json \
  --intent /work/worker-home/pr-intent.json
```

helper は manifest 内の regular file の追加・更新だけを扱い、削除は扱いません。
最大 32 files、1 file 256 KiB、合計 1 MiB で、symlink・scope 外 path を拒否します。
固定 base から専用 branch と通常 PR（`draft: false`）を作ります。公開前に main が承認した base から動いていれば停止します。
intent を保存してから remote mutation に進み、その後の再呼出しは読取による reconciliation だけです。
本文も publication digest に含まれ、保存済み intent と異なる本文で再送しません。
remote の本文が人に編集されていれば不一致として止まり、上書き・復元や別 PR の作成はしません。
通常 PR として直接作成するため、途中で人が ready 化する手順は不要です。
既存の draft PR や説明のない PR を、この更新が自動で変更することはありません。
成否不明や途中失敗で intent を消して再送せず、branch／PR の実態を確認してください。
これらは helper の性質であり、worker 全体の arbitrary command や token の権限を制限するものではありません。

## 5. PR と通知を確認する

PR は `ken2129/c3_neural_compression` の `main` を対象とし、タイトルを `[Codex]` で始めます。
結果の確認では、PR の実在、作者、head commit、base、diff、テスト結果、未完了事項を確認してください。
失敗した実験や未実施の確認を、成功したように報告しません。merge は本人の別判断です。

現在の親側 PR 通知条件は、repository が `ken2129/c3_neural_compression`、
author が `ken2129`、タイトルが `^\[Codex\]`、event が opened です。
同じ `ken2129` 名義なら現在の author filter に一致します。
別 actor を選ぶ場合だけ、その actor に合わせて author filter を変更します。
PR が作成できたこと、通知の設定が保存されたこと、実イベントが dot へ届いたことは、それぞれ別に確認します。
この変更では通知設定の更新も実 webhook の疎通試験も行っていません。

### 実環境 E2E の確認範囲（別途承認して1回だけ）

次の意味のある研究作業を1件選び、dot が実行 Issue と本人承認を用意してから、
通常 manager が読み取って実行する経路を使います。診断の合成 Issue/承認は代用しません。
同じ計算の再実行や既存成果 PR の再公開を検証目的だけで行いません。

合格条件は、途中で人が本文を追加・ready 化・台帳修復・再実行をせず、以下が揃うことです。

1. 本人承認した Issue が1回だけ claim される。
2. Codex が承認範囲の成果物とテスト結果を作り、説明のある通常 PR を最大1件作る。
3. 管理台帳の `WORKER_FINISHED`、`outcome: completed`、PR URL を実 GitHub と照合する。
4. 実際の PR opened event が dot に届き、dot が結果と未検証事項を報告する。
5. 元の worker allocation が終了し、通常の `unless-stopped` による再起動後も同じ Issue を再実行しない。
   manager が次の依頼を待って稼働している状態は正常で、service 全体の停止や exit 0 だけを合格条件にしない。

`WORKER_FINISHED` は worker の申告保存であり、研究結果の独立検証ではありません。
終了コード137はそれだけで成功・失敗の原因を特定せず、時刻・OOM・既存の停止イベントと照合します。
この文書自体は配置、認証の移送、モデル利用、公開の承認ではありません。

## 6. 今すぐできるモデルなし検証

repository root で、通常 Python と最適化 Python の両方を確認します。
これらは test fixture を使うローカル試験です。GitHub token、モデル認証、Docker 起動を必要としません。
`python3 --version` も記録してください。container は安定版 Python 3.11 を使います。
host の precheck も安定版を使い、`3.11.0rc1` のようなリリース候補版を導入判定には使いません。

staging fixture は `pathlib` の内部的な subclass 初期化に依存しない構成です。
互換修正時点では Python 3.10.22 / 3.11.17 / 3.12.14 で、当時の全 988 件を通常・`-O` の両方で確認しました。
3.10.12 と 3.11.0rc1 の実行確認は含みません。rc1 で報告された別の HTTPError 失敗は、
traceback 未確認のため、この staging 修正で解消したとは扱いません。

```sh
python3 -B -m unittest discover -s tests -p 'test_harness_*.py' -v
python3 -B -O -m unittest discover -s tests -p 'test_harness_*.py' -v
```

合格は fixture／契約の検証結果です。GitHub の実際の権限、container 内の実資源境界、
実モデルによる完走、PR 公開、通知の実証とは分けて記録してください。

## 7. local Codex に渡す導入前調査用プロンプト

この変更の PR URL と確認対象の full HEAD SHA を、次の二つの placeholder に入れて渡してください。
調査と承認事項の整理までを依頼するプロンプトです。実運用の開始許可にはなりません。

```text
対象 PR: <この direct PR 変更の PR URL>
確認対象 HEAD: <この PR の 40 桁 HEAD SHA>

この PR の Codex direct PR 経路を、私の環境で導入できるか事前調査してください。
実装・CPU 実験・テスト・結果報告・通常 PR は Codex が担当し、manager は本人承認、
永続的な単回 claim、資源上限、所有が確認できた自ジョブの停止を担当する方式です。

今回は導入前調査までです。既存の PR #71 系の環境と診断版 370f732、container、volume、
設定、認証、台帳、履歴を変更・削除・再起動しないでください。
別の新しい clean checkout を使い、上記 full HEAD SHA と一致することを確認してください。
既存の作業途中 checkout を reset、clean、checkout で書き換えないでください。

まず repository の適用される AGENTS.md と tools/research_goal/DIRECT_PR.md を読んでください。
コード、設定の形式、既存の実行環境と resource allocation を読取専用で確認してください。
認証ファイルの中身や token を表示・転記・ログ出力せず、存在・所有者・mode 等だけを確認してください。
既存 manager token を worker へコピーする方法は使いません。
本人の最新方針では同じ ken2129 名義を選べます。新 account／GitHub App を必須にしないでください。
同じ名義での承認偽造リスクを明記し、owner_identity_risk_accepted の明示 opt-in を確認してください。

この fresh checkout で、文書にある通常 Python と python -O のモデルなし unittest を実行し、
実行コマンド、結果、未検証事項を記録してください。外部サービスへの write、Docker build／create／start、
実モデル呼出しや実 Issue 作成が必要になる試験には進まないでください。
必要なら --template、--worker-pr-template、--direct-plan-template で OFF の設定例だけを新しい作業用ファイルへ出力し、
既存ファイルを上書きしないでください。template の生成や値の編集を、実行承認とは扱わないでください。

次へ進むために私が判断すべき事項を、一つの承認依頼にまとめて提示してください。
- worker の GitHub actor と ID。同じ ken2129／55587773 も選べるが、本人と worker の承認コメントを区別できないこと
- 対象 repository、credential の権限・保管場所・継続利用・失効方法
- main への直接 write、bypass、merge の実際の GitHub 側の制限と残る権限。手動 merge 方針は技術的な本人識別とは別であること
- 新しい container／volume／network、runtime と入力の commit、固定モデル、既存環境との分離
- wall、各プロセス CPU、aggregate memory、PID、work、output の各上限
- worker が自身のモデル認証・GitHub token を読めること、外向き通信、任意 CPU コマンド、
  runtime home／IPC の書込、利用料金のハード上限がないことのリスク
- 公開してよい入力・書込 scope・成果物、source_sha と base_sha の一致、最初の小さい CPU task
- 同じ ken2129 名義なら既存通知 filter を使い、別 actor のときだけ変更する方針と、実 webhook の確認手順

安全性を確認できた部分、設定不足、今回のモデルなし試験では証明できない部分を区別してください。
fine-grained token だけでは main への write や merge を禁止できません。
同じ名義で作った PR を同じ owner が approve する required-review 条件は使えません。
本人には許可し、同じ identity の worker だけを技術的に拒否できると説明しないでください。
worker helper の制限を、worker 全体の security boundary として説明しないでください。
不明な command、flag、API を推測で作らず、対象 HEAD の実装と文書で確かめてください。

新 account／token 作成、credential の継続 access 設定、権限／保護設定の変更、deployment、
実 Issue／承認コメントの投稿、実 Codex／モデル実行、branch push／PR 作成、merge は、
このプロンプトでは許可していません。必要な具体事項を私が承認するまで実行しないでください。
確認できる範囲を終えたら、調査結果と承認依頼を示して停止してください。
```
