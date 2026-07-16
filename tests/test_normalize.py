from ajl.normalize import (
    iter_default_resources,
    iter_path,
    normalize_resource,
    tags_to_map,
)

CONTEXT = {"partition": "aws", "region": "us-east-1", "account": "123456789012"}


def test_tags_to_map_from_kv_list():
    assert tags_to_map([{"Key": "Name", "Value": "web"}, {"Key": "Env", "Value": "prod"}]) == {
        "Name": "web",
        "Env": "prod",
    }


def test_tags_to_map_passthrough_and_empty():
    assert tags_to_map({"Name": "web"}) == {"Name": "web"}
    assert tags_to_map(None) == {}
    assert tags_to_map([]) == {}


def test_iter_path_nested_lists():
    page = {"Reservations": [{"Instances": [{"Id": 1}, {"Id": 2}]}, {"Instances": [{"Id": 3}]}]}
    assert list(iter_path(page, ["Reservations", "Instances"])) == [{"Id": 1}, {"Id": 2}, {"Id": 3}]


def test_iter_path_scalar_list_and_single_dict():
    assert list(iter_path({"TableNames": ["a", "b"]}, ["TableNames"])) == ["a", "b"]
    assert list(iter_path({"Table": {"TableName": "a"}}, ["Table"])) == [{"TableName": "a"}]
    assert list(iter_path({}, ["Missing"])) == []


def test_normalize_resource_arn_format_and_tags():
    cfg = {
        "path": ["SecurityGroups"],
        "type": "ec2:security-group",
        "id": "GroupId",
        "name": "GroupName",
        "arn_format": "arn:{partition}:ec2:{region}:{OwnerId}:security-group/{GroupId}",
        "tags": "Tags",
    }
    item = {
        "GroupId": "sg-123",
        "GroupName": "web",
        "OwnerId": "111122223333",
        "Tags": [{"Key": "Env", "Value": "prod"}],
    }
    result = normalize_resource(item, cfg, CONTEXT, {})
    assert result["Type"] == "ec2:security-group"
    assert result["Id"] == "sg-123"
    assert result["Name"] == "web"
    assert result["Arn"] == "arn:aws:ec2:us-east-1:111122223333:security-group/sg-123"
    assert result["Tags"] == {"Env": "prod"}
    # originals are kept (duplicated), tag list replaced by the map
    assert result["GroupId"] == "sg-123"
    assert result["GroupName"] == "web"
    assert list(result)[:5] == ["Type", "Id", "Name", "Arn", "Tags"]


def test_normalize_resource_name_falls_back_to_tag_name():
    cfg = {"type": "ec2:vpc", "id": "VpcId", "arn_format": "arn:{partition}:ec2:{region}:{OwnerId}:vpc/{VpcId}", "tags": "Tags"}
    item = {"VpcId": "vpc-1", "OwnerId": "1", "Tags": [{"Key": "Name", "Value": "main-vpc"}]}
    assert normalize_resource(item, cfg, CONTEXT, {})["Name"] == "main-vpc"


def test_normalize_resource_missing_arn_var_gives_empty_arn():
    cfg = {"type": "ec2:vpc", "id": "VpcId", "arn_format": "arn:{partition}:ec2:{region}:{OwnerId}:vpc/{VpcId}"}
    assert normalize_resource({"VpcId": "vpc-1"}, cfg, CONTEXT, {})["Arn"] == ""


def test_normalize_resource_arn_field_and_id_from_arn():
    cfg = {"type": "rds:db", "arn": "DBInstanceArn"}
    item = {"DBInstanceArn": "arn:aws:rds:us-east-1:1:db:mydb"}
    result = normalize_resource(item, cfg, CONTEXT, {})
    assert result["Arn"] == item["DBInstanceArn"]
    assert result["Id"] == "mydb"
    assert result["DBInstanceArn"] == item["DBInstanceArn"]


def test_normalize_resource_uppercase_arn_field_is_replaced():
    cfg = {"type": "ssm:parameter", "id": "Name", "name": "Name", "arn": "ARN"}
    result = normalize_resource({"Name": "/app/db", "ARN": "arn:aws:ssm:us-east-1:1:parameter/app/db"}, cfg, CONTEXT, {})
    assert result["Arn"] == "arn:aws:ssm:us-east-1:1:parameter/app/db"
    assert "ARN" not in result


def test_normalize_resource_scalar_and_root_vars():
    cfg = {
        "type": "dynamodb:table",
        "id": "TableName",
        "name": "TableName",
        "scalar_as": "TableName",
        "arn_format": "arn:{partition}:dynamodb:{region}:{account}:table/{TableName}",
    }
    result = normalize_resource("users", cfg, CONTEXT, {})
    assert result["Id"] == "users"
    assert result["Arn"] == "arn:aws:dynamodb:us-east-1:123456789012:table/users"

    cfg = {"type": "s3:object", "id": "Key", "name": "Key", "arn_format": "arn:{partition}:s3:::{root_Name}/{Key}"}
    result = normalize_resource({"Key": "a/b.txt"}, cfg, CONTEXT, {"Name": "my-bucket"})
    assert result["Arn"] == "arn:aws:s3:::my-bucket/a/b.txt"


def test_iter_default_resources():
    assert list(iter_default_resources({"Things": [1, 2], "NextToken": "x"})) == [1, 2]
    page = {"A": [1], "B": [2]}
    assert list(iter_default_resources(page)) == [page]


def test_tags_to_map_lowercase_ecs_style():
    assert tags_to_map([{"key": "Name", "value": "svc"}]) == {"Name": "svc"}


def test_normalize_resource_auto_detects_tags_field():
    cfg = {"type": "x:y", "id": "Id2"}
    item = {"Id2": "a", "Tags": [{"Key": "Env", "Value": "dev"}]}
    result = normalize_resource(item, cfg, CONTEXT, {})
    assert result["Tags"] == {"Env": "dev"}


def test_normalize_resource_preserves_colliding_fields():
    # e.g. a VPN gateway has its own Type field ("ipsec.1")
    cfg = {"type": "ec2:vpn-gateway", "id": "VpnGatewayId"}
    item = {"VpnGatewayId": "vgw-1", "Type": "ipsec.1"}
    result = normalize_resource(item, cfg, CONTEXT, {})
    assert result["Type"] == "ec2:vpn-gateway"
    assert result["OriginalType"] == "ipsec.1"


def test_uri_format_adds_uri_after_tags():
    cfg = {
        "path": ["Buckets"],
        "type": "s3:bucket",
        "id": "Name",
        "name": "Name",
        "arn_format": "arn:{partition}:s3:::{Name}",
        "uri_format": "s3://{Name}",
    }
    record = normalize_resource({"Name": "my-bucket"}, cfg, {"partition": "aws"}, {})
    assert record["Uri"] == "s3://my-bucket"
    assert list(record)[:6] == ["Type", "Id", "Name", "Arn", "Tags", "Uri"]
    # missing template variable -> Uri omitted entirely, not empty
    record = normalize_resource({"Other": "x"}, cfg, {"partition": "aws"}, {})
    assert "Uri" not in record
