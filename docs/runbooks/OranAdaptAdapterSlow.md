# OranAdaptAdapterSlow

**Fires when** the 95th percentile of the calls to one adapter of one port took more than 30 s
over 10 minutes (`adapter_call_duration_seconds`), for 15 minutes.

**Impact.** Stages that call it slow down (see OranAdaptStageSlow); API requests that call it
(model listings, readiness) are slow or time out.

**Check.**
1. `operation`: slow `registry` calls are usually large artifact transfers; a slow `deployment`
   `deploy` or `set_traffic` is the serving system's API itself.
2. The external system's own latency and load.
3. The network path: egress policy, proxies, DNS.

**Fix.** Fix the dependency or its path; raise the adapter's timeout key only if the latency is
expected (each adapter documents its keys in `docs/adapters/`).
