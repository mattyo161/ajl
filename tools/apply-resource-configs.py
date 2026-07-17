#!/usr/bin/env python3
"""Apply ajl resource configs to the service model files.

The model files under src/ajl/models are regenerated from the AWS SDK
definitions (see aws-model-extraction.sh), which wipes out the hand-curated
output shaping. This script re-applies it: the declarative
``output.resources`` configs that drive the generic Type/Id/Name/Arn/Tags
normalizer, plus the hand-written ``output.jq`` programs for APIs whose shape
needs the escape hatch (nested context like ec2 reservations, s3 common
prefixes for prefix fan-out).

Run from anywhere:  python3 tools/apply-resource-configs.py
"""

import json
import os
import sys

MODELS_DIR = os.path.join(os.path.dirname(__file__), "..", "src", "ajl", "models")


def r(path, type_, id_=None, name=None, arn=None, arn_format=None, tags=None, scalar_as=None,
      uri=None):
    cfg = {"path": path, "type": type_}
    if id_:
        cfg["id"] = id_
    if name:
        cfg["name"] = name
    if arn:
        cfg["arn"] = arn
    if arn_format:
        cfg["arn_format"] = arn_format
    if tags:
        cfg["tags"] = tags
    if scalar_as:
        cfg["scalar_as"] = scalar_as
    if uri:
        cfg["uri_format"] = uri
    return cfg


EC2_ARN = "arn:{partition}:ec2:{region}"

EC2_DESCRIBE_INSTANCES_JQ = """\
# Save the reservation details to $reservation
.Reservations[] | {ReservationId, OwnerId, RequesterId, Groups} as $reservation |
# Output each instance and then append the Reservation details at the end
.Instances[] |
# Convert Tags to a Map
.Tags = (.Tags//[] | from_entries) |
# Add common properties
{
  Type: "ec2:instance",
  Id: .InstanceId,
  Name: .Tags.Name//"",
  Arn: "arn:\\($partition//"aws"):ec2:\\(.Region//$region//""):\\($reservation.OwnerId//$account//""):instance/\\(.InstanceId)",
  Reservation: $reservation
} + ."""

# Emits CommonPrefixes as s3:prefix records carrying Bucket/Prefix/Delimiter so
# they can be piped straight back into `ajl s3 list-objects-v2 --params-json -`
# for parallel fan-out across the key space.
S3_LIST_OBJECTS_JQ = """\
. as $r |
((.CommonPrefixes//[])[] |
  {Type: "s3:prefix", Id: .Prefix, Name: .Prefix,
   Arn: "arn:\\($partition//"aws"):s3:::\\($r.Name)/\\(.Prefix)", Tags: {},
   Uri: "s3://\\($r.Name)/\\(.Prefix)",
   Bucket: $r.Name, Delimiter: $r.Delimiter} + .),
((.Contents//[])[] |
  {Type: "s3:object", Id: .Key, Name: .Key,
   Arn: "arn:\\($partition//"aws"):s3:::\\($r.Name)/\\(.Key)", Tags: {},
   Uri: "s3://\\($r.Name)/\\(.Key)",
   Bucket: $r.Name} + .)"""

S3_LIST_OBJECT_VERSIONS_JQ = """\
. as $r |
((.CommonPrefixes//[])[] |
  {Type: "s3:prefix", Id: .Prefix, Name: .Prefix,
   Arn: "arn:\\($partition//"aws"):s3:::\\($r.Name)/\\(.Prefix)", Tags: {},
   Uri: "s3://\\($r.Name)/\\(.Prefix)",
   Bucket: $r.Name, Delimiter: $r.Delimiter} + .),
((.Versions//[])[] |
  {Type: "s3:object-version", Id: .Key, Name: .Key,
   Arn: "arn:\\($partition//"aws"):s3:::\\($r.Name)/\\(.Key)", Tags: {},
   Uri: "s3://\\($r.Name)/\\(.Key)",
   Bucket: $r.Name} + .),
((.DeleteMarkers//[])[] |
  {Type: "s3:delete-marker", Id: .Key, Name: .Key,
   Arn: "arn:\\($partition//"aws"):s3:::\\($r.Name)/\\(.Key)", Tags: {},
   Uri: "s3://\\($r.Name)/\\(.Key)",
   Bucket: $r.Name} + .)"""

# Hosted zone Ids come back as "/hostedzone/Z123..."; strip the prefix so Id
# and the Arn are usable directly.
ROUTE53_LIST_HOSTED_ZONES_JQ = """\
(.HostedZones//[])[] |
.Id = (.Id | sub("^/hostedzone/"; "")) |
{Type: "route53:hostedzone", Id: .Id, Name: .Name,
 Arn: "arn:\\($partition//"aws"):route53:::hostedzone/\\(.Id)", Tags: {}} + ."""

# ListQueues only returns URLs; derive name/account from the URL segments.
SQS_LIST_QUEUES_JQ = """\
(.QueueUrls//[])[] | . as $url | ($url | split("/")) as $p |
{Type: "sqs:queue", Id: $p[-1], Name: $p[-1],
 Arn: "arn:\\($partition//"aws"):sqs:\\($region):\\($p[-2]):\\($p[-1])",
 Tags: {}, QueueUrl: $url}"""

CONFIGS = {
    "acm": {
        "resources": {
            "ListCertificates": [r(["CertificateSummaryList"], "acm:certificate", name="DomainName", arn="CertificateArn")],
        },
    },
    "cloud9": {
        "resources": {
            "ListEnvironments": [r(["environmentIds"], "cloud9:environment", "environmentId", arn_format="arn:{partition}:cloud9:{region}:{account}:environment:{environmentId}", scalar_as="environmentId")],
            "DescribeEnvironments": [r(["environments"], "cloud9:environment", "id", name="name", arn="arn")],
        },
    },
    "cloudformation": {
        "resources": {
            "ListStacks": [r(["StackSummaries"], "cloudformation:stack", "StackName", name="StackName", arn="StackId")],
            "DescribeStacks": [r(["Stacks"], "cloudformation:stack", "StackName", name="StackName", arn="StackId", tags="Tags")],
        },
    },
    "cloudtrail": {
        "resources": {
            "ListTrails": [r(["Trails"], "cloudtrail:trail", "Name", name="Name", arn="TrailARN")],
            "DescribeTrails": [r(["trailList"], "cloudtrail:trail", "Name", name="Name", arn="TrailARN")],
        },
    },
    "cloudwatch": {
        "resources": {
            "DescribeAlarms": [
                r(["MetricAlarms"], "cloudwatch:alarm", "AlarmName", name="AlarmName", arn="AlarmArn"),
                r(["CompositeAlarms"], "cloudwatch:alarm", "AlarmName", name="AlarmName", arn="AlarmArn"),
            ],
        },
    },
    "events": {
        "resources": {
            "ListRules": [r(["Rules"], "events:rule", "Name", name="Name", arn="Arn")],
        },
    },
    "logs": {
        "resources": {
            "DescribeLogGroups": [r(["logGroups"], "logs:log-group", "logGroupName", name="logGroupName", arn_format="arn:{partition}:logs:{region}:{account}:log-group:{logGroupName}")],
        },
    },
    "sagemaker": {
        "resources": {
            "ListModels": [r(["Models"], "sagemaker:model", "ModelName", name="ModelName", arn="ModelArn")],
            "ListEndpointConfigs": [r(["EndpointConfigs"], "sagemaker:endpoint-config", "EndpointConfigName", name="EndpointConfigName", arn="EndpointConfigArn")],
            "ListActions": [r(["ActionSummaries"], "sagemaker:action", "ActionName", name="ActionName", arn="ActionArn")],
            "ListContexts": [r(["ContextSummaries"], "sagemaker:context", "ContextName", name="ContextName", arn="ContextArn")],
        },
    },
    "servicediscovery": {
        "resources": {
            "ListNamespaces": [r(["Namespaces"], "servicediscovery:namespace", "Id", name="Name", arn="Arn")],
            "ListServices": [r(["Services"], "servicediscovery:service", "Id", name="Name", arn="Arn")],
        },
    },
    "transfer": {
        "resources": {
            "ListServers": [r(["Servers"], "transfer:server", "ServerId", name="ServerId", arn="Arn")],
        },
    },
    "ec2": {
        "jq": {
            "DescribeInstances": EC2_DESCRIBE_INSTANCES_JQ,
        },
        "resources": {
            "DescribeSecurityGroups": [r(["SecurityGroups"], "ec2:security-group", "GroupId", name="GroupName", arn_format=EC2_ARN + ":{OwnerId}:security-group/{GroupId}", tags="Tags")],
            "DescribeVpcs": [r(["Vpcs"], "ec2:vpc", "VpcId", arn_format=EC2_ARN + ":{OwnerId}:vpc/{VpcId}", tags="Tags")],
            "DescribeSubnets": [r(["Subnets"], "ec2:subnet", "SubnetId", arn="SubnetArn", tags="Tags")],
            "DescribeVolumes": [r(["Volumes"], "ec2:volume", "VolumeId", arn_format=EC2_ARN + ":{account}:volume/{VolumeId}", tags="Tags")],
            "DescribeImages": [r(["Images"], "ec2:image", "ImageId", name="Name", arn_format=EC2_ARN + "::image/{ImageId}", tags="Tags")],
            "DescribeSnapshots": [r(["Snapshots"], "ec2:snapshot", "SnapshotId", arn_format=EC2_ARN + "::snapshot/{SnapshotId}", tags="Tags")],
            "DescribeNetworkInterfaces": [r(["NetworkInterfaces"], "ec2:network-interface", "NetworkInterfaceId", arn_format=EC2_ARN + ":{OwnerId}:network-interface/{NetworkInterfaceId}", tags="TagSet")],
            "DescribeRouteTables": [r(["RouteTables"], "ec2:route-table", "RouteTableId", arn_format=EC2_ARN + ":{OwnerId}:route-table/{RouteTableId}", tags="Tags")],
            "DescribeInternetGateways": [r(["InternetGateways"], "ec2:internet-gateway", "InternetGatewayId", arn_format=EC2_ARN + ":{OwnerId}:internet-gateway/{InternetGatewayId}", tags="Tags")],
            "DescribeNatGateways": [r(["NatGateways"], "ec2:natgateway", "NatGatewayId", arn_format=EC2_ARN + ":{account}:natgateway/{NatGatewayId}", tags="Tags")],
            "DescribeAddresses": [r(["Addresses"], "ec2:elastic-ip", "AllocationId", arn_format=EC2_ARN + ":{account}:elastic-ip/{AllocationId}", tags="Tags")],
            "DescribeKeyPairs": [r(["KeyPairs"], "ec2:key-pair", "KeyPairId", name="KeyName", arn_format=EC2_ARN + ":{account}:key-pair/{KeyName}", tags="Tags")],
            "DescribeVpcEndpoints": [r(["VpcEndpoints"], "ec2:vpc-endpoint", "VpcEndpointId", arn_format=EC2_ARN + ":{OwnerId}:vpc-endpoint/{VpcEndpointId}", tags="Tags")],
            "DescribeLaunchTemplates": [r(["LaunchTemplates"], "ec2:launch-template", "LaunchTemplateId", name="LaunchTemplateName", arn_format=EC2_ARN + ":{account}:launch-template/{LaunchTemplateId}", tags="Tags")],
            "DescribeNetworkAcls": [r(["NetworkAcls"], "ec2:network-acl", "NetworkAclId", arn_format=EC2_ARN + ":{OwnerId}:network-acl/{NetworkAclId}", tags="Tags")],
            "DescribeVpcPeeringConnections": [r(["VpcPeeringConnections"], "ec2:vpc-peering-connection", "VpcPeeringConnectionId", arn_format=EC2_ARN + ":{account}:vpc-peering-connection/{VpcPeeringConnectionId}", tags="Tags")],
            "DescribeDhcpOptions": [r(["DhcpOptions"], "ec2:dhcp-options", "DhcpOptionsId", arn_format=EC2_ARN + ":{OwnerId}:dhcp-options/{DhcpOptionsId}", tags="Tags")],
            "DescribeVpnGateways": [r(["VpnGateways"], "ec2:vpn-gateway", "VpnGatewayId", arn_format=EC2_ARN + ":{account}:vpn-gateway/{VpnGatewayId}", tags="Tags")],
            "DescribeVpnConnections": [r(["VpnConnections"], "ec2:vpn-connection", "VpnConnectionId", arn_format=EC2_ARN + ":{account}:vpn-connection/{VpnConnectionId}", tags="Tags")],
            "DescribeCustomerGateways": [r(["CustomerGateways"], "ec2:customer-gateway", "CustomerGatewayId", arn_format=EC2_ARN + ":{account}:customer-gateway/{CustomerGatewayId}", tags="Tags")],
            "DescribeTransitGateways": [r(["TransitGateways"], "ec2:transit-gateway", "TransitGatewayId", arn="TransitGatewayArn", tags="Tags")],
            "DescribeClientVpnEndpoints": [r(["ClientVpnEndpoints"], "ec2:client-vpn-endpoint", "ClientVpnEndpointId", arn_format=EC2_ARN + ":{account}:client-vpn-endpoint/{ClientVpnEndpointId}", tags="Tags")],
        },
    },
    "lambda": {
        "resources": {
            "ListFunctions": [r(["Functions"], "lambda:function", "FunctionName", name="FunctionName", arn="FunctionArn")],
            "ListLayers": [r(["Layers"], "lambda:layer", "LayerName", name="LayerName", arn="LayerArn")],
            "ListEventSourceMappings": [r(["EventSourceMappings"], "lambda:event-source-mapping", "UUID", arn="EventSourceMappingArn")],
        },
    },
    "iam": {
        "resources": {
            "ListUsers": [r(["Users"], "iam:user", "UserId", name="UserName", arn="Arn")],
            "ListRoles": [r(["Roles"], "iam:role", "RoleId", name="RoleName", arn="Arn")],
            "ListGroups": [r(["Groups"], "iam:group", "GroupId", name="GroupName", arn="Arn")],
            "ListPolicies": [r(["Policies"], "iam:policy", "PolicyId", name="PolicyName", arn="Arn")],
            "ListInstanceProfiles": [r(["InstanceProfiles"], "iam:instance-profile", "InstanceProfileId", name="InstanceProfileName", arn="Arn")],
            "ListOpenIDConnectProviders": [r(["OpenIDConnectProviderList"], "iam:oidc-provider", arn="Arn")],
        },
    },
    "route53": {
        "jq": {
            "ListHostedZones": ROUTE53_LIST_HOSTED_ZONES_JQ,
        },
        "resources": {
            "ListResourceRecordSets": [r(["ResourceRecordSets"], "route53:rrset", "Name", name="Name")],
            "ListHealthChecks": [r(["HealthChecks"], "route53:healthcheck", "Id", arn_format="arn:{partition}:route53:::healthcheck/{Id}")],
        },
    },
    "elb": {
        "resources": {
            "DescribeLoadBalancers": [r(["LoadBalancerDescriptions"], "elasticloadbalancing:loadbalancer", "LoadBalancerName", name="LoadBalancerName", arn_format="arn:{partition}:elasticloadbalancing:{region}:{account}:loadbalancer/{LoadBalancerName}")],
        },
    },
    "elbv2": {
        "resources": {
            "DescribeLoadBalancers": [r(["LoadBalancers"], "elasticloadbalancing:loadbalancer", "LoadBalancerName", name="LoadBalancerName", arn="LoadBalancerArn")],
            "DescribeTargetGroups": [r(["TargetGroups"], "elasticloadbalancing:targetgroup", "TargetGroupName", name="TargetGroupName", arn="TargetGroupArn")],
            "DescribeListeners": [r(["Listeners"], "elasticloadbalancing:listener", arn="ListenerArn")],
            "DescribeRules": [r(["Rules"], "elasticloadbalancing:listener-rule", arn="RuleArn")],
        },
    },
    "ecs": {
        "resources": {
            "ListClusters": [r(["clusterArns"], "ecs:cluster", arn="clusterArn", scalar_as="clusterArn")],
            "DescribeClusters": [r(["clusters"], "ecs:cluster", "clusterName", name="clusterName", arn="clusterArn", tags="tags")],
            "ListServices": [r(["serviceArns"], "ecs:service", arn="serviceArn", scalar_as="serviceArn")],
            "DescribeServices": [r(["services"], "ecs:service", "serviceName", name="serviceName", arn="serviceArn", tags="tags")],
            "ListTaskDefinitions": [r(["taskDefinitionArns"], "ecs:task-definition", arn="taskDefinitionArn", scalar_as="taskDefinitionArn")],
            "DescribeTaskDefinition": [r(["taskDefinition"], "ecs:task-definition", "family", name="family", arn="taskDefinitionArn")],
            "DescribeCapacityProviders": [r(["capacityProviders"], "ecs:capacity-provider", "name", name="name", arn="capacityProviderArn", tags="tags")],
            "ListTasks": [r(["taskArns"], "ecs:task", arn="taskArn", scalar_as="taskArn")],
            "DescribeTasks": [r(["tasks"], "ecs:task", arn="taskArn", tags="tags")],
        },
    },
    "athena": {
        "resources": {
            "ListWorkGroups": [r(["WorkGroups"], "athena:workgroup", "Name", name="Name", arn_format="arn:{partition}:athena:{region}:{account}:workgroup/{Name}")],
        },
    },
    "backup": {
        "resources": {
            "ListBackupVaults": [r(["BackupVaultList"], "backup:backup-vault", "BackupVaultName", name="BackupVaultName", arn="BackupVaultArn")],
            "ListBackupPlans": [r(["BackupPlansList"], "backup:backup-plan", "BackupPlanId", name="BackupPlanName", arn="BackupPlanArn")],
            "ListProtectedResources": [r(["Results"], "backup:protected-resource", name="ResourceName", arn="ResourceArn")],
            "ListRecoveryPointsByBackupVault": [r(["RecoveryPoints"], "backup:recovery-point", arn="RecoveryPointArn")],
            "ListRecoveryPointsByResource": [r(["RecoveryPoints"], "backup:recovery-point", arn="RecoveryPointArn")],
        },
    },
    "cloudfront": {
        "resources": {
            "ListDistributions": [r(["DistributionList", "Items"], "cloudfront:distribution", "Id", name="DomainName", arn="ARN")],
        },
    },
    "docdb": {
        "resources": {
            "DescribeDBClusters": [r(["DBClusters"], "rds:cluster", "DBClusterIdentifier", arn="DBClusterArn")],
            "DescribeDBInstances": [r(["DBInstances"], "rds:db", "DBInstanceIdentifier", arn="DBInstanceArn")],
        },
    },
    "ecr": {
        "resources": {
            "DescribeRepositories": [r(["repositories"], "ecr:repository", "repositoryName", name="repositoryName", arn="repositoryArn")],
            "DescribeImages": [r(["imageDetails"], "ecr:image", "imageDigest")],
        },
    },
    "efs": {
        "resources": {
            "DescribeFileSystems": [r(["FileSystems"], "elasticfilesystem:file-system", "FileSystemId", name="Name", arn="FileSystemArn", tags="Tags")],
            "DescribeMountTargets": [r(["MountTargets"], "elasticfilesystem:mount-target", "MountTargetId")],
        },
    },
    "eks": {
        "resources": {
            "ListClusters": [r(["clusters"], "eks:cluster", "name", name="name", arn_format="arn:{partition}:eks:{region}:{account}:cluster/{name}", scalar_as="name")],
            "DescribeCluster": [r(["cluster"], "eks:cluster", "name", name="name", arn="arn", tags="tags")],
            "ListAccessEntries": [r(["accessEntries"], "eks:access-entry", arn="accessEntryArn", scalar_as="accessEntryArn")],
            "ListAddons": [r(["addons"], "eks:addon", "addonName", name="addonName", scalar_as="addonName")],
            "DescribeAddon": [r(["addon"], "eks:addon", "addonName", name="addonName", arn="addonArn", tags="tags")],
            "ListNodegroups": [r(["nodegroups"], "eks:nodegroup", "nodegroupName", name="nodegroupName", scalar_as="nodegroupName")],
            "DescribeNodegroup": [r(["nodegroup"], "eks:nodegroup", "nodegroupName", name="nodegroupName", arn="nodegroupArn", tags="tags")],
        },
    },
    "kms": {
        "resources": {
            "ListKeys": [r(["Keys"], "kms:key", "KeyId", arn="KeyArn")],
            "ListAliases": [r(["Aliases"], "kms:alias", "AliasName", name="AliasName", arn="AliasArn")],
        },
    },
    "opensearch": {
        "resources": {
            "ListDomainNames": [r(["DomainNames"], "es:domain", "DomainName", name="DomainName", arn_format="arn:{partition}:es:{region}:{account}:domain/{DomainName}")],
            "DescribeDomains": [r(["DomainStatusList"], "es:domain", "DomainName", name="DomainName", arn="ARN")],
        },
    },
    "secretsmanager": {
        "resources": {
            "ListSecrets": [r(["SecretList"], "secretsmanager:secret", "Name", name="Name", arn="ARN", tags="Tags")],
        },
    },
    "ses": {
        "resources": {
            "ListIdentities": [r(["Identities"], "ses:identity", "Identity", name="Identity", arn_format="arn:{partition}:ses:{region}:{account}:identity/{Identity}", scalar_as="Identity")],
            "ListTemplates": [r(["TemplatesMetadata"], "ses:template", "Name", name="Name", arn_format="arn:{partition}:ses:{region}:{account}:template/{Name}")],
        },
    },
    "sns": {
        "resources": {
            "ListTopics": [r(["Topics"], "sns:topic", arn="TopicArn")],
            "ListSubscriptions": [r(["Subscriptions"], "sns:subscription", arn="SubscriptionArn")],
            "ListPlatformApplications": [r(["PlatformApplications"], "sns:platform-application", arn="PlatformApplicationArn")],
        },
    },
    "sqs": {
        "jq": {
            "ListQueues": SQS_LIST_QUEUES_JQ,
        },
    },
    "wafv2": {
        "resources": {
            "ListWebACLs": [r(["WebACLs"], "wafv2:webacl", "Id", name="Name", arn="ARN")],
            "ListIPSets": [r(["IPSets"], "wafv2:ipset", "Id", name="Name", arn="ARN")],
            "ListRuleGroups": [r(["RuleGroups"], "wafv2:rulegroup", "Id", name="Name", arn="ARN")],
        },
    },
    "rds": {
        "resources": {
            "DescribeDBInstances": [r(["DBInstances"], "rds:db", "DBInstanceIdentifier", arn="DBInstanceArn", tags="TagList")],
            "DescribeDBClusters": [r(["DBClusters"], "rds:cluster", "DBClusterIdentifier", arn="DBClusterArn", tags="TagList")],
            "DescribeDBSnapshots": [r(["DBSnapshots"], "rds:snapshot", "DBSnapshotIdentifier", arn="DBSnapshotArn", tags="TagList")],
            "DescribeDBClusterSnapshots": [r(["DBClusterSnapshots"], "rds:cluster-snapshot", "DBClusterSnapshotIdentifier", arn="DBClusterSnapshotArn", tags="TagList")],
            "DescribeDBSubnetGroups": [r(["DBSubnetGroups"], "rds:subgrp", "DBSubnetGroupName", name="DBSubnetGroupName", arn="DBSubnetGroupArn")],
            "DescribeDBParameterGroups": [r(["DBParameterGroups"], "rds:pg", "DBParameterGroupName", name="DBParameterGroupName", arn="DBParameterGroupArn")],
            "DescribeDBClusterParameterGroups": [r(["DBClusterParameterGroups"], "rds:cluster-pg", "DBClusterParameterGroupName", name="DBClusterParameterGroupName", arn="DBClusterParameterGroupArn")],
            "DescribeEventSubscriptions": [r(["EventSubscriptionsList"], "rds:es", "CustSubscriptionId", arn="EventSubscriptionArn")],
        },
    },
    "s3": {
        "jq": {
            "ListObjects": S3_LIST_OBJECTS_JQ,
            "ListObjectsV2": S3_LIST_OBJECTS_JQ,
            "ListObjectVersions": S3_LIST_OBJECT_VERSIONS_JQ,
        },
        "remove_jq": ["ListBuckets"],
        "resources": {
            "ListBuckets": [r(["Buckets"], "s3:bucket", "Name", name="Name", arn_format="arn:{partition}:s3:::{Name}", uri="s3://{Name}")],
            "ListMultipartUploads": [
                r(["CommonPrefixes"], "s3:prefix", "Prefix", name="Prefix", arn_format="arn:{partition}:s3:::{root_Bucket}/{Prefix}", uri="s3://{root_Bucket}/{Prefix}"),
                r(["Uploads"], "s3:multipart-upload", "UploadId", name="Key", arn_format="arn:{partition}:s3:::{root_Bucket}/{Key}", uri="s3://{root_Bucket}/{Key}"),
            ],
        },
    },
    "ssm": {
        "remove_jq": [
            "DescribeParameters",
            "GetParameters",
            "GetParametersByPath",
            "DescribeInstanceInformation",
            "ListDocuments",
            "DescribeMaintenanceWindows",
            "ListAssociations",
        ],
        "resources": {
            "DescribeParameters": [r(["Parameters"], "ssm:parameter", "Name", name="Name", arn="ARN")],
            "GetParameters": [r(["Parameters"], "ssm:parameter", "Name", name="Name", arn="ARN")],
            "GetParametersByPath": [r(["Parameters"], "ssm:parameter", "Name", name="Name", arn="ARN")],
            "DescribeInstanceInformation": [r(["InstanceInformationList"], "ssm:managed-instance", "InstanceId", name="Name", arn_format="arn:{partition}:ssm:{region}:{account}:managed-instance/{InstanceId}")],
            "ListDocuments": [r(["DocumentIdentifiers"], "ssm:document", "Name", name="Name", arn_format="arn:{partition}:ssm:{region}:{account}:document/{Name}", tags="Tags")],
            "DescribeMaintenanceWindows": [r(["WindowIdentities"], "ssm:maintenancewindow", "WindowId", name="Name", arn_format="arn:{partition}:ssm:{region}:{account}:maintenancewindow/{WindowId}")],
            "ListAssociations": [r(["Associations"], "ssm:association", "AssociationId", name="AssociationName", arn_format="arn:{partition}:ssm:{region}:{account}:association/{AssociationId}")],
            "DescribeSessions": [r(["Sessions"], "ssm:session", "SessionId", arn_format="arn:{partition}:ssm:{region}:{account}:session/{SessionId}")],
        },
    },
    "dynamodb": {
        "resources": {
            "ListTables": [r(["TableNames"], "dynamodb:table", "TableName", name="TableName", arn_format="arn:{partition}:dynamodb:{region}:{account}:table/{TableName}", scalar_as="TableName")],
            "DescribeTable": [r(["Table"], "dynamodb:table", "TableName", name="TableName", arn="TableArn")],
            "ListGlobalTables": [r(["GlobalTables"], "dynamodb:global-table", "GlobalTableName", name="GlobalTableName", arn_format="arn:{partition}:dynamodb::{account}:global-table/{GlobalTableName}")],
        },
    },
}


def main():
    for service, spec in CONFIGS.items():
        path = os.path.join(MODELS_DIR, f"{service}.json")
        with open(path) as fp:
            model = json.load(fp)
        operations = model["operations"]
        changed = []
        for op, jq_program in (spec.get("jq") or {}).items():
            operations[op].setdefault("output", {})["jq"] = jq_program
            changed.append(op)
        for op in spec.get("remove_jq") or []:
            operations[op].get("output", {}).pop("jq", None)
        for op, resource_cfgs in (spec.get("resources") or {}).items():
            output = operations[op].setdefault("output", {})
            members = output.get("members") or {}
            for cfg in resource_cfgs:
                root = (cfg.get("path") or [None])[0]
                if members and root not in members:
                    print(
                        f"WARNING: {service}.{op}: path root {root!r} is not an "
                        f"output member ({sorted(members)})",
                        file=sys.stderr,
                    )
            output["resources"] = resource_cfgs
            changed.append(op)
        with open(path, "w") as fp:
            json.dump(model, fp, indent=2, sort_keys=True)
            fp.write("\n")
        print(f"{service}: updated {len(set(changed))} operations", file=sys.stderr)


if __name__ == "__main__":
    main()
