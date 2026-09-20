# countを外した資源の付け替え。本番へ適用済みになったら削除してよい。

moved {
  from = aws_lambda_function.acquisition_consumer[0]
  to   = aws_lambda_function.acquisition_consumer
}

moved {
  from = aws_lambda_event_source_mapping.acquisition_consumer[0]
  to   = aws_lambda_event_source_mapping.acquisition_consumer
}

moved {
  from = aws_lambda_function.assessment_consumer[0]
  to   = aws_lambda_function.assessment_consumer
}

moved {
  from = aws_lambda_event_source_mapping.assessment_consumer[0]
  to   = aws_lambda_event_source_mapping.assessment_consumer
}

moved {
  from = aws_lambda_function.assessment_outbox_relay[0]
  to   = aws_lambda_function.assessment_outbox_relay
}

moved {
  from = aws_scheduler_schedule.assessment_outbox_relay[0]
  to   = aws_scheduler_schedule.assessment_outbox_relay
}

moved {
  from = aws_lambda_function.auth_rate_limit_cleanup[0]
  to   = aws_lambda_function.auth_rate_limit_cleanup
}

moved {
  from = aws_lambda_function_event_invoke_config.auth_rate_limit_cleanup[0]
  to   = aws_lambda_function_event_invoke_config.auth_rate_limit_cleanup
}

moved {
  from = aws_scheduler_schedule.auth_rate_limit_cleanup[0]
  to   = aws_scheduler_schedule.auth_rate_limit_cleanup
}

moved {
  from = aws_lambda_function.completion_consumer[0]
  to   = aws_lambda_function.completion_consumer
}

moved {
  from = aws_lambda_event_source_mapping.completion_consumer[0]
  to   = aws_lambda_event_source_mapping.completion_consumer
}

moved {
  from = aws_lambda_function.completion_outbox_relay[0]
  to   = aws_lambda_function.completion_outbox_relay
}

moved {
  from = aws_scheduler_schedule.completion_outbox_relay[0]
  to   = aws_scheduler_schedule.completion_outbox_relay
}

moved {
  from = aws_lambda_function.curation_consumer[0]
  to   = aws_lambda_function.curation_consumer
}

moved {
  from = aws_lambda_event_source_mapping.curation_consumer[0]
  to   = aws_lambda_event_source_mapping.curation_consumer
}

moved {
  from = aws_lambda_function.curation_outbox_relay[0]
  to   = aws_lambda_function.curation_outbox_relay
}

moved {
  from = aws_scheduler_schedule.curation_outbox_relay[0]
  to   = aws_scheduler_schedule.curation_outbox_relay
}

moved {
  from = aws_lambda_function.embedding_consumer[0]
  to   = aws_lambda_function.embedding_consumer
}

moved {
  from = aws_lambda_event_source_mapping.embedding_consumer[0]
  to   = aws_lambda_event_source_mapping.embedding_consumer
}

moved {
  from = aws_lambda_function.outbox_relay[0]
  to   = aws_lambda_function.outbox_relay
}

moved {
  from = aws_scheduler_schedule.outbox_relay[0]
  to   = aws_scheduler_schedule.outbox_relay
}

moved {
  from = aws_lambda_function.source_dispatch[0]
  to   = aws_lambda_function.source_dispatch
}

moved {
  from = aws_lambda_function_event_invoke_config.source_dispatch[0]
  to   = aws_lambda_function_event_invoke_config.source_dispatch
}
