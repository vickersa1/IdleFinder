# Idle Finder

Find the EC2 instances and RDS databases that sat idle over the last two weeks, and what those idle hours cost.

One Python file. Read-only. MIT licensed.

```
pip install boto3
python3 costcapper-idle-finder.py --all-regions --csv idle.csv
```

## What it does

1. Lists running EC2 instances and available RDS databases.
2. Reads 14 days of hourly `CPUUtilization` (maximum) from CloudWatch.
3. Counts the hours under 5% CPU and prices them with on-demand rates from the AWS Price List API.
4. Prints a table (and optionally a CSV) with idle share and monthly cost of the idle hours.

It never starts, stops, tags or changes anything, and sends nothing anywhere.

## Permissions

`ReadOnlyAccess` works. The minimum is:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "ec2:DescribeInstances",
      "ec2:DescribeRegions",
      "rds:DescribeDBInstances",
      "cloudwatch:GetMetricData",
      "pricing:GetProducts"
    ],
    "Resource": "*"
  }]
}
```

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `--regions r1 r2` | current region | Regions to scan |
| `--all-regions` | off | Every region enabled for the account |
| `--days N` | 14 | Days of CPU history |
| `--threshold P` | 5 | CPU % below which an hour counts as idle |
| `--csv FILE` | none | Also write results to CSV |
| `--profile NAME` | default | AWS profile |

## Limits

- CPU is a proxy for "idle". A machine waiting on network or disk can look idle.
- Prices are on-demand Linux. Reserved Instances and Savings Plans are not netted out.
- Spot instances, Auto Scaling group members, Aurora, read replicas and Windows are flagged, not priced.
- Storage keeps billing while a machine is stopped, so real savings are a little lower.

## Tests

```
pip install -r requirements-dev.txt
pytest -q
```

## Switching them off

Idle Finder only measures. To switch idle dev and test machines off on a schedule, with a button for your team to turn them back on, see [CostCapper](https://costcapper.com/?ref=github), which runs in your own AWS account.

Questions or fixes: open an issue, or email hello@costcapper.com.
