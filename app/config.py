from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str = ""
    vault_path: str = "/vault"
    inbox_filename: str = "inbox.md"
    webhook_token: str = ""
    todoist_api_key: str = ""
    todoist_mcp_url: str = "http://todoist-mcp:3000/mcp"
    brave_search_mcp_url: str = "http://brave-search-mcp:3001/mcp"
    obsidian_mcp_url: str = "http://obsidian-mcp:3002/mcp"
    user_context_path: str = ""
    # ntfy push. Reached over the shared docker_proxy_network, so no egress from
    # the app container is needed. Empty topic disables notification entirely —
    # the tool is then not offered to the model at all.
    ntfy_url: str = "http://ntfy:80"
    ntfy_topic: str = ""
    ntfy_token: str = ""
    inbox_archive_path: str = "archive/inbox"
    audit_log_path: str = "archive/audit"
    # Raw verbatim capture log, written before the agent runs. Lives under
    # archive/ so is_machine_path keeps it out of inbox processing and the daily
    # summary — it is a safety net, not a second inbox.
    capture_log_path: str = "archive/capture"
    recent_context_hours: int = 24
    process_hour: int = 6
    daily_summary_hour: int = 7

    # Per-role models. The webhook agent was originally on Haiku on the assumption
    # it "runs constantly" — measured reality is ~1.3 captures/day, and because
    # /webhook returns before the agent runs, latency is not a constraint either.
    # Both reasons for the cheap tier were wrong, and its routing mistakes are the
    # expensive kind (a wrong Todoist task is never cleaned up), so it gets the
    # strongest tier. The inbox processor follows the same logic: once a day,
    # and it patches person notes and completes Todoist tasks, so a misjudgment
    # there edits real data. Summary and chat are lower-stakes: Sonnet.
    # Effort is set per call site — Opus 5.5 defaults to medium, Sonnet 5.5 to high.
    agent_model: str = "claude-opus-5-5"
    processor_model: str = "claude-opus-5-5"
    summary_model: str = "claude-sonnet-5-5"
    chat_model: str = "claude-sonnet-5-5"

    # JSONL file persisting the /history feed across restarts; empty = in-memory only
    history_path: str = ""


settings = Settings()
