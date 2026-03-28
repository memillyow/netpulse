# NetPulse Core

Automated network health monitoring and incident remediation on AWS.
Detects degraded endpoints and heals them — no human intervention required.

## Architecture

```
EventBridge (1 min)
       │
       ▼
  Detector Lambda          ← probes endpoints for latency + availability
       │
       │ [degraded endpoint]
       ▼
  DynamoDB                 ← writes OPEN incident record
       │
       ▼
  Step Functions           ← orchestrates remediation workflow
   ┌───┴────────────────────────────┐
   │                                │
   ▼                                │
RETRY (re-probe)            [if STILL_DEGRADED]
   │                                │
   │ [RECOVERED]                    ▼
   │                         REROUTE (simulate traffic shift)
   ▼                                │
RESOLVE                             ▼
(mark RESOLVED               ALERT (SNS → on-call)
 in DynamoDB)                       │
                                    ▼
                               RESOLVE
```

## Services Used

| Service | Role |
|---|---|
| Lambda | Detector + remediator logic |
| Step Functions | Retry → reroute → alert workflow |
| DynamoDB | Incident state + history |
| EventBridge | Scheduled trigger (every 1 min) |
| SNS | On-call alerting |
| CDK | Infrastructure as code |

## Project Structure

```
netpulse/
├── app.py                      # CDK entry point
├── cdk.json                    # CDK config
├── requirements.txt
├── setup.sh                    # Bootstrap script
├── netpulse/
│   └── netpulse_stack.py       # All AWS resources defined here
├── lambda/
│   ├── detector/
│   │   └── handler.py          # Probes endpoints, triggers Step Functions
│   └── remediator/
│       └── handler.py          # RETRY / REROUTE / ALERT / RESOLVE logic
└── tests/
    └── test_detector.py        # Unit tests (moto — no AWS account needed)
```

## Setup

```bash
# 1. Bootstrap your environment
bash setup.sh

# 2. Activate virtualenv
source .venv/bin/activate

# 3. Configure AWS credentials
aws configure

# 4. Bootstrap CDK (one-time per account/region)
cdk bootstrap aws://YOUR_ACCOUNT_ID/us-east-1

# 5. Deploy
cdk deploy
```

## Configuration

Edit the `ENDPOINTS` environment variable in `netpulse_stack.py` to point
at your own endpoints:

```python
"ENDPOINTS": "https://your-api.com/health,https://your-other-service.com/ping",
```

To receive email alerts, uncomment and configure the SNS subscription in
`netpulse_stack.py`:

```python
alert_topic.add_subscription(
    subscriptions.EmailSubscription("your@email.com")
)
```

## Running Tests

```bash
pytest tests/ -v
```

Tests use [moto](https://github.com/getmoto/moto) to mock AWS — no real
AWS account or credentials required.

## Key Design Decisions

**Why Step Functions over a single Lambda?**
Each remediation step (retry, reroute, alert) is a discrete, auditable state.
Step Functions gives us a visual execution history, built-in retry logic,
and a clear separation of concerns — exactly what you'd want in production
where you need to debug *which* step failed and *why*.

**Why DynamoDB for incidents?**
Incident records are write-once, read-many, with access patterns by
`incident_id` (exact lookup) and `endpoint` (range scan via GSI).
DynamoDB's single-digit millisecond latency and pay-per-request billing
make it a natural fit for an event-driven system with spiky write patterns.

**Why EventBridge over a cron Lambda?**
EventBridge decouples the schedule from the Lambda — you can pause,
modify, or swap the target without touching the detector code.
It also integrates natively with CloudWatch metrics for schedule monitoring.

## Extending This Project

- Add CloudWatch custom metrics + anomaly detection alarms
- Deploy probe agents as ECS Fargate tasks across multiple AZs
- Add a QuickSight dashboard for MTTR and incident frequency trends
- Implement real Route53/ALB rerouting in `handle_reroute()`
- Add a dead-letter queue (SQS) for failed Step Functions executions
