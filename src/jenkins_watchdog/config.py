"""Application configuration via pydantic-settings."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    port: int = 8000
    log_level: str = "info"

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
    jira_base_url: str = "https://ctera.atlassian.net"
    jira_user_email: str = ""
    jira_api_token: str = ""
    jira_projects: str = "CI"

    # Agent
    max_tool_rounds: int = 10
    max_investigations_per_scan: int = 10

    model_config = {"env_prefix": "WATCHDOG_"}


settings = Settings()
