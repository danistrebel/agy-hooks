#!/usr/bin/env python3
"""
Antigravity Log Hook.
Captures lifecycle hook events and payloads.
Defaults to local JSONL logging (.agy-local-telemetry/hooks_<cid>.jsonl).
Optionally forwards log entries to Google Cloud Logging when AGY_LOG_EXPORTER="gcp"
or AGY_LOG_EXPORTER="cloud_logging" is configured.
"""

import sys
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any


def sanitize_filename(cid: Optional[str]) -> str:
    if not cid:
        return "default"
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", cid)


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


def export_to_cloud_logging(log_entry: dict, cid: str, hook_event: str, cache_path: Optional[str] = None):
    """Optionally export log entry to Google Cloud Logging."""
    exporter_mode = os.environ.get("AGY_LOG_EXPORTER", "local").lower()
    if exporter_mode not in ("gcp", "cloud_logging", "google_cloud"):
        return

    try:
        from google.cloud import logging as gcp_logging
    except ImportError:
        sys.stderr.write("Google Cloud Logging library not available. Install google-cloud-logging.\n")
        return

    project_id = get_project_id(cache_path)
    if not project_id:
        return

    try:
        client = gcp_logging.Client(project=project_id)
        logger_name = os.environ.get("AGY_LOG_NAME", "antigravity-hooks")
        logger = client.logger(logger_name)

        severity = "INFO"
        if hook_event == "Stop" and log_entry.get("payload", {}).get("error"):
            severity = "ERROR"
        elif log_entry.get("payload", {}).get("error"):
            severity = "WARNING"

        labels = {
            "conversation_id": cid,
            "event": hook_event,
            "model": log_entry.get("payload", {}).get("modelName", "unknown")
        }

        logger.log_struct(
            log_entry,
            severity=severity,
            labels=labels
        )
    except Exception as e:
        sys.stderr.write(f"Failed to export to Cloud Logging: {e}\n")


def run_background_log(hook_event: str, payload: Dict[str, Any]):
    """Background worker logic: writes local JSONL and exports to Cloud Logging."""
    cid = payload.get("conversationId", "default")
    safe_cid = sanitize_filename(cid)

    agents_dir = os.path.dirname(os.path.abspath(__file__))
    workspace_dir = os.path.dirname(agents_dir)
    telemetry_dir = os.path.join(workspace_dir, ".agy-local-telemetry")
    os.makedirs(telemetry_dir, exist_ok=True)
    cache_path = os.path.join(telemetry_dir, "gcp_cache.json")

    log_file = os.path.join(telemetry_dir, f"hooks_{safe_cid}.jsonl")

    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": hook_event,
        "payload": payload
    }

    # 1. Local logging
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
    except Exception as e:
        sys.stderr.write(f"Failed to write local log: {e}\n")

    # 2. Optional Google Cloud Logging export
    export_to_cloud_logging(log_entry, cid, hook_event, cache_path)


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
        run_background_log(hook_event, payload)
        return

    hook_event = sys.argv[1] if len(sys.argv) > 1 else "unknown"

    stdin_content = sys.stdin.read()
    payload = {}
    if stdin_content.strip():
        try:
            payload = json.loads(stdin_content)
        except Exception as e:
            payload = {"_raw": stdin_content, "_error": str(e)}

    # FAST PATH: Respond to Antigravity immediately so agent loop is never blocked
    if hook_event == "PreToolUse":
        response = {"decision": "allow"}
    else:
        response = {}

    sys.stdout.write(json.dumps(response))
    sys.stdout.flush()

    # Fork / detach background worker for disk writing and Cloud Logging export
    detach_and_run(run_background_log, hook_event, payload)


if __name__ == "__main__":
    main()
