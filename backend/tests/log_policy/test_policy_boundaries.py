"""アプリ固有の認証情報の共通禁止と、追加 deny の適用を検証する。"""

import json

import pytest
import structlog

from app.log_policy import BASE_LOG_RULES, LogPolicy, LogPolicyRules, PolicyLogger
from app.log_policy.policies.article_text import ARTICLE_TEXT_KEYS
from app.log_policy.policies.external_content import EXTERNAL_CONTENT_POLICY
from app.log_policy.processor import LogPolicyProcessor

pytestmark = pytest.mark.unit


class TestPurposeRuleConstants:
    """実際の目的ポリシー定数が基底と本文の禁止を保持し、設定名の表記揺れも禁止する。"""

    @pytest.mark.parametrize(
        "key", ["GEMINI_API_KEY", "openaiApiKey", "Deepseek-Api-Key"]
    )
    def test_application_credential_alias_is_denied(self, key) -> None:
        """設定名の大文字・camelCase・ハイフン表記も禁止する。"""
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "info",
            {"event": "failed", key: "synthetic"},
        )
        assert output["_denied_keys"] == [key]
        assert "synthetic" not in json.dumps(output)

    @pytest.mark.parametrize("key", sorted(ARTICLE_TEXT_KEYS))
    def test_external_content_denies_article_fields(self, key) -> None:
        """外部コンテンツのポリシーはdenyで本文項目を除外する。"""
        output = LogPolicyProcessor()(
            PolicyLogger(EXTERNAL_CONTENT_POLICY, structlog.ReturnLogger()),
            "info",
            {"event": "failed", key: "synthetic body"},
        )
        assert key not in output
        assert output["_denied_keys"] == [key]


class TestDenyAndMaskSeparation:
    """deny と mask は項目名で働き、文字列の中に書かれた値は変えない。"""

    def test_inherited_deny_and_mask_apply_to_their_respective_targets(self) -> None:
        """継承済みのdenyで項目を除外し、継承済みのmaskで項目の値を置き換える。"""
        purpose = LogPolicyRules(
            LogPolicy.INFRASTRUCTURE, frozenset(), mask=frozenset({"MaskedSample"})
        )
        purpose = purpose.extend(
            allow=frozenset(), deny=frozenset({"RestrictedSample"})
        )
        rules = purpose.extend(allow=frozenset({"payload"}))
        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()),
            "info",
            {
                "RestrictedSample": "synthetic field",
                "payload": {
                    "RestrictedSample": "synthetic nested",
                    "MaskedSample": "synthetic nested",
                    "count": 1,
                },
            },
        )
        assert "RestrictedSample" not in output
        assert output["_denied_keys"] == ["RestrictedSample"]
        assert output["payload"] == {"MaskedSample": "***", "count": 1}
        assert output["_denied_nested_count"] == 1

    @pytest.mark.parametrize(
        "protection_rules",
        [
            pytest.param({"deny": frozenset({"restricted_sample"})}, id="deny"),
            pytest.param({"mask": frozenset({"restricted_sample"})}, id="mask"),
        ],
    )
    def test_protection_rules_do_not_change_text_content(
        self, protection_rules
    ) -> None:
        """deny・maskに登録したキー名が文字列の中に書かれていても、その値は変えない。"""
        rules = LogPolicyRules(
            LogPolicy.INFRASTRUCTURE, frozenset({"payload"}), **protection_rules
        )
        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()),
            "info",
            {
                "event": "restricted_sample=synthetic",
                "payload": {"note": "restricted_sample=synthetic"},
            },
        )
        assert output["event"] == "restricted_sample=synthetic"
        assert output["payload"] == {"note": "restricted_sample=synthetic"}

    def test_mask_preserves_field_names_when_replacing_values(self) -> None:
        """mask対象の項目名はトップレベルでもネスト内でも残し、値全体を置き換える。"""
        rules = LogPolicyRules(
            LogPolicy.INFRASTRUCTURE,
            frozenset({"payload", "restricted_sample"}),
            mask=frozenset({"restricted_sample"}),
        )
        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()),
            "info",
            {
                "event": "completed",
                "restricted_sample": "visible",
                "payload": {"restricted_sample": "visible"},
            },
        )
        assert output["event"] == "completed"
        assert output["restricted_sample"] == "***"
        assert output["payload"] == {"restricted_sample": "***"}


class TestTopLevelAllow:
    """保護規則の登録はトップレベル項目の出力許可を与えない。"""

    @pytest.mark.parametrize(
        "protection_rules",
        [
            pytest.param(
                {"mask": frozenset({"canonical_url"})},
                id="mask_without_allow",
            ),
            pytest.param(
                {"sanitize": frozenset({"canonical_url"})},
                id="sanitize_without_allow",
            ),
        ],
    )
    def test_protection_rules_do_not_grant_top_level_allow(
        self, protection_rules
    ) -> None:
        """保護規則へ登録してもallowにないトップレベル項目は未登録として落とす。"""
        rules = LogPolicyRules(
            LogPolicy.INFRASTRUCTURE, frozenset(), **protection_rules
        )
        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()),
            "info",
            {"canonical_url": "synthetic"},
        )
        assert "canonical_url" not in output
        assert output["_unregistered_count"] == 1
