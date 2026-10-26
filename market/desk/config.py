"""Runtime configuration: paths, limits, the tool allowlist and the LLM endpoint.

Everything an operator may want to change is an environment variable; nothing here is a secret.
The LLM key is read from the environment, or from the dotenv file named by DESK_ENV_FILE.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # the heros-home checkout


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def read_dotenv_value(path: str | Path, name: str) -> str | None:
    """Read one KEY=value from a dotenv file without loading the rest of it into the environment.

    The file may hold unrelated free text; only an exact `NAME=` line is taken.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:].lstrip()
                if line.startswith(name + "="):
                    value = line[len(name) + 1:].strip().strip('"').strip("'")
                    return value or None
    except OSError:
        return None
    return None


@dataclass(frozen=True)
class Limits:
    """Hard ceilings for one mission. The coordinator refuses work past any of them."""
    max_tasks: int = 80              # total tasks a mission may ever create (incl. replans)
    max_depth: int = 3               # spawn depth: planner=0, its tasks=1, their spawns=2...
    max_parallel: int = 6            # tasks leased at once across all workers
    max_llm_calls: int = 24
    max_llm_tokens: int = 250_000
    max_cost_usd: float = 0.50
    mission_deadline_s: int = 20 * 60
    lease_s: int = 90                # a worker that stops heart-beating loses the task after this
    max_attempts: int = 3            # per task, before escalation
    max_revisions: int = 1           # critic-driven re-analysis rounds per batch


@dataclass(frozen=True)
class Config:
    db_path: Path = Path(_env("DESK_DB", str(ROOT / "market" / "var" / "desk.sqlite3")))
    publish_dir: Path = Path(_env("DESK_PUBLISH_DIR", str(ROOT / "site" / "data" / "market")))
    window_days: int = int(_env("DESK_WINDOW_DAYS", "14"))
    batch_size: int = int(_env("DESK_BATCH_SIZE", "6"))
    max_events: int = int(_env("DESK_MAX_EVENTS", "36"))
    # Earnings matter to the three indices only for the largest companies.
    earnings_min_cap_usd: float = float(_env("DESK_EARNINGS_MIN_CAP", "200e9"))
    user_agent: str = _env(
        "DESK_UA",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36",
    )
    # OpenAI-compatible chat endpoint: the Qwen Token Plan by default (its keys start with sk-sp-).
    llm_base_url: str = _env("DESK_LLM_BASE_URL", "https://token-plan.maas.qianwenaiapi.com/compatible-mode/v1")
    llm_model: str = _env("DESK_LLM_MODEL", "qwen3.7-plus")
    llm_key_name: str = _env("DESK_LLM_KEY_NAME", "QWEN_LLM_API_KEY_V2")
    llm_env_file: str = _env("DESK_ENV_FILE", str(ROOT.parent / ".ENV"))
    # Price per 1M tokens, for the budget guard and the run report. The Token Plan is prepaid, so the
    # defaults are 0 and the call and token limits do the guarding; set these for a metered endpoint.
    llm_usd_per_mtok_in: float = float(_env("DESK_LLM_USD_IN", "0"))
    llm_usd_per_mtok_out: float = float(_env("DESK_LLM_USD_OUT", "0"))
    limits: Limits = field(default_factory=Limits)

    def llm_key(self) -> str | None:
        return os.environ.get(self.llm_key_name) or read_dotenv_value(self.llm_env_file, self.llm_key_name)


# Hosts a tool may reach. Anything else is refused before a socket opens.
ALLOWED_HOSTS = frozenset({
    "www.federalreserve.gov",
    "api.nasdaq.com",
    "query1.finance.yahoo.com",
    "query2.finance.yahoo.com",
    "www.cnbc.com",
    "feeds.content.dowjones.io",
    "token-plan.maas.qianwenaiapi.com",
    "dashscope.aliyuncs.com",
    "dashscope-intl.aliyuncs.com",
})
