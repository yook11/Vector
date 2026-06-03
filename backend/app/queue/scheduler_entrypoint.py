"""統合 scheduler entrypoint — 4 つの stock TaskiqScheduler を 1 event loop で並行実行。

各 scheduler は自分の broker へ kick するため task→queue routing は壊れない (Option B)。
4 プロセス分の full app import (~140MB×4) を 1 回に畳み scheduler VM を 512mb に下げる。

`taskiq scheduler` CLI (``run_scheduler``) が行う処理のうち必要分だけ再現する:
①各 broker に ``is_scheduler_process=True``、②``registry`` の副作用 import (cron 登録)、
③各 ``source.startup()``、④各 ``scheduler.startup()``、
⑤``SchedulerLoop.run`` の無限 loop。CLI arg parse / ``import_object`` 分岐は不要。
"""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import timedelta

import structlog
from taskiq import TaskiqScheduler
from taskiq.abc.schedule_source import ScheduleSource
from taskiq.cli.scheduler.run import SchedulerLoop

import app.queue.registry  # noqa: F401  cron 登録の副作用 import (get_all_tasks を満たす)
from app.logfire_setup import setup_logfire
from app.queue.schedulers import (
    scheduler_briefing,
    scheduler_maintenance,
    scheduler_metadata,
    scheduler_trend_discovery,
)

logger = structlog.get_logger(__name__)

_UPDATE_INTERVAL = timedelta(seconds=60)
_LOOP_INTERVAL = timedelta(seconds=1)
_SCHEDULERS: tuple[TaskiqScheduler, ...] = (
    scheduler_metadata,
    scheduler_trend_discovery,
    scheduler_briefing,
    scheduler_maintenance,
)


def _configure_taskiq_logging() -> None:
    """taskiq scheduler CLI 相当の stdlib ログ可視性を復元する。

    ``taskiq scheduler`` CLI (run_scheduler) は既定 (log_level=INFO) で
    "Starting scheduler" / "Sending task ..." を出す。本 entrypoint は CLI を経由せず
    setup_logfire も stdlib logging を bridge しないため、``taskiq`` logger に INFO
    handler を明示付与し cron 発火の可視性 (= 故障の見える化) を保つ。root は触らず
    taskiq に scope し、他 lib の INFO 騒音は増やさない。
    """
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("[%(asctime)s][%(levelname)-7s][%(name)s] %(message)s")
    )
    taskiq_logger = logging.getLogger("taskiq")
    taskiq_logger.setLevel(logging.INFO)
    taskiq_logger.addHandler(handler)
    taskiq_logger.propagate = False


async def _run_one(scheduler: TaskiqScheduler) -> None:
    """1 scheduler を startup → 無限 loop。終了/cancel 時に必ず shutdown する。

    shutdown は taskiq CLI wrapper ``run_scheduler`` の ``except CancelledError``
    側にあり (run.py)、``SchedulerLoop.run`` 自体は持たない。よって cancel/終了時の
    cleanup (broker.shutdown / source.shutdown) を try/finally で自前保証する。
    ``is_scheduler_process=True`` は WORKER_STARTUP を立てず CLIENT_STARTUP のみ
    発火させ、engine 生成 + AI SDK wiring を出さない (遅延 import を維持する)。
    """
    scheduler.broker.is_scheduler_process = True
    started: list[ScheduleSource] = []
    try:
        for source in scheduler.sources:
            await source.startup()
            started.append(source)
        # broker.startup: CLIENT_STARTUP(log) + middleware + result_backend を起動。
        await scheduler.startup()
        await SchedulerLoop(scheduler).run(
            update_interval=_UPDATE_INTERVAL,
            loop_interval=_LOOP_INTERVAL,
            skip_first_run=False,  # 現運用 (taskiq scheduler 既定) と等価
        )
    finally:
        await scheduler.shutdown()
        for source in started:
            await source.shutdown()


async def _main() -> None:
    _configure_taskiq_logging()
    # setup_logfire は process-level に 1 度だけ (API lifespan と同パターン)。
    setup_logfire("vector-scheduler")
    loops = [
        asyncio.create_task(_run_one(s), name=f"sched_{i}")
        for i, s in enumerate(_SCHEDULERS)
    ]
    stop = asyncio.Event()
    running = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # signal.signal は await 中の wakeup を保証しないため add_signal_handler を使う
        running.add_signal_handler(sig, stop.set)
    stopper = asyncio.create_task(stop.wait(), name="stop")

    done, _pending = await asyncio.wait(
        [*loops, stopper], return_when=asyncio.FIRST_COMPLETED
    )
    # 残り (生存 loop + stopper) を cancel し、各 _run_one の finally を完走させる。
    for task in (*loops, stopper):
        task.cancel()
    await asyncio.gather(*loops, stopper, return_exceptions=True)

    # stopper 以外 (= いずれかの loop) が done = 異常 (loop は無限のはず)。例外の有無を
    # 問わず非0 exit にし「部分稼働」を消す (正常な SIGTERM 経路のみ exit 0)。
    exited_loops = [t for t in loops if t in done]
    if exited_loops:
        for t in exited_loops:
            exc = None if t.cancelled() else t.exception()
            logger.error("scheduler_loop_exited", exc_info=exc)
        # 非0 exit → supervisord autorestart=unexpected → 3連敗で fail_fast。
        raise SystemExit(1)


def main() -> None:
    # 正常 (SIGTERM) は exit 0 / loop 異常終了は SystemExit(1) を伝播する。
    asyncio.run(_main())


if __name__ == "__main__":  # subprocess import 時に scheduler を起動しないため必須
    main()
