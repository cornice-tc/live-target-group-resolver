"""Resolve the live target group of the Express-managed blue/green listener.

CFN sees only the unordered blue/green pair; which is live settles mid-update,
so this waits for settle, then reads the managed rule's weight-100 group.
"""

import logging
import time

import boto3
import cfnresponse

logger = logging.getLogger(__name__)

# The Lambda timeout is 900s; stop early so the cfnresponse round trip still lands.
POLL_DEADLINE_SECONDS = 780
POLL_INTERVAL_SECONDS = 20
FULL_WEIGHT = 100
DROP_INVALID_HEADER_ATTRIBUTE = "routing.http.drop_invalid_header_fields.enabled"


def handler(event, context) -> None:
    """Custom-resource entry: Create/Update respond with the live target group."""
    try:
        if event["RequestType"] in ("Create", "Update"):
            data = {
                "LiveTargetGroupArn": live_target_group_arn(
                    props=event["ResourceProperties"]
                )
            }
        else:
            data = {}
        cfnresponse.send(
            event,
            context,
            cfnresponse.SUCCESS,
            data,
            physicalResourceId=event.get("PhysicalResourceId"),
        )
    except Exception as exc:
        logger.exception("resolve failed")
        cfnresponse.send(
            event,
            context,
            cfnresponse.FAILED,
            {},
            reason=str(exc),
            physicalResourceId=event.get("PhysicalResourceId"),
        )


def live_target_group_arn(*, props):
    """The managed rule's weight-100 target group, read once the service's
    deployment has settled so the weights are the settled ones."""
    cluster, service = cluster_and_service(service_arn=props["ServiceArn"])
    wait_until_settled(ecs=boto3.client("ecs"), cluster=cluster, service=service)
    elbv2 = boto3.client("elbv2")
    rules = all_rules(elbv2=elbv2, listener_arn=props["ListenerArn"])
    arn = pick_live_target_group(rules=rules, service_host=props["ServiceHost"])
    if arn not in set(props["TargetGroupArns"].split(",")):
        raise RuntimeError("the live target group is not in the service's pair")
    harden_load_balancer(elbv2=elbv2, listener_arn=props["ListenerArn"])
    return arn


def harden_load_balancer(*, elbv2, listener_arn: str) -> None:
    """Set the ALB attribute the Express API cannot. Runs on every re-resolution,
    and a failure fails the deployment - hardening that silently skips is drift."""
    listener = elbv2.describe_listeners(ListenerArns=[listener_arn])["Listeners"][0]
    elbv2.modify_load_balancer_attributes(
        LoadBalancerArn=listener["LoadBalancerArn"],
        Attributes=[{"Key": DROP_INVALID_HEADER_ATTRIBUTE, "Value": "true"}],
    )


def cluster_and_service(*, service_arn: str) -> tuple[str, str]:
    """(cluster, service) out of arn:aws:ecs:region:account:service/cluster/name."""
    _service_word, cluster, service = service_arn.split(":", 5)[-1].split("/", 2)
    return cluster, service


def all_rules(*, elbv2, listener_arn: str) -> list:
    """DescribeRules pages at 20 - walk NextMarker until the listener is drained."""
    rules = []
    marker = None
    while True:
        kwargs: dict[str, str] = {"ListenerArn": listener_arn}
        if marker:
            kwargs["Marker"] = marker
        page = elbv2.describe_rules(**kwargs)
        rules.extend(page["Rules"])
        marker = page.get("NextMarker")
        if not marker:
            return rules


def wait_until_settled(*, ecs, cluster: str, service: str) -> None:
    """Poll until exactly one deployment remains and its rollout COMPLETED."""
    deadline = time.time() + POLL_DEADLINE_SECONDS
    while True:
        deployments = ecs.describe_services(cluster=cluster, services=[service])[
            "services"
        ][0]["deployments"]
        if deployment_settled(deployments=deployments):
            return
        if time.time() > deadline:
            raise RuntimeError("the service deployment did not settle in time")
        time.sleep(POLL_INTERVAL_SECONDS)


def deployment_settled(deployments) -> bool:
    """Settled = the canary's shadow deployment is gone and the rollout COMPLETED."""
    return len(deployments) == 1 and deployments[0].get("rolloutState") == "COMPLETED"


def pick_live_target_group(*, rules, service_host: str) -> str:
    """The weight-100 target group of the managed rule serving service_host - a
    tie or a sub-100 winner means the weights were read mid-flip, so raise
    rather than bind a possibly-retired group."""
    for rule in rules:
        for condition in rule.get("Conditions", []):
            hosts = (condition.get("HostHeaderConfig") or {}).get("Values", [])
            if condition.get("Field") != "host-header" or service_host not in hosts:
                continue
            for action in rule["Actions"]:
                if action["Type"] != "forward":
                    continue
                groups = (action.get("ForwardConfig") or {}).get("TargetGroups") or [
                    {"TargetGroupArn": action.get("TargetGroupArn")}
                ]
                top = max(group.get("Weight", FULL_WEIGHT) for group in groups)
                winners = [
                    group for group in groups if group.get("Weight", FULL_WEIGHT) == top
                ]
                if top != FULL_WEIGHT or len(winners) != 1:
                    raise RuntimeError(
                        "the managed host rule has no single weight-100 target group"
                    )
                live_arn: str = winners[0]["TargetGroupArn"]
                return live_arn
    raise RuntimeError("the managed host rule was not found on the listener")
