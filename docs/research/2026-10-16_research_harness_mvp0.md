# Research control MVP0: CPU fixture implementation (2026-10-16)

## 目的

[#56](https://github.com/ken2129/c3_neural_compression/issues/56)の研究制御契約を、実GPU・認証・共有マシン設定を変更せず検証する。計画→承認fixture→CPU小仕事→検証→公開fixture→報告の一巡を先に実装し、本番の未成立条件を明確にする。

## 実施したこと

- `research_harness/`へ標準ライブラリ中心のversion付きschema、source凍結契約、fake chat bridge、SQLite台帳、single-instance lock、CPU process manager、artifact/publisher/storage契約とCLIを追加。
- claim・nonce消費・同時1run予約・attempt予算予約をtransaction化。失効leaseから重複起動せず、取消・revision・expiryを起動barrier解放直前にも確認。
- 固定CPU処理のchecksumを実際に取得し、合成rate fixtureを独立したvalidatorコード経路で再計算。数値は研究測定ではなくテスト入力と明記。
- 公開field allowlist、head/base/artifact binding、必須check不明時拒否、本ハーネス除外、merge不明応答のreadback、Box部分copy/全件hash契約をfakeで検証。
- 現行研究方針に限定結果PRの自動merge方針と未接続状態を併記。#49/#50 READMEの「runtimeがmainにない」表記を歴史的source pinと区別して修正。

## 結果

実装側の最終CPU fixture試験は119件が通常Pythonと`python -O`の両方で合格。`compileall`と`git diff --check`も合格。全体discoveryは234件中209件成功、15件skip、10件error（既存科学テストの未導入依存）であり、全suite合格とはしない。`python -m research_harness demo`は新規一時領域だけで動作し、checksum確認、科学完了、fake merge、fake reportの各状態を返す。本番操作を選択するCLIはなく、能力一覧はlive executionを`BLOCKED_ENV`とする。

既存科学テストは実行可能な依存不要部分を別途確認した。全suiteはこの環境にtorch、JAX、ml_collections、pycocotoolsがないため完走していない。未導入依存を自動installせず、focused試験を全科学処理の合格と呼ばない。

## 考察

同UIDの固定CPU fixtureは、実workerの権限隔離の証拠にならない。実プロセスの再利用識別・停止・復旧ロジックを検証できても、共有マシンのGPU/VRAM/RAM/PID/IO/disk/API hard capや、別identityのissuer/validator/publisherの成立を示さない。S01–S20は遷移・契約の試験対象であり、実transportによる全業務フローの完成ではない。

現行rateは画像ごとの全推定bits／detector resize後非padding画素数の平均を維持。既存`detector_input_protocol.rate_summary`へのadapterを用意するが、実COCO APの独立再計算・実科学profile実行は未接続。合成データの合格から研究結果の正当性を推定しない。

## 決定事項

- このPRはMVP0実装の部分成果として`Refs #56`。#56全体をcloseせず、ハーネス自体のmergeを実行しない。
- 新token、権限変更、daemon設定、Slack等の送信先追加、実Box転送、既存データ削除、GPU実行は行わない。
- 既存個別実験の結果・履歴・予算は変更しない。

## 未完了・次の作業

[runbook](../research_harness.md)の段階受入に従う。まず非実行の実チャットrequest往復とtrusted issuer、許可された対象マシンの保護境界を確定・検証する。その後、別途承認された実CPU canary、限定GPU pilot、必要時のBox、限定実publisherへ進む。未成立なら理由を表示して止まり、same-UIDやtmuxを隔離成立へ読み替えない。
