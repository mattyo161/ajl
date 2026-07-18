#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DATA_DIR="${SCRIPT_DIR}/../.temp/data"
[[ ! -d "${DATA_DIR}" ]] && mkdir -p "${DATA_DIR}"

#export AJL_REGIONS="${AJL_REGIONS:-us-east-1,us-east-2}"
export AJL_CACHE=1h
export AJL_APILOG=1


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
        > "${DATA_DIR}/ecs-container-instances.jsonl")

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
        > "${DATA_DIR}/iam-attached-role-policies.jsonl")

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
        > "${DATA_DIR}/iam-mfa-devices.jsonl")

ajl iam list-groups --all --stamp-session \
| tee "${DATA_DIR}/iam-groups.jsonl" \
| tee >(jq -rc '{Profile,Region,GroupName:.GroupName}' \
        | ajl iam list-group-policies --params-json - --stamp-session --describe \
        > "${DATA_DIR}/iam-group-policies.jsonl") \
| tee >(jq -rc '{Profile,Region,GroupName:.GroupName}' \
        | ajl iam list-attached-group-policies --params-json - --stamp-session \
        > "${DATA_DIR}/iam-attached-group-policies.jsonl")

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
### S3
#####################
ajl s3 list-buckets --stamp-session \
> "${DATA_DIR}/s3-buckets.jsonl"
