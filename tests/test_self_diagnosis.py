"""
Tests de agent_core/self_diagnosis.py::SelfDiagnosisAgent.

Usa un FakeOllamaClient guionado (mismo patrón que test_planner.py) y un
FakeSelfModificationManager inyectado (no el real: acá se prueba la
ORQUESTACIÓN — que se llame a propose() con los argumentos correctos y que
su resultado viaje tal cual — no el pipeline interno de self_modification,
que ya tiene su propia suite en test_self_modification.py). Tampoco
requiere Ollama real ni corre pytest en subproceso.
"""
from __future__ import annotations

import pytest

from agent_core.llm.ollama_client import OllamaError
from agent_core.llm.provider import ChatResponse
from agent_core.self_diagnosis import InvariantCheckResult, SelfDiagnosisAgent
from agent_core.self_modification import SelfModProposal


class FakeOllamaClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, messages, model=None, tools=None):
        self.calls.append({"messages": messages, "model": model})
        if not self.responses:
            raise AssertionError("FakeOllamaClient se quedó sin respuestas guionadas")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeSelfModificationManager:
    def __init__(self, proposal=None):
        self.proposal = proposal
        self.calls = []

    def propose(self, target_path, proposed_source, justification, test_args=None):
        self.calls.append({"target_path": target_path, "proposed_source": proposed_source, "justification": justification})
        return self.proposal


def _healthy_check():
    return InvariantCheckResult(name="algo", healthy=True, detail="todo bien", target_path="algo.py")


def _unhealthy_check():
    return InvariantCheckResult(name="algo", healthy=False, detail="algo está roto", target_path="algo.py")


@pytest.fixture
def fake_proposal():
    return SelfModProposal(
        id="prop-1", target_path="algo.py", proposed_source="# corregido",
        justification="", status="pending_human_approval",
    )


def _agent(tmp_path, responses, check, proposal=None, self_mod=None):
    (tmp_path / "algo.py").write_text("# contenido original\n", encoding="utf-8")
    llm = FakeOllamaClient(responses)
    fake_self_mod = self_mod or FakeSelfModificationManager(proposal=proposal)
    agent = SelfDiagnosisAgent(
        llm_client=llm,
        self_modification=fake_self_mod,
        project_root=tmp_path,
        invariant_checks={"algo": check},
    )
    return agent, llm, fake_self_mod


def test_healthy_invariant_returns_no_issue_without_llm_call(tmp_path):
    agent, llm, self_mod = _agent(tmp_path, responses=[], check=_healthy_check)

    result = agent.diagnose_and_propose_fix("algo")

    assert result.status == "no_issue"
    assert result.proposal is None
    assert llm.calls == []
    assert self_mod.calls == []


def test_unhealthy_invariant_calls_llm_and_proposes_fix(tmp_path, fake_proposal):
    response = ChatResponse(
        content="La causa es X.\n```python\n# contenido corregido\n```"
    )
    agent, _llm, self_mod = _agent(tmp_path, responses=[response], check=_unhealthy_check, proposal=fake_proposal)

    result = agent.diagnose_and_propose_fix("algo")

    assert result.status == "diagnosed"
    assert result.diagnosis == "La causa es X."
    assert result.proposal is fake_proposal
    assert len(self_mod.calls) == 1
    assert self_mod.calls[0]["target_path"] == "algo.py"
    assert self_mod.calls[0]["proposed_source"] == "# contenido corregido\n"
    assert "algo" in self_mod.calls[0]["justification"]


def test_prompt_includes_diagnosis_and_current_source(tmp_path, fake_proposal):
    response = ChatResponse(content="causa.\n```python\nfix\n```")
    agent, llm, _ = _agent(tmp_path, responses=[response], check=_unhealthy_check, proposal=fake_proposal)

    agent.diagnose_and_propose_fix("algo")

    user_message = llm.calls[0]["messages"][-1]["content"]
    assert "algo está roto" in user_message
    assert "# contenido original" in user_message


def test_llm_error_returns_llm_error_status_without_proposing(tmp_path):
    agent, _llm, self_mod = _agent(
        tmp_path, responses=[OllamaError("ollama caído")], check=_unhealthy_check
    )

    result = agent.diagnose_and_propose_fix("algo")

    assert result.status == "llm_error"
    assert result.proposal is None
    assert self_mod.calls == []


def test_response_without_code_fence_does_not_propose_anything(tmp_path):
    response = ChatResponse(content="No estoy seguro de cuál es el problema.")
    agent, _llm, self_mod = _agent(tmp_path, responses=[response], check=_unhealthy_check)

    result = agent.diagnose_and_propose_fix("algo")

    assert result.status == "no_fix_proposed"
    assert result.proposal is None
    assert self_mod.calls == []
    assert result.diagnosis == "No estoy seguro de cuál es el problema."


def test_unknown_invariant_raises_value_error(tmp_path):
    agent, _, _ = _agent(tmp_path, responses=[], check=_healthy_check)

    with pytest.raises(ValueError):
        agent.diagnose_and_propose_fix("no_existe")


def test_diagnosed_run_is_audited(tmp_path, fake_proposal, monkeypatch):
    from audit.audit_log import audit_log

    monkeypatch.setattr(audit_log, "path", tmp_path / "audit.log")
    response = ChatResponse(content="causa.\n```python\nfix\n```")
    agent, _, _ = _agent(tmp_path, responses=[response], check=_unhealthy_check, proposal=fake_proposal)

    agent.diagnose_and_propose_fix("algo")

    entries = audit_log.tail(5)
    assert entries[0]["event_type"] == "self_diagnosis_run"
    assert entries[0]["outcome"] == "success"


def test_healthy_check_is_not_audited(tmp_path, monkeypatch):
    from audit.audit_log import audit_log

    monkeypatch.setattr(audit_log, "path", tmp_path / "audit.log")
    agent, _, _ = _agent(tmp_path, responses=[], check=_healthy_check)

    agent.diagnose_and_propose_fix("algo")

    assert audit_log.tail(5) == []
