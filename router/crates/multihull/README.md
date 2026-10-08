# multihull

Stateless request router for multi-provider GPU inference with automatic failover. Part of the Multihull project.

Run with `multihull --config router.toml`; add `--check-config` to validate the file and exit. Configuration and operation: https://multihull.pages.dev/docs/router/overview/.

The library holds the router's modules: `core` (IO-free routing policy), `proxy`, `cp` (control-plane snapshot sources), `auth`, `obs`, `admin` and `tls`.
