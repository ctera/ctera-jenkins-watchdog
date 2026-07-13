"""Application configuration via pydantic-settings."""

from urllib.parse import quote_plus

from pydantic import model_validator
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

    # External call timeouts (seconds)
    request_timeout_s: float = 15.0

    # Valkey
    valkey_host: str = "valkey.valkey.svc.cluster.local"
    valkey_port: int = 6379
    valkey_ssl: bool = False
    valkey_ca_cert: str = "/etc/valkey-tls/ca.crt"
    valkey_client_cert: str = "/etc/valkey-tls/tls.crt"
    valkey_client_key: str = "/etc/valkey-tls/tls.key"

    # PostgreSQL (v2 business state)
    database_url: str = ""
    database_host: str = "localhost"
    database_port: int = 5432
    database_user: str = "watchdog"
    database_password: str = "watchdog"
    database_name: str = "watchdog"
    database_pool_size: int = 5
    database_max_overflow: int = 5

    # Durable workers
    worker_poll_interval_s: float = 1.0
    worker_lease_seconds: int = 60
    worker_heartbeat_seconds: int = 15

    # LLM (via LiteLLM)
    anthropic_api_key: str = ""
    llm_model: str = "anthropic/claude-sonnet-4-6"
    llm_fallback_models: str = "anthropic/claude-opus-4-6"
    llm_temperature: float = 0.1
    llm_max_tokens: int = 8192
    llm_max_retries: int = 2

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

    # Routing and delivery. Every external integration is opt-in.
    routing_config_path: str = "config/routing.yaml"
    automation_templates_path: str = "templates/automation"
    jira_enabled: bool = False
    github_enabled: bool = False
    github_api_url: str = "https://api.github.com"
    github_token: str = ""
    gitlab_enabled: bool = False
    gitlab_api_url: str = "https://gitlab.com/api/v4"
    gitlab_token: str = ""
    email_enabled: bool = False
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_start_tls: bool = False
    smtp_username: str = ""
    smtp_password: str = ""
    email_from: str = "jenkins-watchdog@localhost"
    email_fallback_recipients: str = ""

    # Agent
    max_tool_rounds: int = 15
    max_investigations_per_scan: int = 12

    @model_validator(mode="after")
    def assemble_database_url(self) -> "Settings":
        if not self.database_url:
            user = quote_plus(self.database_user)
            password = quote_plus(self.database_password)
            self.database_url = (
                f"postgresql+asyncpg://{user}:{password}@{self.database_host}:{self.database_port}/{self.database_name}"
            )
        return self

    model_config = {"env_prefix": "WATCHDOG_", "env_file": ".env", "env_file_encoding": "utf-8"}
