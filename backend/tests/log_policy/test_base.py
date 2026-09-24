"""``LogPolicyRules`` の定義時契約: allow と deny の排他。"""

from __future__ import annotations

import pytest

from app.log_policy import LogPolicy, LogPolicyRules
from app.log_policy.base import BASE_ALLOW, BASE_DENY, BASE_LOG_RULES, BASE_MASK

pytestmark = pytest.mark.unit


class TestDefinition:
    """生成時に正規化と基底の追加を確定し、allow と deny の重複を定義時に拒否する。"""

    def test_base_rules_are_completed_without_a_purpose(self) -> None:
        """目的のない基底ルールにも確定済みallow・denyが含まれる。"""
        assert BASE_LOG_RULES.policy is None
        assert BASE_LOG_RULES.allow == BASE_ALLOW
        assert BASE_LOG_RULES.deny == BASE_DENY
        assert BASE_LOG_RULES.mask == BASE_MASK

    def test_base_mask_is_empty(self) -> None:
        """認証キーはdenyで項目ごと除外するため、基底のmaskには入れない。"""
        assert BASE_MASK == frozenset()

    def test_allow_is_normalized_and_stored_with_base_fields(self) -> None:
        """allowは生成時に正規化と基底の追加を完了し、取得時に再生成しない。"""
        rules = LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset({"RequestCount"}))
        allow = rules.allow
        assert allow == BASE_ALLOW | {"request_count"}
        assert rules.allow is allow

    def test_deny_cannot_overlap_automatic_base_allow(self) -> None:
        """自動追加される基底allowと追加denyの重複も定義時に拒否する。"""
        with pytest.raises(ValueError, match="allow が deny と重複"):
            BASE_LOG_RULES.extend(allow=frozenset(), deny=frozenset({"LoggerName"}))

    def test_deny_is_normalized_and_stored_once(self) -> None:
        """規則が保持する禁止集合は基底と正規化済み追加denyを含み、取得ごとに作り直さない。"""
        rules = LogPolicyRules(
            LogPolicy.INFRASTRUCTURE, frozenset(), frozenset({"RestrictedSample"})
        )
        deny = rules.deny
        assert deny == BASE_DENY | {"restricted_sample"}
        assert rules.deny is deny

    def test_allow_containing_base_deny_key_fails_at_definition(self) -> None:
        """allow に基底の禁止キーを含めると、定義の時点で失敗する。"""
        with pytest.raises(ValueError, match="allow が deny と重複"):
            LogPolicyRules(
                policy=LogPolicy.AI_INFERENCE,
                allow=frozenset({"model", "prompt", "api_key"}),
            )

    def test_allow_containing_own_deny_key_fails_at_definition(self) -> None:
        """目的ポリシー自身の deny と allow の重複も定義時に落ちる。"""
        with pytest.raises(ValueError, match="allow が deny と重複"):
            LogPolicyRules(
                policy=LogPolicy.USER_INTERACTION,
                allow=frozenset({"run_id", "question"}),
                deny=frozenset({"question"}),
            )

    @pytest.mark.parametrize(
        "key",
        [
            "aws_secret_access_key",
            "session_token",
            "AWSAccessKeyId",
            "APIKey",
            "privateKey",
            "Proxy-Authorization",
            "Set-Cookie",
            "refresh_token",
        ],
    )
    def test_credential_alias_cannot_be_allowed(self, key: str) -> None:
        """認証情報の表記揺れも正規化後は基底 deny と重なり、定義時に落ちる。"""
        with pytest.raises(ValueError, match="allow が deny と重複"):
            LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset({key}))

    @pytest.mark.parametrize(
        "key",
        [
            "gemini_api_key",
            "openai_api_key",
            "deepseek_api_key",
            "tavily_api_key",
            "logfire_token",
            "bff_jwt_signing_secret",
            "revalidate_bearer_secret",
            "postgres_auth_password",
            "postgres_app_password",
            "postgres_collect_password",
        ],
    )
    def test_application_credential_cannot_be_allowed(self, key) -> None:
        """実際の認証情報名を共通 deny から allow で解除できない。"""
        with pytest.raises(ValueError, match="allow が deny と重複"):
            LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset({key}))

    def test_direct_definition_includes_base_deny(self) -> None:
        """直接生成した規則にも共通の禁止項目が必ず含まれる。"""
        rules = LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset())
        assert rules.deny == BASE_DENY

    def test_input_changes_do_not_change_constructed_rules(self) -> None:
        """作成に渡した変更可能な入力を書き換えても、確定した各規則は変わらない。"""
        sanitize_fields = {"canonical_url"}
        rules = LogPolicyRules(
            policy=LogPolicy.INFRASTRUCTURE,
            allow=frozenset({"canonical_url"}),
            deny=frozenset({"hidden_field"}),
            mask=frozenset({"private_text"}),
            sanitize=sanitize_fields,
        )

        sanitize_fields.clear()
        sanitize_fields.add("source_url")

        assert rules.allow == BASE_ALLOW | {"canonical_url"}
        assert rules.deny == BASE_DENY | {"hidden_field"}
        assert rules.mask == BASE_MASK | {"private_text"}
        assert rules.sanitize == BASE_LOG_RULES.sanitize | {"canonical_url"}


class TestExtension:
    """継承で祖先の deny を保ち、親の規則を変更せず、allow は明示した分だけを持つ。"""

    def test_repeated_extension_keeps_every_ancestor_deny(self) -> None:
        """継承を重ねても共通・親・子の禁止項目が残る。"""
        parent = LogPolicyRules(
            LogPolicy.INFRASTRUCTURE, frozenset(), frozenset({"parent_private"})
        )
        child = parent.extend(allow=frozenset(), deny=frozenset({"child_private"}))
        descendant = child.extend(allow=frozenset())
        assert descendant.deny == BASE_DENY | {"parent_private", "child_private"}

    def test_extension_keeps_parent_policy_identifier(self) -> None:
        """継承後も親と同じポリシー識別子を使う。"""
        parent = LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset())
        child = parent.extend(allow=frozenset({"count"}))
        assert child.policy is LogPolicy.INFRASTRUCTURE

    def test_extension_does_not_change_parent_rules(self) -> None:
        """子の規則を作っても親の許可・禁止項目を変更しない。"""
        parent = LogPolicyRules(
            LogPolicy.INFRASTRUCTURE,
            frozenset({"parent_count"}),
            frozenset({"parent_private"}),
        )
        child = parent.extend(
            allow=frozenset({"child_count"}), deny=frozenset({"child_private"})
        )
        assert child is not parent
        assert parent.allow == BASE_ALLOW | {"parent_count"}
        assert parent.deny == BASE_DENY | {"parent_private"}

    def test_extension_normalizes_added_deny(self) -> None:
        """継承時に追加する禁止キーも正規化して保持する。"""
        parent = LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset())
        child = parent.extend(allow=frozenset(), deny=frozenset({"ChildPrivate"}))
        assert child.deny == BASE_DENY | {"child_private"}

    def test_extension_cannot_allow_base_deny(self) -> None:
        """継承操作でも共通の禁止項目を許可できない。"""
        parent = LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset())
        with pytest.raises(ValueError, match="allow が deny と重複"):
            parent.extend(allow=frozenset({"APIKey"}))

    def test_extension_cannot_allow_parent_deny(self) -> None:
        """親の禁止項目は表記を変えても子で許可できない。"""
        parent = LogPolicyRules(
            LogPolicy.INFRASTRUCTURE, frozenset(), frozenset({"ParentPrivate"})
        )
        with pytest.raises(ValueError, match="allow が deny と重複"):
            parent.extend(allow=frozenset({"parent-private"}))

    def test_extension_cannot_allow_added_deny(self) -> None:
        """子自身が追加した禁止項目も許可との重複を拒否する。"""
        parent = LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset())
        with pytest.raises(ValueError, match="allow が deny と重複"):
            parent.extend(
                allow=frozenset({"childPrivate"}), deny=frozenset({"child_private"})
            )

    def test_extension_uses_only_explicit_allow(self) -> None:
        """子の許可項目に親の許可項目を自動追加しない。"""
        parent = LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset({"parent_count"}))
        child = parent.extend(allow=frozenset({"child_count"}))
        assert child.allow == BASE_ALLOW | {"child_count"}

    def test_extension_can_leave_allow_empty(self) -> None:
        """空の許可集合を指定した子に親の許可項目が復活しない。"""
        parent = LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset({"parent_count"}))
        child = parent.extend(allow=frozenset())
        assert child.allow == BASE_ALLOW


class TestMask:
    """mask は allow / deny と独立した集合で、継承しても正規化して保持する。"""

    def test_mask_is_normalized_and_stored_with_base_keys(self) -> None:
        """maskは基底と正規化済みの追加キーを生成時に確定する。"""
        rules = LogPolicyRules(
            LogPolicy.INFRASTRUCTURE, frozenset(), mask=frozenset({"PrivateText"})
        )
        mask = rules.mask
        assert mask == BASE_MASK | {"private_text"}
        assert rules.mask is mask

    def test_deny_and_mask_are_independent_sets(self) -> None:
        """追加denyと追加maskを互いの集合へ暗黙に混ぜない。"""
        rules = LogPolicyRules(
            LogPolicy.INFRASTRUCTURE,
            frozenset(),
            deny=frozenset({"hidden_field"}),
            mask=frozenset({"private_text"}),
        )
        assert rules.deny == BASE_DENY | {"hidden_field"}
        assert rules.mask == BASE_MASK | {"private_text"}

    def test_mask_can_overlap_allow_and_deny(self) -> None:
        """maskは構造化項目の選別とは別なのでallow・denyとの重複を許可する。"""
        rules = LogPolicyRules(
            LogPolicy.INFRASTRUCTURE,
            frozenset({"visible_field"}),
            deny=frozenset({"hidden_field"}),
            mask=frozenset({"visible_field", "hidden_field"}),
        )
        assert rules.mask & rules.allow == {"visible_field"}
        assert "hidden_field" in rules.mask & rules.deny

    def test_repeated_extension_keeps_normalized_ancestor_masks(self) -> None:
        """継承を重ねても基底・親・子のmaskを正規化して保持する。"""
        parent = LogPolicyRules(
            LogPolicy.INFRASTRUCTURE, frozenset(), mask=frozenset({"ParentPrivate"})
        )
        child = parent.extend(allow=frozenset(), mask=frozenset({"Child-Private"}))
        descendant = child.extend(allow=frozenset())
        assert descendant.mask == BASE_MASK | {"parent_private", "child_private"}

    def test_extension_does_not_change_parent_mask(self) -> None:
        """子へmaskを追加しても親のmaskを変更しない。"""
        parent = LogPolicyRules(
            LogPolicy.INFRASTRUCTURE, frozenset(), mask=frozenset({"parent_private"})
        )
        parent.extend(allow=frozenset(), mask=frozenset({"child_private"}))
        assert parent.mask == BASE_MASK | {"parent_private"}


class TestSanitize:
    """サニタイズ対象の項目名を正規化し、親子の対象項目を合わせ、対応する処理のない項目名を拒否する。"""

    def test_sanitize_normalizes_field_name(self) -> None:
        """正規化後のサニタイズ対象のキー集合が期待値と一致する。"""
        rules = LogPolicyRules(
            policy=LogPolicy.INFRASTRUCTURE,
            allow=frozenset(),
            sanitize=frozenset({"CanonicalUrl"}),
        )

        assert rules.sanitize == BASE_LOG_RULES.sanitize | {"canonical_url"}

    def test_extension_merges_different_sanitize_fields(self) -> None:
        """子に別の項目を追加すると、親と子の両方がサニタイズ対象になる。"""
        parent = LogPolicyRules(
            policy=LogPolicy.INFRASTRUCTURE,
            allow=frozenset(),
            sanitize=frozenset({"canonical_url"}),
        )

        child = parent.extend(
            allow=frozenset(),
            sanitize=frozenset({"source_url"}),
        )

        assert child.sanitize == BASE_LOG_RULES.sanitize | {
            "canonical_url",
            "source_url",
        }

    def test_extension_does_not_duplicate_same_sanitize_field(self) -> None:
        """親と子で同じ項目を指定しても、サニタイズ対象は重複しない。"""
        parent = LogPolicyRules(
            policy=LogPolicy.INFRASTRUCTURE,
            allow=frozenset(),
            sanitize=frozenset({"canonical_url"}),
        )

        child = parent.extend(
            allow=frozenset(),
            sanitize=frozenset({"canonical_url"}),
        )

        assert child.sanitize == BASE_LOG_RULES.sanitize | {"canonical_url"}

    def test_field_without_registered_sanitizer_fails_at_definition(self) -> None:
        """対応表にない項目名をサニタイズ対象にすると、定義時に拒否する。"""
        with pytest.raises(ValueError, match="対応するサニタイズがない項目"):
            LogPolicyRules(
                policy=LogPolicy.INFRASTRUCTURE,
                allow=frozenset(),
                sanitize=frozenset({"connection_url"}),
            )
