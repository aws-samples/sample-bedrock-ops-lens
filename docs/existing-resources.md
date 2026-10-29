# Deploy into an existing network or with existing IAM roles

[Home](../README.md) · [Central deployment](deployment.md) · [Account scope](account-scope.md)

By default `./deploy.sh` creates everything Lens needs: a VPC with NAT,
security groups, and the required IAM execution roles. Many organizations do
not allow an application stack to create network or IAM resources. This guide
covers deploying Lens into a VPC you already have, with roles your IAM team
created, or any mixture. There is still one command, one `config.yaml`, and one
CloudFormation template; nothing else changes.

## How the choice is made

Choose resources on the **first deployment** of a stack. On updates, omitted
choices retain their deployed values.

1. If `config.yaml` has an `existing_resources:` section, it is used without
   prompting (see [the reference](#configyaml-reference)).
2. Otherwise, at a terminal, `deploy.sh` asks:

   ```text
   Networking
     Use existing networking resources, or create new? [existing/new]:
   IAM roles
     Use existing IAM roles? [existing/new/some]:
     CloudFormation service role ARN for the stacks, or 'none':
   ```

   Optional blank answers mean `new` (or no CloudFormation service role).
   Choosing an existing VPC requires its ID and at least two subnet IDs.
   Each security group and execution role is independent: supply an ID/ARN
   to reuse it, or press Enter to create it. Choose `none` at the WAF prompt
   to deploy without WAF. Answers are saved to `config.yaml`.
3. Without a terminal and without the section, Lens creates its own network and
   roles. `EXISTING_RESOURCES=new` selects that behavior on first deployment.

Before anything is created, every supplied resource is validated against the
account (`scripts/validate-existing-resources.py`), the plan is printed as a
table, and at a terminal you confirm it by typing `yes`:

```text
Resource plan for BedrockOpsLens-20260101-0900
    VPC                    EXISTING    vpc-0abc... (10.60.0.0/16)
    Private subnets        EXISTING    subnet-a (us-east-2a), subnet-b (us-east-2b)
    Lambda security group  EXISTING    sg-0123...
    Database security group CREATE NEW
    ...
    Backend role           EXISTING    arn:aws:iam::...:role/lens-backend
    Schema-init role       CREATE NEW
    NAT / IGW / endpoints  NOT CREATED (existing network)
  Proceed with this plan? Type 'yes' to continue:
```

A failed validation stops the deployment. Lens never falls back to creating a
replacement for a resource you named.

On later runs against the same stack, the network choice is read back from the
stack and cannot change, because a different VPC or subnet set would replace
Aurora and ElastiCache and lose the data. Role ARNs may change. To use a
different network, deploy a new stack with `STACK_NAME_SUFFIX`.

## What an existing network must provide

`infra/examples/existing-network.yaml` is a deployable example. The validator
checks the network configuration it can inspect; verify actual connectivity
from the selected subnets.

| Requirement | Why |
|---|---|
| A VPC with DNS support and DNS hostnames enabled | Aurora and ElastiCache endpoints are resolved by name |
| Two or more **private** subnets in distinct Availability Zones | Aurora and ElastiCache subnet groups require two AZs; Lambda ENIs in a public subnet have no outbound connectivity |
| Reachability to the regional Amazon S3 callback URL through your network or endpoints | The schema-init Lambda reports to CloudFormation through a presigned S3 URL during stack creation |
| Outbound reachability to the AWS APIs Lens calls | Secrets Manager, KMS, STS, CloudWatch, CloudWatch Logs, Cognito, Bedrock, Service Quotas, Cost Explorer, Lambda, SNS, SSM, and S3 |

Lens creates **no** VPC, subnet, internet gateway, NAT gateway, route table,
route, or VPC endpoint in this mode. Aurora, ElastiCache, Cognito, CloudFront,
S3, the Lambda functions, and the schedules are always created by Lens.

**Private networks.** An existing NAT gateway is not required. Your network
may use interface/gateway endpoints, centralized egress, or private routing.
Verify DNS, endpoint policies, security groups, and NACLs from the selected
subnets. A local interface endpoint does not automatically provide access to
another Region's APIs: every configured Region must be reachable, along with
the Cost Explorer endpoint in us-east-1. The validator warns when it cannot
prove connectivity; it cannot test traffic from inside the selected subnets.

### Security groups

Each of the three groups can be supplied or created independently. A supplied
group is used exactly as it is; Lens never adds or removes rules on it. That
means:

- the database group must already admit TCP 5432 from the Lambda group;
- the cache group must already admit TCP 11211 from the Lambda group;
- the Lambda group must allow outbound traffic to the services above.

Ingress can also use CIDRs or prefix lists covering the selected Lambda
subnets. Inspecting prefix lists requires `ec2:GetManagedPrefixListEntries`;
if access is unavailable, the validator reports that coverage is unverified.

When Lens creates the database or cache group inside your VPC, it adds the
ingress rule from whichever Lambda group is in use. Security groups are
VPC-bound, so supplying groups only makes sense with an existing VPC.

## What existing IAM roles need

`infra/examples/existing-execution-roles.yaml` is the authoritative list of
permissions, one role per Lambda, written so an IAM team can deploy it (or copy
the policies) **before** the Lens stack exists. Pass its outputs through
`existing_resources.roles`.

| Role | Trusts | Needs |
|---|---|---|
| `backend` | `lambda.amazonaws.com` | VPC access, the database secret, KMS decrypt via Secrets Manager, `bedrock:InvokeModel*`, Cognito user-pool actions, the app-client SSM parameter, SNS publish/subscribe, invoke the live-pull function |
| `ingester` | `lambda.amazonaws.com` | VPC access, the database secret, CloudWatch, Service Quotas, Cost Explorer, Bedrock catalog/profile reads, read its Lambda configuration, `sts:AssumeRole` on the reader role name, SNS publish, optional Organizations discovery and S3 log reads |
| `schema_init` | `lambda.amazonaws.com` | VPC access, the database secret, `sqs:SendMessage` on the dead-letter queue |
| `prewarm` | `lambda.amazonaws.com` | VPC access only |
| `pre_signup` | `lambda.amazonaws.com` | Basic execution only |
| `post_confirm` | `lambda.amazonaws.com` | Basic execution, `cognito-idp:ListUsersInGroup` and `AdminAddUserToGroup` |
| `edge` | `lambda.amazonaws.com` **and** `edgelambda.amazonaws.com` | Basic execution only (Lambda@Edge, us-east-1 edge stack) |

Two resources exist only after the stack is created, yet the policies have to
name them first. When a main-stack role is supplied on first deployment,
`deploy.sh` sets `StableResourceNames=true` for predictable names:

| Resource | Name | Policy pattern |
|---|---|---|
| Aurora secret | `<stack-name>/db` | `arn:aws:secretsmanager:<region>:<account>:secret:<stack-name>/db-*` |
| Schema-init dead-letter queue | `<stack-name>-schema-init-dlq` | `arn:aws:sqs:<region>:<account>:<stack-name>-schema-init-dlq` |

The stack name is `BedrockOpsLens-<suffix>`; `deploy.sh` prints it during
pre-flight as `stacks:`, and `STACK_NAME_SUFFIX=<suffix>` fixes it ahead of
time so the roles can be created first. The Cognito user pool ID is also
unknown in advance; use `userpool/*` in that Region and account, as the example
does. KMS decryption is granted with a `kms:ViaService` condition on Secrets
Manager rather than a key ARN.

That naming choice stays fixed for the life of the stack. Replacing execution
roles must not replace the database secret. When adding supplied roles to an
older stack, grant them access to its **actual existing** secret and queue
ARNs; the deterministic example policy alone may not cover those older names.

The **ingester role is the principal that monitored accounts trust.**
`setup-pipeline.sh` reads it from the deployed Lambda, so reader roles rolled
out afterwards trust your role automatically. Lambda checks VPC permissions and
the dead-letter-queue permission at function creation, and the schema-init
Lambda reads the secret during stack creation, so the validator simulates those
three before deployment.

When replacing an ingester role, update the reader roles' trust to allow the new
role before switching, retaining the previous principal until the update succeeds.
Switching from generated to supplied execution roles preserves the old generated
roles first, so published Lambda versions keep working during the update. These
retained roles require manual cleanup after no versions or aliases use them.

Because the secret and queue names are fixed in this mode, a deleted stack's
secret stays scheduled for deletion for the Secrets Manager recovery window.
Redeploy under a new suffix, or force-delete the old secret first.

Lambda@Edge role changes publish a new version. Deployment first preserves
the previous function, versions, and generated role so CloudFront can finish
using its old replicas. Keep previous supplied roles usable until those
replicas retire. Retained edge resources require later manual cleanup.

### Web application firewall

By default the edge stack creates a CLOUDFRONT-scope AWS WAF web ACL with the
AWS managed Common and Known-Bad-Inputs rule sets and attaches it to the
distribution. `waf` controls this:

| Value | Effect |
|---|---|
| `new` (default) | Create the web ACL in the edge stack and attach it |
| `none` | Create no web ACL; CloudFront runs without one (for example when WAF is managed centrally or not used) |
| web ACL ARN | Attach an existing CLOUDFRONT-scope web ACL from us-east-1 and create none |

Lens does not modify a supplied web ACL. Once a stack has a Lens-created web
ACL, it cannot be removed or replaced in a single run (the edge stack deploys
first and WAF refuses to delete an ACL that is still attached), so `deploy.sh`
rejects that change; choose `none` or an ARN on the first deployment, or deploy
a new stack. Switching from `none` to `new` later is allowed.

### CloudFormation service role

`cloudformation_service_role_arn` makes CloudFormation assume that role for the
edge, ECR, and main stacks instead of using your credentials. It must trust
`cloudformation.amazonaws.com` and have permission to create every resource in
the templates, including `iam:PassRole` for the execution roles. Once a stack is
associated with a service role, CloudFormation keeps using it on later updates.

## Testing the path end to end

The two example templates double as test fixtures. In a test account:

```bash
SUFFIX=mytest
aws cloudformation deploy --stack-name LensExampleNetwork-$SUFFIX \
  --template-file infra/examples/existing-network.yaml
aws cloudformation deploy --stack-name LensExampleRoles-$SUFFIX \
  --template-file infra/examples/existing-execution-roles.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides LensStackName=BedrockOpsLens-$SUFFIX EnableOrganizationsDiscovery=false
```

Copy the outputs into `existing_resources:` in `config.yaml`, then run
`STACK_NAME_SUFFIX=$SUFFIX ./deploy.sh` (dry run: validates and prints the plan)
and `STACK_NAME_SUFFIX=$SUFFIX ./deploy.sh --yes`. After deployment, confirm
the stack created no network or IAM resources:

```bash
aws cloudformation describe-stack-resources --stack-name BedrockOpsLens-$SUFFIX \
  --query "StackResources[?starts_with(ResourceType,'AWS::EC2::') || ResourceType=='AWS::IAM::Role'].[LogicalResourceId,ResourceType]" \
  --output table
```

With every network and role supplied, that list is empty. `./deploy.sh destroy`
removes the Lens stacks and leaves your network and roles in place.

## config.yaml reference

```yaml
existing_resources:
  network:                      # or `network: new`
    vpc_id: vpc-...
    private_subnet_ids: [subnet-..., subnet-...]
    lambda_security_group_id: sg-... | new
    db_security_group_id: sg-... | new
    cache_security_group_id: sg-... | new
  roles:                        # or `roles: new`
    backend: arn | new
    ingester: arn | new
    schema_init: arn | new
    prewarm: arn | new
    pre_signup: arn | new
    post_confirm: arn | new
    edge: arn | new
  cloudformation_service_role_arn: arn | none   # optional, defaults to none
  waf: new | none | <CLOUDFRONT web ACL ARN>    # optional, defaults to new
```

Only the VPC and subnet IDs are required when selecting an existing network.
Omitted security groups and individual roles are created on first deployment
and preserved on updates. The corresponding
CloudFormation parameters are `ExistingVpcId`, `ExistingVpcCidr` (filled in by
`deploy.sh`), `ExistingPrivateSubnetIds`, `Existing*SecurityGroupId`, and the
`*RoleArn` parameters in `infra/cloudformation.yaml`, plus `EdgeRoleArn` in
`infra/edge-bootstrap.yaml`.

## Existing resources, no WAF, and OU collection

Set the supplied VPC, subnets, and any existing role ARNs in `config.yaml` as
above, plus `existing_resources.waf: none`. If the IAM team creates roles first,
give them the final `BedrockOpsLens-<suffix>` stack name.

```bash
STACK_NAME_SUFFIX=myteam ENABLE_ORGANIZATIONS_DISCOVERY=true \
  SKIP_INITIAL_INGEST=1 ./deploy.sh --yes
STACK_NAME_SUFFIX=myteam ./setup-pipeline.sh --scope ou --ou-id ou-xxxx-yyyyyyyy
```

Use this order for upgrades too: OU setup requires the updated central
template and ingester image and rejects older central stacks before rollout.

Use management-account credentials or a registered StackSets delegated
administrator for service-managed rollout. The running ingester also needs
Organizations read access. Existing cross-account readers can instead use
the documented `--skip-rollout` path. An ordinary member account does not gain
Organizations listing permissions merely because it can assume reader roles.

The first command skips collection until setup selects the OU and installs
readers. Alternatively, configure `monitored_accounts.mode: discover-ou` and
`ou_ids` before deployment if readers already exist. Enabling Organizations
permissions alone does not select the whole organization. Setup persists
the selected scope in CloudFormation, and later redeploys preserve it.
Empty OUs collect nothing; lookup failures stop collection.

## Other resources

- **Single-account setup.** Use `--scope single --skip-rollout` when IAM
  creation is not permitted. This avoids the optional local reader stack.
- **Databases and caches.** Aurora and ElastiCache are always created for Lens.
  Reusing an existing database is not supported.
