"""リサーチのRedis側の依存を、呼ばれた内容を記録するだけの実装に置き換える。"""

import json
from datetime import datetime
from uuid import UUID

from app.agent.live_updates.stream import agent_run_live_stream_key


class RecordingLiveRedis:
    """実行状況の配信先の代わりに、runごとの配信イベントを記録する。"""

    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, str]]] = []

    def pipeline(self) -> "_RecordingPipeline":
        return _RecordingPipeline(self)

    async def exists(self, *keys: str) -> int:
        return 0

    def terminal_events(self, run_id: UUID) -> list[dict]:
        key = agent_run_live_stream_key(run_id)
        return [
            json.loads(fields["payload"])
            for entry_key, fields in self.entries
            if entry_key == key and fields["type"] == "terminal"
        ]


class _RecordingPipeline:
    def __init__(self, redis: RecordingLiveRedis) -> None:
        self._redis = redis
        self._replies: list[object] = []

    def xadd(self, key: str, fields: dict[str, str], **_options) -> None:
        self._redis.entries.append((key, fields))
        self._replies.append(f"{len(self._redis.entries)}-0")

    def expire(self, key: str, seconds: int) -> None:
        self._replies.append(True)

    async def execute(self) -> list[object]:
        return self._replies


class RecordingRunEnqueuer:
    def __init__(self) -> None:
        self.enqueued: list[UUID] = []
        self.failure: Exception | None = None

    async def enqueue(self, run_id: UUID) -> None:
        self.enqueued.append(run_id)
        if self.failure is not None:
            raise self.failure


class RecordingDeadlineScheduler:
    def __init__(self) -> None:
        self.reserved: list[tuple[UUID, datetime]] = []

    async def reserve(self, run_id: UUID, deadline_at: datetime) -> None:
        self.reserved.append((run_id, deadline_at))
