# ssb-seat-tracker

A small, read-only monitor for Temple University's public Ellucian Self-Service Banner
(SSB) class search. It establishes an anonymous search session, resolves a term by its public
description, queries one course, selects an exact CRN, applies Temple's waitlist rule, and alerts
only when availability meaningfully changes.

The tracker does **not** log into TUportal, handle AccessNet credentials or Duo, register, drop, or
stage classes, or call authenticated student-record endpoints.

## Availability rule

Temple warns that seats which appear open can be locked for students already on the waitlist. The
public response does not expose which individual seats are locked, so the tracker uses this
deliberately conservative estimate:

```text
effective seats = max(Banner seats remaining - waitlist actual, 0)
```

This is an application heuristic, not a guarantee from Temple. The tracker considers a section
available only when Banner also reports `openSection=true`; registration can still fail because of
waitlist locks, prerequisites, holds, reserved seats, or other student-specific restrictions.
Request, session, JSON, and validation failures are reported as unknown errors; they are never
recorded as a closed class.

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

Discover Temple's current public term descriptions and codes:

```bash
uv run ssb-seat-tracker --list-terms
```

Perform one check during development:

```bash
uv run ssb-seat-tracker \
  --term "2026 Fall" \
  --subject CIS \
  --course 4526 \
  --crn 31752 \
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

Each successful local CLI check logs the CRN, enrollment, capacity, raw remaining seats, waitlist
count, conservative effective seats, Banner's open flag, and cross-list fields. Lambda emits
structured records containing the CRN and transition outcome without the ntfy topic. An alert is
sent when:

- the first successful observation is open;
- a previously closed section becomes open; or
- an already-open section gains effective seats.

Identical observations do not send repeated alerts. A failed check leaves the last successful
state untouched. HTTP 429 responses honor `Retry-After` when it is expressed in seconds and never
increase polling pressure.

## Development

The normal suite is fully mocked and does not contact Temple:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

An explicitly opt-in smoke test verifies public term discovery against Temple without sending a
notification or querying a specific course:

```bash
SSB_LIVE_TEST=1 uv run pytest -m integration
```

The test configuration uses pytest's recommended importlib mode for new `src/` projects, strict
configuration and registered markers. Shared setup uses fresh fixtures, and HTTP behavior is
isolated with RESPX's pytest router so an unmatched request fails instead of reaching the network.

The application is intentionally limited to four substantive modules:

- `client.py` — Temple SSB HTTP/session behavior;
- `models.py` — external data mapping and availability rules;
- `notifier.py` — console and ntfy delivery; and
- `main.py` — CLI, state transitions, and polling orchestration.

The application-level `check_once` operation depends on small structural ports for section lookup
and notification delivery. The concrete Banner and ntfy clients are adapters assembled by the CLI.
This is a deliberately small application of ports-and-adapters architecture: it isolates business
rules for testing without adding layers that a four-module service does not need.

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

One invocation loads all enabled watches, discovers public terms once, and performs one Banner
course search for every unique `(term, subject, course_number)` group. Before each group, the
client resets Banner's session-scoped class-search form so criteria from the preceding course
cannot leak into the next result set. State is written only after a successful check and, when
needed, successful ntfy delivery. That ordering gives at-least-once alerts: a rare DynamoDB failure
after ntfy accepts a message can produce a duplicate on retry, but a delivery failure cannot
suppress the next alert.

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

### Add a watch

After the application stack exists, get its table name from the stack output and add a record. The
term is the exact public description returned by `--list-terms`:

```bash
aws dynamodb put-item \
  --table-name TABLE_NAME_FROM_STACK_OUTPUT \
  --item file://infrastructure/watch-item.example.json \
  --condition-expression 'attribute_not_exists(crn)'
```

Set `enabled` to false to pause one watch without deleting its last observation. A watch item may
omit `available`, `effective_seats`, and `last_checked_at`; Lambda writes those fields after its
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
