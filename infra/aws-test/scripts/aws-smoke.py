#!/usr/bin/env python3
"""テスト専用AWS環境の実行コマンド。"""

from smoke_runner.controller import entrypoint

if __name__ == "__main__":
    raise SystemExit(entrypoint())
