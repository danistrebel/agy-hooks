# Antigravity Hooks Experiment (`agy-hooks`)

This repository demonstrates how to build and configure custom lifecycle hooks for Google Antigravity (`agy`). It provides 3 streamlined hooks: **Log**, **Trace**, and **Metrics**.

---

## Overview

Antigravity hooks execute scripts or commands at critical points in the agent's execution lifecycle:
- **`PreInvocation`**: Before the agent begins processing a turn.
- **`PreToolUse`**: Before executing a tool call (can inspect arguments and allow or block actions).
- **`PostToolUse`**: Immediately after a tool finishes execution (captures latency, parameters, and errors).
- **`PostInvocation`**: After an invocation turn completes.
- **`Stop`**: When the conversation concludes.

Hooks receive execution context as JSON via `stdin` and return execution control / responses via `stdout` (for example, `{"decision": "allow"}` for `PreToolUse`).

---

## Quickstart

### Run Locally (Zero Cloud Dependencies)
By default, all hooks run locally without requiring GCP credentials or network access. Local telemetry and timeline visualizations are stored in [`.agy-local-telemetry/`](.agy-local-telemetry/):

```bash
agy
```

### Run Remotely / With Google Cloud Telemetry
To run `agy` with full Google Cloud telemetry enabled (forwarding to **Google Cloud Logging**, **Google Cloud Trace**, and **Google Cloud Monitoring**):

```bash
export GOOGLE_CLOUD_PROJECT="$(gcloud config get-value project)"
export AGY_LOG_EXPORTER="gcp"
export AGY_OTEL_EXPORTER="gcp"

agy
```

---

## Repository Structure

```
.
├── .agents/
│   ├── hooks.json          # Hook configuration registering Log, Trace, and Metrics
│   ├── log_hook.py         # Log hook (Local JSONL + optional Cloud Logging)
│   ├── trace_hook.py       # Trace hook (Local ASCII timeline + optional OTel / Cloud Trace)
│   └── metrics_hook.py     # Metrics hook (Cloud Monitoring metrics + local-safe fallback)
├── .agy-local-telemetry/   # Local telemetry generated per conversation (git-ignored)
│   ├── hooks_<conversationId>.jsonl
│   └── timeline_<conversationId>.txt
└── README.md
```

---

## The 3 Hooks: Local vs. Remote Execution

Each hook is designed to operate seamlessly in both offline local environments and enterprise / remote Google Cloud environments.

### 1. `Log` ([`.agents/log_hook.py`](.agents/log_hook.py))

Captures complete hook payloads and lifecycle events (`PreInvocation`, `PreToolUse`, `PostToolUse`, `PostInvocation`, `Stop`).

- **Local Operation**:
  - Automatically records full lifecycle events into [`.agy-local-telemetry/hooks_<conversationId>.jsonl`](.agy-local-telemetry/).
  - Always active; requires zero configuration or cloud authentication.
  - Returns `{"decision": "allow"}` on `PreToolUse` so tool executions proceed smoothly.
- **Remote / Cloud Operation**:
  - When `AGY_LOG_EXPORTER="gcp"` (or `"cloud_logging"`), entries are structured and exported to **Google Cloud Logging**.
  - Log entries are tagged with severity (`INFO`, `WARNING`, `ERROR`), log ID (`antigravity-hooks` or custom via `AGY_LOG_NAME`), and indexed labels: `conversation_id`, `event`, and `model`.
  - Enables centralized search and log queries across developer workstations and CI/CD runners in the Google Cloud Log Explorer.

---

### 2. `Trace` ([`.agents/trace_hook.py`](.agents/trace_hook.py))

Tracks agent turn lifecycles, tool call execution times, and errors (`PreInvocation`, `PreToolUse`, `PostToolUse`, `PostInvocation`, `Stop`).

- **Local Operation**:
  - Automatically generates and continuously updates a human-readable ASCII timeline at [`.agy-local-telemetry/timeline_<conversationId>.txt`](.agy-local-telemetry/).
  - Correlates invocation start times, tool runtimes, and exit statuses without any external services.
- **Remote / Cloud Operation**:
  - Exports distributed OpenTelemetry (OTel) trace spans:
    - **`AGY_OTEL_EXPORTER="gcp"`**: Exports spans directly to **Google Cloud Trace** using `CloudTraceSpanExporter`. Traces are keyed to the `conversationId` and display hierarchical waterfall spans (Conversation root span &rarr; Invocations &rarr; Tool calls).
    - **`AGY_OTEL_EXPORTER="otlp"`**: Forwards spans to an OTLP collector endpoint (e.g. OpenTelemetry Collector, Dynatrace, Datadog).
    - **`AGY_OTEL_EXPORTER="console"`**: Emits OTel span objects directly to console output.
  - Maintains an incremental export state file (`.otel_exported_<conversationId>.json`) in the local telemetry directory to ensure spans are exported only once without duplicates.

#### Example Local Timeline (`.agy-local-telemetry/timeline_<conversationId>.txt`):
```text
Conversation: cbc941b7-c163-49e5-bacb-d84413c840c3 | Model: gemini-3.8-flash-low | Total: 17m 6.0s
------------------------------------------------------------------------
[08:41:01] Invoc #0 (Turn #0) (12.41s)
  └─ run_command (6.21s): List files in repo
[08:41:14] Invoc #1 (Turn #1) (11.69s)
  └─ run_command (5.53s): List all files
[08:41:26] Invoc #2 (Turn #2) (9.71s)
  └─ view_file (3.93s): Read hooks.json
```

---

### 3. `Metrics` ([`.agents/metrics_hook.py`](.agents/metrics_hook.py))

Emits quantitative telemetry for tool usage and conversation turn volume (`PostToolUse`, `PostInvocation`).

- **Local Operation**:
  - Safely ignores metric pushes if Google Cloud credentials / project IDs are absent, ensuring offline and local sessions run with zero latency overhead and zero errors.
  - Can be toggled on or off directly in [`.agents/hooks.json`](.agents/hooks.json) under `"Metrics": { "enabled": true }`.
- **Remote / Cloud Operation**:
  - Emits custom time series metrics to **Google Cloud Monitoring**:
    - **`custom.googleapis.com/antigravity/tool_calls`**:
      - Labels: `tool_name`, `model`, `user_principal`, `conversation_id`, `status` (`ok` or `error`).
    - **`custom.googleapis.com/antigravity/turns`**:
      - Labels: `model`, `user_principal`, `conversation_id`, `turn_number`.
  - User identity is automatically inferred via `gcloud` active account, Application Default Credentials (ADC), or explicit override (`AGY_USER_PRINCIPAL`), enabling per-developer and per-team usage dashboards.

---

## Configuration & Environment Variables

| Hook | Environment Variable | Default | Remote / Cloud Value | Description |
|---|---|---|---|---|
| **Log** | `AGY_LOG_EXPORTER` | `local` | `gcp` or `cloud_logging` | Enable remote streaming to Google Cloud Logging |
| **Log** | `AGY_LOG_NAME` | `antigravity-hooks` | Custom string | Log name / log ID in Cloud Logging |
| **Trace** | `AGY_OTEL_EXPORTER` | `timeline` / unset | `gcp`, `otlp`, or `console` | Export target for distributed tracing spans |
| **Metrics** | `AGY_USER_PRINCIPAL` | Inferred (`gcloud`/ADC/OS) | User / service account email | User identity tag applied to metrics |
| **Global** | `GOOGLE_CLOUD_PROJECT` | Inferred | GCP Project ID | Target GCP project for logs, traces, and metrics |

---

## Google Cloud Setup (Remote Telemetry)

To enable remote telemetry export to Google Cloud, configure the required APIs and IAM roles:

```bash
# 1. Enable Google Cloud APIs
gcloud services enable \
    logging.googleapis.com \
    monitoring.googleapis.com \
    cloudtrace.googleapis.com

# 2. Grant IAM roles to developer or runner account
export PROJECT_ID="$(gcloud config get-value project)"
export USER_ACCOUNT="$(gcloud config get-value account)"

# Logging
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="user:${USER_ACCOUNT}" \
    --role="roles/logging.logWriter"

# Distributed Tracing
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="user:${USER_ACCOUNT}" \
    --role="roles/cloudtrace.agent"

# Cloud Monitoring Metrics
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="user:${USER_ACCOUNT}" \
    --role="roles/monitoring.metricWriter"
```

---

## Hook Registration (`.agents/hooks.json`)

The hooks are registered under [`.agents/hooks.json`](.agents/hooks.json) as standard command-type hooks. Each hook invokes its respective Python script with the lifecycle event name as an argument:

```json
{
  "Log": {
    "enabled": true,
    "PreToolUse": [{ "matcher": "*", "hooks": [{ "type": "command", "command": "./log_hook.py PreToolUse", "timeout": 15 }] }],
    "PostToolUse": [{ "matcher": "*", "hooks": [{ "type": "command", "command": "./log_hook.py PostToolUse", "timeout": 15 }] }],
    "PreInvocation": [{ "type": "command", "command": "./log_hook.py PreInvocation", "timeout": 15 }],
    "PostInvocation": [{ "type": "command", "command": "./log_hook.py PostInvocation", "timeout": 15 }],
    "Stop": [{ "type": "command", "command": "./log_hook.py Stop", "timeout": 15 }]
  },
  "Trace": {
    "enabled": true,
    "PreInvocation": [{ "type": "command", "command": "./trace_hook.py PreInvocation", "timeout": 15 }],
    "PreToolUse": [{ "matcher": "*", "hooks": [{ "type": "command", "command": "./trace_hook.py PreToolUse", "timeout": 15 }] }],
    "PostToolUse": [{ "matcher": "*", "hooks": [{ "type": "command", "command": "./trace_hook.py PostToolUse", "timeout": 15 }] }],
    "PostInvocation": [{ "type": "command", "command": "./trace_hook.py PostInvocation", "timeout": 15 }],
    "Stop": [{ "type": "command", "command": "./trace_hook.py Stop", "timeout": 15 }]
  },
  "Metrics": {
    "enabled": true,
    "PostToolUse": [{ "matcher": "*", "hooks": [{ "type": "command", "command": "./metrics_hook.py PostToolUse", "timeout": 15 }] }],
    "PostInvocation": [{ "type": "command", "command": "./metrics_hook.py PostInvocation", "timeout": 15 }]
  }
}
```
