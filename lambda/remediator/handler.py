"""
NetPulse — Remediator Lambda
-----------------------------
Called by the Step Functions state machine to execute remediation actions.

Actions:
  RETRY   — re-probes the endpoint to see if it recovered on its own
  REROUTE — simulates traffic rerouting away from the degraded endpoint
  ALERT   — publishes an SNS notification to the ops team
  RESOLVE — marks the incident as resolved in DynamoDB
"""
import json
import os
import time
import urllib.request
import urllib.error
import boto3
from datetime import datetime, timezone

# clients initialized outside handler for Lambda container reuse
dynamodb = boto3.resource("dynamodb")
sns_client = boto3.client("sns")

INCIDENTS_TABLE = os.environ["INCIDENTS_TABLE"]
ALERT_TOPIC_ARN = os.environ["ALERT_TOPIC_ARN"]
LATENCY_THRESHOLD_MS = int(os.environ.get("LATENCY_THRESHOLD_MS", "2000"))

table = dynamodb.Table(INCIDENTS_TABLE)


def handle_retry(incident: dict) -> dict:
    """
    Re-probes the endpoint. If it responds healthy, returns RECOVERED.
    Increments remediation_attempts in DynamoDB either way.
    """
    endpoint = incident["endpoint"]
    print(f"[RETRY] Probing {endpoint}")

    try:
        start = time.monotonic()
        req = urllib.request.Request(endpoint, method="GET")
        req.add_header("User-Agent", "NetPulse-Remediator/1.0")
        with urllib.request.urlopen(req, timeout=10) as response:
            latency_ms = int((time.monotonic() - start) * 1000)
            recovered = response.status < 500 and latency_ms < LATENCY_THRESHOLD_MS
    except Exception:
        recovered = False

    # increment attempt counter regardless of outcome
    table.update_item(
        Key={
            "incident_id": incident["incident_id"],
            "timestamp": incident.get("timestamp", _get_timestamp(incident["incident_id"])),
        },
        UpdateExpression="ADD remediation_attempts :one SET last_retry_at = :ts",
        ExpressionAttributeValues={
            ":one": 1,
            ":ts": datetime.now(timezone.utc).isoformat(),
        },
    )

    status = "RECOVERED" if recovered else "STILL_DEGRADED"
    print(f"[RETRY] Result: {status}")
    return {"status": status, "endpoint": endpoint}


def handle_reroute(incident: dict) -> dict:
    """
    Simulates rerouting traffic away from the degraded endpoint.
    In a real system this would update a Route53 record, ALB target group,
    or a config store that your load balancer reads.
    """
    endpoint = incident["endpoint"]
    print(f"[REROUTE] Simulating traffic reroute away from {endpoint}")

    time.sleep(0.5)

    table.update_item(
        Key={
            "incident_id": incident["incident_id"],
            "timestamp": incident.get("timestamp", _get_timestamp(incident["incident_id"])),
        },
        UpdateExpression="SET rerouted = :true, rerouted_at = :ts, #st = :status",
        ExpressionAttributeNames={"#st": "status"},
        ExpressionAttributeValues={
            ":true": True,
            ":ts": datetime.now(timezone.utc).isoformat(),
            ":status": "REROUTED",
        },
    )

    print(f"[REROUTE] Traffic rerouted — incident marked REROUTED")
    return {"status": "REROUTED", "endpoint": endpoint}


def handle_alert(incident: dict) -> dict:
    """
    Publishes an SNS alert so the on-call engineer is notified.
    """
    endpoint = incident["endpoint"]
    reason = incident.get("degradation_reason", "Unknown")
    incident_id = incident["incident_id"]

    subject = f"[NetPulse] Degraded endpoint: {endpoint}"
    message = (
        f"INCIDENT ALERT\n"
        f"--------------\n"
        f"Incident ID : {incident_id}\n"
        f"Endpoint    : {endpoint}\n"
        f"Reason      : {reason}\n"
        f"Status code : {incident.get('status_code', 'N/A')}\n"
        f"Latency     : {incident.get('latency_ms', 'N/A')}ms\n"
        f"Time        : {datetime.now(timezone.utc).isoformat()}\n\n"
        f"Auto-remediation attempted. Manual review may be required."
    )

    sns_client.publish(
        TopicArn=ALERT_TOPIC_ARN,
        Subject=subject,
        Message=message,
    )

    table.update_item(
        Key={
            "incident_id": incident_id,
            "timestamp": incident.get("timestamp", _get_timestamp(incident_id)),
        },
        UpdateExpression="SET alert_sent = :true, alert_sent_at = :ts",
        ExpressionAttributeValues={
            ":true": True,
            ":ts": datetime.now(timezone.utc).isoformat(),
        },
    )

    print(f"[ALERT] SNS notification sent for incident {incident_id}")
    return {"status": "ALERT_SENT", "incident_id": incident_id}


def handle_resolve(incident: dict) -> dict:
    """
    Marks the incident as RESOLVED in DynamoDB.
    """
    incident_id = incident["incident_id"]
    resolved_at = datetime.now(timezone.utc).isoformat()

    table.update_item(
        Key={
            "incident_id": incident_id,
            "timestamp": incident.get("timestamp", _get_timestamp(incident_id)),
        },
        UpdateExpression="SET #st = :resolved, resolved_at = :ts",
        ExpressionAttributeNames={"#st": "status"},
        ExpressionAttributeValues={
            ":resolved": "RESOLVED",
            ":ts": resolved_at,
        },
    )

    print(f"[RESOLVE] Incident {incident_id} marked RESOLVED at {resolved_at}")
    return {"status": "RESOLVED", "incident_id": incident_id, "resolved_at": resolved_at}


def _get_timestamp(incident_id: str) -> str:
    """
    Fallback: queries DynamoDB for the timestamp of an incident by ID.
    Used when Step Functions passes an incident without the sort key.
    """
    response = table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("incident_id").eq(incident_id),
        Limit=1,
    )
    items = response.get("Items", [])
    return items[0]["timestamp"] if items else datetime.now(timezone.utc).isoformat()


# action dispatcher — maps action strings to handler functions
# makes it easy to add new actions without touching the main handler
ACTIONS = {
    "RETRY": handle_retry,
    "REROUTE": handle_reroute,
    "ALERT": handle_alert,
    "RESOLVE": handle_resolve,
}


def lambda_handler(event, context):
    """
    Entry point. Dispatches to the correct action handler based on
    the 'action' field in the event passed by Step Functions.
    """
    action = event.get("action")
    incident = event.get("incident", event)

    print(f"[REMEDIATOR] Action={action} IncidentID={incident.get('incident_id')}")

    if action not in ACTIONS:
        raise ValueError(f"Unknown action: {action}. Valid actions: {list(ACTIONS.keys())}")

    result = ACTIONS[action](incident)
    print(f"[REMEDIATOR] Result: {json.dumps(result)}")
    return result
