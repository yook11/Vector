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
    def test_external_content_preserves_structured_and_assignment_protection(
        self, key
    ) -> None:
        """外部コンテンツのポリシーはdenyで本文項目を除外し、maskで文字列内の本文値を伏せる。"""
        output = LogPolicyProcessor()(
            PolicyLogger(EXTERNAL_CONTENT_POLICY, structlog.ReturnLogger()),
            "info",
            {"event": f"{key}='synthetic body'", key: "synthetic body"},
        )
        assert output["event"] == f"{key}=***"
        assert key not in output


class TestDenyAndMaskSeparation:
    """deny は構造化項目、mask は文字列内の値にだけ働き、互いの効果を与えない。"""

    def test_inherited_deny_and_mask_apply_to_their_respective_targets(self) -> None:
        """継承済みのdenyで構造化項目を除外し、maskで文字列内の値を伏せる。"""
        purpose = LogPolicyRules(
            LogPolicy.INFRASTRUCTURE, frozenset(), mask=frozenset({"RestrictedSample"})
        )
        purpose = purpose.extend(
            allow=frozenset(), deny=frozenset({"RestrictedSample"})
        )
        rules = purpose.extend(allow=frozenset({"payload"}))
        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()),
            "info",
            {
                "event": "RestrictedSample='synthetic event' failed",
                "RestrictedSample": "synthetic field",
                "payload": {"RestrictedSample": "synthetic nested", "count": 1},
            },
        )
        assert output["event"] == "RestrictedSample=*** failed"
        assert "RestrictedSample" not in output
        assert output["_denied_keys"] == ["RestrictedSample"]
        assert output["payload"] == {"count": 1}
        assert output["_denied_nested_count"] == 1

    def test_deny_alone_does_not_mask_text_assignments(self) -> None:
        """追加denyは構造化項目を除外しても文字列内の値はマスクしない。"""
        rules = LogPolicyRules(
            LogPolicy.INFRASTRUCTURE,
            frozenset({"payload"}),
            deny=frozenset({"restricted_sample"}),
        )
        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()),
            "info",
            {
                "event": "restricted_sample=synthetic",
                "restricted_sample": "hidden",
                "payload": {"restricted_sample": "hidden", "count": 1},
            },
        )
        assert output["event"] == "restricted_sample=synthetic"
        assert "restricted_sample" not in output
        assert output["payload"] == {"count": 1}

    def test_mask_alone_does_not_remove_or_replace_structured_values(self) -> None:
        """maskは文字列内のキー付き値にだけ働き、辞書の同名項目は残す。"""
        rules = LogPolicyRules(
            LogPolicy.INFRASTRUCTURE,
            frozenset({"payload", "restricted_sample"}),
            mask=frozenset({"restricted_sample"}),
        )
        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()),
            "info",
            {
                "event": "restricted_sample=synthetic",
                "restricted_sample": "visible",
                "payload": {"restricted_sample": "visible"},
            },
        )
        assert output["event"] == "restricted_sample=***"
        assert output["restricted_sample"] == "visible"
        assert output["payload"] == {"restricted_sample": "visible"}

    def test_mask_does_not_grant_top_level_allow(self) -> None:
        """maskへ追加したキーもallowにないトップレベル項目なら未登録として落とす。"""
        rules = LogPolicyRules(
            LogPolicy.INFRASTRUCTURE, frozenset(), mask=frozenset({"private_text"})
        )
        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()),
            "info",
            {"private_text": "synthetic"},
        )
        assert "private_text" not in output
        assert output["_unregistered_count"] == 1


class TestMaskWiring:
    """目的別 mask が例外項目・ネストのキーと配列・診断キー名に届く。"""

    def test_custom_mask_applies_to_every_exception_field(self) -> None:
        """例外文・型名・frameのファイル名と関数名に、同じ目的別maskを適用する。"""
        error_type = type(
            "RestrictedSample='synthetic class'", (Exception,), {"__module__": "sample"}
        )

        def fail():
            raise error_type("RestrictedSample='synthetic message'")

        fail.__code__ = fail.__code__.replace(
            co_filename="RestrictedSample='synthetic filename'",
            co_name="RestrictedSample='synthetic function'",
        )
        with pytest.raises(error_type) as captured:
            fail()
        rules = LogPolicyRules(
            LogPolicy.INFRASTRUCTURE, frozenset(), mask=frozenset({"RestrictedSample"})
        )
        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()),
            "error",
            {"event": "failed", "exc_info": captured.value},
        )
        assert output["error_class"] == "sample.RestrictedSample=***"
        assert output["error_message"] == "RestrictedSample=***"
        assert output["frames"][-1] == {
            "file": "RestrictedSample=***",
            "function": "RestrictedSample=***",
            "line": output["frames"][-1]["line"],
        }

    def test_nested_keys_and_list_values_use_resolved_mask(self) -> None:
        """ネストのキー名と配列内の文字列にも目的別maskを適用する。"""
        rules = LogPolicyRules(
            LogPolicy.INFRASTRUCTURE,
            frozenset({"payload"}),
            mask=frozenset({"private_text"}),
        )
        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()),
            "info",
            {"payload": {"private_text=synthetic": ["private_text=other"]}},
        )
        assert output["payload"] == {"private_text=***": ["private_text=***"]}

    def test_diagnostic_key_names_use_custom_mask(self) -> None:
        """denyで除外したキー名の診断にも独立した目的別maskを適用する。"""
        rules = LogPolicyRules(
            LogPolicy.INFRASTRUCTURE,
            frozenset(),
            deny=frozenset({"private_text=synthetic"}),
            mask=frozenset({"private_text"}),
        )
        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()),
            "info",
            {"private_text=synthetic": "hidden"},
        )
        assert output["_denied_keys"] == ["private_text=***"]
