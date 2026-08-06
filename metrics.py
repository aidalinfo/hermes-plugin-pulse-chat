# -*- coding: utf-8 -*-
"""Metriques d'execution de l'agent — partie PURE.

L'app ne peut pas deviner le modele employe, les tokens consommes ni la duree
de raisonnement : elle ne voit que ce qui entre et sort. Ces valeurs doivent
donc remonter par le plugin, dans le champ optionnel ``metrics`` du POST
sortant.

Le `metadata` que Hermes passe a ``send()`` est de forme LIBRE et varie selon
les versions et les fournisseurs. Ce module accepte plusieurs conventions
courantes plutot que d'en imposer une : mieux vaut recuperer la mesure sous le
nom qu'elle porte que de n'avoir rien du tout. L'app renormalise et borne de
son cote (shared/analytics.ts) — elle ne fait pas confiance a ce qui arrive.
"""

from typing import Any, Dict, Optional

#: Cles acceptees pour la duree, par ordre de preference.
_DURATION_KEYS = ("duration_ms", "durationMs", "latency_ms", "latencyMs", "elapsed_ms")
#: Cles acceptees pour les tokens (convention Anthropic puis OpenAI).
_INPUT_KEYS = ("input_tokens", "inputTokens", "prompt_tokens", "promptTokens")
_OUTPUT_KEYS = ("output_tokens", "outputTokens", "completion_tokens", "completionTokens")


def _first_number(source: Dict[str, Any], keys) -> Optional[int]:
    for key in keys:
        value = source.get(key)
        if isinstance(value, bool):
            continue  # bool est un int en Python : jamais une mesure
        if isinstance(value, (int, float)) and value == value:  # NaN exclu
            return int(value)
    return None


def _first_string(source: Dict[str, Any], keys) -> Optional[str]:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def extract_metrics(metadata: Any) -> Optional[Dict[str, Any]]:
    """Construit le bloc ``metrics`` du payload, ou ``None`` si rien d'exploitable.

    Renvoyer ``None`` plutot qu'un dict de quatre ``null`` evite d'alourdir
    chaque POST avec un champ vide : le contrat cote app le declare optionnel.
    """
    if not isinstance(metadata, dict):
        return None

    # Un sous-dict `metrics` ou `usage` est prioritaire, sinon on lit a plat.
    nested = metadata.get("metrics")
    usage = metadata.get("usage")
    sources = [s for s in (nested, usage, metadata) if isinstance(s, dict)]
    if not sources:
        return None

    model = None
    input_tokens = None
    output_tokens = None
    duration_ms = None
    for source in sources:
        model = model or _first_string(source, ("model", "modelName", "model_name"))
        if input_tokens is None:
            input_tokens = _first_number(source, _INPUT_KEYS)
        if output_tokens is None:
            output_tokens = _first_number(source, _OUTPUT_KEYS)
        if duration_ms is None:
            duration_ms = _first_number(source, _DURATION_KEYS)

    if model is None and input_tokens is None and output_tokens is None and duration_ms is None:
        return None

    return {
        "model": model,
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "durationMs": duration_ms,
    }
