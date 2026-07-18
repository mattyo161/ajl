#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DATA_DIR="${SCRIPT_DIR}/../.temp/data"
[[ ! -d "${DATA_DIR}" ]] && mkdir -p "${DATA_DIR}"

#export AJL_REGIONS="${AJL_REGIONS:-us-east-1,us-east-2}"
export AJL_CACHE="${AJL_CACHE:-1h}"
export AJL_APILOG="${AJL_APILOG:-1}"


#####################
### ECS
#####################
ajl ecs list-clusters --describe --stamp-session --all \
| tee "${DATA_DIR}/ecs-clusters.jsonl" \
| tee >(jq -rc '{Profile,Region,cluster:.clusterArn}' \
        | ajl ecs list-services --params-json - --stamp-session --describe \
        | tee "${DATA_DIR}/ecs-services.jsonl" \
        | jq -rc '{Profile,Region,Service:.serviceArn}' \
        | ajl ecs list-service-deployments --params-json - --stamp-session \
        > "${DATA_DIR}/ecs-service-deployments.jsonl") \
| tee >(jq -rc '{Profile,Region,cluster:.clusterArn}' \
        | ajl ecs list-tasks --params-json - --stamp-session --describe \
        > "${DATA_DIR}/ecs-tasks.jsonl") \
| tee >(jq -rc '{Profile,Region,cluster:.clusterArn}' \
        | ajl ecs list-container-instances --params-json - --stamp-session --describe \
        > "${DATA_DIR}/ecs-container-instances.jsonl") \
> /dev/null

ajl ecs list-task-definitions --all --stamp-session \
> "${DATA_DIR}/ecs-task-definitions.jsonl"


#####################
### EC2
#####################
# describe-images / describe-snapshots WITHOUT an owner filter return every
# AMI/snapshot visible to the account — that includes the entire public/
# Marketplace catalog (tens of thousands of rows), not just your own. --owners
# self / --owner-ids self is the difference between an inventory and a dump
# of AWS's public catalog.
for op in describe-instances describe-security-groups describe-vpcs describe-subnets \
          describe-volumes describe-network-interfaces describe-route-tables \
          describe-internet-gateways describe-nat-gateways describe-addresses \
          describe-key-pairs describe-vpc-endpoints describe-launch-templates \
          describe-network-acls describe-vpc-peering-connections describe-dhcp-options \
          describe-vpn-gateways describe-vpn-connections describe-customer-gateways \
          describe-transit-gateways describe-client-vpn-endpoints; do
  ajl ec2 "$op" --all --stamp-session > "${DATA_DIR}/ec2-${op#describe-}.jsonl"
done
ajl ec2 describe-images --owners self --all --stamp-session \
> "${DATA_DIR}/ec2-images.jsonl"
ajl ec2 describe-snapshots --owner-ids self --all --stamp-session \
> "${DATA_DIR}/ec2-snapshots.jsonl"


#####################
### RDS
#####################
for op in describe-db-instances describe-db-clusters describe-db-snapshots \
          describe-db-cluster-snapshots describe-db-subnet-groups \
          describe-db-parameter-groups describe-db-cluster-parameter-groups \
          describe-event-subscriptions; do
  ajl rds "$op" --all --stamp-session > "${DATA_DIR}/rds-${op#describe-}.jsonl"
done


#####################
### IAM
#####################
# list-policies WITHOUT --scope Local returns AWS's own managed-policy
# catalog too (~1500 rows on this account vs 43 actually customer-created) —
# the same catalog-pollution trap as ec2 describe-images/describe-snapshots.
ajl iam list-roles --all --stamp-session \
| tee "${DATA_DIR}/iam-roles.jsonl" \
| tee >(jq -rc '{Profile,Region,RoleName:.RoleName}' \
        | ajl iam list-role-policies --params-json - --stamp-session --describe \
        > "${DATA_DIR}/iam-role-policies.jsonl") \
| tee >(jq -rc '{Profile,Region,RoleName:.RoleName}' \
        | ajl iam list-attached-role-policies --params-json - --stamp-session \
        > "${DATA_DIR}/iam-attached-role-policies.jsonl") \
> /dev/null

ajl iam list-users --all --stamp-session \
| tee "${DATA_DIR}/iam-users.jsonl" \
| tee >(jq -rc '{Profile,Region,UserName:.UserName}' \
        | ajl iam list-user-policies --params-json - --stamp-session --describe \
        > "${DATA_DIR}/iam-user-policies.jsonl") \
| tee >(jq -rc '{Profile,Region,UserName:.UserName}' \
        | ajl iam list-attached-user-policies --params-json - --stamp-session \
        > "${DATA_DIR}/iam-attached-user-policies.jsonl") \
| tee >(jq -rc '{Profile,Region,UserName:.UserName}' \
        | ajl iam list-mfa-devices --params-json - --stamp-session --describe \
        > "${DATA_DIR}/iam-mfa-devices.jsonl") \
> /dev/null

ajl iam list-groups --all --stamp-session \
| tee "${DATA_DIR}/iam-groups.jsonl" \
| tee >(jq -rc '{Profile,Region,GroupName:.GroupName}' \
        | ajl iam list-group-policies --params-json - --stamp-session --describe \
        > "${DATA_DIR}/iam-group-policies.jsonl") \
| tee >(jq -rc '{Profile,Region,GroupName:.GroupName}' \
        | ajl iam list-attached-group-policies --params-json - --stamp-session \
        > "${DATA_DIR}/iam-attached-group-policies.jsonl") \
> /dev/null

ajl iam list-policies --scope Local --all --stamp-session \
| tee "${DATA_DIR}/iam-policies.jsonl" \
| jq -rc '{Profile,Region,PolicyArn:.Arn}' \
| ajl iam list-policy-versions --params-json - --stamp-session --describe \
> "${DATA_DIR}/iam-policy-versions.jsonl"

ajl iam list-instance-profiles --all --stamp-session \
> "${DATA_DIR}/iam-instance-profiles.jsonl"

ajl iam list-open-id-connect-providers --all --stamp-session --describe \
> "${DATA_DIR}/iam-oidc-providers.jsonl"

ajl iam list-saml-providers --all --stamp-session --describe \
> "${DATA_DIR}/iam-saml-providers.jsonl"


#####################
### EKS
#####################
ajl eks list-clusters --all --stamp-session --describe \
| tee "${DATA_DIR}/eks-clusters.jsonl" \
| tee >(jq -rc '{Profile,Region,clusterName:.name}' \
        | ajl eks list-nodegroups --params-json - --stamp-session --describe \
        > "${DATA_DIR}/eks-nodegroups.jsonl") \
| tee >(jq -rc '{Profile,Region,clusterName:.name}' \
        | ajl eks list-addons --params-json - --stamp-session --describe \
        > "${DATA_DIR}/eks-addons.jsonl") \
| tee >(jq -rc '{Profile,Region,clusterName:.name}' \
        | ajl eks list-fargate-profiles --params-json - --stamp-session --describe \
        > "${DATA_DIR}/eks-fargate-profiles.jsonl") \
| jq -rc '{Profile,Region,clusterName:.name}' \
| ajl eks list-access-entries --params-json - --stamp-session --describe \
> "${DATA_DIR}/eks-access-entries.jsonl"


#####################
### ROUTE53
#####################
# global service: one pass is enough, no --all fan-out across regions needed
ajl route53 list-hosted-zones --stamp-session \
| tee "${DATA_DIR}/route53-hosted-zones.jsonl" \
| jq -rc '{Profile,Region,HostedZoneId:.Id}' \
| ajl route53 list-resource-record-sets --params-json - --stamp-session \
> "${DATA_DIR}/route53-resource-record-sets.jsonl"

ajl route53 list-health-checks --stamp-session \
> "${DATA_DIR}/route53-health-checks.jsonl"

ajl route53 list-query-logging-configs --stamp-session --describe \
> "${DATA_DIR}/route53-query-logging-configs.jsonl"

ajl route53 list-reusable-delegation-sets --stamp-session --describe \
> "${DATA_DIR}/route53-reusable-delegation-sets.jsonl"


#####################
### ECR
#####################
ajl ecr describe-repositories --all --stamp-session \
| tee "${DATA_DIR}/ecr-repositories.jsonl" \
| jq -rc '{Profile,Region,repositoryName:.Name}' \
| ajl ecr describe-images --params-json - --stamp-session \
> "${DATA_DIR}/ecr-images.jsonl"


#####################
### EFS
#####################
ajl efs describe-file-systems --all --stamp-session \
| tee "${DATA_DIR}/efs-file-systems.jsonl" \
| jq -rc '{Profile,Region,FileSystemId:.Id}' \
| ajl efs describe-mount-targets --params-json - --stamp-session \
> "${DATA_DIR}/efs-mount-targets.jsonl"


#####################
### ELB / ELBV2
#####################
ajl elb describe-load-balancers --all --stamp-session \
> "${DATA_DIR}/elb-classic-load-balancers.jsonl"

ajl elbv2 describe-load-balancers --all --stamp-session \
| tee "${DATA_DIR}/elbv2-load-balancers.jsonl" \
| tee >(jq -rc '{Profile,Region,LoadBalancerArn:.Arn}' \
        | ajl elbv2 describe-target-groups --params-json - --stamp-session \
        > "${DATA_DIR}/elbv2-target-groups.jsonl") \
| jq -rc '{Profile,Region,LoadBalancerArn:.Arn}' \
| ajl elbv2 describe-listeners --params-json - --stamp-session \
| tee "${DATA_DIR}/elbv2-listeners.jsonl" \
| jq -rc '{Profile,Region,ListenerArn:.Arn}' \
| ajl elbv2 describe-rules --params-json - --stamp-session \
> "${DATA_DIR}/elbv2-rules.jsonl"


#####################
### BACKUP
#####################
ajl backup list-backup-vaults --all --stamp-session \
| tee "${DATA_DIR}/backup-vaults.jsonl" \
| jq -rc '{Profile,Region,BackupVaultName:.Name}' \
| ajl backup list-recovery-points-by-backup-vault --params-json - --stamp-session \
> "${DATA_DIR}/backup-recovery-points.jsonl"

ajl backup list-backup-plans --all --stamp-session \
> "${DATA_DIR}/backup-plans.jsonl"

ajl backup list-protected-resources --all --stamp-session \
> "${DATA_DIR}/backup-protected-resources.jsonl"


#####################
### LAMBDA
#####################
ajl lambda list-functions --all --stamp-session \
> "${DATA_DIR}/lambda-functions.jsonl"

ajl lambda list-layers --all --stamp-session \
> "${DATA_DIR}/lambda-layers.jsonl"

ajl lambda list-event-source-mappings --all --stamp-session \
> "${DATA_DIR}/lambda-event-source-mappings.jsonl"


#####################
### SSM
#####################
# metadata only (type/tier/last-modified) — see docs/commands/ssm-params.md;
# use `ajl ssm get` separately for actual values
ajl ssm params --all --stamp-session \
> "${DATA_DIR}/ssm-parameters.jsonl"


#####################
### DOCDB
#####################
# docdb's DescribeDBClusters/DescribeDBInstances hit the *same*
# rds.amazonaws.com endpoint rds itself uses and return every RDS-family
# cluster/instance (Aurora, Neptune, ...) unfiltered — without the Engine
# filter this silently duplicates the rds section's own Aurora clusters
# under a "docdb" label. The Filters param is the fix, not an ajl one.
ajl docdb describe-db-clusters --filters '[{"Name":"engine","Values":["docdb"]}]' \
    --all --stamp-session \
> "${DATA_DIR}/docdb-clusters.jsonl"
ajl docdb describe-db-instances --filters '[{"Name":"engine","Values":["docdb"]}]' \
    --all --stamp-session \
> "${DATA_DIR}/docdb-instances.jsonl"


#####################
### DYNAMODB
#####################
ajl dynamodb list-tables --all --stamp-session --describe \
> "${DATA_DIR}/dynamodb-tables.jsonl"

ajl dynamodb list-global-tables --all --stamp-session --describe \
> "${DATA_DIR}/dynamodb-global-tables.jsonl"

ajl dynamodb list-contributor-insights --all --stamp-session --describe \
> "${DATA_DIR}/dynamodb-contributor-insights.jsonl"

ajl dynamodb list-exports --all --stamp-session --describe \
> "${DATA_DIR}/dynamodb-exports.jsonl"


#####################
### ATHENA
#####################
ajl athena list-work-groups --all --stamp-session \
> "${DATA_DIR}/athena-workgroups.jsonl"

ajl athena list-databases --catalog-name AwsDataCatalog --all --stamp-session --describe \
> "${DATA_DIR}/athena-databases.jsonl"

ajl athena list-named-queries --all --stamp-session --describe \
> "${DATA_DIR}/athena-named-queries.jsonl"


#####################
### SSM (extras beyond `ssm params`)
#####################
ajl ssm describe-instance-information --all --stamp-session \
> "${DATA_DIR}/ssm-instance-information.jsonl"

ajl ssm list-documents --document-filter-list '[{"key":"Owner","value":"Self"}]' \
    --all --stamp-session \
> "${DATA_DIR}/ssm-documents.jsonl"

ajl ssm list-associations --all --stamp-session \
> "${DATA_DIR}/ssm-associations.jsonl"

ajl ssm describe-maintenance-windows --all --stamp-session \
> "${DATA_DIR}/ssm-maintenance-windows.jsonl"


#####################
### SQS
#####################
# GetQueueAttributes with no --attribute-names returns an EMPTY attribute
# map by default (no QueueArn, nothing) — "All" is required to get anything.
ajl sqs list-queues --all --stamp-session \
| tee "${DATA_DIR}/sqs-queues.jsonl" \
| jq -rc '{Profile,Region,QueueUrl}' \
| ajl sqs get-queue-attributes --attribute-names All --params-json - --stamp-session \
> "${DATA_DIR}/sqs-queue-attributes.jsonl"


#####################
### SNS
#####################
ajl sns list-topics --all --stamp-session \
| tee "${DATA_DIR}/sns-topics.jsonl" \
| tee >(jq -rc '{Profile,Region,TopicArn:.Arn}' \
        | ajl sns get-topic-attributes --params-json - --stamp-session \
        > "${DATA_DIR}/sns-topic-attributes.jsonl") \
| jq -rc '{Profile,Region,TopicArn:.Arn}' \
| ajl sns list-subscriptions-by-topic --params-json - --stamp-session \
> "${DATA_DIR}/sns-subscriptions.jsonl"

ajl sns list-platform-applications --all --stamp-session \
> "${DATA_DIR}/sns-platform-applications.jsonl"


#####################
### SES
#####################
# GetIdentity*Attributes return a map keyed by identity ({"VerificationAttributes":
# {"a@b.com": {...}}}), not a list — doesn't fit --describe's list-shaped model,
# so --no-parse + a jq to_entries reshape does it instead. --stamp-session still
# attaches Profile/Region/Account directly onto the raw --no-parse page.
ajl ses list-identities --all --stamp-session \
| tee "${DATA_DIR}/ses-identities.jsonl" \
| jq -rc '{Profile,Region,Account,Identities:[.Identity]}' \
| tee >(ajl ses get-identity-verification-attributes --params-json - --stamp-session --no-parse \
        | jq -c '. as $r | ($r.VerificationAttributes // {}) | to_entries[]
                 | {Type:"ses:identity-verification", Id:.key, Tags:{}} + .value
                 + {Profile:$r.Profile,Region:$r.Region,Account:$r.Account}' \
        > "${DATA_DIR}/ses-identity-verification.jsonl") \
| tee >(ajl ses get-identity-dkim-attributes --params-json - --stamp-session --no-parse \
        | jq -c '. as $r | ($r.DkimAttributes // {}) | to_entries[]
                 | {Type:"ses:identity-dkim", Id:.key, Tags:{}} + .value
                 + {Profile:$r.Profile,Region:$r.Region,Account:$r.Account}' \
        > "${DATA_DIR}/ses-identity-dkim.jsonl") \
| ajl ses get-identity-notification-attributes --params-json - --stamp-session --no-parse \
| jq -c '. as $r | ($r.NotificationAttributes // {}) | to_entries[]
         | {Type:"ses:identity-notification", Id:.key, Tags:{}} + .value
         + {Profile:$r.Profile,Region:$r.Region,Account:$r.Account}' \
> "${DATA_DIR}/ses-identity-notification.jsonl"

ajl ses list-configuration-sets --all --stamp-session --describe \
> "${DATA_DIR}/ses-configuration-sets.jsonl"

ajl ses list-receipt-rule-sets --all --stamp-session --describe \
> "${DATA_DIR}/ses-receipt-rule-sets.jsonl"


#####################
### ACM
#####################
ajl acm list-certificates --all --stamp-session --describe \
> "${DATA_DIR}/acm-certificates.jsonl"


#####################
### WAFV2
#####################
# Get{WebACL,IPSet,RuleGroup} need Name+Scope+Id together, which doesn't fit
# --describe's single-id_field model — --stamp-session puts Scope (a List
# call request param) on every list record instead, so a plain jq chain
# reconstructs the params the Get* call needs.
ajl wafv2 list-web-acls --scope REGIONAL --all --stamp-session \
| tee "${DATA_DIR}/wafv2-web-acls-regional.jsonl" \
| jq -rc '{Profile,Region,Name,Id,Scope}' \
| ajl wafv2 get-web-acl --params-json - --stamp-session \
> "${DATA_DIR}/wafv2-web-acl-details-regional.jsonl"

ajl wafv2 list-ip-sets --scope REGIONAL --all --stamp-session \
| tee "${DATA_DIR}/wafv2-ip-sets-regional.jsonl" \
| jq -rc '{Profile,Region,Name,Id,Scope}' \
| ajl wafv2 get-ip-set --params-json - --stamp-session \
> "${DATA_DIR}/wafv2-ip-set-details-regional.jsonl"

ajl wafv2 list-rule-groups --scope REGIONAL --all --stamp-session \
| tee "${DATA_DIR}/wafv2-rule-groups-regional.jsonl" \
| jq -rc '{Profile,Region,Name,Id,Scope}' \
| ajl wafv2 get-rule-group --params-json - --stamp-session \
> "${DATA_DIR}/wafv2-rule-group-details-regional.jsonl"

# CLOUDFRONT scope is global but must be *queried* from us-east-1 —
# AJL_REGIONS/--all would hit it once per configured region for identical
# results, so pin --region explicitly instead of fanning.
ajl wafv2 list-web-acls --scope CLOUDFRONT --region us-east-1 --stamp-session \
| tee "${DATA_DIR}/wafv2-web-acls-cloudfront.jsonl" \
| jq -rc '{Profile,Region,Name,Id,Scope}' \
| ajl wafv2 get-web-acl --params-json - --region us-east-1 --stamp-session \
> "${DATA_DIR}/wafv2-web-acl-details-cloudfront.jsonl"

ajl wafv2 list-ip-sets --scope CLOUDFRONT --region us-east-1 --stamp-session \
| tee "${DATA_DIR}/wafv2-ip-sets-cloudfront.jsonl" \
| jq -rc '{Profile,Region,Name,Id,Scope}' \
| ajl wafv2 get-ip-set --params-json - --region us-east-1 --stamp-session \
> "${DATA_DIR}/wafv2-ip-set-details-cloudfront.jsonl"

ajl wafv2 list-rule-groups --scope CLOUDFRONT --region us-east-1 --stamp-session \
| tee "${DATA_DIR}/wafv2-rule-groups-cloudfront.jsonl" \
| jq -rc '{Profile,Region,Name,Id,Scope}' \
| ajl wafv2 get-rule-group --params-json - --region us-east-1 --stamp-session \
> "${DATA_DIR}/wafv2-rule-group-details-cloudfront.jsonl"


#####################
### REDSHIFT
#####################
ajl redshift describe-clusters --all --stamp-session \
> "${DATA_DIR}/redshift-clusters.jsonl"

ajl redshift describe-cluster-subnet-groups --all --stamp-session \
> "${DATA_DIR}/redshift-cluster-subnet-groups.jsonl"

ajl redshift describe-cluster-parameter-groups --all --stamp-session \
> "${DATA_DIR}/redshift-cluster-parameter-groups.jsonl"

# describe-cluster-security-groups is EC2-Classic-only and AWS now rejects
# it outright ("Amazon Redshift has discontinued cluster security groups")
# for every VPC-based cluster — which is all of them on modern accounts.
# Not called here; VPC security groups are already covered by ec2's own
# describe-security-groups.

ajl redshift describe-cluster-snapshots --all --stamp-session \
> "${DATA_DIR}/redshift-cluster-snapshots.jsonl"

ajl redshift describe-event-subscriptions --all --stamp-session \
> "${DATA_DIR}/redshift-event-subscriptions.jsonl"

ajl redshift describe-hsm-client-certificates --all --stamp-session \
> "${DATA_DIR}/redshift-hsm-client-certificates.jsonl"

ajl redshift describe-hsm-configurations --all --stamp-session \
> "${DATA_DIR}/redshift-hsm-configurations.jsonl"

ajl redshift describe-snapshot-schedules --all --stamp-session \
> "${DATA_DIR}/redshift-snapshot-schedules.jsonl"

ajl redshift describe-usage-limits --all --stamp-session \
> "${DATA_DIR}/redshift-usage-limits.jsonl"


#####################
### WORKSPACES
#####################
# Owner defaults to your own account for bundles/images — passing
# --owner AMAZON (as with ec2 AMIs / iam managed policies) pulls in AWS's
# entire public catalog (146 bundles / 2 images observed) instead of your
# own. Tags aren't inline on any Describe* response; DescribeTags takes a
# single ResourceId and is chained via --describe.
ajl workspaces describe-workspaces --all --stamp-session --describe \
> "${DATA_DIR}/workspaces-workspaces.jsonl"

ajl workspaces describe-workspace-directories --all --stamp-session --describe \
> "${DATA_DIR}/workspaces-directories.jsonl"

ajl workspaces describe-workspace-bundles --all --stamp-session --describe \
> "${DATA_DIR}/workspaces-bundles.jsonl"

ajl workspaces describe-workspace-images --all --stamp-session --describe \
> "${DATA_DIR}/workspaces-images.jsonl"

ajl workspaces describe-connection-aliases --all --stamp-session --describe \
> "${DATA_DIR}/workspaces-connection-aliases.jsonl"


#####################
### CLOUDFORMATION
#####################
# describe-stacks (not list-stacks) — richer detail for free (Parameters,
# Capabilities, Tags), same shape otherwise. list-type-registrations needs
# an ARN or TypeName input (custom-resource-type registry lookup, not a
# parameter-free "list everything" op) — not called here.
ajl cloudformation describe-stacks --all --stamp-session \
> "${DATA_DIR}/cloudformation-stacks.jsonl"


#####################
### CLOUDTRAIL
#####################
# describe-trails (not list-trails) — richer detail for free (S3BucketName,
# IsMultiRegionTrail, IsOrganizationTrail, ...), same shape otherwise.
ajl cloudtrail describe-trails --all --stamp-session \
> "${DATA_DIR}/cloudtrail-trails.jsonl"


#####################
### CLOUDWATCH
#####################
ajl cloudwatch describe-alarms --all --stamp-session \
> "${DATA_DIR}/cloudwatch-alarms.jsonl"

ajl cloudwatch list-dashboards --all --stamp-session --describe \
> "${DATA_DIR}/cloudwatch-dashboards.jsonl"


#####################
### EVENTS (EventBridge)
#####################
# list-partner-event-sources requires --name-prefix (not a parameter-free
# "list everything" op — it's a partner-integration lookup) — not called here.
ajl events list-rules --all --stamp-session \
> "${DATA_DIR}/events-rules.jsonl"


#####################
### LOGS (CloudWatch Logs)
#####################
ajl logs describe-log-groups --all --stamp-session \
> "${DATA_DIR}/logs-log-groups.jsonl"

ajl logs list-integrations --all --stamp-session --describe \
> "${DATA_DIR}/logs-integrations.jsonl"


#####################
### SAGEMAKER
#####################
ajl sagemaker list-models --all --stamp-session \
> "${DATA_DIR}/sagemaker-models.jsonl"

ajl sagemaker list-endpoint-configs --all --stamp-session \
> "${DATA_DIR}/sagemaker-endpoint-configs.jsonl"

ajl sagemaker list-actions --all --stamp-session \
> "${DATA_DIR}/sagemaker-actions.jsonl"

ajl sagemaker list-contexts --all --stamp-session \
> "${DATA_DIR}/sagemaker-contexts.jsonl"

ajl sagemaker list-data-quality-job-definitions --all --stamp-session --describe \
> "${DATA_DIR}/sagemaker-data-quality-job-definitions.jsonl"

ajl sagemaker list-device-fleets --all --stamp-session --describe \
> "${DATA_DIR}/sagemaker-device-fleets.jsonl"

ajl sagemaker list-human-task-uis --all --stamp-session --describe \
> "${DATA_DIR}/sagemaker-human-task-uis.jsonl"

ajl sagemaker list-model-bias-job-definitions --all --stamp-session --describe \
> "${DATA_DIR}/sagemaker-model-bias-job-definitions.jsonl"

ajl sagemaker list-model-explainability-job-definitions --all --stamp-session --describe \
> "${DATA_DIR}/sagemaker-model-explainability-job-definitions.jsonl"

ajl sagemaker list-model-quality-job-definitions --all --stamp-session --describe \
> "${DATA_DIR}/sagemaker-model-quality-job-definitions.jsonl"

ajl sagemaker list-notebook-instance-lifecycle-configs --all --stamp-session --describe \
> "${DATA_DIR}/sagemaker-notebook-instance-lifecycle-configs.jsonl"


#####################
### SERVICEDISCOVERY (Cloud Map)
#####################
ajl servicediscovery list-namespaces --all --stamp-session \
> "${DATA_DIR}/servicediscovery-namespaces.jsonl"

ajl servicediscovery list-services --all --stamp-session \
| tee "${DATA_DIR}/servicediscovery-services.jsonl" \
| jq -rc '{Profile,Region,ServiceId:.Id}' \
| ajl servicediscovery list-instances --params-json - --stamp-session --describe \
> "${DATA_DIR}/servicediscovery-instances.jsonl"

ajl servicediscovery list-operations --all --stamp-session --describe \
> "${DATA_DIR}/servicediscovery-operations.jsonl"


#####################
### TRANSFER (AWS Transfer Family)
#####################
# list-accesses errors on SERVICE_MANAGED-IdP servers ("Cannot list accesses
# on server with IdP type: SERVICE_MANAGED") — a real API restriction (only
# custom-IdP servers support it), not an ajl bug; per-item error containment
# reports it and keeps going for servers where it's actually valid.
ajl transfer list-servers --all --stamp-session \
| tee "${DATA_DIR}/transfer-servers.jsonl" \
| jq -rc '{Profile,Region,ServerId:.Id}' \
| ajl transfer list-accesses --params-json - --stamp-session --describe \
> "${DATA_DIR}/transfer-accesses.jsonl"

ajl transfer list-connectors --all --stamp-session --describe \
> "${DATA_DIR}/transfer-connectors.jsonl"

ajl transfer list-profiles --all --stamp-session --describe \
> "${DATA_DIR}/transfer-profiles.jsonl"

ajl transfer list-security-policies --all --stamp-session --describe \
> "${DATA_DIR}/transfer-security-policies.jsonl"

ajl transfer list-web-apps --all --stamp-session --describe \
> "${DATA_DIR}/transfer-web-apps.jsonl"

ajl transfer list-workflows --all --stamp-session \
| tee "${DATA_DIR}/transfer-workflows.jsonl" \
| jq -rc '{Profile,Region,WorkflowId:.Id}' \
| ajl transfer list-executions --params-json - --stamp-session --describe \
> "${DATA_DIR}/transfer-executions.jsonl"


#####################
### SECRETSMANAGER
#####################
# metadata only (Name/Arn/rotation/last-accessed/last-changed) — never
# GetSecretValue here, same "list, don't read" boundary as ssm params.
ajl secretsmanager list-secrets --all --stamp-session \
> "${DATA_DIR}/secretsmanager-secrets.jsonl"


#####################
### OPENSEARCH
#####################
ajl opensearch list-domain-names --all --stamp-session --describe \
| tee "${DATA_DIR}/opensearch-domains.jsonl" \
| jq -rc '{Profile,Region,DomainName:.Id}' \
| ajl opensearch list-data-sources --params-json - --stamp-session --describe \
> "${DATA_DIR}/opensearch-data-sources.jsonl"

ajl opensearch list-vpc-endpoints --all --stamp-session --describe \
> "${DATA_DIR}/opensearch-vpc-endpoints.jsonl"


#####################
### ELASTICACHE
#####################
ajl elasticache describe-cache-clusters --show-cache-node-info --all --stamp-session \
> "${DATA_DIR}/elasticache-cache-clusters.jsonl"

ajl elasticache describe-replication-groups --all --stamp-session \
> "${DATA_DIR}/elasticache-replication-groups.jsonl"

ajl elasticache describe-cache-subnet-groups --all --stamp-session \
> "${DATA_DIR}/elasticache-cache-subnet-groups.jsonl"

ajl elasticache describe-cache-parameter-groups --all --stamp-session \
> "${DATA_DIR}/elasticache-cache-parameter-groups.jsonl"

ajl elasticache describe-snapshots --all --stamp-session \
> "${DATA_DIR}/elasticache-snapshots.jsonl"

ajl elasticache describe-serverless-caches --all --stamp-session \
> "${DATA_DIR}/elasticache-serverless-caches.jsonl"


#####################
### CLOUDFRONT
#####################
# global service: one pass is enough, no --all fan-out across regions needed.
# list-{cache,origin-request,response-headers}-policies WITHOUT --type custom
# return AWS's managed-policy catalog too (15/8/5 managed vs 0/0/0 actually
# customer-created on this account) — the same catalog-pollution trap as
# ec2 describe-images, iam list-policies, docdb, and workspaces bundles/images.
ajl cloudfront list-distributions --stamp-session --describe \
| tee "${DATA_DIR}/cloudfront-distributions.jsonl" \
| jq -rc '{Profile,Region,DistributionId:.Id}' \
| ajl cloudfront list-invalidations --params-json - --stamp-session \
> "${DATA_DIR}/cloudfront-invalidations.jsonl"

ajl cloudfront list-streaming-distributions --stamp-session \
> "${DATA_DIR}/cloudfront-streaming-distributions.jsonl"

ajl cloudfront list-cache-policies --type custom --stamp-session \
> "${DATA_DIR}/cloudfront-cache-policies.jsonl"

ajl cloudfront list-origin-request-policies --type custom --stamp-session \
> "${DATA_DIR}/cloudfront-origin-request-policies.jsonl"

ajl cloudfront list-response-headers-policies --type custom --stamp-session \
> "${DATA_DIR}/cloudfront-response-headers-policies.jsonl"

ajl cloudfront list-functions --stamp-session \
> "${DATA_DIR}/cloudfront-functions.jsonl"

ajl cloudfront list-origin-access-controls --stamp-session \
> "${DATA_DIR}/cloudfront-origin-access-controls.jsonl"

ajl cloudfront list-public-keys --stamp-session \
> "${DATA_DIR}/cloudfront-public-keys.jsonl"

ajl cloudfront list-key-groups --stamp-session \
> "${DATA_DIR}/cloudfront-key-groups.jsonl"

ajl cloudfront list-field-level-encryption-configs --stamp-session \
> "${DATA_DIR}/cloudfront-field-level-encryption-configs.jsonl"

ajl cloudfront list-field-level-encryption-profiles --stamp-session \
> "${DATA_DIR}/cloudfront-field-level-encryption-profiles.jsonl"

ajl cloudfront list-realtime-log-configs --stamp-session \
> "${DATA_DIR}/cloudfront-realtime-log-configs.jsonl"

ajl cloudfront list-continuous-deployment-policies --stamp-session \
> "${DATA_DIR}/cloudfront-continuous-deployment-policies.jsonl"


#####################
### KMS
#####################
ajl kms list-keys --all --stamp-session --describe \
> "${DATA_DIR}/kms-keys.jsonl"

ajl kms list-aliases --all --stamp-session \
> "${DATA_DIR}/kms-aliases.jsonl"

ajl kms list-keys --all --stamp-session \
| jq -rc '{Profile,Region,KeyId:.Id}' \
| ajl kms list-key-policies --params-json - --stamp-session --describe \
> "${DATA_DIR}/kms-key-policies.jsonl"


#####################
### S3
#####################
ajl s3 list-buckets --stamp-session \
> "${DATA_DIR}/s3-buckets.jsonl"

# None of these Get* calls echo Bucket back in their response, so Id/Name/
# Arn come back blank — --stamp-session's Bucket (from the params piped in
# below) is the join key back to s3-buckets.jsonl. Several are EXPECTED to
# error per-bucket when that config was never set (NoSuchBucketPolicy,
# NoSuchCORSConfiguration, NoSuchTagSet, NoSuchWebsiteConfiguration,
# ReplicationConfigurationNotFoundError, ObjectLockConfigurationNotFoundError)
# — ajl's per-item error containment reports those to stderr and keeps going,
# same as any other --params-json fan-out.
for op in get-bucket-versioning get-bucket-encryption get-bucket-policy get-bucket-policy-status \
          get-bucket-lifecycle-configuration get-bucket-cors get-bucket-notification-configuration \
          get-bucket-replication get-bucket-logging get-bucket-accelerate-configuration \
          get-bucket-ownership-controls get-bucket-tagging get-bucket-location get-bucket-request-payment \
          get-bucket-acl get-bucket-website get-public-access-block get-object-lock-configuration; do
  jq -rc '{Profile,Region,Bucket:.Name}' "${DATA_DIR}/s3-buckets.jsonl" \
  | ajl s3 "$op" --params-json - --stamp-session \
  > "${DATA_DIR}/s3-${op#get-}.jsonl"
done
