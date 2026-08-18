"""Application configuration via pydantic-settings."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    port: int = 8000
    log_level: str = "info"
    reload: bool = False

    # Jenkins
    jenkins_url: str = "http://jenkins.jenkins.svc.cluster.local:8080"
    jenkins_user: str = ""
    jenkins_token: str = ""
    jenkins_agent_label: str = ""
    jenkins_namespace: str = "jenkins"
    jenkins_failed_build_window_hours: int = 4
    k8s_events_window_minutes: int = 30

    # Prometheus
    prometheus_endpoint: str = "http://prometheus.monitoring.svc.cluster.local:9090"
    prometheus_enabled: bool = True

    # Prompts
    # Where prompts/system.md lives. Empty means "walk up from this file", which is right
    # for a source checkout but WRONG in the container: the package is installed into
    # site-packages while prompts/ is copied to /app/prompts, so the walk-up landed on
    # /usr/local/lib/python3.12/prompts and the loaders silently fell back to a one-line
    # system prompt. The image sets WATCHDOG_PROMPTS_DIR=/app/prompts explicitly.
    prompts_dir: str = ""

    # External call timeouts (seconds)
    request_timeout_s: float = 15.0

    # Valkey
    valkey_host: str = "valkey.valkey.svc.cluster.local"
    valkey_port: int = 6379
    valkey_ssl: bool = False
    valkey_ca_cert: str = "/etc/valkey-tls/ca.crt"
    valkey_client_cert: str = "/etc/valkey-tls/tls.crt"
    valkey_client_key: str = "/etc/valkey-tls/tls.key"

    # LLM (via the Claude Agent SDK, which spawns the bundled `claude` CLI per call)
    # Agent auth. Mint with `claude setup-token`. The app injects this into the Claude
    # Code subprocess itself as the bare CLAUDE_CODE_OAUTH_TOKEN, so it must never be
    # exported globally — that would retarget every other `claude` on the machine.
    # ANTHROPIC_API_KEY is not used and is blanked in the subprocess: an OAuth token has
    # no quota against the raw Messages API, so the Agent SDK is the only path it works on.
    claude_code_oauth_token: str = ""
    # Explicit path to the `claude` binary. Empty means "use the one bundled in the SDK
    # wheel, else whatever is on PATH".
    claude_code_path: str = ""
    # A private, credential-free CLAUDE_CONFIG_DIR for the subprocess. Empty means
    # $HOME/.jenkins-watchdog/claude-home. Set this where $HOME is not writable.
    claude_config_dir: str = ""
    llm_model: str = "claude-sonnet-5"
    # The SDK takes a single fallback model, not a chain.
    llm_fallback_model: str = "claude-opus-5"
    # Every call spawns a CLI subprocess, so concurrency is a real memory cap rather than
    # a politeness knob. A scan can queue up to max_investigations_per_scan; this is what
    # actually runs at once.
    llm_max_concurrent_agents: int = 2
    # No output for this long means the CLI is wedged. Without it a hung subprocess hangs
    # the scan forever instead of failing one investigation.
    llm_agent_idle_timeout_s: float = 180.0

    # OIDC (DEX)
    oidc_issuer: str = ""
    oidc_client_id: str = "jenkins-watchdog"
    oidc_client_secret: str = ""
    oidc_redirect_uri: str = ""
    oidc_allowed_groups: str = "DevOps Team"

    # Jira
    jira_base_url: str = "https://cteranet.atlassian.net"
    jira_user_email: str = ""
    jira_api_token: str = ""
    jira_projects: str = "CI"

    # Source code tools
    github_token: str = ""
    github_org: str = "ctera"
    gitlab_url: str = ""
    gitlab_token: str = ""
    gitlab_group: str = ""

    # Agent
    max_tool_rounds: int = 15
    max_investigations_per_scan: int = 12

    # Scheduler
    scheduler_enabled: bool = False
    scheduler_scan_interval_minutes: int = 60
    scheduler_deep_scan_interval_minutes: int = 1440
    auto_jira_enabled: bool = False
    auto_jira_project: str = "CI"
    auto_jira_assignee_email: str = ""
    auto_jira_severity_threshold: str = "critical"

    model_config = {"env_prefix": "WATCHDOG_", "env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
