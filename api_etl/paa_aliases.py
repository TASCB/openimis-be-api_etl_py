from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Set


DEFAULT_PAA_ALIASES: Dict[str, Dict[str, List[str]]] = {
    "PEMBA": {
        "codes": ["54", "55"],
        "names": ["PEMBA", "KASKAZINI PEMBA", "KUSINI PEMBA"],
    },
    "UNGUJA": {
        "codes": ["51", "52", "53"],
        "names": ["UNGUJA", "KASKAZINI UNGUJA", "KUSINI UNGUJA", "MJINI MAGHARIBI"],
    },
}


def normalize_paa_name(value: Any) -> str:
    if not isinstance(value, str):
        value = "" if value is None else str(value)

    text = unicodedata.normalize("NFD", value.lower().strip())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _scope_key(value: Any) -> str:
    return normalize_paa_name(value).upper()


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if v not in (None, "")]
    return [str(value)]


def _cfg_value(config: Optional[Any], key: str, default=None):
    if config is None:
        return default
    if isinstance(config, dict):
        return config.get(key, default)
    return getattr(config, key, default)


def _merge_alias(alias_map: Dict[str, Dict[str, List[str]]], scope: str, *, names=None, codes=None) -> None:
    key = _scope_key(scope)
    if not key:
        return

    existing = alias_map.setdefault(key, {"codes": [], "names": [key]})
    for name in _as_list(names):
        normalized_name = _scope_key(name)
        if normalized_name and normalized_name not in existing["names"]:
            existing["names"].append(normalized_name)
    for code in _as_list(codes):
        code = code.strip()
        if code and code not in existing["codes"]:
            existing["codes"].append(code)


def get_paa_aliases(config: Optional[Any] = None) -> Dict[str, Dict[str, List[str]]]:
    aliases: Dict[str, Dict[str, List[str]]] = {}

    for scope, data in DEFAULT_PAA_ALIASES.items():
        _merge_alias(
            aliases,
            scope,
            names=data.get("names", []),
            codes=data.get("codes", []),
        )

    if config is None:
        try:
            from api_etl.apps import ApiEtlConfig as config
        except Exception:
            config = None

    configured = _cfg_value(config, "paa_aliases")
    if isinstance(configured, dict):
        for scope, data in configured.items():
            if isinstance(data, dict):
                _merge_alias(
                    aliases,
                    scope,
                    names=data.get("names") or data.get("aliases"),
                    codes=data.get("codes"),
                )
            else:
                _merge_alias(aliases, scope, names=data)

    # Backwards compatibility with the existing questionnaire_district_aliases config.
    questionnaire_aliases = _cfg_value(config, "questionnaire_district_aliases")
    if isinstance(questionnaire_aliases, dict):
        for scope, names in questionnaire_aliases.items():
            _merge_alias(aliases, scope, names=names)

    return aliases


def get_paa_scope_for_location_code(code: Any, config: Optional[Any] = None) -> Optional[str]:
    code = "" if code is None else str(code).strip()
    if not code:
        return None

    aliases = get_paa_aliases(config)

    for scope, data in aliases.items():
        if code in data.get("codes", []):
            return scope

    # Zanzibar child locations are coded under the region code prefix:
    # 54 -> 5402 Micheweni, 55 -> 5501..., 51 -> 5101..., etc.
    for scope, data in aliases.items():
        for parent_code in data.get("codes", []):
            if parent_code and code.startswith(parent_code) and code != parent_code:
                return scope
    return None


def get_paa_scope_for_location_name(name: Any, config: Optional[Any] = None) -> Optional[str]:
    normalized = _scope_key(name)
    if not normalized:
        return None

    for scope, data in get_paa_aliases(config).items():
        names = {_scope_key(alias) for alias in data.get("names", [])}
        if normalized in names:
            return scope
    return None


def get_paa_scope(
    name: Any = None,
    code: Any = None,
    config: Optional[Any] = None,
) -> Optional[str]:
    return get_paa_scope_for_location_code(code, config) or get_paa_scope_for_location_name(
        name, config
    )


def get_location_codes_for_paa_scope(scope: Any, config: Optional[Any] = None) -> List[str]:
    resolved = get_paa_scope_for_location_name(scope, config) or _scope_key(scope)
    if not resolved:
        return []
    return list(get_paa_aliases(config).get(resolved, {}).get("codes", []))


def get_paa_alias_candidates(
    name: Any = None,
    code: Any = None,
    config: Optional[Any] = None,
) -> Set[str]:
    aliases = get_paa_aliases(config)
    candidates = set()

    for value in (name,):
        normalized = normalize_paa_name(value)
        if normalized:
            candidates.add(normalized)

    scope = get_paa_scope(name=name, code=code, config=config)
    if scope and scope in aliases:
        candidates.add(normalize_paa_name(scope))
        for alias in aliases[scope].get("names", []):
            normalized_alias = normalize_paa_name(alias)
            if normalized_alias:
                candidates.add(normalized_alias)

    return candidates


def are_paa_equivalent(left: Any, right: Any, config: Optional[Any] = None) -> bool:
    left_candidates = get_paa_alias_candidates(left, config=config)
    right_candidates = get_paa_alias_candidates(right, config=config)

    left_norm = normalize_paa_name(left)
    right_norm = normalize_paa_name(right)
    if left_norm:
        left_candidates.add(left_norm)
    if right_norm:
        right_candidates.add(right_norm)

    return bool(left_candidates and right_candidates and left_candidates.intersection(right_candidates))


def scopes_for_codes(codes: Iterable[Any], config: Optional[Any] = None) -> Set[str]:
    return {
        scope
        for scope in (get_paa_scope_for_location_code(code, config) for code in codes or [])
        if scope
    }
