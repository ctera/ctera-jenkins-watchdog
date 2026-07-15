import { expect, test, type Page } from "@playwright/test";

const now = "2026-07-13T12:00:00Z";

function scan(overrides: Record<string, unknown> = {}) {
  return {
    id: "scan-active",
    status: "running",
    stage: "detecting",
    mode: "regular",
    categories: [],
    created_at: now,
    started_at: now,
    completed_at: null,
    cancel_requested_at: null,
    attempt_count: 1,
    failure_summary: null,
    urls: {
      detail: "/api/v2/scans/scan-active",
      events: "/api/v2/scans/scan-active/events",
      cancel: "/api/v2/scans/scan-active/cancel",
    },
    ...overrides,
  };
}

function incident(overrides: Record<string, unknown> = {}) {
  return {
    id: "incident-1",
    status: "open",
    severity: "critical",
    title: "Compiler failure across MR builds",
    correlation_rule_id: "jenkins_error_signature",
    correlation_key: "compiler-error",
    source: { kind: "merge_request", confirmed: true, verified: true, profile_id: "portal-backend", provider: "gitlab", repository: "Portal/Backend", change_number: "6836", url: "http://git.ctera.local/Portal/Backend/-/merge_requests/6836" },
    actionability: "actionable",
    classification: "merge_request",
    priority: "critical",
    created_at: now,
    updated_at: now,
    resolved_at: null,
    suppressed_reason: null,
    suppressed_by: null,
    suppressed_at: null,
    occurrence_number: 1,
    affected_resource_count: 2,
    current_observation_count: 2,
    first_seen_at: now,
    last_seen_at: now,
    domain: "builds",
    ...overrides,
  };
}

function jenkinsWorkspace(windowHours: number, failureCount: number) {
  return {
    generated_at: now,
    window_hours: windowHours,
    summary: {
      window_start: now,
      job_count: 12,
      active_job_count: 8,
      build_count: failureCount * 4,
      failure_build_count: failureCount,
      enriched_failure_count: failureCount,
      pending_failure_analysis_count: 0,
      new_failure_count: failureCount,
      running_build_count: 2,
      cumulative_wall_hours: failureCount,
      exact_job_count: 12,
      retention_limited_job_count: 0,
      multibranch_parent_count: 1,
      sync: { status: "succeeded", completed_at: now, updated_at: now, stats: {} },
    },
    new_failures: [],
    active_executions: [],
    recurring_patterns: [],
    busy_jobs: [],
    multibranch: [],
  };
}

function action(overrides: Record<string, unknown> = {}) {
  return {
    id: "action-1",
    incident_id: "incident-1",
    occurrence_id: "occurrence-1",
    action_type: "github_comment",
    destination: "github:ctera/app:42",
    status: "permanently_failed",
    rendered_payload: { body: "Build investigation summary" },
    template_version: "v1",
    external_reference: null,
    attempt_count: 6,
    retry_cycle: 1,
    next_attempt_at: null,
    failure_summary: "HTTP 503",
    created_at: now,
    updated_at: now,
    completed_at: now,
    ...overrides,
  };
}

async function installApi(
  page: Page,
  options: { buildOverrides?: Record<string, unknown>; incidentDetailOverrides?: Record<string, unknown> } = {},
) {
  let currentScan = scan();
  let currentIncident = incident();
  let currentAction = action();
  let currentBuild = jenkinsBuildDetail(options.buildOverrides);
  let currentIncidentDetailOverrides = options.incidentDetailOverrides;
  const eventHeaders: string[] = [];

  await page.route("**/auth/me", (route) => route.fulfill({ json: { authenticated: true, email: "operator@example.com" } }));
  await page.route("**/api/v2/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;

    if (path.endsWith("/events")) {
      const lastEventId = request.headers()["last-event-id"] ?? "";
      eventHeaders.push(lastEventId);
      const sequence = lastEventId === "1" ? 2 : 1;
      if (sequence >= 2) currentScan = scan({ status: "succeeded", stage: "completed", completed_at: now });
      const type = sequence >= 2 ? "scan_completed" : "scan_started";
      const envelope = { sequence, type, occurred_at: now, payload_version: 1, payload: { attempt: 1 } };
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: `id: ${sequence}\nevent: ${type}\ndata: ${JSON.stringify(envelope)}\n\n`,
      });
      return;
    }
    if (path === "/api/v2/scans" && request.method() === "POST") {
      currentScan = scan({ id: "scan-new", status: "queued", stage: "queued", started_at: null, attempt_count: 0 });
      await route.fulfill({ status: 202, json: currentScan });
      return;
    }
    if (path === "/api/v2/scans") {
      await route.fulfill({ json: { items: [currentScan], next_cursor: null } });
      return;
    }
    if (/\/api\/v2\/scans\/[^/]+$/.test(path)) {
      await route.fulfill({ json: currentScan });
      return;
    }
    if (path === "/api/v2/incidents") {
      await route.fulfill({ json: { items: [currentIncident], next_cursor: null } });
      return;
    }
    if (path === "/api/v2/incidents/incident-1/suppress") {
      currentIncident = incident({
        status: "suppressed",
        suppressed_reason: "Planned maintenance",
        suppressed_by: "operator@example.com",
        suppressed_at: now,
      });
      await route.fulfill({ json: currentIncident });
      return;
    }
    if (path === "/api/v2/incidents/incident-1/unsuppress") {
      currentIncident = incident();
      await route.fulfill({ json: currentIncident });
      return;
    }
    if (path === "/api/v2/incidents/incident-1/reinvestigate") {
      const queued = investigationRequest({ source: "manual_incident", mode: "deep" });
      currentIncidentDetailOverrides = {
        ...currentIncidentDetailOverrides,
        investigation_request: queued,
      };
      await route.fulfill({ status: 202, json: queued });
      return;
    }
    if (path === "/api/v2/incidents/incident-1") {
      await route.fulfill({ json: incidentDetail(currentIncident, currentAction, currentIncidentDetailOverrides) });
      return;
    }
    if (path === "/api/v2/actions/action-1/retry") {
      currentAction = action({ status: "pending", attempt_count: 0, retry_cycle: 2, failure_summary: null, completed_at: null });
      await route.fulfill({ json: currentAction });
      return;
    }
    if (path === "/api/v2/actions/action-1") {
      await route.fulfill({
        json: {
          action: currentAction,
          attempts: [{ id: "attempt-1", retry_cycle: 1, attempt_number: 6, status: "permanent_failed", response_metadata: { status_code: 503 }, error_summary: "HTTP 503", started_at: now, completed_at: now }],
        },
      });
      return;
    }
    if (path === "/api/v2/actions") {
      await route.fulfill({ json: { items: [currentAction], next_cursor: null } });
      return;
    }
    if (path === "/api/v2/jenkins/builds/build-1/analyze" && request.method() === "POST") {
      const queued = investigationRequest({ source: "manual_build", mode: request.postDataJSON().mode, build_id: "build-1" });
      currentBuild = jenkinsBuildDetail({ incident_id: "incident-1", investigation_request: queued });
      await route.fulfill({ status: 202, json: queued });
      return;
    }
    if (path === "/api/v2/jenkins/builds/build-1") {
      await route.fulfill({ json: currentBuild });
      return;
    }
    if (path === "/api/v2/chat/stream") {
      const final = { content: "The compiler error is isolated to the merge request change.", references: [], as_of: now, coverage_status: "complete" };
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: [
          `event: tool_call\ndata: ${JSON.stringify({ type: "tool_call", tool: "jenkins_get_build_log", arguments: { job_name: "app/MR-42", build_number: 42 } })}\n\n`,
          `event: tool_result\ndata: ${JSON.stringify({ type: "tool_result", tool: "jenkins_get_build_log", ok: true, duration_ms: 12 })}\n\n`,
          `event: message\ndata: ${JSON.stringify(final)}\n\n`,
        ].join(""),
      });
      return;
    }
    await route.fulfill({ status: 404, json: { detail: { code: "not_mocked" } } });
  });

  return { eventHeaders: () => eventHeaders };
}

function investigation(overrides: Record<string, unknown> = {}) {
  return {
    id: "investigation-1",
    status: "succeeded",
    evidence_hash: "abc123",
    input_version: "v1",
    prompt_version: "v1",
    model: "test-model",
    confidence: "medium",
    usage: { total_tokens: 150 },
    result: {
      root_cause: "Compiler error",
      impact: "MR blocked",
      suggested_fix: "Fix the type mismatch",
      quality_gate: "Jenkins did not provide the failed-test report.",
    },
    error_summary: null,
    created_at: now,
    completed_at: now,
    ...overrides,
  };
}

function investigationRequest(overrides: Record<string, unknown> = {}) {
  return {
    id: "request-1",
    incident_id: "incident-1",
    occurrence_id: "occurrence-1",
    mode: "regular",
    source: "automatic",
    priority: 100,
    evidence_hash: "abc123",
    status: "queued",
    scan_id: null,
    build_id: null,
    requested_by: "operator@example.com",
    attempt_count: 0,
    next_attempt_at: now,
    investigation_id: null,
    error_summary: null,
    created_at: now,
    updated_at: now,
    completed_at: null,
    ...overrides,
  };
}

function jenkinsBuildDetail(overrides: Record<string, unknown> = {}) {
  return {
    id: "build-1",
    job_name: "Portal_Build_DAILY_MR_PATCH",
    build_number: 12358,
    result: "FAILURE",
    url: "https://jenkins/job/portal/12358",
    started_at: now,
    completed_at: now,
    duration_ms: 1_320_000,
    building: false,
    job_type: "pipeline",
    parent: null,
    head_type: "change_request",
    head_name: "MR-42",
    source_provider: "gitlab",
    repository: "Portal/Backend",
    change_number: "6836",
    change_url: "http://git.ctera.local/Portal/Backend/-/merge_requests/6836",
    source_kind: "change_request",
    source_status: "verified",
    source_profile_id: "portal-backend",
    source_profile_registered: true,
    source_branch: "fix/portal-build",
    source_commit_sha: "1234567890abcdef1234567890abcdef12345678",
    source_url: "http://git.ctera.local/Portal/Backend/-/merge_requests/6836",
    source_title: "Fix portal build",
    source_state: "opened",
    source_resolution_method: "root_cause_url+provider_api",
    source_reason: null,
    source_allow_mr_comments: false,
    source_verified_at: now,
    trigger_kind: "scm",
    root_job: "Portal_Build_DAILY_MR_PATCH",
    root_build_number: 12358,
    logical_run_key: "Portal_Build_DAILY_MR_PATCH#12358",
    propagated_failure: false,
    failed_stage: "Compile",
    failure_summary: "TypeScript compilation failed",
    failure_classification: "compilation_error",
    failure_signature: "typescript-error",
    novelty: "new_regression",
    priority_score: 70,
    priority_reasons: ["current blockage +30", "change request blocked +5"],
    coverage: "exact",
    enrichment_status: "log_pending",
    incident_id: null,
    evidence: { error_lines: ["TS2322: Type string is not assignable"], stages: [], causes: [] },
    upstream_builds: [],
    downstream_builds: [],
    incident: null,
    investigation_request: null,
    latest_investigation: null,
    ...overrides,
  };
}

function incidentDetail(
  currentIncident: ReturnType<typeof incident>,
  currentAction: ReturnType<typeof action>,
  overrides: Record<string, unknown> = {},
) {
  return {
    incident: currentIncident,
    observations: [{ scan_id: "scan-active", check_name: "jenkins_failed_builds", stable_identity: "stable", rule_id: "jenkins.failed.v1", resource_id: "job/app", category: "jenkins_failed_build", severity: "critical", summary: "Compile failed", observed_at: now, identity_dimensions: { error_signature: "compiler-error" }, evidence: { build_number: 42 } }],
    occurrences: [{ id: "occurrence-1", number: 1, opened_at: now, last_observed_at: now, resolved_at: null, responsible_checks: ["jenkins_failed_builds"], observation_identities: ["stable"] }],
    latest_investigation: investigation(),
    actions: [currentAction],
    ...overrides,
  };
}

test("scan detail replays and reconnects with Last-Event-ID", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "chromium");
  const api = await installApi(page);
  await page.goto("/scans/scan-active");

  await expect.poll(() => api.eventHeaders().find(Boolean), { timeout: 20_000 }).toBe("1");
  await expect(page.getByText("Scan Completed")).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("scan-detail.png"), fullPage: true });
});

test("operator can enqueue a regular scan", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "chromium");
  await installApi(page);
  await page.goto("/scans");

  const requestPromise = page.waitForRequest((request) => request.url().endsWith("/api/v2/scans") && request.method() === "POST");
  await page.getByRole("button", { name: "Start scan" }).click();
  const request = await requestPromise;

  expect(request.postDataJSON()).toEqual({ mode: "regular", categories: null });
  await expect(page.getByText("Active regular scan")).toBeVisible();
});

test("latest build-history window wins when responses finish out of order", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "chromium");
  let release24 = () => {};
  const hold24 = new Promise<void>((resolve) => {
    release24 = resolve;
  });
  await page.route("**/auth/me", (route) => route.fulfill({ json: { authenticated: true, email: "operator@example.com" } }));
  await page.route("**/api/v2/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/v2/overview") {
      await route.fulfill({ json: { llm_usage: { total_tokens: 0, estimated_cost_usd: 0 } } });
      return;
    }
    if (url.pathname === "/api/v2/jenkins/failures") {
      await route.fulfill({ json: { items: [], next_cursor: null } });
      return;
    }
    if (url.pathname === "/api/v2/jenkins") {
      const windowHours = Number(url.searchParams.get("window_hours"));
      if (windowHours === 24) await hold24;
      if (windowHours === 4) await new Promise((resolve) => setTimeout(resolve, 20));
      await route.fulfill({ json: jenkinsWorkspace(windowHours, windowHours * 10) });
      return;
    }
    await route.fulfill({ status: 404, json: { detail: { code: "not_mocked" } } });
  });
  await page.goto("/overview");
  const failedBuilds = page.getByText("Failed builds", { exact: true }).locator("..");
  await expect(failedBuilds).toContainText("1,680");

  const request24 = page.waitForRequest((request) => request.url().includes("window_hours=24"));
  const response24 = page.waitForResponse((response) => response.url().includes("window_hours=24"));
  await page.getByRole("button", { name: "24h", exact: true }).click();
  await request24;
  expect(await page.getByRole("button", { name: "7d", exact: true }).getAttribute("aria-pressed")).toBe("true");
  expect(await failedBuilds.innerText()).toContain("1,680");
  await page.getByRole("button", { name: "4h", exact: true }).click();

  await expect(failedBuilds).toContainText("40");
  release24();
  await response24;
  await page.waitForTimeout(50);
  await expect(page.getByRole("button", { name: "4h", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(failedBuilds).toContainText("40");
});

test("operator can queue a deep build analysis", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "chromium");
  await installApi(page);
  await page.goto("/jenkins/builds/build-1");

  await expect(page.getByText("Deterministic evidence: Log Pending")).toBeVisible();
  const source = page.getByRole("heading", { name: "Source attribution" }).locator("..");
  await expect(source.getByText("GitLab !6836")).toBeVisible();
  await expect(source.getByText("Verified", { exact: true }).first()).toBeVisible();
  await expect(source.getByText("Unknown", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Not analyzed")).toBeVisible();
  await page.getByRole("button", { name: "Deep" }).click();
  const requestPromise = page.waitForRequest((request) => request.url().endsWith("/api/v2/jenkins/builds/build-1/analyze"));
  await page.getByRole("button", { name: "Analyze build" }).click();
  const request = await requestPromise;

  expect(request.postDataJSON()).toEqual({ mode: "deep" });
  await expect(page.getByText("Agent analysis is queued.")).toBeVisible();
});

test("failed agent run is distinct from a root-cause assessment", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "chromium");
  const diagnostic = "TypeError: Object of type datetime is not JSON serializable";
  const failedRequest = investigationRequest({
    status: "failed",
    attempt_count: 3,
    next_attempt_at: null,
    error_summary: diagnostic,
    completed_at: now,
  });
  const failedInvestigation = investigation({
    status: "failed",
    confidence: "low",
    result: { mode: "regular", deterministic_severity: "critical" },
    error_summary: diagnostic,
  });
  await installApi(page, {
    buildOverrides: {
      incident_id: "incident-1",
      investigation_request: failedRequest,
      latest_investigation: failedInvestigation,
    },
    incidentDetailOverrides: {
      investigation_request: failedRequest,
      latest_investigation: failedInvestigation,
    },
  });

  await page.goto("/jenkins/builds/build-1");
  await expect(page.getByText(/Watchdog agent error, not the Jenkins failure/)).toBeVisible();
  await expect(page.getByText(`Diagnostic: ${diagnostic}`)).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry analysis" })).toBeVisible();
  await expect(page.getByText("Root cause", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Not available", { exact: true })).toHaveCount(0);

  await page.goto("/incidents/incident-1");
  await expect(page.getByText("Not available", { exact: true })).toHaveCount(0);
  await page.getByRole("tab", { name: "Investigation" }).click();
  await expect(page.getByText(/Watchdog agent error, not the incident root cause/)).toBeVisible();
  await expect(page.getByText(`Diagnostic: ${diagnostic}`)).toBeVisible();
  await expect(page.getByText("Root cause", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Not available", { exact: true })).toHaveCount(0);

  await page.getByRole("button", { name: "Reinvestigate" }).click();
  await expect(page.getByText("Agent analysis is queued.")).toBeVisible();
  await expect(page.getByText(/Watchdog agent error/)).toHaveCount(0);
  await expect(page.getByText(`Diagnostic: ${diagnostic}`)).toHaveCount(0);
  await expect(page.getByText("Low confidence", { exact: true })).toHaveCount(0);
});

test("operator can browse, suppress, inspect retry, and chat", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "chromium");
  await installApi(page);
  await page.goto("/incidents");
  await page.getByText("Compiler failure across MR builds").click();
  await expect(page.getByText("Source association")).toBeVisible();
  await expect(page.getByText("Jenkins did not provide the failed-test report.")).toBeVisible();

  await page.getByRole("button", { name: "Suppress" }).click();
  await page.getByLabel("Audit reason").fill("Planned maintenance");
  await page.getByRole("button", { name: "Suppress", exact: true }).last().click();
  await expect(page.getByText(/Suppressed by operator@example.com/)).toBeVisible();

  await page.getByRole("tab", { name: /Actions/ }).click();
  await page.getByText("Github Comment").click();
  await expect(page.getByText("Delivery attempts")).toBeVisible();
  await page.getByRole("button", { name: "Retry" }).click();
  await expect(page.getByText("Pending")).toBeVisible();

  await page.getByRole("button", { name: "Incident", exact: true }).click();
  await page.getByRole("tab", { name: "Chat" }).click();
  await page.getByLabel("Message about this incident").fill("What changed?");
  await page.getByRole("button", { name: "Send message" }).click();
  await expect(page.getByText(/compiler error is isolated/)).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("incident-chat.png"), fullPage: true });
});

test("mobile navigation stays within the viewport", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile");
  await installApi(page);
  await page.goto("/scans");
  await expect(page.getByRole("button", { name: "Start scan" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Incidents" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  const firstAction = await page.getByRole("button", { name: "Jenkins" }).boundingBox();
  const lastAction = await page.getByRole("button", { name: "Assistant" }).boundingBox();
  expect(firstAction?.x).toBeGreaterThanOrEqual(0);
  expect((lastAction?.x ?? 0) + (lastAction?.width ?? 0)).toBeLessThanOrEqual(page.viewportSize()!.width);
  await page.getByRole("button", { name: "Incidents" }).click();
  await expect(page.getByRole("heading", { name: "Incidents" })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("mobile-incidents.png"), fullPage: true });
});
