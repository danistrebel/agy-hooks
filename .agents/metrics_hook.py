#!/usr/bin/env python3
"""
Antigravity Cloud Monitoring (Metrics) Hook.
Captures tool execution metrics and turn counts, exporting them to Google Cloud Monitoring.

Custom Metrics Exported:
1. custom.googleapis.com/antigravity/tool_calls (GAUGE / INT64)
   - Labels: tool_name, model, user_principal, conversation_id, status
2. custom.googleapis.com/antigravity/turns (GAUGE / INT64)
   - Labels: model, user_principal, conversation_id, turn_number
"""

import sys
import json
import os
import subprocess
import time
from typing import Dict, Any, Optional


def load_gcp_cache(cache_path: str) -> Dict[str, Any]:
    """Load cached GCP metadata from disk if within TTL (24h)."""
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if time.time() - data.get("updated_at", 0) < 86400:
                    return data
        except Exception:
            pass
    return {}


def update_gcp_cache(cache_path: str, key: str, val: str):
    """Update a specific key in the local GCP cache."""
    if not val:
        return
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        data = {}
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        data[key] = val
        data["updated_at"] = time.time()
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


def infer_user_principal(cache_path: Optional[str] = None) -> str:
    """Infer user principal from environment, cache, credentials, or gcloud."""
    # 1. Direct environment variable overrides
    for env_var in ("AGY_USER_PRINCIPAL", "GOOGLE_USER_PRINCIPAL", "USER_EMAIL", "GCP_USER_PRINCIPAL"):
        val = os.environ.get(env_var)
        if val and val.strip():
            return val.strip()

    # 2. Check local disk cache
    if cache_path:
        cached = load_gcp_cache(cache_path).get("user_principal")
        if cached and cached.strip():
            return cached.strip()

    # 3. Inferred from gcloud active account
    try:
        res = subprocess.run(
            ["gcloud", "config", "get-value", "account"],
            capture_output=True,
            text=True,
            timeout=2
        )
        if res.returncode == 0 and res.stdout.strip() and "(unset)" not in res.stdout:
            acc = res.stdout.strip()
            if cache_path:
                update_gcp_cache(cache_path, "user_principal", acc)
            return acc
    except Exception:
        pass

    # 4. Inferred from Google default credentials
    try:
        import google.auth
        creds, _ = google.auth.default()
        email = getattr(creds, "service_account_email", None) or getattr(creds, "signer_email", None)
        if email and email.strip():
            if cache_path:
                update_gcp_cache(cache_path, "user_principal", email.strip())
            return email.strip()
    except Exception:
        pass

    # 5. Fallback to OS user or default
    return os.environ.get("USER", "unknown")


def get_project_id(cache_path: Optional[str] = None) -> Optional[str]:
    """Resolve GCP Project ID from env, cache, credentials, or gcloud."""
    # 1. Environment variables
    for env_var in ("GOOGLE_CLOUD_PROJECT", "CLOUDSDK_CORE_PROJECT", "GCP_PROJECT"):
        val = os.environ.get(env_var)
        if val and val.strip():
            return val.strip()

    # 2. Check local disk cache
    if cache_path:
        cached = load_gcp_cache(cache_path).get("project_id")
        if cached and cached.strip():
            return cached.strip()

    # 3. Google application default credentials
    try:
        import google.auth
        _, proj = google.auth.default()
        if proj:
            if cache_path:
                update_gcp_cache(cache_path, "project_id", proj)
            return proj
    except Exception:
        pass

    # 4. gcloud CLI active project
    try:
        res = subprocess.run(
            ["gcloud", "config", "get-value", "project"],
            capture_output=True,
            text=True,
            timeout=2
        )
        if res.returncode == 0 and res.stdout.strip() and "(unset)" not in res.stdout:
            proj = res.stdout.strip()
            if cache_path:
                update_gcp_cache(cache_path, "project_id", proj)
            return proj
    except Exception:
        pass

    return None


def create_point(int_val: int = 1):
    from google.cloud import monitoring_v3
    now = time.time()
    seconds = int(now)
    nanos = int((now - seconds) * 10**9)
    interval = monitoring_v3.TimeInterval({"end_time": {"seconds": seconds, "nanos": nanos}})
    return monitoring_v3.Point({"interval": interval, "value": {"int64_value": int_val}})


def export_tool_call_metric(client, project_id: str, payload: Dict[str, Any], cache_path: str):
    from google.cloud import monitoring_v3
    tool_call = payload.get("toolCall", {})
    tool_name = tool_call.get("name", "unknown")
    model = payload.get("modelName", "unknown")
    cid = payload.get("conversationId", "default")
    user_principal = infer_user_principal(cache_path)
    status = "error" if payload.get("error") else "ok"

    series = monitoring_v3.TimeSeries()
    series.metric.type = "custom.googleapis.com/antigravity/tool_calls"
    series.metric.labels["tool_name"] = tool_name
    series.metric.labels["model"] = model
    series.metric.labels["user_principal"] = user_principal
    series.metric.labels["conversation_id"] = cid
    series.metric.labels["status"] = status

    series.resource.type = "global"
    series.resource.labels["project_id"] = project_id
    series.points = [create_point(1)]

    project_name = f"projects/{project_id}"
    client.create_time_series(name=project_name, time_series=[series])


def export_turn_metric(client, project_id: str, payload: Dict[str, Any], cache_path: str):
    from google.cloud import monitoring_v3
    model = payload.get("modelName", "unknown")
    cid = payload.get("conversationId", "default")
    inv_num = payload.get("invocationNum", 0)
    user_principal = infer_user_principal(cache_path)

    series = monitoring_v3.TimeSeries()
    series.metric.type = "custom.googleapis.com/antigravity/turns"
    series.metric.labels["model"] = model
    series.metric.labels["user_principal"] = user_principal
    series.metric.labels["conversation_id"] = cid
    series.metric.labels["turn_number"] = str(inv_num)

    series.resource.type = "global"
    series.resource.labels["project_id"] = project_id
    series.points = [create_point(1)]

    project_name = f"projects/{project_id}"
    client.create_time_series(name=project_name, time_series=[series])


def run_background_metrics(hook_event: str, payload: Dict[str, Any]):
    """Background worker logic: exports metrics without stalling agent."""
    agents_dir = os.path.dirname(os.path.abspath(__file__))
    workspace_dir = os.path.dirname(agents_dir)
    telemetry_dir = os.path.join(workspace_dir, ".agy-local-telemetry")
    cache_path = os.path.join(telemetry_dir, "gcp_cache.json")

    project_id = get_project_id(cache_path)
    if not project_id:
        return

    try:
        from google.cloud import monitoring_v3
        client = monitoring_v3.MetricServiceClient()
        if hook_event == "PostToolUse":
            export_tool_call_metric(client, project_id, payload, cache_path)
        elif hook_event == "PostInvocation":
            export_turn_metric(client, project_id, payload, cache_path)
    except ImportError:
        pass
    except Exception as e:
        sys.stderr.write(f"Cloud Monitoring metric export error: {e}\n")


def detach_and_run(worker_fn, *args):
    """Detaches execution into background so the hook parent exits immediately."""
    if hasattr(os, "fork"):
        pid = os.fork()
        if pid > 0:
            os._exit(0)

        # Child process: detach session and file descriptors
        try:
            os.setsid()
            devnull = os.open(os.devnull, os.O_RDWR)
            os.dup2(devnull, 0)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
            os.close(devnull)
        except Exception:
            pass

        try:
            worker_fn(*args)
        except Exception:
            pass
        finally:
            os._exit(0)
    else:
        # Fallback for Windows without os.fork
        try:
            hook_event, payload = args
            p = subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), "--async-worker", hook_event],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
            p.stdin.write(json.dumps(payload).encode("utf-8"))
            p.stdin.close()
        except Exception:
            pass
        os._exit(0)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--async-worker":
        hook_event = sys.argv[2] if len(sys.argv) > 2 else "unknown"
        stdin_content = sys.stdin.read()
        payload = json.loads(stdin_content) if stdin_content.strip() else {}
        run_background_metrics(hook_event, payload)
        return

    hook_event = sys.argv[1] if len(sys.argv) > 1 else "unknown"

    stdin_content = sys.stdin.read()
    payload = {}
    if stdin_content.strip():
        try:
            payload = json.loads(stdin_content)
        except Exception:
            pass

    # FAST PATH: Respond to Antigravity immediately so agent loop is never blocked
    if hook_event == "PreToolUse":
        response = {"decision": "allow"}
    else:
        response = {}

    sys.stdout.write(json.dumps(response))
    sys.stdout.flush()

    # Fork / detach background worker to process Cloud Monitoring metrics
    detach_and_run(run_background_metrics, hook_event, payload)


if __name__ == "__main__":
    main()
