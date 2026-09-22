"""
Planner: descompone un objetivo en subtareas ordenadas antes de que
AgentLoop las ejecute una por una, en vez de que el agente improvise un
paso ReAct a la vez sin ver el objetivo completo descompuesto.

Diseño deliberadamente conservador: Planner.plan() nunca puede hacer
que el agente falle por su cuenta — cualquier problema (Ollama caído,
respuesta no parseable, lista de pasos vacía) degrada a un plan de un
solo paso igual al objetivo completo, es decir, exactamente el
comportamiento de hoy sin planner. PlanningAgentLoop reutiliza
AgentLoop.run() sin modificarlo — cada subtarea es un ciclo ReAct
completo e independiente, compartiendo la misma memoria entre pasos.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from agent_core.llm.agent_loop import AgentLoop, AgentRunResult, AgentStep
from agent_core.llm.json_extraction import extract_json_object
from agent_core.llm.ollama_client import OllamaClient
from sdk.permissions import Permission
from agent_core.llm.provider import LLMProvider, ProviderError
from utils.logger import get_logger

logger = get_logger(__name__)

PLANNER_SYSTEM_PROMPT = """Eres el planificador de kal, un agente de IA con herramientas reales \
(ejecutar código, memoria, generación de imagen/audio/video, y más). Tu única tarea es descomponer \
el objetivo del usuario en una lista ORDENADA de subtareas concretas, cada una ejecutable de forma \
independiente por un agente con esas herramientas.

Reglas:
- Si el objetivo ya es una sola acción simple (una pregunta, un cálculo, generar un solo artefacto),
  devolvé un único paso igual al objetivo — no lo descompongas artificialmente.
- Cada paso debe ser autocontenido y accionable, no una descripción vaga.
- Respondé ÚNICAMENTE con un JSON de la forma {"steps": ["paso 1", "paso 2", ...]}, sin texto
  alrededor ni explicaciones.
"""


@dataclass
class PlanStep:
    description: str


@dataclass
class Plan:
    goal: str
    steps: list[PlanStep] = field(default_factory=list)


@dataclass
class PlanStepResult:
    step: str
    result: AgentRunResult


@dataclass
class PlanRunResult:
    goal: str
    plan: Plan
    step_results: list[PlanStepResult] = field(default_factory=list)
    final_answer: str = ""
    status: str = "success"  # success | llm_error | max_steps_exceeded


class Planner:
    def __init__(self, llm_client: LLMProvider | None = None):
        self.llm = llm_client or OllamaClient()

    def plan(self, goal: str, model: str | None = None) -> Plan:
        messages = [
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": goal},
        ]
        try:
            response = self.llm.chat(messages, model=model)
        except ProviderError as e:
            logger.warning(f"Planner: no se pudo contactar al proveedor de LLM, degradando a plan de un solo paso: {e}")
            return self._single_step_plan(goal)

        steps = self._parse_steps(response.content)
        if not steps:
            logger.info("Planner: respuesta no parseable o sin pasos, degradando a plan de un solo paso")
            return self._single_step_plan(goal)
        return Plan(goal=goal, steps=[PlanStep(description=s) for s in steps])

    @staticmethod
    def _single_step_plan(goal: str) -> Plan:
        return Plan(goal=goal, steps=[PlanStep(description=goal)])

    @staticmethod
    def _parse_steps(content: str) -> list[str]:
        data = extract_json_object(content)
        if data is None:
            return []
        steps = data.get("steps")
        if not isinstance(steps, list):
            return []
        return [s for s in steps if isinstance(s, str) and s.strip()]


class PlanningAgentLoop:
    def __init__(self, agent_loop: AgentLoop, planner: Planner | None = None):
        self.agent_loop = agent_loop
        self.planner = planner or Planner(llm_client=agent_loop.llm)

    def run(
        self,
        goal: str,
        model: str | None = None,
        use_planner: bool = True,
        max_steps: int | None = None,
        history: list[dict] | None = None,
        session_context: dict | None = None,
        denied_permissions: frozenset[Permission] = frozenset(),
        client: str | None = None,
        required_capabilities: list[str] | None = None,
        on_step: Callable[[AgentStep], None] | None = None,
    ) -> PlanRunResult:
        plan = self.planner.plan(goal, model=model) if use_planner else Planner._single_step_plan(goal)

        step_results: list[PlanStepResult] = []
        for step in plan.steps:
            run_result = self.agent_loop.run(
                step.description, model=model, max_steps=max_steps,
                history=history, session_context=session_context,
                denied_permissions=denied_permissions, client=client,
                required_capabilities=required_capabilities, on_step=on_step,
            )
            step_results.append(PlanStepResult(step=step.description, result=run_result))
            if run_result.status == "llm_error":
                # Ollama caído no se recupera en el siguiente paso — cortar
                # ahí en vez de seguir acumulando fallos idénticos.
                return PlanRunResult(
                    goal=goal, plan=plan, step_results=step_results,
                    final_answer=run_result.final_answer, status="llm_error",
                )

        final_answer = (
            step_results[0].result.final_answer
            if len(step_results) == 1
            else self._synthesize(goal, step_results, model=model)
        )
        return PlanRunResult(
            goal=goal, plan=plan, step_results=step_results,
            final_answer=final_answer, status=self._aggregate_status(step_results),
        )

    def _synthesize(self, goal: str, step_results: list[PlanStepResult], model: str | None = None) -> str:
        summary = "\n".join(f"- {r.step}: {r.result.final_answer}" for r in step_results)
        messages = [
            {
                "role": "system",
                "content": (
                    "Eres kal-in. Te doy un objetivo original y los resultados de cada subtarea ya "
                    "ejecutada para cumplirlo. Da la respuesta final al usuario, directa y concisa, "
                    "integrando esos resultados — no vuelvas a ejecutar nada."
                ),
            },
            {"role": "user", "content": f"Objetivo: {goal}\n\nResultados de subtareas:\n{summary}"},
        ]
        try:
            response = self.agent_loop.llm.chat(messages, model=model)
            return response.content
        except ProviderError:
            # Degrada con gracia: mostrar los resultados crudos si la
            # llamada de síntesis falla, en vez de perder el trabajo ya hecho.
            return summary

    @staticmethod
    def _aggregate_status(step_results: list[PlanStepResult]) -> str:
        statuses = {r.result.status for r in step_results}
        if "max_steps_exceeded" in statuses:
            return "max_steps_exceeded"
        return "success"
