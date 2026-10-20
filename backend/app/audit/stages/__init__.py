"""pipeline_events 監査の per-stage semantic API。

各 stage の audit row 組み立て (payload shape の SSoT) を集約する。

- ``dispatch`` — acquisition task fan-out
- ``briefing`` — 週次 LLM briefing

各 *AuditRepository は ``app.audit.repository.PipelineEventRepository`` を
compose し、generic な append SQL は repository に委譲する。本 package の
責務は Stage 固有の payload shape、``outcome_code``、failure projection の決定に
閉じる。
"""
