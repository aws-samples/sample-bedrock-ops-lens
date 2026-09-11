"""Stack teardown must not hang — measured against a real DELETE.

`./deploy.sh destroy` is in the README, so a teardown that takes an hour and ends
in DELETE_FAILED is a defect a first-time user hits on their way out.

Observed on a real teardown of BedrockOpsLens-intg in us-east-2:

    02:12:02.373  SchemaInit          DELETE_IN_PROGRESS
    02:12:03.667  S3GatewayEndpoint   DELETE_COMPLETE     <-- 1.3s later
    02:12:20.169  PrivateRouteTable   DELETE_COMPLETE
    02:12:41.165  NatGateway          DELETE_COMPLETE
    ...47 minutes of nothing...

The SchemaInit Lambda runs in PRIVATE subnets. To finish deleting, a custom
resource must PUT a response to a presigned S3 callback URL — but every route to
S3 had just been removed. Nothing appeared in CloudWatch either, because log
delivery needs the same egress. CloudFormation then waited out the default
one-hour custom-resource timeout and finished DELETE_FAILED, needing
`delete-stack --retain-resources SchemaInit` by hand.

An S3 gateway endpoint had already been added for exactly this reason, but it had
no ordering relationship with the custom resource, so it was torn down first
anyway. CloudFormation deletes a resource's dependencies AFTER the resource, so
the egress path has to be declared as a dependency to outlive it.

Run: .venv/bin/python -m pytest tests/test_infra_teardown.py -q
"""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "infra/cloudformation.yaml"


def _load():
    class L(yaml.SafeLoader):
        pass
    L.add_multi_constructor("!", lambda loader, suffix, node: None)
    return yaml.load(TEMPLATE.read_text(), Loader=L)


def _code_only(path: Path) -> str:
    """Source with comments and string literals stripped.

    These fixes document the bug they fix, so a raw substring scan matches the
    explanation and reports the bug as still present.
    """
    import io
    import tokenize
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(path.read_text()).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return " ".join(out)


@pytest.fixture(scope="module")
def tpl():
    return _load()


def test_schema_init_outlives_its_egress_path(tpl):
    """The dependency list is what keeps the callback route alive during DELETE."""
    dep = tpl["Resources"]["SchemaInit"].get("DependsOn") or []
    if isinstance(dep, str):
        dep = [dep]
    for needed in ("S3GatewayEndpoint", "DefaultPrivateRoute", "NatGateway",
                   # The associations are the load-bearing part: detaching the
                   # private subnets from PrivateRouteTable drops them onto the
                   # main route table, which has neither the NAT route nor the
                   # endpoint route. A second measured teardown failed on exactly
                   # this, with the endpoint/route/NAT all still present.
                   "PrivateSubnet1Assoc", "PrivateSubnet2Assoc"):
        assert needed in dep, (
            f"SchemaInit must DependsOn {needed}, or CloudFormation may delete it "
            f"first and strand the custom-resource callback")


def test_schema_init_bounds_a_lost_callback_to_minutes_not_an_hour(tpl):
    """Belt and braces: if the response is ever lost again, fail fast."""
    st = tpl["Resources"]["SchemaInit"]["Properties"].get("ServiceTimeout")
    assert st is not None, (
        "ServiceTimeout was documented in a comment but never set, which is why "
        "the hang cost 60 minutes instead of 5")
    assert 60 <= int(st) <= 3600


def test_the_callback_fails_fast_instead_of_stalling(tpl):
    """`timeout=20` measured 122.9s to give up, because urllib applies the
    timeout per resolved address. One doomed attempt consumed the whole Lambda
    budget and CloudFormation learned nothing."""
    code = _code_only(ROOT / "backend/app/schema_init.py")
    assert "timeout = 20" not in code, "per-address retries made 20s cost 123s"
    assert "timeout = 8" in code
    src = (ROOT / "backend/app/schema_init.py").read_text()
    assert "attempt" in src, "a lost callback must be retried and logged"


def test_the_s3_gateway_endpoint_still_serves_the_private_route_table(tpl):
    """A gateway endpoint attached to the private route table is what makes the
    callback reachable without NAT. Losing that attachment silently reintroduces
    the hang."""
    ep = tpl["Resources"]["S3GatewayEndpoint"]
    assert ep["Type"] == "AWS::EC2::VPCEndpoint"
    assert ep["Properties"]["VpcEndpointType"] == "Gateway", (
        "an Interface endpoint would add ENIs, which slow teardown instead")


def test_every_depends_on_target_exists(tpl):
    """A typo in DependsOn is accepted by YAML and rejected only at deploy time."""
    names = set(tpl["Resources"])
    bad = []
    for name, res in tpl["Resources"].items():
        dep = res.get("DependsOn") or []
        if isinstance(dep, str):
            dep = [dep]
        bad += [(name, d) for d in dep if d not in names]
    assert not bad, f"DependsOn references unknown resources: {bad}"


def test_the_delete_path_is_explained_where_someone_will_change_it(tpl):
    """This ordering looks removable to anyone tidying the template, so the
    reason has to sit next to it."""
    src = TEMPLATE.read_text()
    block = src.split("SchemaInit:")[1][:2500]
    assert "DELETE" in block and "callback" in block.lower()
