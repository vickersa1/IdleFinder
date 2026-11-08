#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 CostCapper, LLC. Permission is granted to use, copy, modify and share this file
# under the MIT licence: https://opensource.org/license/mit
"""CostCapper Idle Finder: what your idle EC2 instances and RDS databases cost.

Read-only. It looks at running EC2 instances and available RDS databases, counts the hours in the
last 14 days when each was idle (hourly maximum CPU under a threshold), and estimates what switching
it off for those hours would save, using on-demand prices from the AWS Price List API.

It never starts, stops, tags or changes anything. Run it with credentials that have only:
  ec2:DescribeInstances, ec2:DescribeRegions, rds:DescribeDBInstances,
  cloudwatch:GetMetricData, pricing:GetProducts

Usage:
  python3 costcapper-idle-finder.py                    # the current region
  python3 costcapper-idle-finder.py --regions us-east-1 eu-west-1
  python3 costcapper-idle-finder.py --all-regions --csv idle.csv

Requires Python 3.9+ and boto3 (pip install boto3). https://costcapper.com/tools/idle-finder.html
"""

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta, timezone

import boto3

__version__ = "1.0.0"
HOURS_PER_MONTH = 730
RDS_ENGINES = {"postgres": "PostgreSQL", "mysql": "MySQL", "mariadb": "MariaDB"}


def ec2_price(pricing, region, instance_type):
    """On-demand Linux price per hour, or None if the Price List has no single match."""
    resp = pricing.get_products(ServiceCode="AmazonEC2", MaxResults=10, Filters=[
        {"Type": "TERM_MATCH", "Field": f, "Value": v} for f, v in [
            ("instanceType", instance_type), ("regionCode", region), ("operatingSystem", "Linux"),
            ("tenancy", "Shared"), ("preInstalledSw", "NA"), ("capacitystatus", "Used"),
            ("licenseModel", "No License required")]])
    return _hourly(resp)


def rds_price(pricing, region, instance_class, engine, multi_az):
    name = RDS_ENGINES.get(engine)
    if not name:
        return None
    resp = pricing.get_products(ServiceCode="AmazonRDS", MaxResults=10, Filters=[
        {"Type": "TERM_MATCH", "Field": f, "Value": v} for f, v in [
            ("instanceType", instance_class), ("regionCode", region), ("databaseEngine", name),
            ("deploymentOption", "Multi-AZ" if multi_az else "Single-AZ")]])
    return _hourly(resp)


def _hourly(resp):
    prices = set()
    for item in resp.get("PriceList", []):
        product = json.loads(item) if isinstance(item, str) else item
        for term in product.get("terms", {}).get("OnDemand", {}).values():
            for dim in term["priceDimensions"].values():
                # RDS lists "unused capacity reservation" hours at the same rate; skip those.
                if dim.get("unit") == "Hrs" and "reservation" not in dim.get("description", "").lower():
                    prices.add(float(dim["pricePerUnit"]["USD"]))
    prices.discard(0.0)
    return prices.pop() if len(prices) == 1 else None


def idle_hours(cloudwatch, namespace, dimension, ident, start, end, threshold):
    """(idle hours, hours with data) from the hourly maximum CPUUtilization."""
    resp = cloudwatch.get_metric_data(StartTime=start, EndTime=end, MetricDataQueries=[{
        "Id": "cpu", "ReturnData": True, "MetricStat": {
            "Metric": {"Namespace": namespace, "MetricName": "CPUUtilization",
                       "Dimensions": [{"Name": dimension, "Value": ident}]},
            "Period": 3600, "Stat": "Maximum"}}])
    values = resp["MetricDataResults"][0]["Values"] if resp["MetricDataResults"] else []
    return sum(1 for v in values if v < threshold), len(values)


def scan_region(session, region, pricing, days, threshold):
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)  # noqa: UP017 - Python 3.9+
    start = end - timedelta(days=days)
    ec2 = session.client("ec2", region_name=region)
    rds = session.client("rds", region_name=region)
    cw = session.client("cloudwatch", region_name=region)
    rows = []
    for page in ec2.get_paginator("describe_instances").paginate(
            Filters=[{"Name": "instance-state-name", "Values": ["running"]}]):
        for res in page["Reservations"]:
            for inst in res["Instances"]:
                tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                note = ""
                if inst.get("InstanceLifecycle") == "spot":
                    note = "Spot instance: stopping may not apply"
                elif "aws:autoscaling:groupName" in tags:
                    note = "Auto Scaling group member: scale the group instead"
                elif inst.get("PlatformDetails", "Linux/UNIX") != "Linux/UNIX":
                    note = f"{inst.get('PlatformDetails')}: Linux price shown"
                price = ec2_price(pricing, region, inst["InstanceType"])
                idle, seen = idle_hours(cw, "AWS/EC2", "InstanceId", inst["InstanceId"],
                                        start, end, threshold)
                rows.append(_row(region, "EC2", inst["InstanceId"], tags.get("Name", ""),
                                 inst["InstanceType"], price, idle, seen, note))
    for page in rds.get_paginator("describe_db_instances").paginate():
        for db in page["DBInstances"]:
            if db.get("DBInstanceStatus") != "available":
                continue
            note = ""
            if db.get("ReadReplicaSourceDBInstanceIdentifier"):
                note = "Read replica: AWS can't stop these"
            elif db["Engine"].startswith("aurora"):
                note = "Aurora: stop the cluster, not one instance"
            price = rds_price(pricing, region, db["DBInstanceClass"], db["Engine"],
                              db.get("MultiAZ", False))
            idle, seen = idle_hours(cw, "AWS/RDS", "DBInstanceIdentifier", db["DBInstanceIdentifier"],
                                    start, end, threshold)
            rows.append(_row(region, "RDS", db["DBInstanceIdentifier"], db["DBInstanceIdentifier"],
                             db["DBInstanceClass"], price, idle, seen, note))
    return rows


def _row(region, kind, ident, name, size, price, idle, seen, note):
    share = idle / seen if seen else 0.0
    monthly = price * HOURS_PER_MONTH if price is not None else None
    saving = monthly * share if monthly is not None else None
    if not seen:
        note = (note + "; " if note else "") + "no CPU data"
    return {"region": region, "type": kind, "id": ident, "name": name, "size": size,
            "price_per_hour": price, "monthly_24x7": monthly, "idle_share": share,
            "monthly_saving": saving, "note": note}


def money(x):
    return "?" if x is None else f"${x:,.0f}"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Estimate what idle EC2 instances and RDS databases cost. Read-only.")
    ap.add_argument("--regions", nargs="+", help="regions to scan (default: the current region)")
    ap.add_argument("--all-regions", action="store_true", help="scan every region enabled for the account")
    ap.add_argument("--days", type=int, default=14, help="days of CPU history to read (default 14)")
    ap.add_argument("--threshold", type=float, default=5.0,
                    help="an hour counts as idle when maximum CPU is below this percent (default 5)")
    ap.add_argument("--csv", metavar="FILE", help="also write the results to a CSV file")
    ap.add_argument("--profile", help="AWS profile to use")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)

    session = boto3.Session(profile_name=args.profile) if args.profile else boto3.Session()
    if args.all_regions:
        regions = [r["RegionName"] for r in session.client("ec2").describe_regions()["Regions"]]
    else:
        regions = args.regions or [session.region_name or "us-east-1"]
    pricing = session.client("pricing", region_name="us-east-1")

    rows = []
    for region in regions:
        print(f"Scanning {region}…", file=sys.stderr)
        rows += scan_region(session, region, pricing, args.days, args.threshold)
    rows.sort(key=lambda r: r["monthly_saving"] or 0, reverse=True)

    if not rows:
        print("No running EC2 instances or available RDS databases found.")
        return 0
    head = (f"{'Region':<14} {'Type':<4} {'Name or ID':<32} {'Size':<16} "
            f"{'24/7 a month':>12} {'Idle':>5} {'Could save':>11}  Note")
    print(head)
    print("-" * len(head))
    for r in rows:
        label = (r["name"] or r["id"])[:32]
        print(f"{r['region']:<14} {r['type']:<4} {label:<32} {r['size']:<16} {money(r['monthly_24x7']):>12} "
              f"{r['idle_share']:>5.0%} {money(r['monthly_saving']):>11}  {r['note']}")
    total = sum(r["monthly_saving"] or 0 for r in rows)
    print(f"\nPossible saving: about {money(total)} a month, "
          "from switching machines off in the hours they were idle.")
    print("Compute only, on-demand prices; storage is still charged while stopped. Savings Plans or Reserved")
    print("Instances lower the real figure. Turn machines off on a schedule, or let your team switch them on")
    print("when needed: https://costcapper.com/?ref=idle-finder")
    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"Wrote {args.csv}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
