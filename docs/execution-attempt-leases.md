# Execution Attempt Leases

Every executable attempt receives a unique lease token and expiry. A scheduled execution, an execution whose previous retryable attempt completed, or an `executing` execution whose lease expired receives an `execute` admission and a new attempt. A concurrent delivery receives `active_attempt` while the current lease is valid and is delayed with NAK until reclaim is possible. `waiting_feedback` and `duplicate_terminal` deliveries never invoke the handler and are ACKed because durable state already owns or completed the work.

Request workers renew ownership on the NATS progress-heartbeat cadence. Renewal and completion compare both attempt ID and token, and cannot succeed after expiry. Completion clears the lease. Reclaim closes the expired attempt as retryable before creating the next attempt, so a stale worker cannot complete after another worker takes ownership.

`EXECUTION_ATTEMPT_LEASE_SECONDS` optionally sets the lease duration. It defaults to `NATS_ACK_WAIT` and must be at least twice `NATS_HEARTBEAT_INTERVAL`, leaving a bounded renewal margin. Cancellation stops renewal without completing the attempt; a process crash has the same durable outcome once the stored lease expires.

Migration `00012.execution-attempt-leases` gives pre-existing incomplete attempts already-expired leases so their next delivery can recover them immediately.
