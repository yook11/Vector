# bootstrapのDB管理ロール参照

- Problem: DB管理用3ロールの追加後、bootstrap専用担当による全stateのrefreshがGetRole拒否で停止する。
- Evidence: 実planのAccessDenied、bootstrap-accessの許可対象、Terraform providerのIAM role読取処理を照合した。
- Invariants: 対象は同一アカウントのcontroller・exec・taskの完全一致ARNに限定し、GetRole・GetRolePolicy・ListRolePolicies・ListAttachedRolePoliciesだけを追加する。変更・PassRole・AssumeRole・秘密値参照は追加しない。
- Non-goals: DB管理ロールの変更権限の委譲、state分割、SSO設定変更。
- Done: 限定した読取権限と対象外・書込拒否のテストを追加し、管理者経路で実行権限を反映してから専用ロールでRelay境界のplan・applyを再開する。

管理者のbootstrap-access planはmanage-vector-bootstrap inline policyの更新1件だけであることを確認する。適用後、bootstrap専用ロールで全stateをrefreshして4つのRelay境界の変更だけを適用する。本体Terraformの適用は既存GitHub承認経路を使用する。
