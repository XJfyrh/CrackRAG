WITH local AS (
 SELECT COALESCE(sum(amount_cny),0) AS known,
        COALESCE(sum(reserved_upper_cny) FILTER(WHERE state<>'SETTLED'),0) AS retained,
        count(*) AS attempts FROM llm_calls WHERE provider='deepseek'
), opening AS (
 SELECT COALESCE(max(known_cny),0) AS known,COALESCE(max(retained_cny),0) AS retained
 FROM release_opening_balance
)
SELECT jsonb_build_object('opening_known_cny',opening.known,'opening_retained_cny',opening.retained,
 'local_known_cny',local.known,'local_unresolved_cny',local.retained,'local_paid_attempts',local.attempts,
 'project_occupied_cny',opening.known+opening.retained+local.known+local.retained,
 'project_remaining_cny',greatest(0,100-opening.known-opening.retained-local.known-local.retained))
FROM local CROSS JOIN opening;
