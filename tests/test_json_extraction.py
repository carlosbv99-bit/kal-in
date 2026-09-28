"""Tests de agent_core/llm/json_extraction.py."""
from __future__ import annotations

from agent_core.llm.json_extraction import extract_json_array, extract_json_object


def test_extracts_bare_json():
    assert extract_json_object('{"name": "run_code", "arguments": {}}') == {
        "name": "run_code", "arguments": {},
    }


def test_extracts_json_wrapped_in_markdown_fence():
    content = 'Voy a hacer esto:\n```json\n{"steps": ["uno", "dos"]}\n```'
    assert extract_json_object(content) == {"steps": ["uno", "dos"]}


def test_extracts_json_embedded_in_surrounding_text():
    content = 'Claro, aquí tenés: {"steps": ["uno"]} espero que sirva'
    assert extract_json_object(content) == {"steps": ["uno"]}


def test_returns_none_for_empty_content():
    assert extract_json_object("") is None
    assert extract_json_object("   ") is None


def test_returns_none_for_plain_text_without_json():
    assert extract_json_object("No necesito ninguna herramienta para responder esto.") is None


def test_returns_none_for_non_object_json():
    assert extract_json_object("[1, 2, 3]") is None


def test_returns_none_for_malformed_json():
    assert extract_json_object("{esto no es json valido") is None


def test_falls_back_to_embedded_json_when_fence_has_no_object_inside():
    content = '```json\nno es json\n```\npero esto sí: {"steps": ["a"]}'
    assert extract_json_object(content) == {"steps": ["a"]}


# --- extract_json_array() ---
#
# BUG REAL ENCONTRADO EN USO: propose_project_files a veces se
# "imitaba" como texto plano con un array JSON crudo de archivos, sin
# el envoltorio {"name", "arguments"} que reconoce extract_json_object
# — necesitaba su propio detector.


def test_extracts_bare_json_array():
    assert extract_json_array('[{"path": "a.txt", "content": "x"}]') == [{"path": "a.txt", "content": "x"}]


def test_extracts_json_array_wrapped_in_markdown_fence():
    content = 'Te propongo:\n```json\n[{"path": "a.txt", "content": "x"}]\n```'
    assert extract_json_array(content) == [{"path": "a.txt", "content": "x"}]


def test_extracts_json_array_embedded_in_surrounding_text():
    content = 'Acá está: [{"path": "a.txt", "content": "x"}] espero que sirva'
    assert extract_json_array(content) == [{"path": "a.txt", "content": "x"}]


def test_array_returns_none_for_empty_content():
    assert extract_json_array("") is None
    assert extract_json_array("   ") is None


def test_array_returns_none_for_non_array_json():
    assert extract_json_array('{"name": "run_code"}') is None


def test_array_returns_none_for_malformed_json():
    assert extract_json_array("[esto no es json valido") is None


def test_array_tolerates_a_literal_unescaped_newline_inside_a_string_value():
    """
    BUG REAL ENCONTRADO EN USO: proponiendo un proyecto Android real, el
    modelo escribió un salto de línea LITERAL dentro del valor de
    "content" (código Java multilínea) en vez de escaparlo como \\n —
    técnicamente JSON inválido, que json.loads() estricto rechazaba
    entero por un solo carácter mal escapado, perdiendo la propuesta
    completa.
    """
    content = '[{"path": "a.java", "content": "linea1\nlinea2"}]'
    result = extract_json_array(content)
    assert result == [{"path": "a.java", "content": "linea1\nlinea2"}]


def test_object_tolerates_a_literal_unescaped_newline_inside_a_string_value():
    content = '{"name": "run_code", "arguments": {"code": "linea1\nlinea2"}}'
    result = extract_json_object(content)
    assert result == {"name": "run_code", "arguments": {"code": "linea1\nlinea2"}}


# --- allow_embedded=False (B-1, auditoría externa Likay-OS, 2026-09-26):
# usado por agent_core/llm/agent_loop.py::_extract_fallback_tool_call()
# para el camino que EJECUTA una herramienta — un JSON con forma de
# tool call citado dentro de prosa más larga (p.ej. contenido de una
# página web que el modelo resume) no debe contar como candidato ahí. ---


def test_allow_embedded_false_rejects_json_found_within_surrounding_text():
    content = 'Claro, aquí tenés: {"steps": ["uno"]} espero que sirva'
    assert extract_json_object(content, allow_embedded=False) is None


def test_allow_embedded_false_still_accepts_the_whole_content_as_json():
    assert extract_json_object('{"name": "run_code", "arguments": {}}', allow_embedded=False) == {
        "name": "run_code", "arguments": {},
    }


def test_allow_embedded_false_still_accepts_a_fenced_json_block():
    content = 'Voy a hacer esto:\n```json\n{"name": "run_code", "arguments": {}}\n```'
    assert extract_json_object(content, allow_embedded=False) == {"name": "run_code", "arguments": {}}


def test_array_allow_embedded_false_rejects_json_found_within_surrounding_text():
    content = 'Los archivos son: [{"path": "a.py", "content": "x"}] avisame si necesitás más'
    assert extract_json_array(content, allow_embedded=False) is None


def test_array_allow_embedded_false_still_accepts_the_whole_content_as_json():
    content = '[{"path": "a.py", "content": "x"}]'
    assert extract_json_array(content, allow_embedded=False) == [{"path": "a.py", "content": "x"}]
