import dataclasses
from pathlib import Path

from arc_pi.config import Config

PROJECT = Path(__file__).resolve().parents[1]

DEFAULTS = dict(
    project_dir=PROJECT, openai_api_key='offline-test', openai_base_url='http://127.0.0.1:1/v1',
    model='offline', pi_reasoning='medium', pi_time_budget_seconds=7200, pi_max_model_calls=100,
    pi_max_output_tokens=1000, pi_context_bytes=8388608, pi_repair_rounds=0,
    pi_smoke_port=None, pi_bash_path=None, run_token_budget=10 ** 12)


def make_config(**overrides) -> Config:
    """Config for offline tests; keys a harness version does not know are ignored."""
    values = {**DEFAULTS, **overrides}
    names = {field.name for field in dataclasses.fields(Config)}
    return Config(**{key: value for key, value in values.items() if key in names})
