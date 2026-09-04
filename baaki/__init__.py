"""Baaki — a bounded, auditable AI agent that recovers overdue receivables for Indian SMEs."""

__version__ = "0.1.0"


def _load_env() -> None:
    """Load .env from the repo root, for every entry point.

    This lives at the package root rather than in baaki.app: the simulation CLI never imports
    the web app, so putting it there left `baaki run --brain openai` without a key and silently
    falling back to the rules brain. Real environment variables always win over the file.
    """
    from pathlib import Path

    try:
        from dotenv import load_dotenv
    except ImportError:      # dotenv is a convenience, not a requirement
        return
    env = Path(__file__).resolve().parent.parent / ".env"
    if env.exists():
        load_dotenv(env, override=False)


_load_env()
