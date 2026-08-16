# Paper-Trading Operational Acceptance — 2026-08-11

## Decision

- Paper-trading operational acceptance: **PASS**.
- Database least-privilege acceptance: **PASS for the tested configuration**.
- Worker credential-isolation acceptance: **PASS for the tested configuration**.
- Freshness and process-exit recovery acceptance: **PASS for the tested configuration**.
- Real-capital authorization: **NOT GRANTED**.
- Final HTTPS/authenticated public route: **PENDING DNS**.
- Legacy Binance-key revocation: **PENDING USER ACTION**.

This is a deliberately narrow operational acceptance. It is not a blanket
production, security, profitability, or real-capital certification.

## Accepted Controls

- Every non-local PostgreSQL HBA rule uses `scram-sha-256`; no non-local
  `trust` or `md5` rule is present.
- Application workloads use the scoped `vibe_worker` and `vibe_app` roles.
  No PostgreSQL superuser connection string is part of the application
  runtime.
- A negative write test performed as `vibe_dev` returned
  `permission denied` as required.
- All nine worker freshness healthchecks reported healthy with zero restarts.
- Both 15-minute workers completed two consecutive post-rollout cycles while
  restart recovery was disabled.
- Process-exit recovery was enabled only after that observation. Docker health
  failures remain observable because Docker restart policies do not restart a
  container merely for becoming unhealthy.
- Worker and supervisor environments contain no Binance, Bybit, OKX, Gate,
  Render, or Telegram credential variables.
- Binance market data is obtained through public unauthenticated endpoints.

## Evidence Integrity Identifier and Scope

The captured evidence file is:

```text
/root/vibe-evidence/post-role-fingerprint-20260811_001838.txt
SHA-256: 14e97f956dd249365f820b893df77ebcb29afb667132d4e2069da0927c4eea33
Captured: 2026-08-11T00:18:38Z
```

The digest is an integrity identifier for the bytes of that evidence file. It
can detect a content change when recomputed and compared with this recorded
value, but it does not establish provenance, authorship, or approval. Those
properties require an additional trusted mechanism such as a digital
signature or MAC. The evidence file records:

- the rendered Compose-project hash at capture time;
- active database connection identities;
- active HBA rules;
- paper/live safety flags;
- running container image IDs; and
- a catalog-driven PostgreSQL schema/data manifest.

It does not automatically certify a later deployment, current mutable ledger
contents, DNS, TLS, external authentication, exchange-side credential state,
or observations made after 00:18:38Z. Freshness and negative-write results are
separate operational evidence unless incorporated into a newly generated,
hashed evidence bundle.

Configuration or image equivalence must be established by a new capture and
an explicit comparison against declared mutable fields. The digest must never
be treated as a certificate that silently follows configuration drift.

## Capital Boundary

The accepted deployment is paper-only. The recorded sample was:

- 152 closed trades;
- approximately `+$74.91` gross P&L;
- `-$15.17` fees;
- `+$59.74` net P&L; and
- eight of nine arms net positive.

The sample is not statistically sufficient for real-capital authorization.
The 150-trade checkpoint is a kill checkpoint, not a promotion gate, and the
nine-arm comparison is exposed to selection and multiple-comparison bias.

Morning Glory had four retained trades, approximately `-$0.28` net P&L, and a
t-statistic near `-0.47`. No inference should be made from that sample beyond
continuing paper observation.

Any real-capital proposal requires a separate acceptance record with
pre-registered sample sizes and statistical thresholds, per-arm analysis,
exit-reason analysis, venue-specific costs, capital/risk limits, credential
controls, execution reconciliation, and rollback conditions.

## Network and Authentication Boundary

At acceptance time, `trading.mostarindustries.com` did not resolve.
Consequently:

- HTTPS and application authentication were staged but not externally proven;
- port `5899` remained temporarily public; and
- rebinding `5899` to loopback was not authorized.

The required completion order is:

1. Confirm the DNS A record resolves to `31.97.180.251`.
2. Issue the certificate and prove the HTTPS dashboard path.
3. Enable and positively test authentication through that path.
4. Rebind port `5899` to `127.0.0.1`.
5. Confirm both `5899` and `8899` listen only on `127.0.0.1`.

## Credential-Revocation Boundary

The running system neither requires nor exposes the retired Binance key.
Revocation of that key in Binance's console remains a user-controlled action
needed to eliminate any residual validity outside this deployment.

## Explicit Non-Claims

This acceptance does not certify profitability, statistical significance,
suitability for real capital, complete Internet-facing security, working DNS,
working production HTTPS, final public-route authentication, third-party key
revocation, or behavior outside the captured and separately observed tested
configuration.

Any change crossing these boundaries requires fresh evidence and a new
acceptance decision.
