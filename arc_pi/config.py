"""Project-local configuration; process environment always wins."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import os
import sys
import re
from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parents[1]


class ConfigError(ValueError):
    """Named configuration error (pre-flight, before any model work)."""


class MissingApiKeyError(ConfigError):
    pass

MAX_MODEL_CALLS = 200_000_000
MAX_OUTPUT_TOKENS_PER_CALL = 16_384
REASONING_LEVELS = ('off', 'low', 'medium', 'high', 'max')


@dataclass(frozen=True)
class Config:
    project_dir: Path
    openai_api_key: str
    openai_base_url: str
    model: str
    pi_reasoning: str
    pi_time_budget_seconds: int
    pi_max_model_calls: int
    pi_max_output_tokens: int
    pi_context_bytes: int
    pi_repair_rounds: int
    pi_smoke_port: int | None
    pi_bash_path: str | None
    pi_reviewer: bool = False
    # Compact a session once a request's input tokens pass 80% of this.
    pi_context_tokens: int = 200_000
    visual_model: str = ''
    visual_api_key: str = ''
    visual_base_url: str = ''
    # PI_REVIEWER=self: the executor reviews its own batches (single agent).
    pi_self_review: bool = False
    # Stop starting model calls once input+output tokens reach this (0 = no cap).
    pi_max_run_tokens: int = 0
    # Stop starting model calls once the estimated cost reaches this (CNY, 0 = no cap).
    pi_max_run_cost_cny: float = 0.0
    # Model prices in CNY per 1M tokens: uncached input, output, cache-hit input.
    pi_price_input: float = 0.0
    pi_price_output: float = 0.0
    pi_price_cache: float = 0.0
    # Model steps per prompt before the engine stops and the harness sends "continue" (0 = none).
    # Models that make one tool call per step (glm) need more than parallel callers (deepseek).
    pi_max_turns_per_prompt: int = 0
    # batch meta-tool: 'auto' (only models that rarely call tools in parallel), '1' on, '0' off
    pi_batch_tool: str = 'auto'
    # time box for the executor's review of its own batch (0 = none: it stops when fix rounds
    # make no progress)
    pi_self_review_seconds: int = 0
    # wall-time limit for one prompt (all of its model steps) before the harness aborts it (0 = none)
    pi_prompt_seconds: int = 0
    # time kept at the end for the wrap-up (whole-app review, full suite, repair); 0 = scaled
    pi_final_phase_seconds: int = 0
    # PI_BATCH_REVIEW=0: no review rounds after each batch (only the static contract check and
    # the final whole-app review); faster runs
    pi_batch_review: bool = True
    # planned seconds per batch: the batch count is fitted to (budget - final phase) / this
    pi_seconds_per_batch: int = 600
    # PI_BLIND_TEST=1: a second session writes requirement-only scenario tests in parallel with
    # the build; they become the final verification suite that the repair rounds work against
    pi_blind_test: bool = False
    # PI_FINAL_REVIEW=0: no whole-app review (reference pictures) before final verification
    pi_final_review: bool = True

    def bridge_config(self, tool_port: int, spill_dir: Path) -> dict:
        return {**{name.upper(): getattr(self, name) for name in (
            'openai_base_url', 'model', 'pi_reasoning', 'pi_time_budget_seconds',
            'pi_max_model_calls', 'pi_max_output_tokens', 'pi_context_bytes',
            'pi_repair_rounds', 'pi_max_turns_per_prompt', 'pi_batch_tool')},
            'toolPort': tool_port, 'spillDir': str(spill_dir.resolve()),
            **({'PI_BASH_PATH': self.pi_bash_path} if self.pi_bash_path else {})}


# CNY per 1M tokens (uncached input, output, cache-hit input) for the cost cap, matched by model
# family so a platform MODEL override is still priced; PI_PRICE_* override these.
MODEL_PRICES = ((re.compile(r'glm', re.I), (0.8, 2.8, 0.23)),
                (re.compile(r'deepseek', re.I), (3.0, 9.0, 0.1)))
# Unknown model: price at the most expensive known rate so the cost cap never switches off.
FALLBACK_PRICES = (3.0, 9.0, 0.1)


def model_prices(model: str) -> tuple[float, float, float]:
    for pattern, prices in MODEL_PRICES:
        if pattern.search(model):
            return prices
    print(f'[arc_pi] cost cap: no price for {model}; using {FALLBACK_PRICES}', file=sys.stderr)
    return FALLBACK_PRICES


def number(name: str, default: float = 0.0) -> float:
    raw = os.environ.get(name, str(default)).strip() or str(default)
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f'{name} must be a number') from exc
    if value < 0:
        raise ValueError(f'{name} must not be negative')
    return value


def integer(name: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(f'{name} must be an integer') from exc
    if value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f'{name} outside allowed range {minimum}..{maximum or "unbounded"}')
    return value


def load_config(*, allow_missing_key: bool = False) -> Config:
    load_dotenv(PROJECT_DIR / '.env', override=False)
    key = os.environ.get('OPENAI_API_KEY', '').strip()
    if not key and not allow_missing_key:
        raise MissingApiKeyError(
            f'OPENAI_API_KEY is not set: fill {PROJECT_DIR / ".env"} with '
            'your model gateway key (or provide it in the process '
            'environment) and confirm MODEL is permitted')
    reasoning = os.environ.get('PI_REASONING', 'medium').strip()
    if reasoning not in REASONING_LEVELS:
        raise ValueError(f'PI_REASONING must be one of {", ".join(REASONING_LEVELS)}')
    from urllib.parse import urlsplit
    base = os.environ.get('OPENAI_BASE_URL', 'https://api.arc-bench.com/v1').rstrip('/')
    url = urlsplit(base)
    if url.scheme not in {'https', 'http'} or not url.hostname or url.username or url.password:
        raise ValueError('OPENAI_BASE_URL must be an HTTP(S) URL without credentials')
    model = os.environ.get('MODEL', 'deepseek-v4-flash').strip()
    if not model:
        raise ValueError('MODEL must not be empty')
    reviewer_raw = os.environ.get('PI_REVIEWER', '1').strip()
    if reviewer_raw not in ('0', '1', 'self'):
        raise ValueError('PI_REVIEWER must be 0, 1 or self')
    prices = model_prices(model)
    batch_tool = os.environ.get('PI_BATCH_TOOL', 'auto').strip() or 'auto'
    if batch_tool not in ('auto', '0', '1'):
        raise ValueError('PI_BATCH_TOOL must be auto, 0 or 1')
    return Config(
        project_dir=PROJECT_DIR, openai_api_key=key, openai_base_url=base, model=model,
        pi_reasoning=reasoning,
        pi_time_budget_seconds=integer('PI_TIME_BUDGET_SECONDS', 2400),
        pi_max_model_calls=MAX_MODEL_CALLS, pi_max_output_tokens=MAX_OUTPUT_TOKENS_PER_CALL,
        pi_context_bytes=integer('PI_CONTEXT_BYTES', 8388608),
        pi_repair_rounds=integer('PI_REPAIR_ROUNDS', 2, 0, 6),
        pi_smoke_port=(integer('PI_SMOKE_PORT', 43300, 1, 65535)
                       if os.environ.get('PI_SMOKE_PORT') else None),
        pi_bash_path=os.environ.get('PI_BASH_PATH') or None,
        pi_reviewer=reviewer_raw == '1',
        pi_context_tokens=integer('PI_CONTEXT_TOKENS', 200_000, 20_000),
        visual_model=os.environ.get('VISUAL_MODEL', '').strip(),
        visual_api_key=os.environ.get('VISUAL_API_KEY', '').strip() or key,
        visual_base_url=(os.environ.get('VISUAL_BASE_URL', '').strip() or base).rstrip('/'),
        pi_self_review=reviewer_raw == 'self',
        pi_max_run_tokens=integer('PI_MAX_RUN_TOKENS', 0, 0),
        pi_max_run_cost_cny=number('PI_MAX_RUN_COST_CNY'),
        pi_price_input=number('PI_PRICE_INPUT', prices[0]),
        pi_price_output=number('PI_PRICE_OUTPUT', prices[1]),
        pi_price_cache=number('PI_PRICE_CACHE_HIT', prices[2]),
        pi_max_turns_per_prompt=integer('PI_MAX_TURNS_PER_PROMPT', 0, 0, 10000),
        pi_batch_tool=batch_tool,
        pi_self_review_seconds=integer('PI_SELF_REVIEW_SECONDS', 0, 0, 36000),
        pi_prompt_seconds=integer('PI_PROMPT_SECONDS', 0, 0, 36000),
        pi_final_phase_seconds=integer('PI_FINAL_PHASE_SECONDS', 0, 0, 14400),
        pi_batch_review=os.environ.get('PI_BATCH_REVIEW', '1').strip() != '0',
        pi_seconds_per_batch=integer('PI_SECONDS_PER_BATCH', 600, 60, 36000),
        pi_blind_test=os.environ.get('PI_BLIND_TEST', '0').strip() == '1',
        pi_final_review=os.environ.get('PI_FINAL_REVIEW', '1').strip() != '0')
