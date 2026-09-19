"""指定キーに基づく文字列内の値のマスク。"""

from __future__ import annotations

import pytest

from app.log_policy.base import BASE_MASK
from app.log_policy.mask import mask_assignments

pytestmark = pytest.mark.unit


class TestKeyMatching:
    """文字列内のキーは正規化後の完全一致で照合し、部分一致や内容だけでは伏せない。"""

    def test_authorization_word_in_path_is_preserved(self) -> None:
        """パスに含まれるAuthorizationという語を認証ヘッダー扱いしない。"""
        sample = "GET /repos/owner/Authorization-utils/contents/README returned 200"
        assert mask_assignments(sample, BASE_MASK) == sample

    def test_iam_token_port_setting_is_preserved(self) -> None:
        """IAMトークン関連のポート設定をtokenという部分文字列で伏せない。"""
        sample = "rds_iam_auth_token_port=5432 hide_parameters=True"
        assert mask_assignments(sample, BASE_MASK) == sample

    def test_token_usage_metrics_are_preserved(self) -> None:
        """トークン数の計量値をtokenという部分文字列で伏せない。"""
        sample = "completion_tokens=128 max_tokens=1024"
        assert mask_assignments(sample, BASE_MASK) == sample

    def test_client_token_assignment_is_preserved(self) -> None:
        """clientTokenをtokenの部分文字列として伏せない。"""
        sample = "clientToken=synthetic failed"
        assert mask_assignments(sample, BASE_MASK) == sample

    def test_password_hash_assignment_is_preserved(self) -> None:
        """password_hashをpasswordの部分文字列として伏せない。"""
        sample = "password_hash=synthetic failed"
        assert mask_assignments(sample, BASE_MASK) == sample

    def test_my_password_assignment_is_preserved(self) -> None:
        """my_passwordをpasswordの部分文字列として伏せない。"""
        sample = "my_password=synthetic failed"
        assert mask_assignments(sample, BASE_MASK) == sample

    @pytest.mark.parametrize("key", ["PrivateText", "private-text", "private_text"])
    def test_mask_normalizes_assignment_keys(self, key: str) -> None:
        """文字列内のキーも正規化後の完全一致でmaskに照合する。"""
        assert mask_assignments(f"{key}=synthetic", frozenset({"private_text"})) == (
            f"{key}=***"
        )

    def test_mask_does_not_detect_credentials_in_content(self) -> None:
        """mask対象でない値は既知のAPIキー形式でもmask単体では置換しない。"""
        text = "other=sk-proj-abcdef0123456789ABCDEFxyz"
        assert mask_assignments(text, BASE_MASK) == text


class TestValueTermination:
    """引用符・エスケープ・改行・区切り文字・空白で値の終端を決め、後続の診断を残す。"""

    def test_short_token_assignment_is_hidden(self) -> None:
        """短いtoken値もキー名に基づいて伏せる。"""
        text = "token=x"
        expected = "token=***"
        assert mask_assignments(text, BASE_MASK) == expected

    def test_short_api_key_header_is_hidden(self) -> None:
        """短いAPIキーヘッダー値もキー名に基づいて伏せる。"""
        text = "x-api-key: x"
        expected = "x-api-key: ***"
        assert mask_assignments(text, BASE_MASK) == expected

    def test_postgres_password_assignment_keeps_command_context(self) -> None:
        """PGPASSWORDの値を伏せ、前後のコマンド文脈を残す。"""
        text = "env PGPASSWORD=hunter2redacted psql failed"
        expected = "env PGPASSWORD=*** psql failed"
        assert mask_assignments(text, BASE_MASK) == expected

    def test_quoted_password_with_spaces_is_hidden(self) -> None:
        """空白を含む引用符付きパスワードを全体置換する。"""
        text = "password='synthetic private value'"
        expected = "password=***"
        assert mask_assignments(text, BASE_MASK) == expected

    def test_quoted_password_with_escaped_quotes_is_hidden(self) -> None:
        """エスケープされた引用符でパスワードの置換を打ち切らない。"""
        text = 'password="synthetic \\"private\\" value"'
        expected = "password=***"
        assert mask_assignments(text, BASE_MASK) == expected

    def test_quoted_multiline_password_is_hidden(self) -> None:
        """改行を含む引用符付きパスワードを全体置換する。"""
        text = "password='synthetic\nprivate value'"
        expected = "password=***"
        assert mask_assignments(text, BASE_MASK) == expected

    def test_password_in_dict_repr_keeps_other_fields(self) -> None:
        """辞書表現のパスワードを伏せ、ユーザー名とホストを残す。"""
        text = "{'user': 'vector', 'password': 'hunter2redacted', 'host': 'db'}"
        expected = "{'user': 'vector', 'password': ***, 'host': 'db'}"
        assert mask_assignments(text, BASE_MASK) == expected

    def test_api_key_in_header_dict_is_hidden(self) -> None:
        """ヘッダー辞書表現のAPIキー値を全体置換する。"""
        text = "headers={'x-api-key': 'super-secret-value-here-1234'}"
        expected = "headers={'x-api-key': ***}"
        assert mask_assignments(text, BASE_MASK) == expected

    def test_gemini_api_key_assignment_keeps_context(self) -> None:
        """アプリ設定名gemini_api_keyの値を伏せ、後続の原因文を残す。"""
        text = "gemini_api_key='synthetic secret' failed"
        expected = "gemini_api_key=*** failed"
        assert mask_assignments(text, BASE_MASK) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param(
                "password='synthetic private value",
                "password=***",
                id="unterminated",
            ),
            pytest.param(
                "password='synthetic private value\\",
                "password=***",
                id="trailing_escape",
            ),
        ],
    )
    def test_unterminated_quoted_value_is_redacted_to_the_end(
        self, text: str, expected: str
    ) -> None:
        """閉じていない引用符は末尾まで伏せ、バックスラッシュで終わっても断片を残さない。"""
        assert mask_assignments(text, BASE_MASK) == expected

    def test_api_key_assignment_stops_at_whitespace_and_keeps_following_text(
        self,
    ) -> None:
        """認証ヘッダー以外のキー付き値は空白まで伏せ、同じ行の後続文を残す。"""
        text = "x-api-key: secret host=db"
        expected = "x-api-key: *** host=db"
        assert mask_assignments(text, BASE_MASK) == expected

    def test_unquoted_password_stops_at_query_delimiter(self) -> None:
        """引用符なしのpasswordは&の手前で止め、後続パラメーターを残す。"""
        text = "password=secret&host=db"
        expected = "password=***&host=db"
        assert mask_assignments(text, BASE_MASK) == expected

    def test_unquoted_password_stops_at_comma(self) -> None:
        """引用符なしのpasswordはカンマの手前で止め、後続の項目を残す。"""
        text = "password=secret,next=1"
        expected = "password=***,next=1"
        assert mask_assignments(text, BASE_MASK) == expected

    def test_common_assignment_rule_hides_quoted_aws_secret_key(self) -> None:
        """引用符付きAWS秘密キーをキー名に基づいて全体置換する。"""
        key = "aws_secret_access_key"
        assert (
            mask_assignments(f"{key}='synthetic private value' region=x", BASE_MASK)
            == f"{key}=*** region=x"
        )

    def test_common_assignment_rule_hides_quoted_aws_session_token(self) -> None:
        """引用符付きAWSセッショントークンをキー名に基づいて全体置換する。"""
        key = "aws_session_token"
        assert (
            mask_assignments(f"{key}='synthetic private value' region=x", BASE_MASK)
            == f"{key}=*** region=x"
        )

    def test_common_assignment_rule_hides_unquoted_aws_secret_key(self) -> None:
        """引用符のないAWS秘密キーも共通処理が値全体を伏せ、後続の診断情報を残す。"""
        text = "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY region=x"
        assert mask_assignments(text, BASE_MASK) == "aws_secret_access_key=*** region=x"


class TestHeaderLineValues:
    """Authorization / Cookie / Basic は行末まで値として伏せる。"""

    def test_authorization_in_header_dict_is_hidden(self) -> None:
        """ヘッダー辞書表現のAuthorization値をBearer部分も含めて伏せる。"""
        text = (
            "headers={'Authorization': "
            "'Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.abc'}"
        )
        expected = "headers={'Authorization': ***}"
        assert mask_assignments(text, BASE_MASK) == expected

    def test_basic_authorization_value_is_hidden(self) -> None:
        """Basic認証のAuthorization値を認証方式も含めて伏せる。"""
        text = "Authorization: Basic dTpw"
        expected = "Authorization: ***"
        assert mask_assignments(text, BASE_MASK) == expected

    def test_cookie_header_hides_all_cookie_pairs(self) -> None:
        """Cookieヘッダー内の複数のCookie値をセミコロン以降も含めて伏せる。"""
        text = "Cookie: session=synthetic; second=private"
        expected = "Cookie: ***"
        assert mask_assignments(text, BASE_MASK) == expected

    def test_cookie_header_starting_with_bracket_still_hides_all_pairs(self) -> None:
        """Cookieが括弧で始まっても行全体の保護を優先し次の行は残す。"""
        text = "Cookie: [SECRET_ONE]; second=SECRET_TWO\nstatus=failed"
        expected = "Cookie: ***\nstatus=failed"
        assert mask_assignments(text, BASE_MASK) == expected

    def test_authorization_header_starting_with_bracket_hides_whole_line(self) -> None:
        """Authorizationが括弧で始まっても行内の後続認証情報を残さない。"""
        text = "Authorization: [SECRET_ONE] SECRET_TWO\nstatus=failed"
        expected = "Authorization: ***\nstatus=failed"
        assert mask_assignments(text, BASE_MASK) == expected

    def test_authorization_header_hides_the_remainder_of_the_line(self) -> None:
        """Authorizationは同じ行の後続文まで値として伏せる。"""
        text = "Authorization: Bearer secret host=db"
        expected = "Authorization: ***"
        assert mask_assignments(text, BASE_MASK) == expected


class TestContainerValues:
    """括弧の入れ子と引用符を追跡してコンテナ値全体を伏せ、壊れた終端は末尾まで伏せる。"""

    @pytest.mark.parametrize(
        "text",
        [
            "content=['PRIVATE_ONE', 'PRIVATE_TWO'] status=failed",
            "content={'first': 'PRIVATE_ONE', 'second': 'PRIVATE_TWO'} status=failed",
            "content=('PRIVATE_ONE', 'PRIVATE_TWO') status=failed",
            "content=[] status=failed",
            "content={} status=failed",
            "content=() status=failed",
        ],
    )
    def test_article_container_value_is_masked_as_a_whole(self, text: str) -> None:
        """本文のコンテナ全体を伏せ、終端後の診断を残す。"""
        assert (
            mask_assignments(text, frozenset({"content"}))
            == "content=*** status=failed"
        )

    def test_article_container_tracks_mixed_nested_brackets(self) -> None:
        """異なる括弧の入れ子でも外側の値の終端まで伏せる。"""
        text = "content={'parts': [('PRIVATE_ONE',), ['PRIVATE_TWO']]} retry=2"
        assert mask_assignments(text, frozenset({"content"})) == "content=*** retry=2"

    def test_article_container_ignores_brackets_inside_both_quote_styles(self) -> None:
        """引用符内の不対応な括弧を構造の終端と誤認しない。"""
        text = "content=['PRIVATE_ONE ]})', \"PRIVATE_TWO [{(\"] retry=2"
        assert mask_assignments(text, frozenset({"content"})) == "content=*** retry=2"

    def test_article_container_ignores_escaped_quotes(self) -> None:
        """エスケープされた引用符の後の括弧も本文として伏せる。"""
        text = r"""content=["PRIVATE_ONE \" ]", 'PRIVATE_TWO \' }'] retry=2"""
        assert mask_assignments(text, frozenset({"content"})) == "content=*** retry=2"

    def test_article_container_accepts_escaped_backslash_before_closing_quote(
        self,
    ) -> None:
        """偶数個のバックスラッシュに続く引用符は値の終端として扱う。"""
        text = r"""content=["PRIVATE_ONE\\", 'PRIVATE_TWO'] retry=2"""
        assert mask_assignments(text, frozenset({"content"})) == "content=*** retry=2"

    def test_article_container_preserves_outer_dictionary_fields(self) -> None:
        """マスク対象を囲む辞書の閉じ括弧や安全な兄弟項目を残す。"""
        text = '{"content": {"parts": ["PRIVATE_ONE", "PRIVATE_TWO"]}, "count": 2}'
        expected = '{"content": ***, "count": 2}'
        assert mask_assignments(text, frozenset({"content"})) == expected

    def test_article_container_accepts_whitespace_after_assignment(self) -> None:
        """代入後の空白と改行を保ち、複数行の本文値全体を伏せる。"""
        text = "content: \n ['PRIVATE_ONE',\n 'PRIVATE_TWO'] retry=2"
        assert (
            mask_assignments(text, frozenset({"content"})) == "content: \n *** retry=2"
        )

    @pytest.mark.parametrize(
        "text",
        [
            "content=['PRIVATE_ONE', 'PRIVATE_TWO' status=failed",
            "content={'parts': ['PRIVATE_ONE', 'PRIVATE_TWO'] retry=2",
            "content=('PRIVATE_ONE', 'PRIVATE_TWO' retry=2",
        ],
    )
    def test_unclosed_article_container_masks_to_end(self, text: str) -> None:
        """閉じ括弧が欠けた本文は後続項目らしい文字列も含め末尾まで伏せる。"""
        assert mask_assignments(text, frozenset({"content"})) == "content=***"

    @pytest.mark.parametrize(
        "text",
        [
            "content=['PRIVATE_ONE'} status=failed",
            "content={'parts': ['PRIVATE_ONE')} retry=2",
            "content=(['PRIVATE_ONE')] retry=2",
        ],
    )
    def test_mismatched_article_brackets_mask_to_end(self, text: str) -> None:
        """対応しない閉じ括弧を検出した本文は末尾まで伏せる。"""
        assert mask_assignments(text, frozenset({"content"})) == "content=***"

    @pytest.mark.parametrize(
        "text",
        [
            'content=["PRIVATE_ONE] status=failed',
            "content=['PRIVATE_ONE] status=failed",
            'content=["PRIVATE_ONE' + "\\",
        ],
    )
    def test_unclosed_quote_inside_article_container_masks_to_end(
        self, text: str
    ) -> None:
        """コンテナ内の引用符が閉じなければ括弧があっても末尾まで伏せる。"""
        assert mask_assignments(text, frozenset({"content"})) == "content=***"

    def test_multiple_article_values_are_masked_independently(self) -> None:
        """複数の本文項目をそれぞれ丸ごと伏せ、中間と末尾の診断を残す。"""
        text = "content=['PRIVATE_ONE'] count=2 summary={'part': 'PRIVATE_TWO'} retry=2"
        expected = "content=*** count=2 summary=*** retry=2"
        assert mask_assignments(text, frozenset({"content", "summary"})) == expected

    def test_article_value_with_nested_assignment_does_not_restart_masking(
        self,
    ) -> None:
        """本文の内部に同じキーの代入があっても外側の範囲を丸ごと伏せる。"""
        text = "content=['content=PRIVATE_ONE', 'PRIVATE_TWO'] retry=2"
        assert mask_assignments(text, frozenset({"content"})) == "content=*** retry=2"

    def test_password_container_is_masked_as_a_whole(self) -> None:
        """認証情報もコンテナに変わった値全体を伏せて接続先を残す。"""
        text = "password=['SECRET_ONE', 'SECRET_TWO'] host=db"
        assert mask_assignments(text, BASE_MASK) == "password=*** host=db"

    def test_non_target_container_preserves_its_structure(self) -> None:
        """対象外のコンテナ自体は残し、その内部の本文項目だけを伏せる。"""
        text = "payload={'content': ['PRIVATE_ONE', 'PRIVATE_TWO'], 'count': 2} retry=2"
        expected = "payload={'content': ***, 'count': 2} retry=2"
        assert mask_assignments(text, frozenset({"content"})) == expected
