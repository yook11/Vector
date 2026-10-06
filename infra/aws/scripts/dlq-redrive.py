#!/usr/bin/env python3
"""一時踏み台経由のDLQ運用コマンド。"""

from sqs_operations.redrive import main

if __name__ == "__main__":
    raise SystemExit(main())
