"""Walk-forward research harness for edge discovery.

Everything here is offline research tooling: no live orders, no paper-session
mutation. Data flows one way -- Binance history in, gauntlet reports out. A
strategy only earns promotion consideration by beating every baseline
out-of-sample, after costs, on data it never saw during selection.
"""
