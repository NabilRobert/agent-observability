-- Every recorded step of every traced Pitstop run, as one JSON array, in
-- recorded order. Read-only. trace_payloads (the prompts, responses and
-- built context) is deliberately never read.
SELECT coalesce(json_agg(row_to_json(t) ORDER BY t.run_id, t.seq), '[]'::json)
FROM (
  SELECT e.id, e.run_id, e.parent_id, e.seq, e.kind, e.name, e.status,
         to_char(e.started_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"') AS started_at,
         to_char(e.ended_at   AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"') AS ended_at,
         e.model, e.tokens_in, e.tokens_out,
         e.cost_usd::text AS cost_usd,
         e.summary, e.meta, e.error_message
  FROM trace_events e
) t;
