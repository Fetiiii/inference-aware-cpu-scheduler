# Outcome-blind duration gate — final pre-execution amendment

The former 105–180-minute range is withdrawn as a hardware prediction. Recovered original request-only medians are approximately42.47s BIG_ONLY and33.56s ALL_CORES. They exclude startup/model loading and do not measure COMPACT4 or ALL_ALLOWED4. Historical warmup task timings do not establish total fresh-server loading cost.

FULL:72 maximum requests (48 if policy scientifically NOT_ELIGIBLE). STATIC_SIGNAL:48 requests, policy NOT_PLANNED. Both preserve all six final static rounds, six same-configuration training and six held-out validation requests. Before deployment authors approve BOTH complete schedules and the exact technical selection rule. Base plan AUTO is never rewritten. The collaborator cannot choose a tier or shorten a running experiment.

For planning arithmetic only, consider four-worker request cost45/60/90s and per-request non-measured startup/warmup/cleanup overhead10/10/20s; use34s as an illustrative ALL_CORES12 request cost, not a new measurement. Applying25% allowance,30s per-slot cooldown,30s initial cooldown and600s archive reserve gives approximately:

| Assumed four-worker request / overhead | FULL | STATIC_SIGNAL |
|---|---:|---:|
|45s /10s|127min|87min|
|60s /10s|147min|100min|
|90s /20s|201min — exceeds budget|134min|

Additional setup/hash/calibration/retry time and the new300s safety margin are not measured in this historical V2 illustration. COMPACT4 may be slower still. This is not an assurance or an evidence-based runtime range. No new workload was timed to create this table; it is not the executable selection rule.

After exactly four excluded technical smoke requests, BEFORE exploration/calibration/validation/final results, forecast FULL using1.25×max(startup+warmup+request elapsed sum, total backend elapsed)+30s per remaining slot. Unknown selected-initial/policy masks conservatively use the maximum allowance across ALL four smoke arms. Include elapsed preparation/build/smoke,600s archive reserve and300s additional safety margin. Reserve ALL24 conditional policy slots before scientific eligibility is known. If FULL fits10800s freeze FULL; otherwise forecast STATIC_SIGNAL and freeze it if it fits; otherwise freeze STOP/archive. Only technical elapsed fields enter the decision: never TTFT, ITL, TPS, phase separation or statistical outcomes. The immutable tier_decision.json records sanitized inputs/hash, rule/schedule hashes and both forecasts. Resume verifies the original decision, never reselects with a later clock. No mid-session downgrade after selection.

The maximum session budget is10800s including600s archive reserve, starting at preflight preparation. Optional approved compilation is capped at900s INSIDE this budget, not added to it. Account for previous segments, archive time and wall-clock pauses across resume; a clock rollback stops for review. Before each request additionally reserve its bounded startup+warmup+measured deadlines and cleanup/cooldown. Scientific negative calibration/validation skips the predeclared policy cohort, not valid static observations. Exceeding the budget never changes sample counts or acceptance criteria. Forecasts cannot guarantee archive/disk speed or recovery from power loss; failures still attempt verified archival, with package recovery available.
