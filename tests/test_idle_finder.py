"""The free Idle Finder script published at costcapper.com/tools/: read-only, and its arithmetic."""

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

SCRIPT = Path(__file__).parent.parent / "costcapper-idle-finder.py"


def load():
    spec = importlib.util.spec_from_file_location("idle_finder", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakePricing:
    """Price List answers in the shape AWS returns: a JSON string per product."""

    PRICES = {("AmazonEC2", "t3.large"): 0.0832, ("AmazonRDS", "db.t3.medium"): 0.072}

    def get_products(self, ServiceCode, Filters, MaxResults):  # noqa: N803 - boto3 argument names
        size = next(f["Value"] for f in Filters if f["Field"] == "instanceType")
        price = self.PRICES.get((ServiceCode, size))
        if price is None:
            return {"PriceList": []}
        dims = {"a": {"unit": "Hrs", "description": f"${price} per On Demand Linux {size} Instance Hour",
                      "pricePerUnit": {"USD": str(price)}}}
        if ServiceCode == "AmazonRDS":
            dims["b"] = {"unit": "Hrs", "description": "unused capacity reservation hour",
                         "pricePerUnit": {"USD": str(price)}}
        return {"PriceList": [json.dumps({"terms": {"OnDemand": {"t": {"priceDimensions": dims}}}})]}


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        yield


def put_cpu(ident, namespace, dimension, busy_hours, hours=48):
    cw = boto3.client("cloudwatch")
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    for h in range(1, hours + 1):
        cw.put_metric_data(Namespace=namespace, MetricData=[{
            "MetricName": "CPUUtilization", "Dimensions": [{"Name": dimension, "Value": ident}],
            "Timestamp": now - timedelta(hours=h) + timedelta(minutes=5),
            "Value": 60.0 if h <= busy_hours else 1.0, "Unit": "Percent"}])


def test_estimates_saving_from_idle_hours(aws):
    finder = load()
    ec2 = boto3.client("ec2")
    ami = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    tags = [{"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": "dev-box"}]}]
    iid = ec2.run_instances(ImageId=ami, InstanceType="t3.large", MinCount=1, MaxCount=1,
                            TagSpecifications=tags)["Instances"][0]["InstanceId"]
    boto3.client("rds").create_db_instance(
        DBInstanceIdentifier="dev-db", DBInstanceClass="db.t3.medium", Engine="postgres",
        MasterUsername="u", MasterUserPassword="password123", AllocatedStorage=20)  # noqa: S106 - moto
    put_cpu(iid, "AWS/EC2", "InstanceId", busy_hours=12)          # 12 of 48 hours busy
    put_cpu("dev-db", "AWS/RDS", "DBInstanceIdentifier", busy_hours=24)  # 24 of 48 busy

    rows = finder.scan_region(boto3.Session(), "us-east-1", FakePricing(), days=2, threshold=5.0)
    by_id = {r["id"]: r for r in rows}

    box = by_id[iid]
    assert box["name"] == "dev-box"
    assert box["price_per_hour"] == pytest.approx(0.0832)
    assert box["monthly_24x7"] == pytest.approx(0.0832 * 730)
    assert box["idle_share"] == pytest.approx(36 / 48, abs=0.03)
    assert box["monthly_saving"] == pytest.approx(0.0832 * 730 * box["idle_share"])

    db = by_id["dev-db"]
    assert db["price_per_hour"] == pytest.approx(0.072)  # the reservation line at the same rate is ignored
    assert db["idle_share"] == pytest.approx(0.5, abs=0.03)


def test_unknown_price_and_missing_data_are_reported_not_guessed(aws):
    finder = load()
    ec2 = boto3.client("ec2")
    ami = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    ec2.run_instances(ImageId=ami, InstanceType="m5.large", MinCount=1, MaxCount=1)
    [row] = finder.scan_region(boto3.Session(), "us-east-1", FakePricing(), days=2, threshold=5.0)
    assert row["price_per_hour"] is None and row["monthly_saving"] is None
    assert "no CPU data" in row["note"]


def test_it_only_reads(aws):
    """Every AWS call the script makes is a describe, get or list."""
    source = SCRIPT.read_text()
    import re
    calls = set(re.findall(r"\.(\w+)\(", source))
    verbs = r"(start|stop|terminate|create|delete|modify|put|tag|untag|reboot)_"
    writes = {c for c in calls if re.match(verbs, c)}
    assert not writes, writes
