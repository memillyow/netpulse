from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    aws_dynamodb as dynamodb,
    aws_lambda as _lambda,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as tasks,
    aws_events as events,
    aws_events_targets as targets,
    aws_sns as sns,
    aws_sns_subscriptions as subscriptions,
    aws_iam as iam,
    aws_logs as logs,
    CfnOutput,
)
from constructs import Construct
import os


class NetPulseStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ------------------------------------------------------------------ #
        # DynamoDB — incidents table                                           #
        # ------------------------------------------------------------------ #
        incidents_table = dynamodb.Table(
            self,
            "IncidentsTable",
            table_name="netpulse-incidents",
            partition_key=dynamodb.Attribute(
                name="incident_id",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="timestamp",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,  # swap to RETAIN before prod
            point_in_time_recovery=True,
        )

        # GSI — query all incidents for a given endpoint
        incidents_table.add_global_secondary_index(
            index_name="endpoint-index",
            partition_key=dynamodb.Attribute(
                name="endpoint",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="timestamp",
                type=dynamodb.AttributeType.STRING,
            ),
        )

        # ------------------------------------------------------------------ #
        # SNS — alert topic                                                    #
        # ------------------------------------------------------------------ #
        alert_topic = sns.Topic(
            self,
            "AlertTopic",
            topic_name="netpulse-alerts",
            display_name="NetPulse Incident Alerts",
        )

        # Uncomment and set your email to receive alerts:
        # alert_topic.add_subscription(
        #     subscriptions.EmailSubscription("your@email.com")
        # )

        # ------------------------------------------------------------------ #
        # Lambda — remediator (called by Step Functions)                       #
        # ------------------------------------------------------------------ #
        remediator_fn = _lambda.Function(
            self,
            "RemediatorFunction",
            function_name="netpulse-remediator",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset("lambda/remediator"),
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={
                "INCIDENTS_TABLE": incidents_table.table_name,
                "ALERT_TOPIC_ARN": alert_topic.topic_arn,
            },
            log_retention=logs.RetentionDays.ONE_WEEK,
        )

        incidents_table.grant_read_write_data(remediator_fn)
        alert_topic.grant_publish(remediator_fn)

        # ------------------------------------------------------------------ #
        # Step Functions — remediation state machine                          #
        # ------------------------------------------------------------------ #

        # Task: attempt auto-retry
        retry_task = tasks.LambdaInvoke(
            self,
            "RetryEndpoint",
            lambda_function=remediator_fn,
            payload=sfn.TaskInput.from_object({
                "action": "RETRY",
                "incident.$": "$",
            }),
            result_path="$.retry_result",
        )

        # Task: reroute traffic away from degraded node
        reroute_task = tasks.LambdaInvoke(
            self,
            "RerouteTraffic",
            lambda_function=remediator_fn,
            payload=sfn.TaskInput.from_object({
                "action": "REROUTE",
                "incident.$": "$",
            }),
            result_path="$.reroute_result",
        )

        # Task: send alert (retry + reroute both failed or reroute succeeded)
        alert_task = tasks.LambdaInvoke(
            self,
            "SendAlert",
            lambda_function=remediator_fn,
            payload=sfn.TaskInput.from_object({
                "action": "ALERT",
                "incident.$": "$",
            }),
            result_path="$.alert_result",
        )

        # Task: mark incident resolved
        resolve_task = tasks.LambdaInvoke(
            self,
            "ResolveIncident",
            lambda_function=remediator_fn,
            payload=sfn.TaskInput.from_object({
                "action": "RESOLVE",
                "incident.$": "$",
            }),
        )

        # Choice: did retry succeed?
        retry_succeeded = sfn.Choice(self, "DidRetrySucceed?")

        # State machine definition:
        # retry → if success → resolve
        #               → if fail  → reroute → alert → resolve
        definition = (
            retry_task.next(
                retry_succeeded
                .when(
                    sfn.Condition.string_equals("$.retry_result.Payload.status", "RECOVERED"),
                    resolve_task,
                )
                .otherwise(
                    reroute_task.next(alert_task).next(resolve_task)
                )
            )
        )

        state_machine = sfn.StateMachine(
            self,
            "RemediationStateMachine",
            state_machine_name="netpulse-remediation",
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            timeout=Duration.minutes(5),
            logs=sfn.LogOptions(
                destination=logs.LogGroup(
                    self,
                    "StateMachineLogs",
                    retention=logs.RetentionDays.ONE_WEEK,
                ),
                level=sfn.LogLevel.ALL,
            ),
        )

        # ------------------------------------------------------------------ #
        # Lambda — detector (runs on a schedule, triggers state machine)      #
        # ------------------------------------------------------------------ #
        detector_fn = _lambda.Function(
            self,
            "DetectorFunction",
            function_name="netpulse-detector",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset("lambda/detector"),
            timeout=Duration.seconds(60),
            memory_size=256,
            environment={
                "INCIDENTS_TABLE": incidents_table.table_name,
                "STATE_MACHINE_ARN": state_machine.state_machine_arn,
                # Comma-separated list of endpoints to probe
                "ENDPOINTS": "https://httpbin.org/status/200,https://httpbin.org/status/500",
                "LATENCY_THRESHOLD_MS": "2000",
            },
            log_retention=logs.RetentionDays.ONE_WEEK,
        )

        incidents_table.grant_read_write_data(detector_fn)
        state_machine.grant_start_execution(detector_fn)

        # EventBridge decouples the schedule from the Lambda — easy to pause or swap target without touching detector code
        rule = events.Rule(
            self,
            "DetectorSchedule",
            schedule=events.Schedule.rate(Duration.minutes(1)),
            description="Triggers NetPulse detector to probe endpoints",
        )
        rule.add_target(targets.LambdaFunction(detector_fn))

        # ------------------------------------------------------------------ #
        # Outputs                                                              #
        # ------------------------------------------------------------------ #
        CfnOutput(self, "IncidentsTableName", value=incidents_table.table_name)
        CfnOutput(self, "StateMachineArn", value=state_machine.state_machine_arn)
        CfnOutput(self, "DetectorFunctionName", value=detector_fn.function_name)
        CfnOutput(self, "AlertTopicArn", value=alert_topic.topic_arn)
