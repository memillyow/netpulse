"""
Tests for the NetPulse detector Lambda.
Uses moto to mock AWS services — no real AWS account needed.
"""
import json
import os
import pytest
from unittest.mock import patch, MagicMock
from moto import mock_aws
import boto3

# Set env vars before importing handler
os.environ["INCIDENTS_TABLE"] = "netpulse-incidents"
os.environ["STATE_MACHINE_ARN"] = "arn:aws:states:us-east-1:123456789:stateMachine:netpulse"
os.environ["ENDPOINTS"] = "https://httpbin.org/status/200"
os.environ["LATENCY_THRESHOLD_MS"] = "2000"


@pytest.fixture
def aws_credentials():
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["AWS_REGION"] = "us-east-1"


@pytest.fixture
def dynamodb_table(aws_credentials):
    with mock_aws():
        client = boto3.resource("dynamodb", region_name="us-east-1")
        table = client.create_table(
            TableName="netpulse-incidents",
            KeySchema=[
                {"AttributeName": "incident_id", "KeyType": "HASH"},
                {"AttributeName": "timestamp", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "incident_id", "AttributeType": "S"},
                {"AttributeName": "timestamp", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield table


class TestProbeEndpoint:
    def test_healthy_endpoint_returns_healthy(self):
        from lambda_detector.handler import probe_endpoint

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = probe_endpoint("https://example.com")

        assert result["healthy"] is True
        assert result["status_code"] == 200
        assert result["error"] is None

    def test_5xx_response_is_unhealthy(self):
        import urllib.error
        from lambda_detector.handler import probe_endpoint

        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.HTTPError(
                       url="https://example.com",
                       code=500,
                       msg="Internal Server Error",
                       hdrs=None,
                       fp=None,
                   )):
            result = probe_endpoint("https://example.com")

        assert result["healthy"] is False
        assert result["status_code"] == 500
        assert "500" in result["degradation_reason"]

    def test_connection_failure_is_unhealthy(self):
        from lambda_detector.handler import probe_endpoint

        with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
            result = probe_endpoint("https://example.com")

        assert result["healthy"] is False
        assert result["error"] is not None

    def test_high_latency_is_unhealthy(self):
        """Endpoint responds 200 but is too slow."""
        import time
        from lambda_detector.handler import probe_endpoint

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        def slow_urlopen(*args, **kwargs):
            time.sleep(0.01)  # tiny sleep, but we mock monotonic to fake latency
            return mock_response

        with patch("urllib.request.urlopen", side_effect=slow_urlopen):
            with patch("time.monotonic", side_effect=[0.0, 3.0]):  # 3000ms
                result = probe_endpoint("https://example.com")

        assert result["healthy"] is False
        assert "latency" in result["degradation_reason"].lower()


class TestWriteIncident:
    @mock_aws
    def test_incident_written_to_dynamodb(self, aws_credentials):
        # Setup table
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="netpulse-incidents",
            KeySchema=[
                {"AttributeName": "incident_id", "KeyType": "HASH"},
                {"AttributeName": "timestamp", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "incident_id", "AttributeType": "S"},
                {"AttributeName": "timestamp", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        from lambda_detector import handler as detector
        detector.table = ddb.Table("netpulse-incidents")

        probe_result = {
            "url": "https://example.com",
            "status_code": 500,
            "latency_ms": 150,
            "healthy": False,
            "error": "HTTP error: 500",
            "degradation_reason": "HTTP error: 500 Internal Server Error",
        }

        incident_id = detector.write_incident(probe_result)

        assert incident_id is not None
        assert len(incident_id) == 36  # UUID format

        # Verify it's in DynamoDB
        response = ddb.Table("netpulse-incidents").query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key("incident_id").eq(incident_id)
        )
        assert len(response["Items"]) == 1
        item = response["Items"][0]
        assert item["endpoint"] == "https://example.com"
        assert item["status"] == "OPEN"
        assert item["status_code"] == 500
