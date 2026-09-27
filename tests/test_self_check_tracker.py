"""
Tests de agent_core/llm/self_check_tracker.py::SelfCheckTracker —
extraído de agent_core/llm/agent_loop.py (2026-07-27) para bajar la
densidad de ese archivo. Estos tests cubren la clase de forma aislada;
tests/test_agent_loop.py sigue cubriendo el comportamiento end-to-end
vía AgentLoop.run().
"""
from __future__ import annotations

from dataclasses import dataclass

from agent_core.llm.self_check_tracker import SelfCheckTracker


@dataclass
class _FakeArtifact:
    uri: str


def test_a_tool_is_not_checked_before_any_analyze_image_call():
    tracker = SelfCheckTracker()
    assert tracker.is_checked("image_generation") is False


def test_analyze_image_over_an_artifact_generated_this_turn_marks_the_origin_tool_as_checked():
    tracker = SelfCheckTracker()
    tracker.record_artifact(_FakeArtifact(uri="data/artifacts/images/x.png"), "image_generation")

    tracker.note_check_if_applicable("analyze_image", {"image_path": "data/artifacts/images/x.png"})

    assert tracker.is_checked("image_generation") is True
    assert tracker.as_frozenset() == frozenset({"image_generation"})


def test_analyze_image_over_an_unrelated_image_never_marks_anything():
    """No debe confundirse una imagen EXTERNA (no generada en este
    turno) con un autochequeo real."""
    tracker = SelfCheckTracker()
    tracker.record_artifact(_FakeArtifact(uri="data/artifacts/images/x.png"), "image_generation")

    tracker.note_check_if_applicable("analyze_image", {"image_path": "otra/imagen/no/generada/aca.png"})

    assert tracker.is_checked("image_generation") is False


def test_note_check_if_applicable_ignores_non_analyze_image_tools():
    tracker = SelfCheckTracker()
    tracker.record_artifact(_FakeArtifact(uri="data/artifacts/images/x.png"), "image_generation")

    tracker.note_check_if_applicable("image_editing", {"image_path": "data/artifacts/images/x.png"})

    assert tracker.is_checked("image_generation") is False


def test_record_artifact_ignores_artifacts_without_a_uri():
    tracker = SelfCheckTracker()
    tracker.record_artifact(_FakeArtifact(uri=""), "image_generation")

    tracker.note_check_if_applicable("analyze_image", {"image_path": ""})

    assert tracker.is_checked("image_generation") is False


def test_record_artifact_ignores_none():
    tracker = SelfCheckTracker()
    tracker.record_artifact(None, "image_generation")  # no debe lanzar
    assert tracker.artifact_paths_this_turn == {}


def test_should_cut_turn_only_after_the_second_rejection():
    tracker = SelfCheckTracker()

    assert tracker.should_cut_turn("image_generation") is False
    tracker.record_rejection("image_generation")
    assert tracker.should_cut_turn("image_generation") is False
    tracker.record_rejection("image_generation")
    assert tracker.should_cut_turn("image_generation") is True


def test_build_cut_short_final_answer_cites_the_last_real_artifact_when_one_exists():
    tracker = SelfCheckTracker()
    tracker.record_artifact(_FakeArtifact(uri="data/artifacts/images/v1.png"), "image_generation")
    tracker.record_artifact(_FakeArtifact(uri="data/artifacts/images/v2.png"), "image_generation")

    answer = tracker.build_cut_short_final_answer("image_generation")

    assert "Generé el resultado" in answer
    assert "Puedes ver el resultado arriba" in answer


def test_build_cut_short_final_answer_is_honest_when_nothing_was_ever_generated():
    tracker = SelfCheckTracker()

    answer = tracker.build_cut_short_final_answer("image_generation")

    assert "No logré completar el pedido" in answer
