#!/usr/bin/env python3
"""
Antigravity Trace Hook.
Unifies ASCII timeline visualization and OpenTelemetry (OTel) distributed tracing.

By default (or when AGY_OTEL_EXPORTER is not set / 'console' / 'timeline'):
- Renders continuous ASCII timeline to .agy-local-telemetry/timeline_<cid>.txt

When AGY_OTEL_EXPORTER is configured:
- 'gcp': Exports to Google Cloud Trace
- 'otlp': Exports to OTLP collector
- 'console': Exports OTel span summaries to stdout/stderr AND updates local timeline
- 'timeline' or default: Updates local timeline file
"""

import sys
import json
import os
import glob
import re
import uuid
import hashlib
import subprocess
import time
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional


# ==========================================
# Helpers & Parsing
# ==========================================

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
    for env_var in ("GOOGLE_CLOUD_PROJECT", "CLOUDSDK_CORE_PROJECT", "GCP_PROJECT"):
        val = os.environ.get(env_var)
        if val and val.strip():
            return val.strip()

    if cache_path:
        cached = load_gcp_cache(cache_path).get("project_id")
        if cached and cached.strip():
            return cached.strip()

    try:
        import google.auth
        _, proj = google.auth.default()
        if proj:
            if cache_path:
                update_gcp_cache(cache_path, "project_id", proj)
            return proj
    except Exception:
        pass

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


def parse_iso(ts_str: Optional[str]) -> Optional[datetime]:
    if not ts_str:
        return None
    try:
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1] + "+00:00"
        return datetime.fromisoformat(ts_str)
    except Exception:
        return None


def get_step_start_from_transcript(transcript_path: Optional[str], step_idx: Any) -> Optional[datetime]:
    if not transcript_path or not os.path.exists(transcript_path):
        return None
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                step = json.loads(line)
                if step.get("step_index") == step_idx:
                    return parse_iso(step.get("created_at"))
    except Exception:
        pass
    return None


def dt_to_nano(dt: Optional[datetime]) -> int:
    if not dt:
        dt = datetime.now(timezone.utc)
    return int(dt.timestamp() * 1e9)


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    if seconds < 0:
        seconds = 0
    if seconds < 1.0:
        return f"{seconds * 1000:.0f}ms"
    elif seconds < 60.0:
        return f"{seconds:.2f}s"
    else:
        mins = int(seconds // 60)
        secs = seconds % 60
        return f"{mins}m {secs:.1f}s"


def sanitize_filename(cid: Optional[str]) -> str:
    if not cid:
        return "default"
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", cid)


def conversation_id_to_trace_id(cid: str) -> int:
    """Convert conversation ID (UUID) or string into a 128-bit int (OTel trace_id)."""
    try:
        return uuid.UUID(cid).int
    except Exception:
        return int(hashlib.md5(cid.encode("utf-8")).hexdigest(), 16)


def span_id_from_seed(seed: str) -> int:
    """Generate a stable 64-bit int (OTel span_id) from seed string."""
    return int(hashlib.md5(seed.encode("utf-8")).hexdigest()[:16], 16)


def extract_tool_details(payload: Dict[str, Any]):
    tool_call = payload.get("toolCall", {})
    name = tool_call.get("name", "unknown")
    args = tool_call.get("args", {})

    summary = ""
    if args.get("toolSummary"):
        summary = str(args["toolSummary"])
    elif args.get("toolAction"):
        summary = str(args["toolAction"])
    elif "CommandLine" in args:
        cmd = str(args["CommandLine"]).strip().replace("\n", " ")
        summary = f"`{cmd[:40]}...`" if len(cmd) > 40 else f"`{cmd}`"
    elif "AbsolutePath" in args:
        summary = os.path.basename(str(args["AbsolutePath"]))
    elif "TargetFile" in args:
        summary = os.path.basename(str(args["TargetFile"]))
    elif "Url" in args:
        u = str(args["Url"])
        summary = f"{u[:40]}..." if len(u) > 40 else u
    elif "query" in args:
        q = str(args["query"])
        summary = f"\"{q[:40]}...\"" if len(q) > 40 else f"\"{q}\""

    return name, summary, args


def read_conversation_events(telemetry_dir: str, cid: str) -> List[Dict[str, Any]]:
    """Reads all recorded events for a conversation from the telemetry jsonl file."""
    safe_cid = sanitize_filename(cid)
    jsonl_path = os.path.join(telemetry_dir, f"hooks_{safe_cid}.jsonl")
    if not os.path.exists(jsonl_path):
        return []

    events = []
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue
    except Exception as e:
        sys.stderr.write(f"Error reading {jsonl_path}: {e}\n")
    return events


# ==========================================
# 1. Local Timeline View
# ==========================================

def build_conversation_timeline(events: List[Dict[str, Any]], cid: str, model_name: Optional[str]) -> str:
    first_ts = None
    last_ts = None
    for ev in events:
        ts = parse_iso(ev.get("timestamp"))
        if ts:
            if not first_ts or ts < first_ts:
                first_ts = ts
            if not last_ts or ts > last_ts:
                last_ts = ts

    total_sec = (last_ts - first_ts).total_seconds() if first_ts and last_ts else None

    lines = []
    lines.append(f"Conversation: {cid} | Model: {model_name or 'unknown'} | Total: {format_duration(total_sec)}")
    lines.append("-" * 72)

    invocations = []
    current_inv = None
    open_tools = {}

    for ev in events:
        event_type = ev.get("event")
        ts = parse_iso(ev.get("timestamp"))
        payload = ev.get("payload", {})

        if event_type == "PreInvocation":
            seq_idx = len(invocations)
            inv_num = payload.get("invocationNum", seq_idx)
            current_inv = {
                "seq_idx": seq_idx,
                "num": inv_num,
                "start_ts": ts,
                "end_ts": None,
                "tools": [],
                "stop": None
            }
            invocations.append(current_inv)
        elif event_type == "PreToolUse":
            step_idx = payload.get("stepIdx", len(current_inv["tools"]) if current_inv else 0)
            tool_name, summary, _ = extract_tool_details(payload)
            tool_data = {
                "stepIdx": step_idx,
                "name": tool_name,
                "summary": summary,
                "start_ts": ts,
                "end_ts": None,
                "error": None
            }
            open_tools[step_idx] = tool_data
            if current_inv:
                current_inv["tools"].append(tool_data)
        elif event_type == "PostToolUse":
            step_idx = payload.get("stepIdx")
            tool_data = open_tools.get(step_idx) if step_idx is not None else None
            if tool_data:
                tool_data["end_ts"] = ts
                if payload.get("error"):
                    tool_data["error"] = payload.get("error")
            else:
                # Fallback if PreToolUse was not received / captured
                t_name, summary, _ = extract_tool_details(payload)
                start_ts = None
                t_path = payload.get("transcriptPath")
                if t_path and os.path.exists(t_path):
                    start_ts = get_step_start_from_transcript(t_path, step_idx)
                if not start_ts:
                    if current_inv and current_inv["tools"] and current_inv["tools"][-1]["end_ts"]:
                        start_ts = current_inv["tools"][-1]["end_ts"]
                    elif current_inv and current_inv["start_ts"]:
                        start_ts = current_inv["start_ts"]
                    else:
                        start_ts = ts
                tool_data = {
                    "stepIdx": step_idx,
                    "name": t_name,
                    "summary": summary,
                    "start_ts": start_ts,
                    "end_ts": ts,
                    "error": payload.get("error")
                }
                if step_idx is not None:
                    open_tools[step_idx] = tool_data
                if current_inv:
                    current_inv["tools"].append(tool_data)
        elif event_type == "PostInvocation":
            if current_inv:
                current_inv["end_ts"] = ts
            else:
                # Fallback if PreInvocation was not received
                seq_idx = len(invocations)
                inv_num = payload.get("invocationNum", seq_idx)
                current_inv = {
                    "seq_idx": seq_idx,
                    "num": inv_num,
                    "start_ts": ts,
                    "end_ts": ts,
                    "tools": [],
                    "stop": None
                }
                invocations.append(current_inv)
        elif event_type == "Stop":
            stop_data = {
                "ts": ts,
                "reason": payload.get("terminationReason", "COMPLETED"),
                "error": payload.get("error", "")
            }
            if current_inv:
                current_inv["stop"] = stop_data
            elif invocations:
                invocations[-1]["stop"] = stop_data

    # Format compact continuous timeline
    for inv in invocations:
        inv_dur = (inv["end_ts"] - inv["start_ts"]).total_seconds() if (inv["start_ts"] and inv["end_ts"]) else None
        time_str = inv["start_ts"].strftime("%H:%M:%S") if inv["start_ts"] else "--:--:--"
        lines.append(f"[{time_str}] Invoc #{inv['seq_idx']} (Turn #{inv['num']}) ({format_duration(inv_dur)})")

        tools = inv["tools"]
        has_stop = bool(inv.get("stop"))
        if not tools:
            tree = "  ├─" if has_stop else "  └─"
            lines.append(f"{tree} Direct response ({format_duration(inv_dur)})")
        else:
            for idx, tool in enumerate(tools):
                is_last = (idx == len(tools) - 1) and not has_stop
                tree = "  └─" if is_last else "  ├─"
                t_dur = (tool["end_ts"] - tool["start_ts"]).total_seconds() if (tool["start_ts"] and tool["end_ts"]) else None
                status = " [ERR]" if tool.get("error") else ""
                detail = f": {tool['summary']}" if tool['summary'] else ""
                lines.append(f"{tree} {tool['name']}{status} ({format_duration(t_dur)}){detail}")

        if inv.get("stop"):
            lines.append(f"  └─ Stop: {inv['stop']['reason']}")

    return "\n".join(lines) + "\n"


def render_local_timeline(telemetry_dir: str, cid: str, events: List[Dict[str, Any]]):
    """Generates the timeline_<cid>.txt file."""
    if not events:
        return

    model_name = None
    for ev in events:
        if ev.get("payload", {}).get("modelName"):
            model_name = ev["payload"]["modelName"]
            break

    content = build_conversation_timeline(events, cid, model_name)
    fname = f"timeline_{sanitize_filename(cid)}.txt"
    out_path = os.path.join(telemetry_dir, fname)
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        sys.stderr.write(f"Failed to write timeline to {out_path}: {e}\n")


# ==========================================
# 2. OpenTelemetry Trace Exporter
# ==========================================

def get_otel_exporter(cache_path: Optional[str] = None):
    """
    Returns an OTel SpanExporter based on configuration.
    Environment variables:
      AGY_OTEL_EXPORTER: 'console' / 'timeline' (default), 'gcp' (Cloud Trace), 'otlp', 'none'
    """
    exporter_type = os.environ.get("AGY_OTEL_EXPORTER", "").strip().lower()

    if exporter_type in ("none", "off", "disabled"):
        return None

    if exporter_type == "console":
        try:
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter
            return ConsoleSpanExporter()
        except ImportError:
            return None

    if exporter_type == "otlp":
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            return OTLPSpanExporter()
        except ImportError:
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
                return OTLPSpanExporter()
            except ImportError:
                sys.stderr.write("OTLP exporter not installed. Install opentelemetry-exporter-otlp.\n")
                return None

    if exporter_type == "gcp":
        try:
            from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
            project_id = get_project_id(cache_path)
            return CloudTraceSpanExporter(project_id=project_id) if project_id else CloudTraceSpanExporter()
        except Exception as e:
            sys.stderr.write(f"Failed to initialize CloudTraceSpanExporter: {e}\n")
            return None

    return None


def export_otel_spans(cid: str, events: List[Dict[str, Any]], telemetry_dir: str, cache_path: Optional[str] = None):
    exporter = get_otel_exporter(cache_path)
    if not exporter or not events:
        return

    try:
        from opentelemetry.trace import SpanContext, SpanKind, StatusCode, Status
        from opentelemetry.sdk.trace import ReadableSpan
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace.export import SpanExportResult
    except ImportError:
        return

    trace_id = conversation_id_to_trace_id(cid)
    root_span_id = span_id_from_seed(f"root_{cid}")

    state_file = os.path.join(telemetry_dir, f".otel_exported_{sanitize_filename(cid)}.json")
    exported_keys = set()
    if os.path.exists(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as sf:
                exported_keys = set(json.load(sf))
        except Exception:
            exported_keys = set()

    invocations: List[Dict[str, Any]] = []
    current_inv: Optional[Dict[str, Any]] = None
    open_tools: Dict[Any, Dict[str, Any]] = {}
    model_name = "unknown"
    workspace_path = ""

    for ev in events:
        event_type = ev.get("event")
        ts = parse_iso(ev.get("timestamp"))
        payload = ev.get("payload", {})
        if payload.get("modelName"):
            model_name = payload.get("modelName")
        if payload.get("workspacePaths") and not workspace_path:
            workspace_path = payload.get("workspacePaths")[0]

        if event_type == "PreInvocation":
            seq_idx = len(invocations)
            inv_num = payload.get("invocationNum", seq_idx)
            current_inv = {
                "seq_idx": seq_idx,
                "inv_num": inv_num,
                "start_ts": ts,
                "end_ts": None,
                "tools": [],
                "stop": None
            }
            invocations.append(current_inv)
        elif event_type == "PreToolUse":
            step_idx = payload.get("stepIdx", len(current_inv["tools"]) if current_inv else 0)
            t_name, summary, args = extract_tool_details(payload)
            tool_data = {
                "stepIdx": step_idx,
                "name": t_name,
                "summary": summary,
                "args": args,
                "start_ts": ts,
                "end_ts": None,
                "error": None
            }
            open_tools[step_idx] = tool_data
            if current_inv:
                current_inv["tools"].append(tool_data)
        elif event_type == "PostToolUse":
            step_idx = payload.get("stepIdx")
            tool_data = open_tools.get(step_idx) if step_idx is not None else None
            if tool_data:
                tool_data["end_ts"] = ts
                if payload.get("error"):
                    tool_data["error"] = payload.get("error")
            else:
                # Fallback if PreToolUse was not captured
                t_name, summary, args = extract_tool_details(payload)
                start_ts = None
                t_path = payload.get("transcriptPath")
                if t_path and os.path.exists(t_path):
                    start_ts = get_step_start_from_transcript(t_path, step_idx)
                if not start_ts:
                    if current_inv and current_inv["tools"] and current_inv["tools"][-1]["end_ts"]:
                        start_ts = current_inv["tools"][-1]["end_ts"]
                    elif current_inv and current_inv["start_ts"]:
                        start_ts = current_inv["start_ts"]
                    else:
                        start_ts = ts
                tool_data = {
                    "stepIdx": step_idx,
                    "name": t_name,
                    "summary": summary,
                    "args": args,
                    "start_ts": start_ts,
                    "end_ts": ts,
                    "error": payload.get("error")
                }
                if step_idx is not None:
                    open_tools[step_idx] = tool_data
                if current_inv:
                    current_inv["tools"].append(tool_data)
        elif event_type == "PostInvocation":
            if current_inv:
                current_inv["end_ts"] = ts
            else:
                # Fallback if PreInvocation was not received
                seq_idx = len(invocations)
                inv_num = payload.get("invocationNum", seq_idx)
                current_inv = {
                    "seq_idx": seq_idx,
                    "inv_num": inv_num,
                    "start_ts": ts,
                    "end_ts": ts,
                    "tools": [],
                    "stop": None
                }
                invocations.append(current_inv)
        elif event_type == "Stop":
            stop_data = {
                "ts": ts,
                "reason": payload.get("terminationReason", "COMPLETED"),
                "error": payload.get("error", "")
            }
            if current_inv:
                current_inv["stop"] = stop_data
            elif invocations:
                invocations[-1]["stop"] = stop_data

    resource = Resource.create({
        "service.name": "antigravity",
        "service.namespace": "assistant",
        "gen_ai.system": "antigravity",
        "conversation.id": cid,
        "gen_ai.request.model": model_name,
        "workspace.path": workspace_path
    })

    spans_to_export: List[ReadableSpan] = []

    # 1. Root Span for conversation session
    first_ts = parse_iso(events[0].get("timestamp")) if events else None
    last_ts = parse_iso(events[-1].get("timestamp")) if events else None
    is_conversation_stopped = any(ev.get("event") == "Stop" for ev in events)

    if is_conversation_stopped and "root" not in exported_keys:
        root_ctx = SpanContext(trace_id=trace_id, span_id=root_span_id, is_remote=False)
        root_span = ReadableSpan(
            name=f"Conversation {cid[:8]}",
            context=root_ctx,
            parent=None,
            resource=resource,
            attributes={
                "gen_ai.conversation.id": cid,
                "gen_ai.model": model_name,
                "session.closed": True
            },
            events=[],
            links=[],
            kind=SpanKind.SERVER,
            status=Status(status_code=StatusCode.OK),
            start_time=dt_to_nano(first_ts),
            end_time=dt_to_nano(last_ts)
        )
        spans_to_export.append(root_span)
        exported_keys.add("root")

    # 2. Invocations & Tool spans
    for inv in invocations:
        seq_idx = inv["seq_idx"]
        inv_num = inv["inv_num"]
        inv_key = f"inv_seq_{seq_idx}"
        inv_span_id = span_id_from_seed(f"{cid}_inv_seq_{seq_idx}")
        inv_ctx = SpanContext(trace_id=trace_id, span_id=inv_span_id, is_remote=False)
        parent_ctx = SpanContext(trace_id=trace_id, span_id=root_span_id, is_remote=False)

        if inv["end_ts"] and inv_key not in exported_keys:
            inv_span = ReadableSpan(
                name=f"Invocation #{seq_idx} (Turn #{inv_num})",
                context=inv_ctx,
                parent=parent_ctx,
                resource=resource,
                attributes={
                    "gen_ai.operation.name": "chat",
                    "gen_ai.request.model": model_name,
                    "invocation.seq_index": seq_idx,
                    "invocation.turn_number": inv_num,
                    "conversation.id": cid
                },
                events=[],
                links=[],
                kind=SpanKind.INTERNAL,
                status=Status(status_code=StatusCode.OK),
                start_time=dt_to_nano(inv["start_ts"]),
                end_time=dt_to_nano(inv["end_ts"])
            )
            spans_to_export.append(inv_span)
            exported_keys.add(inv_key)

        for tool in inv["tools"]:
            step_idx = tool["stepIdx"]
            tool_key = f"tool_step_{step_idx}"
            if tool["end_ts"] and tool_key not in exported_keys:
                tool_span_id = span_id_from_seed(f"{cid}_tool_step_{step_idx}")
                tool_ctx = SpanContext(trace_id=trace_id, span_id=tool_span_id, is_remote=False)

                tool_name = tool["name"]
                tool_summary = tool["summary"]
                err = tool.get("error")

                span_status = Status(status_code=StatusCode.ERROR if err else StatusCode.OK, description=str(err) if err else None)

                attrs = {
                    "gen_ai.tool.name": tool_name,
                    "gen_ai.tool.summary": tool_summary,
                    "gen_ai.step_idx": step_idx,
                    "conversation.id": cid
                }
                args = tool.get("args", {})
                if "CommandLine" in args:
                    attrs["tool.command_line"] = str(args["CommandLine"])[:2048]
                if "AbsolutePath" in args:
                    attrs["tool.file_path"] = str(args["AbsolutePath"])
                if "TargetFile" in args:
                    attrs["tool.target_file"] = str(args["TargetFile"])
                if "Url" in args:
                    attrs["tool.url"] = str(args["Url"])

                tool_span = ReadableSpan(
                    name=f"Tool: {tool_name}",
                    context=tool_ctx,
                    parent=inv_ctx,
                    resource=resource,
                    attributes=attrs,
                    events=[],
                    links=[],
                    kind=SpanKind.INTERNAL,
                    status=span_status,
                    start_time=dt_to_nano(tool["start_ts"]),
                    end_time=dt_to_nano(tool["end_ts"])
                )
                spans_to_export.append(tool_span)
                exported_keys.add(tool_key)

    if spans_to_export:
        try:
            result = exporter.export(spans_to_export)
            if result == SpanExportResult.SUCCESS or str(result) == "SpanExportResult.SUCCESS":
                with open(state_file, "w", encoding="utf-8") as sf:
                    json.dump(list(exported_keys), sf)
            else:
                sys.stderr.write(f"OTel export failed: {result}\n")
        except Exception as e:
            sys.stderr.write(f"OTel export exception: {e}\n")


def record_event_if_missing(telemetry_dir: str, cid: str, hook_event: str, payload: Dict[str, Any]):
    safe_cid = sanitize_filename(cid)
    jsonl_path = os.path.join(telemetry_dir, f"hooks_{safe_cid}.jsonl")
    os.makedirs(telemetry_dir, exist_ok=True)

    already_logged = False
    if os.path.exists(jsonl_path):
        try:
            with open(jsonl_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
                for line in reversed(lines[-5:]):
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
                    if entry.get("event") == hook_event:
                        p = entry.get("payload", {})
                        if hook_event in ("PreToolUse", "PostToolUse"):
                            if p.get("stepIdx") == payload.get("stepIdx"):
                                already_logged = True
                                break
                        elif hook_event in ("PreInvocation", "PostInvocation"):
                            if p.get("invocationNum") == payload.get("invocationNum"):
                                already_logged = True
                                break
                        elif hook_event == "Stop":
                            already_logged = True
                            break
        except Exception:
            pass

    if not already_logged:
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": hook_event,
            "payload": payload
        }
        try:
            with open(jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
        except Exception as e:
            sys.stderr.write(f"Failed to record event in trace hook: {e}\n")


def run_background_trace(hook_event: str, payload: Dict[str, Any]):
    """Background worker logic: renders timeline and exports traces without blocking."""
    cid = payload.get("conversationId", "default")
    agents_dir = os.path.dirname(os.path.abspath(__file__))
    workspace_dir = os.path.dirname(agents_dir)
    telemetry_dir = os.path.join(workspace_dir, ".agy-local-telemetry")
    cache_path = os.path.join(telemetry_dir, "gcp_cache.json")

    if hook_event in ("PreInvocation", "PreToolUse", "PostToolUse", "PostInvocation", "Stop"):
        record_event_if_missing(telemetry_dir, cid, hook_event, payload)
        events = read_conversation_events(telemetry_dir, cid)
        if events:
            # 1. Update local ASCII timeline
            render_local_timeline(telemetry_dir, cid, events)
            # 2. Export OTel distributed traces if configured
            export_otel_spans(cid, events, telemetry_dir, cache_path)


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
    # Handle manual CLI commands to inspect the timeline (Synchronous)
    if len(sys.argv) > 1 and sys.argv[1] in ("--timeline", "timeline", "--print", "print", "--show", "show"):
        agents_dir = os.path.dirname(os.path.abspath(__file__))
        workspace_dir = os.path.dirname(agents_dir)
        telemetry_dir = os.path.join(workspace_dir, ".agy-local-telemetry")
        cid = sys.argv[2] if len(sys.argv) > 2 else None
        if not cid:
            jsonl_files = glob.glob(os.path.join(telemetry_dir, "hooks_*.jsonl"))
            if jsonl_files:
                jsonl_files.sort(key=os.path.getmtime, reverse=True)
                latest = os.path.basename(jsonl_files[0])
                cid = latest.replace("hooks_", "").replace(".jsonl", "")
        if cid:
            events = read_conversation_events(telemetry_dir, cid)
            model_name = None
            for ev in events:
                if ev.get("payload", {}).get("modelName"):
                    model_name = ev["payload"]["modelName"]
                    break
            sys.stdout.write(build_conversation_timeline(events, cid, model_name))
        else:
            sys.stderr.write("No telemetry found in .agy-local-telemetry/\n")
        return

    # Handle async worker invocation
    if len(sys.argv) > 1 and sys.argv[1] == "--async-worker":
        worker_event = sys.argv[2] if len(sys.argv) > 2 else "unknown"
        stdin_content = sys.stdin.read()
        payload = json.loads(stdin_content) if stdin_content.strip() else {}
        run_background_trace(worker_event, payload)
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

    # Fork / detach background worker to update timeline and export traces
    detach_and_run(run_background_trace, hook_event, payload)


if __name__ == "__main__":
    main()
