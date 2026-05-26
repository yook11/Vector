"""pipeline_events 監査の per-stage semantic API。

各 stage の audit row 組み立て (payload shape の SSoT) を集約する。

- ``acquisition`` — Stage 1 (article_acquisition)
- ``completion`` — Stage 2 (article_completion)
- ``curation`` — Stage 3 (curation)
- ``assessment`` — Stage 4 (assessment)
- ``embedding`` — Stage 5 (embedding)
- ``briefing`` — 週次 LLM briefing

各 *AuditRepository は ``app.audit.repository.PipelineEventRepository`` を
compose し、generic な append SQL は repository に委譲する。本 package の
責務は Stage 固有の payload shape と Layer1Category / code の決定に閉じる。
"""
