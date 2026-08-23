# BUG-11 · Sync chat applies immediately with no approval identifier · **High**
Sync POST /v1/chat returned 200 with edits already applied and no job_id / no awaiting_approval state. No identifier to approve or reject. For an approval-gated editor, an apply lane without an approval handle is a bypass. Repro: POST /v1/chat → observe response has no job_id and document already mutated.

# BUG-12 · Read staleness window and export-before-apply race · **Medium-High**
Roster/read issued within ~3 s of an apply returned stale content (old hash). Export requested immediately after apply returned a document without the just-applied edit. The edit was applied but the export missed it — silent data loss. Repro: apply → roster at 120 ms (stale) vs 3100 ms (fresh) → export immediately → diff against source.

# BUG-13 · Session upload silently overwrites held document · **Medium**
Re-uploading a document to the same session_id returned 200 with same durable IDs but replaced content, no version bump or conflict. Caller cannot tell whether it created or clobbered. Repro: upload file A to session S → upload file B to same S → fetch durable_id → content is B, no warning.
