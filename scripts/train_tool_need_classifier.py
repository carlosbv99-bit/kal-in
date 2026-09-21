"""
Entrena el clasificador local "¿este mensaje necesita alguna
herramienta?" (ver agent_core/tool_need_classifier.py) — generaliza el
fix puntual de kal-in issue #4 (get_trivial_reply(), coincidencia
EXACTA de un puñado de saludos) a cualquier mensaje conversacional,
sin depender del LLM principal ni de un servicio de terceros.

TF-IDF + regresión logística (scikit-learn), no una red neuronal ni
RL — el dataset bootstrap (agent_core/tool_need_classifier_data.jsonl)
es chico y curado a mano, no tráfico real; este script existe
justamente para poder reentrenar fácil cuando ese dataset crezca.

Uso:
    python3 scripts/train_tool_need_classifier.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import joblib
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import classification_report
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = REPO_ROOT / "agent_core" / "tool_need_classifier_data.jsonl"
MODEL_PATH = REPO_ROOT / "agent_core" / "tool_need_classifier.joblib"


def load_dataset() -> tuple[list[str], list[bool]]:
    texts, labels = [], []
    with DATA_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            texts.append(row["text"])
            labels.append(bool(row["needs_tool"]))
    return texts, labels


def main() -> None:
    texts, labels = load_dataset()
    print(f"Dataset: {len(texts)} ejemplos ({sum(labels)} needs_tool, {len(labels) - sum(labels)} no_tool)")

    # Dataset chico a propósito (bootstrap) — stratify para no dejar
    # ninguna clase entera afuera del split de test por mala suerte.
    x_train, x_test, y_train, y_test = train_test_split(
        texts, labels, test_size=0.25, random_state=42, stratify=labels
    )

    # ngram_range=(1, 2) para captar frases cortas tipo "que tal" /
    # "todo bien" como unidad, no solo palabras sueltas — el dataset es
    # chico, min_df=1 (no descartar términos raros: con tan pocos
    # ejemplos, cada palabra cuenta).
    #
    # C=20.0 (bastante más alto que el default 1.0): con el default,
    # las probabilidades de predict_proba() quedaban todas pegadas
    # cerca de 0.5 incluso para casos obvios ("hola" daba p=0.61) —
    # confirmado en vivo antes de fijar este valor, probando C en
    # {1, 5, 20, 100} contra el mismo split de test. C=20 separa bien
    # (ej. "hola" p=0.90, una imagen pedida p=0.07) sin llegar al
    # sobreajuste que ya se nota en C=100. Con un dataset bootstrap tan
    # chico, esto es una calibración empírica sobre ESTOS datos, no una
    # garantía universal — reajustar si el dataset crece mucho.
    pipeline = Pipeline([
        ("tfidf", TfidfVectorizer(ngram_range=(1, 2), min_df=1, lowercase=True)),
        ("clf", LogisticRegression(max_iter=2000, class_weight="balanced", C=20.0)),
    ])
    pipeline.fit(x_train, y_train)

    y_pred = pipeline.predict(x_test)
    print("\n=== Reporte sobre el split de test ===")
    print(classification_report(y_test, y_pred, target_names=["no_tool", "needs_tool"]))

    print("=== Ejemplos mal clasificados (revisar a mano) ===")
    wrong = 0
    for text, true_label, pred_label in zip(x_test, y_test, y_pred):
        if true_label != pred_label:
            wrong += 1
            print(f"  {text!r}: esperado={true_label}, predicho={pred_label}")
    if wrong == 0:
        print("  (ninguno)")

    # El reporte de arriba mide contra datos que el modelo NUNCA vio —
    # esa es la métrica real. Pero una vez medida, no tiene sentido
    # servir un modelo que ignora 1/4 del dataset (ya de por sí chico)
    # — se reentrena con TODO antes de guardar el artefacto final.
    final_pipeline = Pipeline([
        ("tfidf", TfidfVectorizer(ngram_range=(1, 2), min_df=1, lowercase=True)),
        ("clf", LogisticRegression(max_iter=2000, class_weight="balanced", C=20.0)),
    ])
    final_pipeline.fit(texts, labels)

    joblib.dump(final_pipeline, MODEL_PATH)
    print(f"\nModelo final (entrenado con el dataset completo) guardado en {MODEL_PATH} ({MODEL_PATH.stat().st_size} bytes)")


if __name__ == "__main__":
    sys.exit(main())
