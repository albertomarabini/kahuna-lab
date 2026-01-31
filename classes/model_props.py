# classes/infrastructure/vertex_pricing.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from pathlib import Path
import os
import commentjson
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()
LLM_PRICING_ENV_PATH = os.getenv("LLM_PRICING_ENV_PATH")


def _load_pricing_config() -> Dict[str, Any]:
    """
    Load pricing + thresholds + internal configurations from a JSON-with-comments file.
    Fails fast if the file or required top-level keys are missing.
    """
    cfg_path = Path(os.getenv("LLM_PRICING_ENV_PATH"))
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"LLM pricing config file not found at '{cfg_path}'. "
        )

    with cfg_path.open("r", encoding="utf-8") as f:
        data = commentjson.load(f)

    for key in ("MAX_RECORD_THRESHOLD", "MODEL_BASE_PRICE_TABLE", "INTERNAL_CONFIGURATIONS", "SINGLE_MULTIPLIERS"):
        if key not in data or not isinstance(data[key], dict):
            raise ValueError(f"Pricing config missing or invalid key: {key}")

    return data


_PRICING_CONFIG = _load_pricing_config()
MAX_RECORD_THRESHOLD: Dict[str, int] = _PRICING_CONFIG["MAX_RECORD_THRESHOLD"]
MODEL_BASE_PRICE_TABLE: Dict[str, Any] = _PRICING_CONFIG["MODEL_BASE_PRICE_TABLE"]
INTERNAL_CONFIGURATIONS: Dict[str, Any] = _PRICING_CONFIG["INTERNAL_CONFIGURATIONS"]
SINGLE_MULTIPLIERS: Dict[str,float] = _PRICING_CONFIG["SINGLE_MULTIPLIERS"]

#! MODEL BOUNDARIES

def get_model_max_threshold(model_name_str: str) -> int:
    model_name, _ = parse_model_name(model_name_str)
    return MAX_RECORD_THRESHOLD.get(model_name, 15)

#! PRICING API

def _per_million_stripe(rate_usd: float, tokens: int) -> float:
    if rate_usd <= 0.0 or tokens <= 0:
        return 0.0
    rate_usd_cents = rate_usd * 100
    return rate_usd_cents * (tokens / 1_000_000.0)

def estimate_cost_usd(
    llm_model_name: str,
    prompt_tokens: int,
    completion_tokens: int,
    model_configuration:str = None,
    service_tier: str = None,
) -> float:
    """
    Estimate USD cost for a single request, using Vertex per-1M-token prices.

    - Uses prompt_tokens as "input context length" to select short vs long band
      for llm_model_name that have a 200K-token threshold.
    - For llm_model_name without a long band, uses the same rate for both.
    - For OpenAI model (have a service_tier) behaves depending on the # OpenAI Models and service tier
    - All behave according to the multipliers associated with model_configuration in INTERNAL_CONFIGURATIONS
    - llm_model_name is an actual LLM model
    """
    pricing = MODEL_BASE_PRICE_TABLE.get(llm_model_name)
    multipliers = get_expense_multipliers(model_configuration, llm_model_name)
    if pricing is None:
        raise ValueError(f"Missing Price Table for Model {llm_model_name}")

    # !OpenAI Models
    if is_openai_model(llm_model_name):
        pricing = pricing.get(service_tier, pricing.get("default", None))
        if not pricing:
            raise ValueError(f"Missing Price Tiers for GPT Model {llm_model_name}")
        in_rate = pricing["input_short"]
        out_rate = pricing["output_short"]
    # !VertexAI Models
    elif pricing.get("long_threshold_tokens", None) is not None and pricing.get("input_long", None) is not None:
        # decide which band to use
        if prompt_tokens > pricing.long_threshold_tokens:
            print(f"\033[93m\033[3mlong_threshold_tokens\033[0m")
            in_rate = pricing["input_long"]
            out_rate = pricing["output_long"] if pricing["output_long"] is not None else pricing["output_short"]
        else:
            in_rate = pricing["input_short"]
            out_rate = pricing["output_short"]
    else:
        in_rate = pricing["input_short"]
        out_rate = pricing["output_short"]

    cost = _per_million_stripe(in_rate, prompt_tokens) + _per_million_stripe(out_rate, completion_tokens)
    price = _per_million_stripe(in_rate, prompt_tokens) * multipliers[0]
    price += _per_million_stripe(out_rate, completion_tokens) * multipliers[1]
    return float(cost), float(price)

def get_internal_model_configuration(configuration_name: str) -> Tuple[str,str,str]:
    """
    Gets and validates a cmodel_configuration from the INTERNAL_CONFIGURATIONS table
    Internal Model configurations are touples of 1 to 3 internal_model_name values
    that define what our main, architect and secondary models are
    This method is designed to fail fast if a configuration is not yet in place
    Or the data within is not correct
    """
    mdl_confs = []; exp_mult = None
    try:
        if INTERNAL_CONFIGURATIONS.get(configuration_name, None):
            mdl_confs=INTERNAL_CONFIGURATIONS.get(configuration_name).get("model_configuration",None)
            exp_mult=INTERNAL_CONFIGURATIONS.get(configuration_name).get("expense_multiplier",None)
        else:
            raise ValueError(f"get_internal_model_configuration: configuration name not found: {configuration_name}")
        if not mdl_confs or not exp_mult:
            raise ValueError(f"get_internal_model_configuration: configuration {configuration_name} missing either model_configuration or expense_multiplier")

        if len(mdl_confs)== 2:
            mdl_confs = [mdl_confs[0], mdl_confs[1], mdl_confs[1]]
        elif len(mdl_confs)==1:
            mdl_confs = [mdl_confs[0], mdl_confs[0], mdl_confs[0]]
        elif len(mdl_confs) != 3:
            raise ValueError(
                f"get_internal_model_configuration: expected 1–3 models, got {len(mdl_confs)} for {configuration_name}"
            )

        # !Validating Configuration
        b_mdls = set()
        for c in mdl_confs:
            b_name, _ = parse_model_name(c)
            b_mdls.add(b_name)
        # !Are all the model names being used in the MODEL_BASE_PRICE_TABLE
        missing = [n for n in b_mdls if n not in MODEL_BASE_PRICE_TABLE]
        if missing:
            raise ValueError(
                f"get_internal_model_configuration: model name(s) {missing} not found in INTERNAL_CONFIGURATIONS.model_configuration {configuration_name}"
            )
        # !Are all the models in model_configuration having a record in expense_multiplier
        if b_mdls != set(exp_mult.keys()):
            raise ValueError(
                f"get_internal_model_configuration: INTERNAL_CONFIGURATIONS model name(s) in model_configuration not found in expense_multiplier for config {configuration_name}"
            )
        if any(len(exp_mult[k]) < 2 for k in exp_mult.keys()):
            raise ValueError(
                f"get_internal_model_configuration: INTERNAL_CONFIGURATIONS.expense_multiplier missing multiplier records {configuration_name}"
            )


        return tuple(mdl_confs)

    except Exception as e:
        raise ValueError from e


def get_expense_multipliers(configuration_name: str, llm_model_name: str) -> float:
    """
    llm_model_name is the actual name used by the llm, not a wildcard
    """
    try:
        if llm_model_name:
            if configuration_name and INTERNAL_CONFIGURATIONS.get(configuration_name, None):
                em = INTERNAL_CONFIGURATIONS.get(configuration_name).get("expense_multiplier", None)
                if em:
                    mult = em.get(llm_model_name, None)
                    if mult != None:
                        return float(mult[0]), float(mult[1]) # prompt_tokens, completion _tokens
            else:
                mult = SINGLE_MULTIPLIERS.get(llm_model_name, None)
                if mult != None:
                        return float(mult[0]), float(mult[1]) # prompt_tokens, completion _tokens
    except Exception as e:
        raise Exception("get_internal_model_configuration", e)

    raise ValueError(f"get_internal_model_configuration: configuration name or model not found:{configuration_name},{llm_model_name}")


# !######################################################################################################
#! UTILS
# !######################################################################################################

def is_openai_model(model_name) -> bool:
    # keep it simple; adjust if you start using exotic names
    prefixes = ("gpt-", "gpt4", "gpt-4", "gpt-5")
    return any(model_name.startswith(p) for p in prefixes)

def parse_model_name(raw: str) -> Tuple[str, Dict[str, Any]]:
    """Parse strings like:
        - 'gpt-5.1_low_low'
        - 'gpt-5.1_standard'
        - 'gpt-5.1_fast'
        - 'gpt-5.1_deep_low'
    into (base_model, openai_params).
    """
    raw = (raw or "").strip()
    if not raw:
        raise ValueError(f"parse_model_name: No Model Name passed. ")

    parts = raw.split("_")
    base = parts[0]
    if len(parts) <= 1:
        return base, {}

    verbosity: Optional[str] = None
    reasoning_effort: Optional[str] = None
    service_tier: Optional[str] = None

    verbosity_tokens = {"low", "medium", "high"}
    reasoning_tokens = {"none", "minimal", "low", "medium", "high", "xhigh"}
    service_tier_tokens = {"auto", "default", "flex", "priority"}

    # Wildcard presets tuned for GPT-5.1
    wildcards: Dict[str, Tuple[Optional[str], Optional[str], Optional[str]]] = {
        # “Standard” good default: concise + some reasoning
        # Docs show low/low as a recommended fast configuration for 5.1.
        "standard": ("low", "low", None),
        "std": ("low", "low", None),
        "medium":("medium", "medium", None),
        "stronger":("medium", "high", None),
        "strongest":("medium", "xhigh", None),

        # Maximum speed on 5.1: none reasoning, short answers
        "fast": ("low", "none", None),

        # Deeper analysis: keep verbosity moderate, crank reasoning
        "deep": ("medium", "high", None),
        "analyze": ("medium", "high", None),

        # Using flex pricing
        "standard-flex": ("low", "low",   "flex"),
        "fast-flex":     ("low", "none",  "flex"),
        "deep-flex":     ("medium","high","flex"),

        # Using priority pricing
        "standard-priority": ("low","low","priority"),
    }

    unknown = []
    for tok in parts[1:]:
        t = tok.strip().lower()
        if not t:
            continue

        # 1) Wildcard presets
        if t in wildcards:
            w_verb, w_reason, w_tier = wildcards[t]
            if verbosity is None and w_verb is not None:
                verbosity = w_verb
            if reasoning_effort is None and w_reason is not None:
                reasoning_effort = w_reason
            if service_tier is None and w_tier is not None:
                service_tier = w_tier
            continue

        # 2) Explicit verbosity token
        if verbosity is None and t in verbosity_tokens:
            verbosity = t
            continue

        # 3) Explicit reasoning token
        if reasoning_effort is None and t in reasoning_tokens:
            reasoning_effort = t
            continue

        # 4) Explicit service tier token (flex/priority/default/auto)
        if service_tier is None and t in service_tier_tokens:
            service_tier = t
            continue

        # 5) Unknown token
        unknown.append(t)

    if unknown:
        raise ValueError(f"parse_model_name: Unknown model suffix token(s) {unknown} in '{raw}'. ")

    params: Dict[str, Any] = {}
    if verbosity is not None:
        params.setdefault("text", {})["verbosity"] = verbosity
    if reasoning_effort is not None:
        params.setdefault("reasoning", {})["effort"] = reasoning_effort
    if service_tier is not None:
        params["service_tier"] = service_tier
    else:
        params["service_tier"] = "default"

    return base, params

