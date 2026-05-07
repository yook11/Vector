"""supervisord eventlistener: program FATAL で supervisord 全体を shutdown する.

意図: 一過性故障は autorestart=unexpected + startretries=3 で吸収。3 retries
経過しても起動できない永続バグのみ FATAL に遷移、本 listener が捕捉して
supervisord 自身に SIGTERM → 全 program を stopasgroup で停止 → supervisord exit
→ container exit → Docker / Fly.io が auto-restart。

restart loop が docker ps で visible になるため、永続バグの存在は外側から
明示的に観測できる (一過性故障では発火しないので sibling worker や LLM 呼出を
無駄に巻き込まない)。

graceful shutdown (docker stop) は supervisord が SIGTERM を伝搬し
STOPPING → STOPPED 経路で program を停止、FATAL イベントは発火しないため
本 listener も起動しない。
"""

from __future__ import annotations

import os
import signal
import sys

from supervisor.childutils import listener


def main() -> None:
    while True:
        headers, _payload = listener.wait(sys.stdin, sys.stdout)
        event = headers.get("eventname", "")
        if event == "PROCESS_STATE_FATAL":
            print(
                "fail_fast: program reached FATAL state, shutting down supervisord",
                file=sys.stderr,
                flush=True,
            )
            os.kill(os.getppid(), signal.SIGTERM)
            sys.exit(0)
        listener.ok(sys.stdout)


if __name__ == "__main__":
    main()
