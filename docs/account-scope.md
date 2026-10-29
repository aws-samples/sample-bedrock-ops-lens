# Choose the accounts to monitor

[Home](../README.md) · [Central deployment](deployment.md) · [Operations](operations.md)

The central Lambda collects operational metrics and quotas from the accounts
you configure. Each target needs a read-only reader role trusting the central
ingester's actual IAM role. The default reader name is `BedrockOpsLensReader`;
[custom names](multi-account-setup.md#custom-reader-role-name) must match the
central `ReaderRoleName` parameter and target configuration.

Run the following commands from the central deployment's checkout with its
AWS credentials. Setup finds the stack using `.deploy-stack-name`, or an explicit
`STACK_NAME_SUFFIX`. Use `DEPLOY_REGION` for a deployment Region override.

| Option | Deployment method | Organizations required? | StackSets required? |
|---|---|---|---|
| 1 | Central account only | No | No |
| 2 | One or more OUs | Yes | Yes, service-managed |
| 3 | Whole organization root | Yes | Yes, service-managed |
| 4a | Explicit list, self-managed StackSets | No, including bootstrap | Yes |
| 4b | Explicit list, roles deployed by account owners | No | No |

The [README deployment steps](../README.md#deploy) cover both single-account and
OU collection. If you started with the single-account flow, Organizations
permissions are disabled. For options 2 or 3, enable discovery in the central
deployment first:

```bash
ENABLE_ORGANIZATIONS_DISCOVERY=true ./deploy.sh --yes
```

Use that setting only when Organizations is permitted and configured for the
chosen deployment identity. Setup rejects Organizations scopes on a central
stack whose discovery parameter is disabled.

## Option 1: single account

```bash
./setup-pipeline.sh --scope single
```

No StackSet is used. This setup path deploys a local reader stack; use
`--skip-rollout` to omit it when using existing execution roles. Ingestion
normally uses the central Lambda's own credentials for its own account.

## Option 2: organizational units

Use the service-managed StackSet route from the Organizations management account,
or pass `--delegated-admin` from a registered delegated administrator. Complete
[AWS's service-managed prerequisites](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/stacksets-prereqs-service-managed.html),
including trusted access, before rollout.

For an existing Lens installation, run `deploy.sh --yes` from this release
first to upgrade the central template, permissions, and ingester image.
OU setup rejects older central stacks before changing readers or collection.

```bash
./setup-pipeline.sh --scope ou --ou-id ou-xxxx-yyyyyyyy
```

For multiple OUs, comma-separate their IDs. Auto-deployment is enabled so new
accounts joining the targeted OUs receive the reader role.

OU selection controls both where the roles are deployed and what is collected.
The ingester is set to `discover-ou` with the same OU IDs: on every run it
resolves the OU membership (child OUs included) with
`organizations:ListAccountsForParent` and collects only from those accounts, so
accounts elsewhere in the organization are neither contacted nor reported as
failures, and new members are picked up without reconfiguration. A membership
lookup failure fails the run rather than widening or narrowing the scope. The
central credentials must be the management account or a registered delegated
administrator (StackSets delegated-administrator registration grants the
read-only Organizations access this needs). Cost Explorer data is filtered to
the same accounts. Use option 4 with an explicit list when you need a fixed
collection scope.

## Option 3: organization root

Use the same service-managed prerequisites as option 2, targeting the whole
organization root:

```bash
./setup-pipeline.sh --scope org-root
```

## Option 4: explicit account list without Organizations

Both routes work without Organizations:

- **4a — self-managed StackSets:** follow the [bootstrap and setup guide](multi-account-setup.md).
  Create the central administration role before the target execution roles,
  then run setup with the complete account list.
- **4b — each account owner deploys the reader:** follow the
  [manual guide](manual-multi-account-setup.md) for the commands in A/B/D/E and
  central C. Configure C with `--scope accounts --skip-rollout` after the readers
  exist. No StackSet administration or execution roles are needed.

The guides cover both CSV and file inputs, operator permissions, ExternalId,
role ownership, custom names, and runtime verification. The central account is
not automatically included in an explicit list.

## Account names

Tables show account ID and name separately; CSV exports retain both fields.
Dropdown labels show “name (ID)”. Resolution order is:

1. The optional `account_names` map in `config.yaml`.
2. The Organizations account name when using `discover-org` or `discover-ou` mode.
3. `account:GetAccountInformation` through the account's reader role, which
   works without Organizations.

If a name cannot be resolved, the account ID is still shown.

## What `setup-pipeline.sh` does

1. Validates the central stack and resolves its ingester role and reader name.
2. Uses existing readers with `--skip-rollout`, or deploys them through the
   selected CloudFormation scope.
3. Persists `single`, `explicit`, `discover-ou`, or `discover-org` and its IDs
   in CloudFormation, preserving unrelated parameters and checking for drift.
4. Invokes ingestion and checks the Lambda response and reported module results.

Re-run with the **complete intended account list** when updating an explicit
scope. Removing an account stops collection after reconfiguration; it does not
delete IAM roles or historical rows. `--dry-run` validates inputs and reads the
central configuration without writes. `--skip-ingest` leaves runtime collection
unverified. Redeploying the central stack preserves the running ingester's
scope. Empty OU membership collects nothing, and lookup errors fail the run.

## Scale

Start with a few accounts and inspect each account/Region and module result
before expanding. Account count, Regions, available data, and API throttling
determine whether ingestion fits Lambda's 15-minute limit. A time-budget stop
for invocation logs can be resumable; it still means coverage is incomplete.

Setup configures one existing ingester. Repeating it for different OUs does not
create additional ingesters or independent shards. Larger fleets need a
separately designed scheduling or partitioning strategy.

Reader-role deployment does not grant unrelated billing access or configure
invocation-log delivery. See [coverage limitations](multi-account-setup.md#what-this-setup-does-not-configure).
