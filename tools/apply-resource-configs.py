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


def d(operation, id_field, param, kind="scalar", batch_size=None, scope=None):
    """A ``--describe`` pairing on a List operation: after listing, call
    ``operation`` for each result (``kind="scalar"``, one call per id — the
    common case, no batching support needed) or in ``batch_size``-sized
    batches (``kind="array"``, the target op takes an identifier list).
    ``id_field`` is the field on the *shaped* list record holding the
    identifier; ``param`` is the describe op's real input member name for
    it. ``scope`` names input params of the *list* call (e.g. a required
    parent id like ``RoleName``/``cluster``) that must be forwarded
    unchanged to every describe call, since the describe op needs the same
    scope but the list response never repeats it."""
    cfg = {"operation": operation, "id_field": id_field, "param": param, "kind": kind}
    if kind == "array":
        cfg["batch_size"] = batch_size or 100
    if scope:
        cfg["scope"] = scope
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
            # ACM tags require a separate ListTagsForCertificate call, not
            # part of DescribeCertificate's response — not chained here
            "DescribeCertificate": [r(["Certificate"], "acm:certificate", name="DomainName", arn="CertificateArn")],
        },
        "describe": {
            "ListCertificates": d("DescribeCertificate", id_field="Arn", param="CertificateArn"),
        },
    },
    "cloud9": {
        "resources": {
            "ListEnvironments": [r(["environmentIds"], "cloud9:environment", "environmentId", arn_format="arn:{partition}:cloud9:{region}:{account}:environment:{environmentId}", scalar_as="environmentId")],
            "DescribeEnvironments": [r(["environments"], "cloud9:environment", "id", name="name", arn="arn")],
        },
        "describe": {
            # AWS docs: max 25 environment ids per DescribeEnvironments call
            "ListEnvironments": d("DescribeEnvironments", id_field="Id", param="environmentIds",
                                  kind="array", batch_size=25),
        },
    },
    "cloudformation": {
        "resources": {
            "ListStacks": [r(["StackSummaries"], "cloudformation:stack", "StackName", name="StackName", arn="StackId")],
            "DescribeStacks": [r(["Stacks"], "cloudformation:stack", "StackName", name="StackName", arn="StackId", tags="Tags")],
            "ListTypeRegistrations": [r(["RegistrationTokenList"], "cloudformation:type-registration", "RegistrationToken", scalar_as="RegistrationToken")],
            "DescribeTypeRegistration": [r([], "cloudformation:type-registration", "RegistrationToken")],
        },
        "describe": {
            "ListTypeRegistrations": d("DescribeTypeRegistration", id_field="Id", param="RegistrationToken"),
        },
    },
    "cloudtrail": {
        "resources": {
            "ListTrails": [r(["Trails"], "cloudtrail:trail", "Name", name="Name", arn="TrailARN")],
            "DescribeTrails": [r(["trailList"], "cloudtrail:trail", "Name", name="Name", arn="TrailARN")],
        },
        "describe": {
            # trailNameList isn't formally required by DescribeTrails, but
            # passing the names we already listed is exactly the point here
            "ListTrails": d("DescribeTrails", id_field="Id", param="trailNameList",
                            kind="array", batch_size=10),
        },
    },
    "cloudwatch": {
        "resources": {
            "DescribeAlarms": [
                r(["MetricAlarms"], "cloudwatch:alarm", "AlarmName", name="AlarmName", arn="AlarmArn"),
                r(["CompositeAlarms"], "cloudwatch:alarm", "AlarmName", name="AlarmName", arn="AlarmArn"),
            ],
            "ListDashboards": [r(["DashboardEntries"], "cloudwatch:dashboard", "DashboardName", name="DashboardName")],
            "GetDashboard": [r([], "cloudwatch:dashboard", "DashboardName", name="DashboardName")],
        },
        "describe": {
            "ListDashboards": d("GetDashboard", id_field="Id", param="DashboardName"),
        },
    },
    "events": {
        "resources": {
            "ListRules": [r(["Rules"], "events:rule", "Name", name="Name", arn="Arn")],
            "ListPartnerEventSources": [r(["PartnerEventSources"], "events:partner-event-source", "Name", name="Name", arn="Arn")],
            "DescribePartnerEventSource": [r([], "events:partner-event-source", "Name", name="Name", arn="Arn")],
        },
        "describe": {
            "ListPartnerEventSources": d("DescribePartnerEventSource", id_field="Id", param="Name"),
        },
    },
    "logs": {
        "resources": {
            "DescribeLogGroups": [r(["logGroups"], "logs:log-group", "logGroupName", name="logGroupName", arn_format="arn:{partition}:logs:{region}:{account}:log-group:{logGroupName}")],
            "ListIntegrations": [r(["integrationSummaries"], "logs:integration", "integrationName", name="integrationName")],
            "GetIntegration": [r([], "logs:integration", "integrationName", name="integrationName")],
        },
        "describe": {
            "ListIntegrations": d("GetIntegration", id_field="Id", param="integrationName"),
        },
    },
    "sagemaker": {
        "resources": {
            "ListModels": [r(["Models"], "sagemaker:model", "ModelName", name="ModelName", arn="ModelArn")],
            "DescribeModel": [r([], "sagemaker:model", "ModelName", name="ModelName", arn="ModelArn")],
            "ListEndpointConfigs": [r(["EndpointConfigs"], "sagemaker:endpoint-config", "EndpointConfigName", name="EndpointConfigName", arn="EndpointConfigArn")],
            "DescribeEndpointConfig": [r([], "sagemaker:endpoint-config", "EndpointConfigName", name="EndpointConfigName", arn="EndpointConfigArn")],
            "ListActions": [r(["ActionSummaries"], "sagemaker:action", "ActionName", name="ActionName", arn="ActionArn")],
            "ListContexts": [r(["ContextSummaries"], "sagemaker:context", "ContextName", name="ContextName", arn="ContextArn")],
            "ListDataQualityJobDefinitions": [r(["JobDefinitionSummaries"], "sagemaker:data-quality-job-definition", "MonitoringJobDefinitionName", name="MonitoringJobDefinitionName", arn="MonitoringJobDefinitionArn")],
            "DescribeDataQualityJobDefinition": [r([], "sagemaker:data-quality-job-definition", "JobDefinitionName", name="JobDefinitionName", arn="JobDefinitionArn")],
            "ListDeviceFleets": [r(["DeviceFleetSummaries"], "sagemaker:device-fleet", "DeviceFleetName", name="DeviceFleetName", arn="DeviceFleetArn")],
            "DescribeDeviceFleet": [r([], "sagemaker:device-fleet", "DeviceFleetName", name="DeviceFleetName", arn="DeviceFleetArn")],
            "ListHumanTaskUis": [r(["HumanTaskUiSummaries"], "sagemaker:human-task-ui", "HumanTaskUiName", name="HumanTaskUiName", arn="HumanTaskUiArn")],
            "DescribeHumanTaskUi": [r([], "sagemaker:human-task-ui", "HumanTaskUiName", name="HumanTaskUiName", arn="HumanTaskUiArn")],
            "ListModelBiasJobDefinitions": [r(["JobDefinitionSummaries"], "sagemaker:model-bias-job-definition", "MonitoringJobDefinitionName", name="MonitoringJobDefinitionName", arn="MonitoringJobDefinitionArn")],
            "DescribeModelBiasJobDefinition": [r([], "sagemaker:model-bias-job-definition", "JobDefinitionName", name="JobDefinitionName", arn="JobDefinitionArn")],
            "ListModelExplainabilityJobDefinitions": [r(["JobDefinitionSummaries"], "sagemaker:model-explainability-job-definition", "MonitoringJobDefinitionName", name="MonitoringJobDefinitionName", arn="MonitoringJobDefinitionArn")],
            "DescribeModelExplainabilityJobDefinition": [r([], "sagemaker:model-explainability-job-definition", "JobDefinitionName", name="JobDefinitionName", arn="JobDefinitionArn")],
            "ListModelQualityJobDefinitions": [r(["JobDefinitionSummaries"], "sagemaker:model-quality-job-definition", "MonitoringJobDefinitionName", name="MonitoringJobDefinitionName", arn="MonitoringJobDefinitionArn")],
            "DescribeModelQualityJobDefinition": [r([], "sagemaker:model-quality-job-definition", "JobDefinitionName", name="JobDefinitionName", arn="JobDefinitionArn")],
            "ListNotebookInstanceLifecycleConfigs": [r(["NotebookInstanceLifecycleConfigs"], "sagemaker:notebook-instance-lifecycle-config", "NotebookInstanceLifecycleConfigName", name="NotebookInstanceLifecycleConfigName", arn="NotebookInstanceLifecycleConfigArn")],
            "DescribeNotebookInstanceLifecycleConfig": [r([], "sagemaker:notebook-instance-lifecycle-config", "NotebookInstanceLifecycleConfigName", name="NotebookInstanceLifecycleConfigName", arn="NotebookInstanceLifecycleConfigArn")],
        },
        "describe": {
            "ListModels": d("DescribeModel", id_field="Id", param="ModelName"),
            "ListEndpointConfigs": d("DescribeEndpointConfig", id_field="Id", param="EndpointConfigName"),
            "ListDataQualityJobDefinitions": d("DescribeDataQualityJobDefinition", id_field="Id", param="JobDefinitionName"),
            "ListDeviceFleets": d("DescribeDeviceFleet", id_field="Id", param="DeviceFleetName"),
            "ListHumanTaskUis": d("DescribeHumanTaskUi", id_field="Id", param="HumanTaskUiName"),
            "ListModelBiasJobDefinitions": d("DescribeModelBiasJobDefinition", id_field="Id", param="JobDefinitionName"),
            "ListModelExplainabilityJobDefinitions": d("DescribeModelExplainabilityJobDefinition", id_field="Id", param="JobDefinitionName"),
            "ListModelQualityJobDefinitions": d("DescribeModelQualityJobDefinition", id_field="Id", param="JobDefinitionName"),
            "ListNotebookInstanceLifecycleConfigs": d("DescribeNotebookInstanceLifecycleConfig", id_field="Id", param="NotebookInstanceLifecycleConfigName"),
        },
    },
    "servicediscovery": {
        "resources": {
            "ListNamespaces": [r(["Namespaces"], "servicediscovery:namespace", "Id", name="Name", arn="Arn")],
            "ListServices": [r(["Services"], "servicediscovery:service", "Id", name="Name", arn="Arn")],
            "ListInstances": [r(["Instances"], "servicediscovery:instance", "Id")],
            "GetInstance": [r(["Instance"], "servicediscovery:instance", "Id")],
            "ListOperations": [r(["Operations"], "servicediscovery:operation", "Id")],
            "GetOperation": [r(["Operation"], "servicediscovery:operation", "Id")],
        },
        "describe": {
            "ListInstances": d("GetInstance", id_field="Id", param="InstanceId", scope=["ServiceId"]),
            "ListOperations": d("GetOperation", id_field="Id", param="OperationId"),
        },
    },
    "transfer": {
        "resources": {
            "ListServers": [r(["Servers"], "transfer:server", "ServerId", name="ServerId", arn="Arn")],
            "ListAccesses": [r(["Accesses"], "transfer:access", "ExternalId")],
            "DescribeAccess": [r(["Access"], "transfer:access", "ExternalId")],
            "ListConnectors": [r(["Connectors"], "transfer:connector", "ConnectorId", arn="Arn")],
            "DescribeConnector": [r(["Connector"], "transfer:connector", "ConnectorId", arn="Arn")],
            "ListExecutions": [r(["Executions"], "transfer:execution", "ExecutionId")],
            "DescribeExecution": [r(["Execution"], "transfer:execution", "ExecutionId")],
            "ListProfiles": [r(["Profiles"], "transfer:profile", "ProfileId", arn="Arn")],
            "DescribeProfile": [r(["Profile"], "transfer:profile", "ProfileId", arn="Arn")],
            "ListSecurityPolicies": [r(["SecurityPolicyNames"], "transfer:security-policy", "SecurityPolicyName", scalar_as="SecurityPolicyName")],
            "DescribeSecurityPolicy": [r(["SecurityPolicy"], "transfer:security-policy", "SecurityPolicyName", name="SecurityPolicyName")],
            "ListWebApps": [r(["WebApps"], "transfer:web-app", "WebAppId", arn="Arn")],
            "DescribeWebApp": [r(["WebApp"], "transfer:web-app", "WebAppId", arn="Arn")],
            "ListWorkflows": [r(["Workflows"], "transfer:workflow", "WorkflowId", arn="Arn")],
            "DescribeWorkflow": [r(["Workflow"], "transfer:workflow", "WorkflowId", arn="Arn")],
        },
        "describe": {
            "ListAccesses": d("DescribeAccess", id_field="Id", param="ExternalId", scope=["ServerId"]),
            "ListConnectors": d("DescribeConnector", id_field="Id", param="ConnectorId"),
            "ListExecutions": d("DescribeExecution", id_field="Id", param="ExecutionId", scope=["WorkflowId"]),
            "ListProfiles": d("DescribeProfile", id_field="Id", param="ProfileId"),
            "ListSecurityPolicies": d("DescribeSecurityPolicy", id_field="Id", param="SecurityPolicyName"),
            "ListWebApps": d("DescribeWebApp", id_field="Id", param="WebAppId"),
            "ListWorkflows": d("DescribeWorkflow", id_field="Id", param="WorkflowId"),
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
            # GetOpenIDConnectProvider's response never echoes back the ARN
            # either — same id-fallback situation as GetSAMLProvider below
            "GetOpenIDConnectProvider": [r([], "iam:oidc-provider", tags="Tags")],
            # inline policies have no ARN (identified by RoleName+PolicyName only)
            "ListRolePolicies": [r(["PolicyNames"], "iam:role-policy", "PolicyName", scalar_as="PolicyName")],
            "GetRolePolicy": [r([], "iam:role-policy", "PolicyName", name="PolicyName")],
            "ListGroupPolicies": [r(["PolicyNames"], "iam:group-policy", "PolicyName", scalar_as="PolicyName")],
            "GetGroupPolicy": [r([], "iam:group-policy", "PolicyName", name="PolicyName")],
            "ListUserPolicies": [r(["PolicyNames"], "iam:user-policy", "PolicyName", scalar_as="PolicyName")],
            "GetUserPolicy": [r([], "iam:user-policy", "PolicyName", name="PolicyName")],
            "ListMFADevices": [r(["MFADevices"], "iam:mfa-device", "SerialNumber", name="SerialNumber")],
            "GetMFADevice": [r([], "iam:mfa-device", "SerialNumber", name="SerialNumber")],
            "ListPolicyVersions": [r(["Versions"], "iam:policy-version", "VersionId", name="VersionId")],
            "GetPolicyVersion": [r(["PolicyVersion"], "iam:policy-version", "VersionId", name="VersionId")],
            # GetSAMLProvider's response never echoes back the ARN you asked
            # for — run_describe_chain's scalar-kind id fallback fills it in
            "ListSAMLProviders": [r(["SAMLProviderList"], "iam:saml-provider", arn="Arn")],
            "GetSAMLProvider": [r([], "iam:saml-provider", tags="Tags")],
            "ListAttachedRolePolicies": [r(["AttachedPolicies"], "iam:attached-role-policy", "PolicyName", name="PolicyName", arn="PolicyArn")],
            "ListAttachedGroupPolicies": [r(["AttachedPolicies"], "iam:attached-group-policy", "PolicyName", name="PolicyName", arn="PolicyArn")],
            "ListAttachedUserPolicies": [r(["AttachedPolicies"], "iam:attached-user-policy", "PolicyName", name="PolicyName", arn="PolicyArn")],
        },
        "describe": {
            "ListRolePolicies": d("GetRolePolicy", id_field="Id", param="PolicyName", scope=["RoleName"]),
            "ListGroupPolicies": d("GetGroupPolicy", id_field="Id", param="PolicyName", scope=["GroupName"]),
            "ListUserPolicies": d("GetUserPolicy", id_field="Id", param="PolicyName", scope=["UserName"]),
            "ListMFADevices": d("GetMFADevice", id_field="Id", param="SerialNumber", scope=["UserName"]),
            "ListPolicyVersions": d("GetPolicyVersion", id_field="Id", param="VersionId", scope=["PolicyArn"]),
            "ListSAMLProviders": d("GetSAMLProvider", id_field="Arn", param="SAMLProviderArn"),
            "ListOpenIDConnectProviders": d("GetOpenIDConnectProvider", id_field="Arn",
                                            param="OpenIDConnectProviderArn"),
            # ListSSHPublicKeys skipped: GetSSHPublicKey needs a required
            # Encoding param that isn't derivable from the list response
            # (found this ListOpenIDConnectProviders was wrongly grouped
            # with it before — it has no such gap, see AGENTS.md)
        },
    },
    "route53": {
        "jq": {
            "ListHostedZones": ROUTE53_LIST_HOSTED_ZONES_JQ,
        },
        "resources": {
            "ListResourceRecordSets": [r(["ResourceRecordSets"], "route53:rrset", "Name", name="Name")],
            "ListHealthChecks": [r(["HealthChecks"], "route53:healthcheck", "Id", arn_format="arn:{partition}:route53:::healthcheck/{Id}")],
            "ListQueryLoggingConfigs": [r(["QueryLoggingConfigs"], "route53:query-logging-config", "Id")],
            "GetQueryLoggingConfig": [r(["QueryLoggingConfig"], "route53:query-logging-config", "Id")],
            "ListReusableDelegationSets": [r(["DelegationSets"], "route53:reusable-delegation-set", "Id")],
            "GetReusableDelegationSet": [r(["DelegationSet"], "route53:reusable-delegation-set", "Id")],
        },
        "describe": {
            "ListQueryLoggingConfigs": d("GetQueryLoggingConfig", id_field="Id", param="Id"),
            "ListReusableDelegationSets": d("GetReusableDelegationSet", id_field="Id", param="Id"),
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
            "ListContainerInstances": [r(["containerInstanceArns"], "ecs:container-instance", arn="containerInstanceArn", scalar_as="containerInstanceArn")],
            "DescribeContainerInstances": [r(["containerInstances"], "ecs:container-instance", arn="containerInstanceArn", tags="tags")],
        },
        "describe": {
            "ListClusters": d("DescribeClusters", id_field="Id", param="clusters",
                              kind="array", batch_size=100),
            "ListTasks": d("DescribeTasks", id_field="Id", param="tasks",
                           kind="array", batch_size=100, scope=["cluster"]),
            "ListServices": d("DescribeServices", id_field="Id", param="services",
                              kind="array", batch_size=10, scope=["cluster"]),
            "ListContainerInstances": d("DescribeContainerInstances", id_field="Id",
                                        param="containerInstances", kind="array",
                                        batch_size=100, scope=["cluster"]),
            # task-def ARNs' id-from-arn-tail would be just the revision
            # number ("3"), not usable as DescribeTaskDefinition's identifier
            # (needs "family:revision" or the full ARN) — use Arn, not Id
            "ListTaskDefinitions": d("DescribeTaskDefinition", id_field="Arn", param="taskDefinition"),
        },
    },
    "athena": {
        "resources": {
            "ListWorkGroups": [r(["WorkGroups"], "athena:workgroup", "Name", name="Name", arn_format="arn:{partition}:athena:{region}:{account}:workgroup/{Name}")],
            "ListCalculationExecutions": [r(["Calculations"], "athena:calculation-execution", "CalculationExecutionId")],
            "GetCalculationExecution": [r([], "athena:calculation-execution", "CalculationExecutionId")],
            "ListDatabases": [r(["DatabaseList"], "athena:database", "Name", name="Name")],
            "GetDatabase": [r(["Database"], "athena:database", "Name", name="Name")],
            "ListNamedQueries": [r(["NamedQueryIds"], "athena:named-query", "NamedQueryId", scalar_as="NamedQueryId")],
            "BatchGetNamedQuery": [r(["NamedQueries"], "athena:named-query", "NamedQueryId", name="Name")],
            "ListPreparedStatements": [r(["PreparedStatements"], "athena:prepared-statement", "StatementName", name="StatementName")],
            "BatchGetPreparedStatement": [r(["PreparedStatements"], "athena:prepared-statement", "StatementName", name="StatementName")],
            "ListQueryExecutions": [r(["QueryExecutionIds"], "athena:query-execution", "QueryExecutionId", scalar_as="QueryExecutionId")],
            "BatchGetQueryExecution": [r(["QueryExecutions"], "athena:query-execution", "QueryExecutionId")],
        },
        "describe": {
            "ListCalculationExecutions": d("GetCalculationExecution", id_field="Id", param="CalculationExecutionId"),
            "ListDatabases": d("GetDatabase", id_field="Id", param="DatabaseName", scope=["CatalogName"]),
            "ListNamedQueries": d("BatchGetNamedQuery", id_field="Id", param="NamedQueryIds",
                                  kind="array", batch_size=50),
            "ListPreparedStatements": d("BatchGetPreparedStatement", id_field="Id", param="PreparedStatementNames",
                                        kind="array", batch_size=50, scope=["WorkGroup"]),
            "ListQueryExecutions": d("BatchGetQueryExecution", id_field="Id", param="QueryExecutionIds",
                                     kind="array", batch_size=50),
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
            "GetDistribution": [r(["Distribution"], "cloudfront:distribution", "Id", name="DomainName", arn="ARN")],
            "ListStreamingDistributions": [r(["StreamingDistributionList", "Items"], "cloudfront:streaming-distribution", "Id", name="DomainName", arn="ARN")],
            "ListInvalidations": [r(["InvalidationList", "Items"], "cloudfront:invalidation", "Id")],
            "ListRealtimeLogConfigs": [r(["RealtimeLogConfigs", "Items"], "cloudfront:realtime-log-config", "Name", name="Name", arn="ARN")],
            "ListFunctions": [r(["FunctionList", "Items"], "cloudfront:function", "Name", name="Name")],
            "ListOriginAccessControls": [r(["OriginAccessControlList", "Items"], "cloudfront:origin-access-control", "Id", name="Name")],
            "ListPublicKeys": [r(["PublicKeyList", "Items"], "cloudfront:public-key", "Id", name="Name")],
            "ListFieldLevelEncryptionConfigs": [r(["FieldLevelEncryptionList", "Items"], "cloudfront:field-level-encryption-config", "Id")],
            "ListFieldLevelEncryptionProfiles": [r(["FieldLevelEncryptionProfileList", "Items"], "cloudfront:field-level-encryption-profile", "Id", name="Name")],
            # These four wrap the actual resource one level deeper
            # (Items[].CachePolicy, Items[].KeyGroup, ...) — the nested
            # object already carries everything GetCachePolicy/GetKeyGroup/
            # etc. would (verified: same members), so listing alone is
            # complete; no separate Get curation or --describe pairing
            # needed, same as iam.ListRoles already returning full detail.
            # None of the four have an Arn/ARN field at any level, and none
            # accept a CloudFront-namespace ARN via ListTagsForResource
            # (verified live: "InvalidArgument ... resource type: cache-policy
            # is invalid") — Arn is left blank rather than guessed.
            "ListCachePolicies": [r(["CachePolicyList", "Items", "CachePolicy"], "cloudfront:cache-policy", "Id")],
            "ListOriginRequestPolicies": [r(["OriginRequestPolicyList", "Items", "OriginRequestPolicy"], "cloudfront:origin-request-policy", "Id")],
            "ListResponseHeadersPolicies": [r(["ResponseHeadersPolicyList", "Items", "ResponseHeadersPolicy"], "cloudfront:response-headers-policy", "Id")],
            "ListKeyGroups": [r(["KeyGroupList", "Items", "KeyGroup"], "cloudfront:key-group", "Id")],
            "ListContinuousDeploymentPolicies": [r(["ContinuousDeploymentPolicyList", "Items", "ContinuousDeploymentPolicy"], "cloudfront:continuous-deployment-policy", "Id")],
        },
        "describe": {
            "ListDistributions": d("GetDistribution", id_field="Id", param="Id"),
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
    "elasticache": {
        "resources": {
            "DescribeCacheClusters": [r(["CacheClusters"], "elasticache:cache-cluster", "CacheClusterId", arn="ARN")],
            "DescribeReplicationGroups": [r(["ReplicationGroups"], "elasticache:replication-group", "ReplicationGroupId", arn="ARN")],
            "DescribeCacheSubnetGroups": [r(["CacheSubnetGroups"], "elasticache:subnet-group", "CacheSubnetGroupName", name="CacheSubnetGroupName", arn="ARN")],
            "DescribeCacheParameterGroups": [r(["CacheParameterGroups"], "elasticache:parameter-group", "CacheParameterGroupName", name="CacheParameterGroupName", arn="ARN")],
            "DescribeSnapshots": [r(["Snapshots"], "elasticache:snapshot", "SnapshotName", name="SnapshotName", arn="ARN")],
            "DescribeServerlessCaches": [r(["ServerlessCaches"], "elasticache:serverless-cache", "ServerlessCacheName", name="ServerlessCacheName", arn="ARN")],
            "DescribeUsers": [r(["Users"], "elasticache:user", "UserId", name="UserName", arn="ARN")],
            "DescribeUserGroups": [r(["UserGroups"], "elasticache:user-group", "UserGroupId", arn="ARN")],
        },
    },
    "eks": {
        "resources": {
            "ListClusters": [r(["clusters"], "eks:cluster", "name", name="name", arn_format="arn:{partition}:eks:{region}:{account}:cluster/{name}", scalar_as="name")],
            "DescribeCluster": [r(["cluster"], "eks:cluster", "name", name="name", arn="arn", tags="tags")],
            "ListAccessEntries": [r(["accessEntries"], "eks:access-entry", arn="accessEntryArn", scalar_as="accessEntryArn")],
            "DescribeAccessEntry": [r(["accessEntry"], "eks:access-entry", arn="accessEntryArn", tags="tags")],
            "ListAddons": [r(["addons"], "eks:addon", "addonName", name="addonName", scalar_as="addonName")],
            "DescribeAddon": [r(["addon"], "eks:addon", "addonName", name="addonName", arn="addonArn", tags="tags")],
            "ListNodegroups": [r(["nodegroups"], "eks:nodegroup", "nodegroupName", name="nodegroupName", scalar_as="nodegroupName")],
            "DescribeNodegroup": [r(["nodegroup"], "eks:nodegroup", "nodegroupName", name="nodegroupName", arn="nodegroupArn", tags="tags")],
            "ListFargateProfiles": [r(["fargateProfileNames"], "eks:fargate-profile", "fargateProfileName", scalar_as="fargateProfileName")],
            "DescribeFargateProfile": [r(["fargateProfile"], "eks:fargate-profile", "fargateProfileName", name="fargateProfileName", arn="fargateProfileArn", tags="tags")],
            "ListUpdates": [r(["updateIds"], "eks:update", "updateId", scalar_as="updateId")],
            "DescribeUpdate": [r(["update"], "eks:update", "id")],
        },
        "describe": {
            "ListClusters": d("DescribeCluster", id_field="Id", param="name"),
            "ListAccessEntries": d("DescribeAccessEntry", id_field="Arn", param="principalArn", scope=["clusterName"]),
            "ListAddons": d("DescribeAddon", id_field="Id", param="addonName", scope=["clusterName"]),
            "ListNodegroups": d("DescribeNodegroup", id_field="Id", param="nodegroupName", scope=["clusterName"]),
            "ListFargateProfiles": d("DescribeFargateProfile", id_field="Id", param="fargateProfileName", scope=["clusterName"]),
            "ListUpdates": d("DescribeUpdate", id_field="Id", param="updateId", scope=["name"]),
            # ListIdentityProviderConfigs skipped: DescribeIdentityProviderConfig
            # needs a {type, name} structure, not a scalar id — not yet supported
        },
    },
    "kms": {
        "resources": {
            "ListKeys": [r(["Keys"], "kms:key", "KeyId", arn="KeyArn")],
            "ListAliases": [r(["Aliases"], "kms:alias", "AliasName", name="AliasName", arn="AliasArn")],
            "ListKeyPolicies": [r(["PolicyNames"], "kms:key-policy", "PolicyName", scalar_as="PolicyName")],
            "GetKeyPolicy": [r([], "kms:key-policy", "PolicyName", name="PolicyName")],
            "DescribeKey": [r(["KeyMetadata"], "kms:key", "KeyId", arn="Arn")],
        },
        "describe": {
            "ListKeys": d("DescribeKey", id_field="Id", param="KeyId"),
            "ListKeyPolicies": d("GetKeyPolicy", id_field="Id", param="PolicyName", scope=["KeyId"]),
        },
    },
    "opensearch": {
        "resources": {
            "ListDomainNames": [r(["DomainNames"], "es:domain", "DomainName", name="DomainName", arn_format="arn:{partition}:es:{region}:{account}:domain/{DomainName}")],
            "DescribeDomains": [r(["DomainStatusList"], "es:domain", "DomainName", name="DomainName", arn="ARN")],
            "ListDataSources": [r(["DataSources"], "es:data-source", "Name", name="Name")],
            "GetDataSource": [r([], "es:data-source", "Name", name="Name")],
            "ListVpcEndpoints": [r(["VpcEndpointSummaryList"], "es:vpc-endpoint", "VpcEndpointId", arn="DomainArn")],
        },
        "describe": {
            # DescribeDomains takes DomainNames (array) not a scalar name;
            # AWS docs cap it at 5 per call, not stated in the model
            "ListDomainNames": d("DescribeDomains", id_field="Id", param="DomainNames",
                                 kind="array", batch_size=5),
            "ListDataSources": d("GetDataSource", id_field="Id", param="Name", scope=["DomainName"]),
            # conservative batch size — OpenSearch VPC endpoints per domain
            # are typically few, and the real documented max isn't in the model
            "ListVpcEndpoints": d("DescribeVpcEndpoints", id_field="Id", param="VpcEndpointIds",
                                  kind="array", batch_size=5),
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
            "ListConfigurationSets": [r(["ConfigurationSets"], "ses:configuration-set", "Name", name="Name")],
            "DescribeConfigurationSet": [r(["ConfigurationSet"], "ses:configuration-set", "Name", name="Name")],
            "ListReceiptRuleSets": [r(["RuleSets"], "ses:receipt-rule-set", "Name", name="Name")],
            "DescribeReceiptRuleSet": [r(["Metadata"], "ses:receipt-rule-set", "Name", name="Name")],
            "ListIdentityPolicies": [r(["PolicyNames"], "ses:identity-policy", "PolicyName", scalar_as="PolicyName")],
        },
        "describe": {
            "ListConfigurationSets": d("DescribeConfigurationSet", id_field="Id", param="ConfigurationSetName"),
            "ListReceiptRuleSets": d("DescribeReceiptRuleSet", id_field="Id", param="RuleSetName"),
            # conservative batch size — SES per-identity policy counts are
            # typically small, and the real documented max isn't in the model
            "ListIdentityPolicies": d("GetIdentityPolicies", id_field="Id", param="PolicyNames",
                                      kind="array", batch_size=10, scope=["Identity"]),
        },
    },
    "sns": {
        "resources": {
            "ListTopics": [r(["Topics"], "sns:topic", arn="TopicArn")],
            "ListSubscriptions": [r(["Subscriptions"], "sns:subscription", arn="SubscriptionArn")],
            "ListSubscriptionsByTopic": [r(["Subscriptions"], "sns:subscription", arn="SubscriptionArn")],
            "ListPlatformApplications": [r(["PlatformApplications"], "sns:platform-application", arn="PlatformApplicationArn")],
            # response is {"Attributes": {flat map, includes TopicArn}} — the
            # map IS the resource, path descends into it directly (no list)
            "GetTopicAttributes": [r(["Attributes"], "sns:topic-attributes", arn="TopicArn")],
        },
    },
    "sqs": {
        "jq": {
            "ListQueues": SQS_LIST_QUEUES_JQ,
        },
        "resources": {
            # same shape as sns GetTopicAttributes — a flat attribute map,
            # not a list; QueueArn lives inside it (only present when
            # --attribute-names All was passed — the API defaults to none)
            "GetQueueAttributes": [r(["Attributes"], "sqs:queue-attributes", arn="QueueArn")],
        },
    },
    "wafv2": {
        "resources": {
            "ListWebACLs": [r(["WebACLs"], "wafv2:webacl", "Id", name="Name", arn="ARN")],
            "GetWebACL": [r(["WebACL"], "wafv2:webacl", "Id", name="Name", arn="ARN")],
            "ListIPSets": [r(["IPSets"], "wafv2:ipset", "Id", name="Name", arn="ARN")],
            "GetIPSet": [r(["IPSet"], "wafv2:ipset", "Id", name="Name", arn="ARN")],
            "ListRuleGroups": [r(["RuleGroups"], "wafv2:rulegroup", "Id", name="Name", arn="ARN")],
            "GetRuleGroup": [r(["RuleGroup"], "wafv2:rulegroup", "Id", name="Name", arn="ARN")],
        },
        # Get{WebACL,IPSet,RuleGroup} need Name+Scope+Id together, not a
        # single scalar id — doesn't fit --describe's one-id_field model.
        # --stamp-session already puts Scope on every list record (it was
        # the list call's own request param), so a plain jq chain covering
        # {Name,Id,Scope} works without inventing a multi-field describe.
    },
    "redshift": {
        "resources": {
            "DescribeClusters": [r(["Clusters"], "redshift:cluster", "ClusterIdentifier", arn="ClusterNamespaceArn", tags="Tags")],
            "DescribeClusterSubnetGroups": [r(["ClusterSubnetGroups"], "redshift:subnet-group", "ClusterSubnetGroupName", name="ClusterSubnetGroupName", arn_format="arn:{partition}:redshift:{region}:{account}:subnetgroup:{ClusterSubnetGroupName}", tags="Tags")],
            "DescribeClusterParameterGroups": [r(["ParameterGroups"], "redshift:parameter-group", "ParameterGroupName", name="ParameterGroupName", arn_format="arn:{partition}:redshift:{region}:{account}:parametergroup:{ParameterGroupName}", tags="Tags")],
            "DescribeClusterSecurityGroups": [r(["ClusterSecurityGroups"], "redshift:security-group", "ClusterSecurityGroupName", name="ClusterSecurityGroupName", arn_format="arn:{partition}:redshift:{region}:{account}:securitygroup:{ClusterSecurityGroupName}", tags="Tags")],
            "DescribeClusterSnapshots": [r(["Snapshots"], "redshift:snapshot", "SnapshotIdentifier", arn="SnapshotArn", tags="Tags")],
            "DescribeEventSubscriptions": [r(["EventSubscriptionsList"], "redshift:event-subscription", "CustSubscriptionId", name="CustSubscriptionId", arn_format="arn:{partition}:redshift:{region}:{account}:eventsubscription:{CustSubscriptionId}", tags="Tags")],
            "DescribeHsmClientCertificates": [r(["HsmClientCertificates"], "redshift:hsm-client-certificate", "HsmClientCertificateIdentifier", name="HsmClientCertificateIdentifier", arn_format="arn:{partition}:redshift:{region}:{account}:hsmclientcertificate:{HsmClientCertificateIdentifier}", tags="Tags")],
            "DescribeHsmConfigurations": [r(["HsmConfigurations"], "redshift:hsm-configuration", "HsmConfigurationIdentifier", name="HsmConfigurationIdentifier", arn_format="arn:{partition}:redshift:{region}:{account}:hsmconfiguration:{HsmConfigurationIdentifier}", tags="Tags")],
            "DescribeSnapshotSchedules": [r(["SnapshotSchedules"], "redshift:snapshot-schedule", "ScheduleIdentifier", name="ScheduleIdentifier", tags="Tags")],
            "DescribeUsageLimits": [r(["UsageLimits"], "redshift:usage-limit", "UsageLimitId", tags="Tags")],
        },
    },
    "workspaces": {
        "resources": {
            "DescribeWorkspaces": [r(["Workspaces"], "workspaces:workspace", "WorkspaceId", name="WorkspaceName", arn_format="arn:{partition}:workspaces:{region}:{account}:workspace/{WorkspaceId}")],
            "DescribeWorkspaceDirectories": [r(["Directories"], "workspaces:directory", "DirectoryId", name="DirectoryName", arn_format="arn:{partition}:workspaces:{region}:{account}:directory/{DirectoryId}")],
            "DescribeWorkspaceBundles": [r(["Bundles"], "workspaces:bundle", "BundleId", name="Name", arn_format="arn:{partition}:workspaces:{region}:{account}:workspacebundle/{BundleId}")],
            "DescribeWorkspaceImages": [r(["Images"], "workspaces:image", "ImageId", name="Name", arn_format="arn:{partition}:workspaces:{region}:{account}:workspaceimage/{ImageId}")],
            "DescribeConnectionAliases": [r(["ConnectionAliases"], "workspaces:connection-alias", "AliasId", arn_format="arn:{partition}:workspaces:{region}:{account}:connectionalias/{AliasId}")],
            # Tags aren't inline on any Describe* response — DescribeTags
            # takes a single ResourceId (any of the ARNs above) and returns
            # a bare TagList, so it's chained via --describe rather than
            # folded into the list resources above.
            "DescribeTags": [r(["TagList"], "workspaces:tags", "Key", name="Key")],
        },
        "describe": {
            "DescribeWorkspaces": d("DescribeTags", id_field="Id", param="ResourceId"),
            "DescribeWorkspaceDirectories": d("DescribeTags", id_field="Id", param="ResourceId"),
            "DescribeWorkspaceBundles": d("DescribeTags", id_field="Id", param="ResourceId"),
            "DescribeWorkspaceImages": d("DescribeTags", id_field="Id", param="ResourceId"),
            "DescribeConnectionAliases": d("DescribeTags", id_field="Id", param="ResourceId"),
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
            "ListBucketAnalyticsConfigurations": [r(["AnalyticsConfigurationList"], "s3:analytics-configuration", "Id")],
            "GetBucketAnalyticsConfiguration": [r(["AnalyticsConfiguration"], "s3:analytics-configuration", "Id")],
            "ListBucketIntelligentTieringConfigurations": [r(["IntelligentTieringConfigurationList"], "s3:intelligent-tiering-configuration", "Id")],
            "GetBucketIntelligentTieringConfiguration": [r(["IntelligentTieringConfiguration"], "s3:intelligent-tiering-configuration", "Id")],
            "ListBucketMetricsConfigurations": [r(["MetricsConfigurationList"], "s3:metrics-configuration", "Id")],
            "GetBucketMetricsConfiguration": [r(["MetricsConfiguration"], "s3:metrics-configuration", "Id")],
            # None of these echo Bucket back in their response (verified via
            # boto3 shape introspection) — unlike ListMultipartUploads above,
            # there's no root_Bucket to build an Id/Arn from. --stamp-session
            # carries the Bucket request param onto the record instead
            # (inventory.sh's jq chain is what supplies it, same as ecs's
            # `cluster` scope and wafv2's `Scope`); Id/Name/Arn stay blank
            # here rather than guessed.
            "GetBucketVersioning": [r([], "s3:bucket-versioning")],
            "GetBucketEncryption": [r([], "s3:bucket-encryption")],
            "GetBucketPolicy": [r([], "s3:bucket-policy")],
            "GetBucketPolicyStatus": [r([], "s3:bucket-policy-status")],
            "GetBucketLifecycleConfiguration": [r([], "s3:bucket-lifecycle")],
            "GetBucketCors": [r([], "s3:bucket-cors")],
            "GetBucketNotificationConfiguration": [r([], "s3:bucket-notification")],
            "GetBucketReplication": [r([], "s3:bucket-replication")],
            "GetBucketLogging": [r([], "s3:bucket-logging")],
            "GetBucketAccelerateConfiguration": [r([], "s3:bucket-accelerate")],
            "GetBucketOwnershipControls": [r([], "s3:bucket-ownership-controls")],
            "GetBucketTagging": [r([], "s3:bucket-tagging", tags="TagSet")],
            "GetBucketLocation": [r([], "s3:bucket-location")],
            "GetBucketRequestPayment": [r([], "s3:bucket-request-payment")],
            "GetBucketAcl": [r([], "s3:bucket-acl")],
            "GetBucketWebsite": [r([], "s3:bucket-website")],
            "GetPublicAccessBlock": [r([], "s3:bucket-public-access-block")],
            "GetObjectLockConfiguration": [r([], "s3:bucket-object-lock")],
        },
        "describe": {
            "ListBucketAnalyticsConfigurations": d("GetBucketAnalyticsConfiguration", id_field="Id", param="Id", scope=["Bucket"]),
            "ListBucketIntelligentTieringConfigurations": d("GetBucketIntelligentTieringConfiguration", id_field="Id", param="Id", scope=["Bucket"]),
            "ListBucketMetricsConfigurations": d("GetBucketMetricsConfiguration", id_field="Id", param="Id", scope=["Bucket"]),
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
            "DescribeGlobalTable": [r(["GlobalTableDescription"], "dynamodb:global-table", "GlobalTableName", name="GlobalTableName", arn="GlobalTableArn")],
            "ListContributorInsights": [r(["ContributorInsightsSummaries"], "dynamodb:contributor-insights", "TableName", name="TableName")],
            "DescribeContributorInsights": [r([], "dynamodb:contributor-insights", "TableName", name="TableName")],
            "ListExports": [r(["ExportSummaries"], "dynamodb:export", arn="ExportArn")],
            "DescribeExport": [r(["ExportDescription"], "dynamodb:export", arn="ExportArn")],
        },
        "describe": {
            "ListTables": d("DescribeTable", id_field="Id", param="TableName"),
            "ListGlobalTables": d("DescribeGlobalTable", id_field="Id", param="GlobalTableName"),
            "ListContributorInsights": d("DescribeContributorInsights", id_field="Id", param="TableName"),
            "ListExports": d("DescribeExport", id_field="Arn", param="ExportArn"),
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
                if not cfg.get("path"):
                    continue  # deliberate: the whole response is the resource
                root = cfg["path"][0]
                if members and root not in members:
                    print(
                        f"WARNING: {service}.{op}: path root {root!r} is not an "
                        f"output member ({sorted(members)})",
                        file=sys.stderr,
                    )
            output["resources"] = resource_cfgs
            changed.append(op)
        for op, describe_cfg in (spec.get("describe") or {}).items():
            target = describe_cfg["operation"]
            if target not in operations:
                print(f"WARNING: {service}.{op}: describe target {target!r} is not "
                      f"a known operation", file=sys.stderr)
            else:
                target_members = (operations[target].get("input") or {}).get("members") or {}
                if target_members and describe_cfg["param"] not in target_members:
                    print(
                        f"WARNING: {service}.{op}: describe param "
                        f"{describe_cfg['param']!r} is not a {target!r} input "
                        f"member ({sorted(target_members)})",
                        file=sys.stderr,
                    )
            operations[op].setdefault("output", {})["describe"] = describe_cfg
            changed.append(op)
        with open(path, "w") as fp:
            json.dump(model, fp, indent=2, sort_keys=True)
            fp.write("\n")
        print(f"{service}: updated {len(set(changed))} operations", file=sys.stderr)


if __name__ == "__main__":
    main()
