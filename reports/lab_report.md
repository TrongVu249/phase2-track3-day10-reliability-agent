# Day 10 Reliability Report

## 1. Architecture summary

This lab implements a reliability layer for an LLM gateway. A request first checks the semantic cache, then flows through per-provider circuit breakers and a provider fallback chain. If every provider fails or is unavailable, the gateway returns a static degraded response instead of crashing.

```
User Request
    |
    v
[ReliabilityGateway]
    |
    +--> [Semantic Cache: memory or Redis] -- hit --> cached response
    |
    +--> [CircuitBreaker: primary] -- allowed --> Primary Provider
    |
    +--> [CircuitBreaker: backup]  -- allowed --> Backup Provider
    |
    +--> [CircuitBreaker: rescue]  -- allowed --> Rescue Provider
    |
    +--> [Static fallback response]
```

Core reliability controls:

- `CircuitBreaker` implements `CLOSED -> OPEN -> HALF_OPEN -> CLOSED`, transition logging, fail-fast behavior, and probe recovery.
- `ResponseCache` uses word tokens plus character 3-gram cosine similarity, TTL eviction, privacy guardrails, and false-hit rejection for mismatched 4-digit numbers.
- `SharedRedisCache` stores query/response hashes in Redis so multiple gateway instances can share cache state.
- `ReliabilityGateway` routes requests through cache, primary provider, backup provider, rescue provider, and static fallback.
- `RunMetrics` captures availability, error rate, P50/P95/P99 latency, fallback success rate, cache hit rate, circuit opens, recovery time, and cost impact.

## 2. Configuration

| Setting | Value | Rationale |
|---|---:|---|
| primary fail_rate | 0.25 | Baseline primary has enough failures to exercise fallback and circuit breaker behavior. |
| backup fail_rate | 0.05 | Backup is more reliable, making it suitable as the fallback provider. |
| rescue fail_rate | 0.0 | Final reliability tier prevents provider-chain failures from reaching static fallback. |
| failure_threshold | 3 | Opens the circuit after repeated failures while avoiding overreacting to one transient error. |
| reset_timeout_seconds | 0.5 | Gives failed providers a short recovery window while still producing recovery evidence in the lab load test. |
| success_threshold | 1 | One successful probe closes the circuit quickly in the lab's short load test. |
| cache backend | redis | Verifies shared cache behavior for multi-instance deployments. |
| cache TTL | 300 seconds | Keeps responses fresh while allowing repeated lab queries to hit cache. |
| similarity_threshold | 0.92 | Conservative threshold to reduce accidental semantic false hits. |
| load_test requests | 100 per scenario | Produces 300 total requests across the three configured chaos scenarios. |
| load_test seed | 42 | Makes chaos metrics reproducible across repeated runs. |

## 3. SLO definitions

| SLI | SLO target | Actual value | Met? |
|---|---|---:|---|
| Availability | >= 99% | 100.00% | Yes |
| Latency P95 | < 2500 ms | 319.20 ms | Yes |
| Fallback success rate | >= 95% | 100.00% | Yes |
| Cache hit rate | >= 10% | 69.33% | Yes |
| Recovery time | < 5000 ms | 819.37 ms | Yes |

Result: all defined SLOs are met in this reproducible run. The rescue provider absorbed residual provider-chain failures, while Redis cache reduced cost and circuit pressure.

## 4. Metrics

Generated from `reports/metrics.json`:

| Metric | Value |
|---|---:|
| total_requests | 300 |
| availability | 1.0000 |
| error_rate | 0.0000 |
| latency_p50_ms | 277.61 |
| latency_p95_ms | 319.20 |
| latency_p99_ms | 356.88 |
| fallback_success_rate | 1.0000 |
| cache_hit_rate | 0.6933 |
| circuit_open_count | 25 |
| recovery_time_ms | 819.3731307983398 |
| estimated_cost | 0.038506 |
| estimated_cost_saved | 0.208000 |

## 5. Cache comparison

| Metric | Without cache | With Redis cache | Delta |
|---|---:|---:|---:|
| latency_p50_ms | 271.24 | 277.61 | +6.37 ms |
| latency_p95_ms | 316.57 | 319.20 | +2.63 ms |
| estimated_cost | 0.134580 | 0.038506 | -0.096074 (-71.4%) |
| cache_hit_rate | 0.0000 | 0.6933 | +0.6933 |
| circuit_open_count | 55 | 25 | -30 |

Caching reduced estimated provider cost by 71.4% and reduced circuit-open events by 30 in this run. Latency stayed roughly flat because simulated provider latency dominates, but the cache sharply reduced provider calls and therefore cost exposure.

## 6. Redis shared cache evidence

Redis matters because in-memory cache entries are local to one process. In production, multiple gateway replicas behind a load balancer need shared cache state; otherwise the same repeated query can miss on every instance.

Verification evidence:

```text
$ docker compose up -d
Container phase2-track3-day10-reliability-agent-redis-1 Started

$ python -m pytest tests\test_redis_cache.py -q
......                                                                   [100%]
6 passed in 1.73s
```

The Redis test suite verifies:

- exact set/get through Redis
- TTL expiry
- shared state across two `SharedRedisCache` instances
- privacy-sensitive queries are not cached
- false-hit detection rejects similar prompts with different years

`SharedRedisCache` uses Redis hashes with deterministic query hashes, a configurable prefix, and Redis `EXPIRE` for cleanup. The chaos runner clears the lab cache prefix at the start of each Redis-backed simulation so repeated runs are reproducible.

## 7. Chaos scenarios

| Scenario | Expected behavior | Observed behavior | Pass/Fail |
|---|---|---|---|
| primary_timeout_100 | Primary provider fails 100%; backup should absorb traffic and circuit should open. | Fallback path handled the injected primary failure while preserving availability. | Pass |
| primary_flaky_50 | Primary provider fails intermittently; circuit should reduce repeated failed calls. | Circuit breaker opened during flaky periods and traffic continued through backup/cache. | Pass |
| all_healthy | Providers use configured baseline rates; most traffic should use primary or cache. | Gateway served requests through primary/cache with fallback available for residual failures. | Pass |

All configured scenarios passed their explicit reliability criteria in `metrics.json`.

## 8. Test and quality evidence

```text
$ python -m pytest -q
35 passed, 7 xpassed in 3.77s

$ python -m ruff check src tests scripts
All checks passed!

$ python -m mypy src scripts
Success: no issues found in 10 source files
```

The 7 XPASS results come from the lab's TODO tests. They are expected once the TODO implementations are complete.

## 9. Failure analysis

Remaining weakness: circuit breaker state is still process-local. Redis shares cache state, but if one gateway instance opens the primary circuit, another instance will not know and may continue sending traffic to the failing provider until it independently reaches the failure threshold.

Proposed fix: store circuit breaker state in Redis with atomic counters, timestamps, and TTLs. That would let all gateway replicas share provider health state and fail fast consistently across the fleet.

## 10. Next steps

1. Add Redis-backed circuit breaker state for multi-instance fail-fast behavior.
2. Add per-scenario metric breakdowns to the report so each scenario shows availability, fallback rate, and circuit opens independently.
3. Run longer load tests with deterministic random seeds to make SLO evidence less noisy across larger samples.
