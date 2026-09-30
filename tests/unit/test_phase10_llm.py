"""Hardening Phase 10: the LLM is optional and fenced.

- LLM_ENABLED is off by default and the whole pipeline runs with egress blocked (a test that
  fails on any outbound connection or name lookup).
- Three provider adapters (anthropic, gemini, openai-compatible) and the adapter template pass
  the LLM conformance suite against local wire-format doubles (tests/unit/llm_doubles.py).
- The guard enforces timeouts' consequences, retries with backoff, a circuit breaker and
  input/token/cost caps; every refusal is typed and degrades to the deterministic rules with the
  reason recorded on the decision, the audit trail and the job's ``llm_calls``.
- Prompts are versioned and every call is stamped with the prompt's id, version and hash.

The live provider smoke is heavy and runs only with ORAN_LLM_LIVE=1.
"""

from __future__ import annotations

import ipaddress
import json
import os
import pickle
import re
import socket
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from llm_doubles import INPUT_TOKENS, OUTPUT_TOKENS, REPLY, closed_port_url, local_llm
from sqlalchemy import select
from test_phase9_orchestrator import (  # noqa: F401 - pytest fixtures
    _frame,
    _seed_model,
    registry,
    session_factory,
)
from test_phase15_decision import _package

from oran_adapt.adapters import llm_providers as providers
from oran_adapt.bootstrap import build_llm
from oran_adapt.conformance.llm import LLM_CHECKS, LlmContext, run_llm
from oran_adapt.core import metrics
from oran_adapt.core.config import Settings
from oran_adapt.core.enums import AuditAction, EngineKind, Strategy
from oran_adapt.core.errors import (
    ConfigurationError,
    LlmBudgetExceededError,
    LlmCircuitOpenError,
    LlmUnavailableError,
)
from oran_adapt.core.outbound import OutboundPolicy
from oran_adapt.core.schemas import DriftEvent
from oran_adapt.db.base import session_scope
from oran_adapt.db.models import AuditLog, LlmUsage
from oran_adapt.decision.engine import decide
from oran_adapt.llm import calls
from oran_adapt.llm.guard import (
    CircuitBreaker,
    GuardedLlmClient,
    GuardPolicy,
    MemoryUsageLedger,
    SqlUsageLedger,
    reset_breakers,
)
from oran_adapt.llm.prompts import ADAPTATION_CODE, STRATEGY_SELECTION, get_prompt
from oran_adapt.orchestrator.pipeline import run_adaptation_job

ROOT = Path(__file__).resolve().parents[2]
KEY = "sk-phase10-secret-0123456789"
LOOPBACK = OutboundPolicy(["127.0.0.1"])


@pytest.fixture(autouse=True)
def _fresh_breakers():
    reset_breakers()
    yield
    reset_breakers()


def _fallbacks(reason: str) -> float:
    return metrics.LLM_FALLBACKS.labels(reason)._value.get()


class _Inner:
    """An LLMPort that fails ``fail`` times, then answers and reports 100 in / 50 out tokens."""

    def __init__(self, reply: str = "ok", *, fail: int = 0, error: Exception | None = None):
        self.reply, self.fail, self.error = reply, fail, error
        self.calls = 0

    def complete(self, *, system: str, prompt: str) -> str:
        self.calls += 1
        if self.fail:
            self.fail -= 1
            raise self.error or LlmUnavailableError("down", provider="inner")
        calls.report_usage(100, 50)
        return self.reply


def _guard(inner, ledger=None, **policy) -> tuple[GuardedLlmClient, list[float]]:
    slept: list[float] = []
    client = GuardedLlmClient(inner, "inner", GuardPolicy(**policy), ledger, sleep=slept.append)
    return client, slept


def _ask(client) -> str:
    return client.complete(system="system", prompt="prompt")


# ---- off by default ----------------------------------------------------------------------------
@pytest.mark.smoke
def test_llm_is_off_by_default_and_build_llm_returns_none() -> None:
    settings = Settings(_env_file=None, llm_provider="openai-compatible",
                        llm_openai_base_url="https://llm.example.com/v1", llm_openai_model="m")
    assert Settings(_env_file=None).llm_enabled is False
    assert build_llm(settings) is None  # a provider is named, but LLM_ENABLED is off


@pytest.mark.smoke
def test_enabling_the_llm_without_a_provider_is_a_configuration_error() -> None:
    with pytest.raises(ConfigurationError, match="LLM_PROVIDER"):
        Settings(_env_file=None, llm_enabled=True, llm_provider="none")


@pytest.mark.smoke
def test_an_enabled_provider_comes_wrapped_in_the_guard() -> None:
    settings = Settings(_env_file=None, llm_enabled=True, llm_provider="openai-compatible",
                        llm_openai_base_url="https://llm.example.com/v1", llm_openai_model="m")
    client = build_llm(settings)
    assert isinstance(client, GuardedLlmClient)
    assert client.provider == "openai-compatible"
    assert pickle.loads(pickle.dumps(client)).provider == "openai-compatible"


# ---- provider conformance ----------------------------------------------------------------------
def _settings_for(kind: str, url: str) -> Settings:
    keys = {
        "anthropic": {"anthropic_base_url": url, "anthropic_api_key": KEY},
        "gemini": {"gemini_base_url": url, "gemini_api_key": KEY},
        "openai-compatible": {"llm_openai_base_url": url, "llm_openai_model": "local",
                              "llm_openai_api_key": KEY},
    }[kind]
    return Settings(_env_file=None, llm_timeout_s=1, **keys)


_FACTORIES = {"anthropic": providers._anthropic, "gemini": providers._gemini,
              "openai-compatible": providers._openai_compatible}


@pytest.mark.smoke
@pytest.mark.parametrize("kind", sorted(_FACTORIES))
def test_provider_adapter_conformance(kind: str) -> None:
    make = _FACTORIES[kind]
    with local_llm(kind) as server:
        port = make(_settings_for(kind, server.url))
        ctx = LlmContext(unreachable=lambda: make(_settings_for(kind, closed_port_url())),
                         secret=KEY, expected_reply=REPLY)
        assert run_llm(port, ctx) == list(LLM_CHECKS)
        assert server.requests, "the double saw no request"
        sent = json.dumps(server.requests[0]["body"])
        assert "Say hello." in sent  # the user turn reached the provider
        calls.take_usage()
        port.complete(system="s", prompt="p")
        assert calls.take_usage() == calls.Usage(INPUT_TOKENS, OUTPUT_TOKENS)


@pytest.mark.smoke
def test_template_adapter_conformance(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "templates" / "llm-adapter"))
    sys.modules.pop("adapter", None)
    import adapter  # the template module

    def make(url: str):
        return adapter.JsonLlmClient(url, timeout_s=1, max_tokens=64, policy=LOOPBACK,
                                     api_key=KEY)

    with local_llm("template") as server:
        ctx = LlmContext(unreachable=lambda: make(closed_port_url()), secret=KEY,
                         expected_reply=REPLY)
        assert run_llm(make(server.url), ctx) == list(LLM_CHECKS)
    sys.modules.pop("adapter", None)


@pytest.mark.smoke
def test_an_endpoint_outside_the_outbound_policy_is_refused_before_sending() -> None:
    client = providers.OpenAICompatibleLlmClient(
        "http://169.254.169.254/v1", "m", 1, max_tokens=8, policy=OutboundPolicy())
    with pytest.raises(LlmUnavailableError) as caught:
        client.complete(system="s", prompt="p")
    assert caught.value.to_dict()["context"]["cause"] == "OutboundBlockedError"


@pytest.mark.smoke
def test_http_clients_are_only_built_by_the_outbound_policy() -> None:
    offenders = []
    pattern = re.compile(r"\bhttpx2?\.(Async)?Client\(")
    for path in (ROOT / "src" / "oran_adapt").rglob("*.py"):
        if path.name == "outbound.py" and path.parent.name == "core":
            continue
        if pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


# ---- the guard ---------------------------------------------------------------------------------
@pytest.mark.smoke
def test_retries_back_off_exponentially_and_sum_usage() -> None:
    inner = _Inner(fail=2)
    client, slept = _guard(inner, max_retries=2, retry_backoff_s=0.5)
    with calls.recording() as made:
        assert calls.ask(client, get_prompt(STRATEGY_SELECTION), "u") == "ok"
    assert inner.calls == 3
    assert slept == [0.5, 1.0]
    assert made[0].outcome == "ok" and made[0].provider == "inner"
    assert made[0].output_tokens == 50  # the two failed attempts count their input only


@pytest.mark.smoke
def test_the_last_failure_is_raised_typed() -> None:
    client, slept = _guard(_Inner(fail=5), max_retries=1, retry_backoff_s=0.1)
    with pytest.raises(LlmUnavailableError):
        _ask(client)
    assert slept == [0.1]


@pytest.mark.smoke
def test_an_unexpected_adapter_exception_becomes_llm_unavailable() -> None:
    client, _ = _guard(_Inner(fail=1, error=RuntimeError("provider bug")))
    with pytest.raises(LlmUnavailableError) as caught:
        _ask(client)
    assert caught.value.reason == "unavailable"
    assert caught.value.to_dict()["context"]["cause"] == "RuntimeError"


@pytest.mark.smoke
def test_the_circuit_opens_after_repeated_failures_and_refuses_without_sending() -> None:
    inner = _Inner(fail=2)
    client, _ = _guard(inner, breaker_failure_threshold=2)
    for _ in range(2):
        with pytest.raises(LlmUnavailableError):
            _ask(client)
    with pytest.raises(LlmCircuitOpenError) as caught:
        _ask(client)
    assert caught.value.reason == "circuit_open"
    assert inner.calls == 2  # the third call was never sent
    assert metrics.LLM_CIRCUIT_OPEN.labels("inner")._value.get() == 1


@pytest.mark.smoke
def test_the_breaker_half_opens_after_the_reset_time() -> None:
    now = [0.0]
    breaker = CircuitBreaker("p", threshold=1, reset_s=10, clock=lambda: now[0])
    breaker.failure()
    with pytest.raises(LlmCircuitOpenError):
        breaker.acquire()
    now[0] = 10.0
    breaker.acquire()  # the one trial call
    assert breaker.state == breaker.HALF_OPEN
    breaker.failure()  # the trial failed: open again at once
    assert breaker.state == breaker.OPEN
    now[0] = 20.0
    breaker.acquire()
    breaker.success()
    assert breaker.state == breaker.CLOSED


@pytest.mark.smoke
def test_a_prompt_over_the_input_cap_is_refused_before_sending() -> None:
    inner = _Inner()
    client, _ = _guard(inner, max_input_tokens=5)
    with pytest.raises(LlmBudgetExceededError) as caught:
        client.complete(system="s" * 40, prompt="p" * 40)
    assert caught.value.reason == "budget_exceeded"
    assert caught.value.to_dict()["context"]["cap"] == "max_input_tokens"
    assert inner.calls == 0


@pytest.mark.smoke
def test_the_token_budget_is_enforced() -> None:
    inner, ledger = _Inner(), MemoryUsageLedger()
    client, _ = _guard(inner, ledger, token_budget=200, max_output_tokens=50)
    _ask(client)  # 150 tokens spent
    with pytest.raises(LlmBudgetExceededError) as caught:
        _ask(client)  # 150 + (input + 50 worst-case output) > 200
    assert caught.value.to_dict()["context"]["cap"] == "token_budget"
    assert inner.calls == 1


def _cost_capped(ledger) -> tuple[GuardedLlmClient, _Inner]:
    inner = _Inner()
    client, _ = _guard(inner, ledger, cost_budget=0.45, max_output_tokens=50,
                       cost_per_1k_input=1.0, cost_per_1k_output=2.0)
    return client, inner


@pytest.mark.smoke
def test_the_cost_budget_is_enforced() -> None:
    ledger = MemoryUsageLedger()
    client, inner = _cost_capped(ledger)
    _ask(client)
    _ask(client)  # 0.2 each: 0.4 spent
    with pytest.raises(LlmBudgetExceededError) as caught:
        _ask(client)  # 0.4 + worst case 0.1 > 0.45
    assert caught.value.to_dict()["context"]["cap"] == "cost_budget"
    assert inner.calls == 2
    assert [round(row[3], 6) for row in ledger.rows] == [0.2, 0.2]


def test_the_cost_budget_is_shared_through_the_database(migrated_settings) -> None:
    first, _ = _cost_capped(SqlUsageLedger(migrated_settings.database_url))
    second, inner = _cost_capped(SqlUsageLedger(migrated_settings.database_url))
    with calls.recording("job-cost"):
        calls.ask(first, get_prompt(STRATEGY_SELECTION), "u")
        calls.ask(first, get_prompt(STRATEGY_SELECTION), "u")
    with pytest.raises(LlmBudgetExceededError):
        _ask(pickle.loads(pickle.dumps(second)))  # another process, the same budget
    assert inner.calls == 0

    ledger = SqlUsageLedger(migrated_settings.database_url)
    with ledger._get_engine().connect() as conn:
        rows = conn.execute(select(LlmUsage)).all()
    assert len(rows) == 2
    assert {(r.job_id, r.prompt_id, r.prompt_version, r.outcome) for r in rows} == {
        ("job-cost", STRATEGY_SELECTION, "1", "ok")}
    assert ledger.spent(datetime.now(UTC) - timedelta(hours=1))[1] == pytest.approx(0.4)


@pytest.mark.smoke
def test_an_unreadable_ledger_refuses_rather_than_overspends(tmp_path) -> None:
    missing = f"sqlite:///{(tmp_path / 'no-such-table.db').as_posix()}"
    client, _ = _guard(_Inner(), SqlUsageLedger(missing), cost_budget=1.0)
    with pytest.raises(LlmBudgetExceededError):
        _ask(client)


# ---- prompts -----------------------------------------------------------------------------------
@pytest.mark.smoke
def test_prompts_are_versioned_and_stamped(tmp_path) -> None:
    builtin = get_prompt(STRATEGY_SELECTION)
    assert builtin.stamp() == {"prompt_id": STRATEGY_SELECTION, "prompt_version": "1",
                               "prompt_sha256": builtin.sha256}
    assert get_prompt(ADAPTATION_CODE).version == "1"

    (tmp_path / f"{STRATEGY_SELECTION}@2.txt").write_text("Choose one.", encoding="utf-8")
    newest = get_prompt(STRATEGY_SELECTION, Settings(_env_file=None, llm_prompt_dir=str(tmp_path)))
    assert (newest.version, newest.system) == ("2", "Choose one.")
    assert newest.sha256 != builtin.sha256

    pinned = Settings(_env_file=None, llm_prompt_dir=str(tmp_path),
                      llm_prompt_versions={STRATEGY_SELECTION: "1"})
    assert get_prompt(STRATEGY_SELECTION, pinned) == builtin
    with pytest.raises(ConfigurationError):
        get_prompt(STRATEGY_SELECTION, Settings(_env_file=None,
                                                llm_prompt_versions={STRATEGY_SELECTION: "9"}))


# ---- the decision degrades to the rules, with the reason recorded ------------------------------
SETTINGS = Settings(_env_file=None)


@pytest.mark.smoke
def test_no_llm_decides_by_the_rules() -> None:
    decision = decide(_package(), SETTINGS, None)
    assert decision.source == "FALLBACK"
    assert decision.llm == {"used": False, "fallback_reason": "disabled"}


def test_an_unreachable_provider_degrades_to_the_rules(migrated_settings) -> None:
    settings = migrated_settings.model_copy(update={
        "llm_enabled": True, "llm_provider": "openai-compatible", "llm_timeout_s": 1,
        "llm_openai_base_url": closed_port_url(), "llm_openai_model": "m"})
    client = build_llm(settings)
    assert client is not None
    before = _fallbacks("unavailable")
    with calls.recording("job-unreachable") as made:
        decision = decide(_package(), settings, client)

    assert decision.source == "FALLBACK"
    assert decision.strategy == decide(_package(), settings, None).strategy
    assert decision.llm is not None
    assert decision.llm["used"] is False
    assert decision.llm["fallback_reason"] == "unavailable"
    assert decision.llm["prompt_id"] == STRATEGY_SELECTION
    assert decision.llm["prompt_sha256"] == get_prompt(STRATEGY_SELECTION).sha256
    assert [(c.outcome, c.error_code, c.provider) for c in made] == [
        ("unavailable", "LLM_UNAVAILABLE", "openai-compatible")]
    assert _fallbacks("unavailable") == before + 1


@pytest.mark.smoke
@pytest.mark.parametrize(("inner", "policy", "reason"), [
    (_Inner(reply="this is not json"), {}, "invalid_output"),
    (_Inner(), {"max_input_tokens": 1}, "budget_exceeded"),
])
def test_every_llm_failure_is_a_recorded_fallback(inner, policy, reason) -> None:
    client, _ = _guard(inner, **policy)
    decision = decide(_package(), SETTINGS, client)
    assert decision.source == "FALLBACK"
    assert decision.llm is not None and decision.llm["fallback_reason"] == reason


@pytest.mark.smoke
def test_an_open_circuit_is_a_recorded_fallback() -> None:
    client, _ = _guard(_Inner(fail=1), breaker_failure_threshold=1)
    decide(_package(), SETTINGS, client)  # fails and opens the circuit
    decision = decide(_package(), SETTINGS, client)
    assert decision.llm is not None and decision.llm["fallback_reason"] == "circuit_open"


@pytest.mark.smoke
def test_a_valid_llm_choice_is_stamped() -> None:
    reply = json.dumps({"strategy": "FULL_RETRAINING", "confidence": 0.9, "rationale": "x"})
    client, _ = _guard(_Inner(reply=reply))
    decision = decide(_package(), SETTINGS, client)
    assert decision.source == "LLM"
    assert decision.llm is not None
    assert decision.llm["used"] is True and decision.llm["fallback_reason"] is None
    assert decision.llm["prompt_version"] == "1"


# ---- end to end --------------------------------------------------------------------------------
# Pinned serving requirements skip MLflow's inference subprocess (tens of seconds per save here).
PINNED = ["scikit-learn"]


def _pinned(settings: Settings) -> Settings:
    return settings.model_copy(update={"mlflow_pip_requirements": PINNED})


def _loopback(host: object) -> bool:
    if not isinstance(host, str):
        return True  # AF_UNIX paths and the like never leave the machine
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture
def egress_blocked(monkeypatch) -> list[tuple[str, object]]:
    """Fail, and record, every connection or name lookup that would leave the machine."""
    attempts: list[tuple[str, object]] = []
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def _refuse(kind: str, target: object) -> OSError:
        attempts.append((kind, target))
        return OSError(f"egress blocked by the test: {kind} {target!r}")

    def connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if not _loopback(host):
            raise _refuse("connect", address)
        return real_connect(self, address)

    def connect_ex(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if not _loopback(host):
            raise _refuse("connect", address)
        return real_connect_ex(self, address)

    def getaddrinfo(host, *args, **kwargs):
        if host is not None and not _loopback(host if isinstance(host, str) else host.decode()):
            raise socket.gaierror(str(_refuse("resolve", host)))
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return attempts


def test_the_egress_blocker_catches_an_outbound_request(egress_blocked) -> None:
    client = providers.OpenAICompatibleLlmClient(
        "https://llm.example.com/v1", "m", 1, max_tokens=8, policy=OutboundPolicy())
    with pytest.raises(LlmUnavailableError):
        client.complete(system="s", prompt="p")
    assert egress_blocked  # the blocker saw it, so an empty list below means none was made


def test_mlflow_telemetry_is_off_unless_opted_in(registry) -> None:  # noqa: F811
    from mlflow.telemetry import get_telemetry_client

    assert registry._telemetry is False
    assert get_telemetry_client() is None
    assert pickle.loads(pickle.dumps(registry))._telemetry is False


def test_pinned_pip_requirements_are_written_without_inference(tmp_path) -> None:
    from sklearn.linear_model import LogisticRegression

    from oran_adapt.adapters.registry.mlflow.flavors import MlflowFlavorHandler

    frame = _frame(20, prb_lo=0.0, prb_hi=1.0, prb_seed=1, rsrp_seed=10)
    model = LogisticRegression().fit(frame[["prb_util", "rsrp"]], frame["label"])
    out = MlflowFlavorHandler((), PINNED).save(model, "sklearn", str(tmp_path / "m"))
    requirements = (Path(out) / "requirements.txt").read_text(encoding="utf-8").split()
    assert "scikit-learn" in requirements


def test_the_pipeline_runs_end_to_end_with_egress_blocked(
    egress_blocked, session_factory, registry, migrated_settings, tmp_path  # noqa: F811
) -> None:
    historical = _frame(60, prb_lo=0.0, prb_hi=1.0, prb_seed=1, rsrp_seed=10)
    drifted = _frame(60, prb_lo=5.0, prb_hi=6.0, prb_seed=3, rsrp_seed=11)
    _seed_model(session_factory, registry, migrated_settings, model_id="offline-model",
                mlflow_name="offline_model_mlflow", historical=historical, drifted=drifted,
                warm_start=False, pip_requirements=PINNED)
    migrated_settings = _pinned(migrated_settings)
    before = _fallbacks("disabled")

    with session_scope(session_factory) as session:
        result = run_adaptation_job(
            session, DriftEvent(model_id="offline-model", drift_detected=True),
            migrated_settings, registry=registry, llm_client=build_llm(migrated_settings),
            workdir=str(tmp_path / "work"),
        )

    assert result.outcome == "REGISTERED", result.reason
    assert result.strategy == Strategy.FULL_RETRAINING
    assert result.decision is not None and result.decision.llm is not None
    assert result.decision.llm["fallback_reason"] == "disabled"
    assert result.llm_calls == []
    assert _fallbacks("disabled") > before
    assert egress_blocked == []


def test_a_failed_llm_adaptation_retrains_instead_of_failing_the_job(
    session_factory, registry, migrated_settings, tmp_path  # noqa: F811
) -> None:
    historical = _frame(60, prb_lo=0.0, prb_hi=1.0, prb_seed=1, rsrp_seed=10)
    drifted = _frame(60, prb_lo=0.1, prb_hi=1.1, prb_seed=2, rsrp_seed=11)
    _seed_model(session_factory, registry, migrated_settings, model_id="llm-down-model",
                mlflow_name="llm_down_model_mlflow", historical=historical, drifted=drifted,
                warm_start=True, pip_requirements=PINNED)
    migrated_settings = _pinned(migrated_settings)
    # The decision is made by the rules (the reply is not JSON), and the adaptation code the
    # same reply stands for is refused by the sandbox's static check.
    client, _ = _guard(_Inner(reply="import os\nos.system('echo hi')\n"))

    with session_scope(session_factory) as session:
        result = run_adaptation_job(
            session, DriftEvent(model_id="llm-down-model", drift_detected=True),
            migrated_settings, registry=registry, llm_client=client,
            workdir=str(tmp_path / "work"),
        )

    assert result.outcome == "REGISTERED", result.reason
    assert result.strategy == Strategy.FINE_TUNING
    assert result.candidate is not None
    assert result.candidate.engine == EngineKind.SKLEARN_FULL_RETRAIN
    assert result.candidate.applied_strategy == Strategy.FULL_RETRAINING
    assert "unsafe_code" in result.candidate.adaptation_note
    assert [c["prompt_id"] for c in result.llm_calls] == [STRATEGY_SELECTION, ADAPTATION_CODE]
    with session_scope(session_factory) as session:
        audited = session.scalars(select(AuditLog).where(
            AuditLog.action == AuditAction.ADAPTER_GENERATED,
            AuditLog.model_id == "llm-down-model")).all()
    assert [a.detail for a in audited] == [{"fallback_reason": "unsafe_code"}]


# ---- live provider (off by default) ------------------------------------------------------------
@pytest.mark.heavy
@pytest.mark.skipif(os.environ.get("ORAN_LLM_LIVE") != "1",
                    reason="live provider smoke: set ORAN_LLM_LIVE=1 and configure a provider"
                           " [owner=TNDEVIL4U expires=2027-03-31]")
def test_live_provider_smoke() -> None:
    settings = Settings()
    client = build_llm(settings)
    assert client is not None, "ORAN_LLM_LIVE=1 needs LLM_ENABLED=true and a provider"
    with calls.recording() as made:
        text = calls.ask(client, get_prompt(STRATEGY_SELECTION), "Reply with {}.")
    assert text
    assert made[0].outcome == "ok"
