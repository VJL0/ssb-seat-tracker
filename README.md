# ssb-seat-tracker

A small, read-only monitor for Temple University's public Ellucian Self-Service Banner (SSB)
enrollment endpoint. It watches one or more CRNs in the fixed Banner term `202636` and alerts only
when Temple's raw available-seat count transitions from zero or below to above zero.

The tracker does **not** log into TUportal, handle AccessNet credentials or Duo, register, drop, or
stage classes, or call authenticated student-record endpoints.

## Availability rule

The tracker uses Temple's provided count directly:

```text
open = Enrollment Seats Available > 0
```

It does not derive seats from capacity and enrollment, and negative values mean over-enrolled, not
open. The response does not include Banner's `openSection` flag or course metadata. Registration
can still fail because of prerequisites, holds, reserved seats, or other student-specific
restrictions. Request and HTML-schema failures are never recorded as a closed class.

## Setup

Install the locked dependencies with [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```

No Temple credentials are needed or accepted.

For local ntfy configuration, copy the example and replace its placeholder topic:

```bash
cp .env.example .env
set -a
source .env
set +a
```

The AWS values in the same example correspond to GitHub `production` environment variables; the
local tracker only requires `NTFY_TOPIC`.

## Usage

Check a list of CRNs once during development:

```bash
uv run ssb-seat-tracker \
  --crn 53150 53151 53152 \
  --once
```

Omit `--once` to watch at the responsible default interval of 60 seconds. Intervals below 60
seconds are rejected. A local `.ssb-seat-tracker-state.json` file suppresses duplicate alerts and
is ignored by Git. Use `--state-file PATH` to choose another location.

Without ntfy configuration, opening alerts are printed to the console.

### ntfy

Subscribe to a private topic in the ntfy app, then provide the same topic through the environment:

```bash
export NTFY_TOPIC="your-private-topic"
```

For local use, you can instead place only the topic name in `.ntfy-topic` at the project root. The
topic is effectively a password, is never logged, and both `.ntfy-topic` and `.env` are ignored by
Git. Opening alerts are published through ntfy with high priority and a rotating-light tag.

## What gets reported

Each successful check logs the CRN, Temple's available-seat count, and transition outcome. Opening
alerts include enrollment, capacity, available seats, and waitlist count. An alert is sent only
when:

- the previous successful observation had `seats_available <= 0`; and
- the current observation has `seats_available > 0`.

The first observation establishes state without alerting. Changes such as `1 → 2` do not repeat an
alert. A failed check leaves the last successful state untouched. Transient transport failures and
HTTP 408, 429, 500, 502, 503, and 504 responses receive at most two retries with short exponential
backoff; permanent and malformed responses are not retried.

## Development

The normal suite is fully mocked and does not contact Temple:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Focused tests cover the fixed-term HTML contract, CRN-list lookup, watch-cycle transitions and
failure safety, and ntfy delivery. Parameterized cases exercise equivalent failures and
non-notification transitions without duplicating test logic. HTTP behavior is isolated with
RESPX's strict pytest router, so the normal suite cannot accidentally reach Temple or ntfy.

One opt-in live contract test checks Temple's current response schema without asserting volatile
seat counts or CRNs:

```bash
SSB_LIVE_TEST=1 uv run pytest -m live
```

The application is intentionally limited to four substantive modules:

- `client.py` — Temple SSB HTTP/session behavior;
- `models.py` — external data mapping and availability rules;
- `notifier.py` — console and ntfy delivery; and
- `main.py` — CLI, state transitions, and polling orchestration.

The watch cycle depends on small structural ports for enrollment lookup, persistence, and
notification delivery. The concrete Banner, DynamoDB, local JSON, and ntfy adapters are assembled
at the application boundary.

## AWS deployment

The production path is a bounded Lambda invocation rather than a permanently running polling
process:

```text
EventBridge Scheduler (1 minute) -> Lambda -> Temple public SSB
                                      |  \-> ntfy
                                      \----> DynamoDB watch state
```

The SAM template uses Python 3.14 on ARM64, 256 MB of memory, a 30-second timeout, reserved
concurrency of one, no customer VPC, a 14-day log retention policy, and an error alarm. The
DynamoDB table uses on-demand billing and is retained if the stack is deleted. Scheduler retries
are disabled, so a failed watch remains eligible for the next one-minute scheduled invocation.

One invocation loads all enabled CRNs and posts them to Temple's enrollment-info endpoint with the
fixed term `202636`, using one shared HTTP connection pool and at most five concurrent requests.
State is written only after a successful check and, when needed, successful ntfy delivery. That
ordering gives at-least-once alerts: a rare DynamoDB failure after ntfy accepts a message can
produce a duplicate on retry, but a delivery failure cannot suppress the next alert.

The ntfy topic is not in CloudFormation, GitHub, logs, or the repository. Create it as a standard
SSM `SecureString` encrypted with the AWS-managed Parameter Store key:

```bash
aws ssm put-parameter \
  --name /ssb-seat-tracker/ntfy-topic \
  --type SecureString \
  --value "$NTFY_TOPIC"
```

### One-time GitHub OIDC bootstrap

Deploy the bootstrap with administrator credentials once. `GitHubSubjectClaim` must be the exact
claim issued for the protected `production` GitHub environment. GitHub repositories using the new
immutable-identity format must include the owner and repository IDs; do not substitute a wildcard.

```bash
aws cloudformation deploy \
  --template-file infrastructure/github-oidc.yaml \
  --stack-name ssb-seat-tracker-bootstrap \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    GitHubSubjectClaim='repo:OWNER/REPOSITORY:environment:production'
```

If the account already has GitHub's OIDC provider, also pass
`ExistingGitHubOidcProviderArn=arn:aws:iam::ACCOUNT:oidc-provider/token.actions.githubusercontent.com`.
The bootstrap creates a private, encrypted artifact bucket; a GitHub role restricted to the exact
OIDC audience and subject; and a separate CloudFormation execution role. This keeps the GitHub job
on short-lived credentials and prevents it from directly creating application resources.

Create a protected GitHub environment named `production`, restrict it to `main`, and set these
environment variables from the bootstrap outputs and your account:

- `AWS_ACCOUNT_ID`
- `AWS_REGION`
- `AWS_DEPLOY_ROLE_ARN`
- `AWS_CFN_EXECUTION_ROLE_ARN`
- `AWS_ARTIFACT_BUCKET`
- `NTFY_TOPIC_PARAMETER_NAME` (`/ssb-seat-tracker/ntfy-topic`)
- `ALERT_EMAIL`

The deploy workflow tests and validates the application before it requests AWS credentials. It
then builds the ARM64 package with the locked dependency graph and deploys through CloudFormation.
All third-party GitHub actions are pinned to immutable commit IDs. Confirm the SNS subscription
email after the first deployment.

### Manage production watches

After the application stack exists, manage its CRNs with the project CLI:

```bash
uv run ssb-seat-tracker watch add 53150
uv run ssb-seat-tracker watch list
uv run ssb-seat-tracker watch disable 53150
uv run ssb-seat-tracker watch enable 53150
uv run ssb-seat-tracker watch remove 53150
```

The CLI uses your current AWS credentials and region. It reads the table name from
`WATCHES_TABLE_NAME` when that environment variable is set; otherwise it discovers the
`WatchesTableName` output from the `ssb-seat-tracker` CloudFormation stack. For another deployment,
use `watch --stack-name STACK ...` or `watch --table-name TABLE ...`.

Adding a CRN uses a conditional write, so it will never overwrite an existing watch. Disabling a
watch preserves its last observation; enabling it resumes checks, and removing it deletes the
record. A new watch starts enabled, with Lambda adding `seats_available` and `updated_at` after its
first successful check.

### Production verification

The normal suite isolates application rules and AWS adapters with fresh fakes. This is deliberate:
AWS recommends using mocks for business and failure behavior while also testing deployed managed
service configuration in the cloud. CI runs:

```bash
uv lock --check
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest
sam validate --lint
sam build
```

After deployment, verify the stack itself rather than emulating Scheduler, IAM, or DynamoDB:

```bash
aws lambda invoke --function-name ssb-seat-tracker-tracker response.json
aws logs tail /aws/lambda/ssb-seat-tracker-tracker --since 10m
```

Cost depends on region, account-level free-tier eligibility, traffic, logs, and notification use.
Review the current AWS pricing pages before deployment and configure an account-level AWS Budget
or Cost Anomaly Detection separately if you need a spending backstop; this stack does not create
one.

Design references:

- [pytest good integration practices](https://docs.pytest.org/en/stable/explanation/goodpractices.html)
- [RESPX pytest and routing guide](https://lundberg.github.io/respx/guide/)
- [AWS ports-and-adapters guidance](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/hexagonal-architecture.html)
- [AWS Lambda Python dependency packaging](https://docs.aws.amazon.com/lambda/latest/dg/python-package.html)
- [AWS Lambda testing guidance](https://docs.aws.amazon.com/lambda/latest/dg/testing-guide.html)
- [AWS SAM ScheduleV2](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/sam-property-function-schedulev2.html)
- [AWS SAM custom Makefile builds](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/building-custom-runtimes.html)
- [AWS Parameter Store SecureString](https://docs.aws.amazon.com/systems-manager/latest/userguide/secure-string-parameter-kms-encryption.html)
- [GitHub OIDC with AWS](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws)
- [Temple registration waitlisting](https://registrar.temple.edu/registration-waitlisting)
- [AWS Lambda pricing](https://aws.amazon.com/lambda/pricing/)
- [Amazon EventBridge pricing](https://aws.amazon.com/eventbridge/pricing/)
- [Amazon DynamoDB pricing](https://aws.amazon.com/dynamodb/pricing/)
- [AWS Budgets pricing](https://aws.amazon.com/aws-cost-management/aws-budgets/pricing/)
- [ntfy publishing API](https://docs.ntfy.sh/publish/)
