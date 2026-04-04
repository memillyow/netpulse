"""
NetPulse — Detector Lambda
--------------------------
Probes a list of HTTP endpoints for availability and latency.
If an endpoint is degraded (down or slow), writes an incident to DynamoDB
and triggers the Step Functions remediation state machine.
"""
import json
import os
import time
import uuid
import urllib.request
import urllib.error
import boto3
from datetime import datetime, timezone

# clients are initialized outside the handler to take advantage of
# Lambda container reuse — avoids reconnecting on every invocation
dynamodb = boto3.resource("dynamodb")
sfn_client = boto3.client("stepfunctions")

INCIDENTS_TABLE = os.environ["INCIDENTS_TABLE"]
STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]
ENDPOINTS = os.environ.get("ENDPOINTS", "").split(",")
LATENCY_THRESHOLD_MS = int(os.environ.get("LATENCY_THRESHOLD_MS", "2000"))

table = dynamodb.Table(INCIDENTS_TABLE)


def probe_endpoint(url: str) -> dict:
    """
    Probes a single HTTP endpoint.
    Returns a dict with: url, status_code, latency_ms, healthy, error
    """
    start = time.monotonic()
    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", "NetPulse-Detector/1.0")
        with urllib.request.urlopen(req, timeout=10) as response:
            latency_ms = int((time.monotonic() - start) * 1000)
            status_code = response.status
            # treat anything 5xx or over latency threshold as unhealthy
            healthy = status_code < 500 and latency_ms < LATENCY_THRESHOLD_MS
            return {
                "url": url,
                "status_code": status_code,
                "latency_ms": latency_ms,
                "healthy": healthy,
                "error": None,
                "degradation_reason": (
                    f"High latency: {latency_ms}ms > {LATENCY_THRESHOLD_MS}ms"
                    if latency_ms >= LATENCY_THRESHOLD_MS
                    else None
                ),
            }
    except urllib.error.HTTPError as e:
        latency_ms = int((time.monotonic() - start) * 1000)
        return {
            "url": url,
            "status_code": e.code,
            "latency_ms": latency_ms,
            "healthy": False,
            "error": str(e),
            "degradation_reason": f"HTTP error: {e.code} {e.reason}",
        }
    except Exception as e:
        latency_ms = int((time.monotonic() - start) * 1000)
        return {
            "url": url,
            "status_code": None,
            "latency_ms": latency_ms,
            "healthy": False,
            "error": str(e),
            "degradation_reason": f"Connection failed: {str(e)}",
        }


def write_incident(probe_result: dict) -> str:
    """
    Writes a new incident record to DynamoDB.
    Returns the generated incident_id.
    """
    incident_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()

    table.put_item(
        Item={
            "incident_id": incident_id,
            "timestamp": timestamp,
            "endpoint": probe_result["url"],
            "status": "OPEN",
            "status_code": probe_result.get("status_code"),
            "latency_ms": probe_result["latency_ms"],
            "degradation_reason": probe_result["degradation_reason"],
            "error": probe_result.get("error"),
            "remediation_attempts": 0,
            "resolved_at": None,
        }
    )
    print(f"[INCIDENT CREATED] {incident_id} — {probe_result['url']}")
    return incident_id


def trigger_remediation(incident_id: str, probe_result: dict) -> None:
    """
    Starts a Step Functions execution for this incident.
    Execution name includes timestamp to avoid conflicts on rapid retriggers.
    """
    execution_input = {
        "incident_id": incident_id,
        "endpoint": probe_result["url"],
        "status_code": probe_result.get("status_code"),
        "latency_ms": probe_result["latency_ms"],
        "degradation_reason": probe_result["degradation_reason"],
    }

    response = sfn_client.start_execution(
        stateMachineArn=STATE_MACHINE_ARN,
        name=f"incident-{incident_id[:8]}-{int(time.time())}",
        input=json.dumps(execution_input),
    )
    print(f"[STATE MACHINE STARTED] execution: {response['executionArn']}")


def lambda_handler(event, context):
    """
    Entry point. Probes all configured endpoints and handles degraded ones.
    """
    print(f"[DETECTOR] Probing {len(ENDPOINTS)} endpoint(s)")
    results = {"probed": 0, "healthy": 0, "degraded": 0, "incidents": []}

    for url in ENDPOINTS:
        url = url.strip()
        if not url:
            continue

        probe = probe_endpoint(url)
        results["probed"] += 1

        print(
            f"[PROBE] {url} — "
            f"status={probe['status_code']} "
            f"latency={probe['latency_ms']}ms "
            f"healthy={probe['healthy']}"
        )

        if probe["healthy"]:
            results["healthy"] += 1
        else:
            results["degraded"] += 1
            incident_id = write_incident(probe)
            trigger_remediation(incident_id, probe)
            results["incidents"].append(incident_id)

    # log summary so we can track probe results in CloudWatch
    print(
        f"[DETECTOR COMPLETE] "
        f"probed={results['probed']} "
        f"healthy={results['healthy']} "
        f"degraded={results['degraded']}"
    )
    return results
