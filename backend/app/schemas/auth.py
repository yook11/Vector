"""Auth request/response schemas."""

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from pydantic.alias_generators import to_camel

# --- XSS対策 Step 1: ホワイトリスト方式の入力バリデーション ---
#
# 「何を禁止するか（ブラックリスト）」ではなく
# 「何を許可するか（ホワイトリスト）」で定義する。
# ブラックリストは漏れが生じやすい
# （<script>を禁止しても <img onerror=...> が通る等）。
# ホワイトリストなら、許可していない文字は全て自動的に拒否される。
#
# 許可する文字:
#   \w (re.UNICODE) = Unicode文字(日本語等) + 英数字 + アンダースコア
#   \s             = スペース（"田中 太郎", "John Doe" 等の区切り）
#   \-             = ハイフン（"yook-1" 等）
#
# 許可しない文字（結果として排除される）:
#   < > & " ' / \ { } 等 — HTMLタグやスクリプトに使われる記号
DISPLAY_NAME_RE = re.compile(r"^[\w\s\-]+$", re.UNICODE)


class LoginRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    email: EmailStr
    password: str


class RegisterRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    email: EmailStr
    password: str

    # max_length=100: DBカラム (users.display_name VARCHAR(100)) と整合させる。
    # min_length=1:   値を入力した以上は意味のある文字列であるべき。
    #                 display_name 自体は任意項目（None 許可）なので、
    #                 「入力しない」は OK、「空文字や空白のみ」は NG。
    display_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
    )

    @field_validator("display_name", mode="before")
    @classmethod
    def strip_display_name(cls, v: object) -> object:
        """前後の空白を除去する。

        "  田中太郎  " → "田中太郎" に正規化する。
        空白のみの入力 "   " → strip 後に "" → min_length=1 で 422 エラー。

        mode="before" を使う理由:
          Field の min_length/max_length チェックより先に strip を実行するため。
          mode="after" だと strip 前の値で長さチェックされてしまう。
          raw input は任意の型が来うるため、isinstance で str を確認する。
        """
        if isinstance(v, str):
            return v.strip()
        return v

    @field_validator("display_name", mode="after")
    @classmethod
    def validate_display_name_chars(cls, v: str | None) -> str | None:
        """ホワイトリストに一致しない文字が含まれていたら拒否する。

        mode="after" を使う理由:
          strip 済み・長さチェック済みの値に対して文字種チェックを行うため。
          strip 前の値でチェックすると、前後の空白が誤って引っかかる可能性がある。
        """
        if v is None:
            return None
        if not DISPLAY_NAME_RE.match(v):
            raise ValueError(
                "Display name can only contain letters, numbers, spaces, "
                "hyphens, and underscores"
            )
        return v


class TokenResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    refresh_token: str


class UserResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: int
    email: str
    display_name: str | None
    role: str
    is_active: bool
    created_at: datetime
