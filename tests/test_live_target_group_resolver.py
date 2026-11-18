from collections.abc import Iterator
from types import SimpleNamespace

import pytest

import live_target_group_resolver as resolver

SERVICE_ARN = "arn:aws:ecs:us-west-2:123456789012:service/default/app"
ENDPOINT_HOST = "app.example.com"
LISTENER_ARN = "arn:aws:elasticloadbalancing:us-west-2:123456789012:listener/app/x/y"
LIVE_ARN = "arn:aws:elasticloadbalancing:us-west-2:123456789012:targetgroup/live/x"
RETIRED_ARN = (
    "arn:aws:elasticloadbalancing:us-west-2:123456789012:targetgroup/retired/y"
)
PAIR_ARNS = f"{LIVE_ARN},{RETIRED_ARN}"
LOAD_BALANCER_ARN = (
    "arn:aws:elasticloadbalancing:us-west-2:123456789012:loadbalancer/app/x"
)


def host_rule(
    *, target_groups: list[dict[str, object]], host: str = ENDPOINT_HOST
) -> dict[str, object]:
    """The managed listener rule's shape: host-header condition + forward action."""
    return {
        "Conditions": [
            {"Field": "host-header", "HostHeaderConfig": {"Values": [host]}}
        ],
        "Actions": [
            {"Type": "forward", "ForwardConfig": {"TargetGroups": target_groups}}
        ],
    }


def test_cluster_and_service_parses_the_service_arn() -> None:
    assert resolver.cluster_and_service(service_arn=SERVICE_ARN) == (
        "default",
        "app",
    )


def test_deployment_settled_requires_one_completed_deployment() -> None:
    canary: list[dict[str, str]] = [
        {"rolloutState": "IN_PROGRESS"},
        {"rolloutState": "COMPLETED"},
    ]
    assert resolver.deployment_settled(deployments=canary) is False
    in_progress = resolver.deployment_settled(
        deployments=[{"rolloutState": "IN_PROGRESS"}]
    )
    assert in_progress is False
    settled = resolver.deployment_settled(deployments=[{"rolloutState": "COMPLETED"}])
    assert settled is True


def test_pick_prefers_the_weight_100_group_of_the_matching_host_rule() -> None:
    rule = host_rule(
        target_groups=[
            {"TargetGroupArn": RETIRED_ARN, "Weight": 0},
            {"TargetGroupArn": LIVE_ARN, "Weight": 100},
        ]
    )
    picked = resolver.pick_live_target_group(rules=[rule], service_host=ENDPOINT_HOST)
    assert picked == LIVE_ARN


def test_pick_falls_back_to_a_plain_forward_action() -> None:
    rule: dict[str, object] = {
        "Conditions": [
            {"Field": "host-header", "HostHeaderConfig": {"Values": [ENDPOINT_HOST]}}
        ],
        "Actions": [{"Type": "forward", "TargetGroupArn": LIVE_ARN}],
    }
    picked = resolver.pick_live_target_group(rules=[rule], service_host=ENDPOINT_HOST)
    assert picked == LIVE_ARN


def test_pick_ignores_other_hosts_and_raises_when_absent() -> None:
    other_host = host_rule(
        target_groups=[{"TargetGroupArn": LIVE_ARN, "Weight": 100}],
        host="other.example.com",
    )
    with pytest.raises(RuntimeError, match="host rule was not found"):
        resolver.pick_live_target_group(rules=[other_host], service_host=ENDPOINT_HOST)


@pytest.mark.parametrize("weights", [(100, 100), (95, 5)])
def test_pick_raises_without_a_single_weight_100_group(
    weights: tuple[int, int],
) -> None:
    rule = host_rule(
        target_groups=[
            {"TargetGroupArn": RETIRED_ARN, "Weight": weights[0]},
            {"TargetGroupArn": LIVE_ARN, "Weight": weights[1]},
        ]
    )
    with pytest.raises(RuntimeError, match="no single weight-100"):
        resolver.pick_live_target_group(rules=[rule], service_host=ENDPOINT_HOST)


def test_all_rules_walks_the_marker_pages() -> None:
    filler = host_rule(
        target_groups=[{"TargetGroupArn": RETIRED_ARN, "Weight": 100}],
        host="other.example.com",
    )
    pages: list[dict[str, object]] = [
        {"Rules": [filler] * 20, "NextMarker": "page2"},
        {
            "Rules": [
                host_rule(target_groups=[{"TargetGroupArn": LIVE_ARN, "Weight": 100}])
            ]
        },
    ]
    rules = resolver.all_rules(elbv2=FakeElb(pages=pages), listener_arn=LISTENER_ARN)
    assert len(rules) == 21
    assert rules[-1] == host_rule(
        target_groups=[{"TargetGroupArn": LIVE_ARN, "Weight": 100}]
    )


def test_harden_load_balancer_sets_the_attribute_on_the_listener_lb() -> None:
    elb = FakeElb()
    resolver.harden_load_balancer(elbv2=elb, listener_arn=LISTENER_ARN)
    assert elb.listener_arns == [LISTENER_ARN]
    assert elb.modified == (
        LOAD_BALANCER_ARN,
        [{"Key": resolver.DROP_INVALID_HEADER_ATTRIBUTE, "Value": "true"}],
    )


def test_handler_hardens_the_load_balancer_on_every_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settled: list[dict[str, object]] = [{"rolloutState": "COMPLETED"}]
    elb = FakeElb()
    sent, _ = run_handler(monkeypatch, ecs=FakeEcs(iter([settled])), elb=elb)
    assert sent == [("SUCCESS", {"LiveTargetGroupArn": LIVE_ARN}, None)]
    assert elb.modified[0] == LOAD_BALANCER_ARN


def test_handler_fails_the_deployment_when_hardening_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingElb(FakeElb):
        def modify_load_balancer_attributes(
            self, *, LoadBalancerArn: str, Attributes: list[dict[str, str]]
        ) -> dict[str, object]:
            raise RuntimeError("modify denied")

    settled: list[dict[str, object]] = [{"rolloutState": "COMPLETED"}]
    sent, _ = run_handler(monkeypatch, ecs=FakeEcs(iter([settled])), elb=FailingElb())
    assert sent == [("FAILED", {}, None)]


class FakeEcs:
    """DescribeServices answers each queued deployments shape once per poll."""

    def __init__(self, responses: Iterator[list[dict[str, object]]]) -> None:
        self.responses = responses

    def describe_services(
        self, *, cluster: str, services: list[str]
    ) -> dict[str, object]:
        del cluster, services
        return {"services": [{"deployments": next(self.responses)}]}


class FakeElb:
    """Serves one queued page per describe_rules call - order models pagination."""

    def __init__(self, pages: list[dict[str, object]] | None = None) -> None:
        self.pages = pages or [
            {
                "Rules": [
                    host_rule(
                        target_groups=[{"TargetGroupArn": LIVE_ARN, "Weight": 100}]
                    )
                ]
            }
        ]

    def describe_rules(
        self, *, ListenerArn: str, Marker: str | None = None
    ) -> dict[str, object]:
        del ListenerArn, Marker
        return self.pages.pop(0)

    def describe_listeners(self, *, ListenerArns: list[str]) -> dict[str, object]:
        self.listener_arns = ListenerArns
        return {"Listeners": [{"LoadBalancerArn": LOAD_BALANCER_ARN}]}

    def modify_load_balancer_attributes(
        self, *, LoadBalancerArn: str, Attributes: list[dict[str, str]]
    ) -> dict[str, object]:
        self.modified = (LoadBalancerArn, Attributes)
        return {}


class FakeBoto3:
    def __init__(self, *, ecs: object, elb: object) -> None:
        self._clients = {"ecs": ecs, "elbv2": elb}

    def client(self, service: str) -> object:
        return self._clients[service]


class FakeTime:
    """time.time advances only inside sleep, so the deadline check stays honest."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def event(
    *, request_type: str = "Create", physical_resource_id: str | None = None
) -> dict[str, object]:
    resource_event: dict[str, object] = {
        "RequestType": request_type,
        "ResourceProperties": {
            "ServiceArn": SERVICE_ARN,
            "ListenerArn": LISTENER_ARN,
            "ServiceHost": ENDPOINT_HOST,
            "TargetGroupArns": PAIR_ARNS,
        },
    }
    if physical_resource_id is not None:
        resource_event["PhysicalResourceId"] = physical_resource_id
    return resource_event


def run_handler(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ecs: object,
    elb: FakeElb | None = None,
    request_type: str = "Create",
    physical_resource_id: str | None = None,
) -> tuple[list[tuple[str, dict[str, object], str | None]], FakeTime]:
    fake_time = FakeTime()
    monkeypatch.setattr(
        resolver, "boto3", FakeBoto3(ecs=ecs, elb=elb or FakeElb()), raising=False
    )
    monkeypatch.setattr(resolver, "time", fake_time, raising=False)
    sent: list[tuple[str, dict[str, object], str | None]] = []
    monkeypatch.setattr(
        resolver,
        "cfnresponse",
        SimpleNamespace(
            SUCCESS="SUCCESS",
            FAILED="FAILED",
            send=lambda *args, **kw: sent.append(
                (args[2], args[3], kw.get("physicalResourceId"))
            ),
        ),
        raising=False,
    )
    resolver.handler(
        event(request_type=request_type, physical_resource_id=physical_resource_id),
        context=SimpleNamespace(),
    )
    return sent, fake_time


def test_handler_polls_until_settled_then_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsettled: list[dict[str, object]] = [{"rolloutState": "IN_PROGRESS"}]
    settled: list[dict[str, object]] = [{"rolloutState": "COMPLETED"}]
    sent, fake_time = run_handler(monkeypatch, ecs=FakeEcs(iter([unsettled, settled])))
    assert sent == [("SUCCESS", {"LiveTargetGroupArn": LIVE_ARN}, None)]
    assert fake_time.sleeps


def test_handler_reanswers_on_update(monkeypatch: pytest.MonkeyPatch) -> None:
    settled: list[dict[str, object]] = [{"rolloutState": "COMPLETED"}]
    sent, _ = run_handler(
        monkeypatch,
        ecs=FakeEcs(iter([settled])),
        request_type="Update",
        physical_resource_id="resolver-1",
    )
    assert sent == [("SUCCESS", {"LiveTargetGroupArn": LIVE_ARN}, "resolver-1")]


def test_handler_answers_delete_without_resolving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent, _ = run_handler(monkeypatch, ecs=FakeEcs(iter([])), request_type="Delete")
    assert sent == [("SUCCESS", {}, None)]


def test_handler_reports_failure_when_the_deployment_never_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    polls: list[int] = []

    class NeverSettlingEcs:
        def describe_services(
            self, *, cluster: str, services: list[str]
        ) -> dict[str, object]:
            del cluster, services
            polls.append(1)
            return {"services": [{"deployments": [{"rolloutState": "IN_PROGRESS"}]}]}

    sent, _ = run_handler(monkeypatch, ecs=NeverSettlingEcs())
    assert sent == [("FAILED", {}, None)]
    assert len(polls) > 1, "the handler must retry until the deadline, not once"


def test_live_arn_must_belong_to_the_resource_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settled: list[dict[str, object]] = [{"rolloutState": "COMPLETED"}]
    monkeypatch.setattr(
        resolver,
        "boto3",
        FakeBoto3(ecs=FakeEcs(iter([settled])), elb=FakeElb()),
        raising=False,
    )
    monkeypatch.setattr(resolver, "time", FakeTime(), raising=False)
    props: dict[str, object] = {
        "ServiceArn": SERVICE_ARN,
        "ListenerArn": LISTENER_ARN,
        "ServiceHost": ENDPOINT_HOST,
        "TargetGroupArns": (
            "arn:aws:elasticloadbalancing:us-west-2:123456789012:targetgroup/other/z"
        ),
    }
    with pytest.raises(RuntimeError, match="not in the service's pair"):
        resolver.live_target_group_arn(props=props)
