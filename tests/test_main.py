import io
import json


from ajl import main as ajl_main
from ajl.main import (
    Emitter,
    Runner,
    coerce_param,
    coerce_params,
    dumps,
    parse_extra_options,
    run_operation,
    runtime_version,
    shape_page,
)


def test_runtime_version_falls_back_without_a_repo(monkeypatch):
    monkeypatch.setattr(ajl_main, "_find_repo_root", lambda: None)
    assert runtime_version() == ajl_main.__version__


def test_runtime_version_falls_back_when_git_is_unavailable(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(ajl_main, "_find_repo_root", lambda: tmp_path)

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("no git binary")

    monkeypatch.setattr(ajl_main.subprocess, "run", fake_run)
    assert runtime_version() == ajl_main.__version__


def test_runtime_version_uses_git_describe_when_available(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(ajl_main, "_find_repo_root", lambda: tmp_path)

    class FakeCompleted:
        returncode = 0
        stdout = "v0.2.0-18-g1671e69-dirty\n"

    monkeypatch.setattr(ajl_main.subprocess, "run", lambda *a, **k: FakeCompleted())
    assert runtime_version() == "v0.2.0-18-g1671e69-dirty"


def test_parse_extra_options_kebab_to_pascal():
    options = parse_extra_options(["--bucket-name", "my-bucket", "--max-results", "5"])
    assert options == {"BucketName": "my-bucket", "MaxResults": "5"}


def test_parse_extra_options_flags_and_multivalue():
    options = parse_extra_options(["--dry-run", "--instance-ids", "i-1", "i-2"])
    assert options == {"DryRun": True, "InstanceIds": ["i-1", "i-2"]}


def test_coerce_param_types():
    assert coerce_param("5", {"type": "integer"}) == 5
    assert coerce_param("true", {"type": "boolean"}) is True
    assert coerce_param('[{"Name":"vpc-id","Values":["vpc-1"]}]', {"type": "list"}) == [
        {"Name": "vpc-id", "Values": ["vpc-1"]}
    ]
    assert coerce_param("solo", {"type": "list"}) == ["solo"]
    assert coerce_param("plain", {"type": "string"}) == "plain"
    assert coerce_param("plain", None) == "plain"
    assert coerce_param('{"A":1}', None) == {"A": 1}


def test_coerce_params_uses_model_and_filters():
    # ec2 DescribeInstances: MaxResults is an integer member
    params = coerce_params({"MaxResults": "5"}, "ec2", "DescribeInstances")
    assert params == {"MaxResults": 5}
    # filter_to_input drops fields a previous ajl stage added
    params = coerce_params(
        {"Bucket": "b", "Prefix": "p/", "Type": "s3:prefix", "Arn": "arn:...", "Tags": {}},
        "s3",
        "ListObjectsV2",
        filter_to_input=True,
    )
    assert params == {"Bucket": "b", "Prefix": "p/"}


def test_coerce_params_remaps_lower_camel_case_members():
    # ecs is one of the few APIs whose input members are lowerCamelCase
    # (cluster, tasks, ...), not PascalCase; a guessed "--cluster" -> "Cluster"
    # CLI flag, or a piped record's field, must still resolve.
    params = coerce_params({"Cluster": "my-cluster"}, "ecs", "ListTasks")
    assert params == {"cluster": "my-cluster"}
    params = coerce_params(
        {"Cluster": "my-cluster", "Type": "ecs:cluster", "Arn": "arn:...", "Tags": {}},
        "ecs",
        "DescribeTasks",
        filter_to_input=True,
    )
    assert params == {"cluster": "my-cluster"}


def test_dumps_handles_datetime():
    import datetime

    line = dumps({"When": datetime.datetime(2026, 7, 13, 12, 0, 0)})
    assert json.loads(line)["When"].startswith("2026-07-13T12:00:00")


class FakePaginator:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **params):
        yield from self.pages


class FakeMeta:
    partition = "aws"
    region_name = "us-east-1"

    def __init__(self, method_to_api_mapping=None):
        self.method_to_api_mapping = method_to_api_mapping or {}


class FakeClient:
    """Duck-typed boto3 client for run_operation tests."""

    meta = FakeMeta()

    def __init__(self, pages, paginate=True):
        self.pages = pages
        self._paginate = paginate

    def can_paginate(self, operation):
        return self._paginate

    def get_paginator(self, operation):
        return FakePaginator(self.pages)

    def __getattr__(self, name):
        pages = iter(self.pages)

        def call(**params):
            return next(pages)

        return call


class Options:
    no_parse = False
    no_paginate = False
    max_items = None
    fetch_tags = False
    workers = 1
    verbose = False
    stamp_session = False
    describe = False


def make_runner(client):
    runner = Runner(default_region="us-east-1")
    key = runner.session_key()
    runner._clients[(key, "ec2", False)] = client
    runner._accounts[key] = "123456789012"
    return runner, key


def collect_emitted(runner, key, client, options=None):
    out = io.StringIO()
    emitter = Emitter(stream=out)
    run_operation(runner, emitter, options or Options(), "ec2", "describe-vpcs", {}, key)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def test_run_operation_paginated_and_normalized():
    pages = [
        {"Vpcs": [{"VpcId": "vpc-1", "OwnerId": "1", "Tags": [{"Key": "Name", "Value": "main"}]}]},
        {"Vpcs": [{"VpcId": "vpc-2", "OwnerId": "1"}]},
    ]
    client = FakeClient(pages)
    runner, key = make_runner(client)
    records = collect_emitted(runner, key, client)
    assert [r["ajl"]["id"] for r in records] == ["vpc-1", "vpc-2"]
    assert records[0]["ajl"]["type"] == "ec2:vpc"
    assert records[0]["ajl"]["name"] == "main"
    assert records[0]["ajl"]["arn"] == "arn:aws:ec2:us-east-1:1:vpc/vpc-1"
    assert records[0]["ajl"]["tags"] == {"Name": "main"}


def test_run_operation_stamp_session_merges_request_params():
    # a response often doesn't echo back what it was asked for (ecs ListTasks
    # returns task ARNs, never the cluster you asked about) — --stamp-session
    # should attach the resolved request params into ajl.stamp
    pages = [{"Vpcs": [{"VpcId": "vpc-1", "OwnerId": "1"}]}]
    client = FakeClient(pages)
    runner, key = make_runner(client)
    options = Options()
    options.stamp_session = True
    out = io.StringIO()
    emitter = Emitter(stream=out)
    run_operation(runner, emitter, options, "ec2", "describe-vpcs",
                   {"Filter": "x"}, key)
    records = [json.loads(line) for line in out.getvalue().splitlines()]
    assert records[0]["ajl"]["stamp"]["Filter"] == "x"
    # ajl.id (the real resource's id) lives in a separate namespace from
    # ajl.stamp — nesting makes the old clobber risk structurally impossible
    assert records[0]["ajl"]["id"] == "vpc-1"


class FakeEcsDescribeClient:
    """list_clusters + describe_clusters — the kind="array" (batch) pilot."""

    meta = FakeMeta({"list_clusters": "ListClusters", "describe_clusters": "DescribeClusters"})

    def __init__(self, cluster_count):
        self.cluster_ids = [f"c{i}" for i in range(cluster_count)]
        self.describe_calls = []

    def can_paginate(self, operation):
        return False

    def list_clusters(self, **params):
        return {"clusterArns": [f"arn:aws:ecs:us-east-1:123:cluster/{cid}"
                                 for cid in self.cluster_ids]}

    def describe_clusters(self, **params):
        self.describe_calls.append(params)
        return {"clusters": [
            {"clusterArn": f"arn:aws:ecs:us-east-1:123:cluster/{name}", "clusterName": name}
            for name in params["clusters"]
        ]}


def test_run_operation_describe_array_batches_ids():
    client = FakeEcsDescribeClient(cluster_count=5)
    runner = Runner(default_region="us-east-1")
    key = runner.session_key()
    runner._clients[(key, "ecs", False)] = client
    runner._accounts[key] = "123456789012"
    options = Options()
    options.describe = True
    out = io.StringIO()
    run_operation(runner, Emitter(stream=out), options, "ecs", "list-clusters", {}, key)
    records = [json.loads(line) for line in out.getvalue().splitlines()
               if not line.startswith("ajl:")]
    assert len(client.describe_calls) == 1  # 5 ids fit in one default-100 batch
    assert sorted(r["ajl"]["id"] for r in records) == ["c0", "c1", "c2", "c3", "c4"]
    assert records[0]["ajl"]["arn"].startswith("arn:aws:ecs:")
    # the batch identifier isn't re-stamped onto records
    assert "clusters" not in records[0]["ajl"].get("stamp", {})


def test_run_operation_describe_array_respects_curated_batch_size(monkeypatch):
    from ajl import modelconfig

    client = FakeEcsDescribeClient(cluster_count=5)
    runner = Runner(default_region="us-east-1")
    key = runner.session_key()
    runner._clients[(key, "ecs", False)] = client
    runner._accounts[key] = "123456789012"
    cfg = modelconfig.get_operation_config("ecs", "ListClusters")
    monkeypatch.setitem(cfg["output"]["describe"], "batch_size", 2)
    options = Options()
    options.describe = True
    out = io.StringIO()
    run_operation(runner, Emitter(stream=out), options, "ecs", "list-clusters", {}, key)
    assert [len(c["clusters"]) for c in client.describe_calls] == [2, 2, 1]


class FakeIamDescribeClient:
    """list_role_policies + get_role_policy — the kind="scalar"+scope pilot."""

    meta = FakeMeta({"list_role_policies": "ListRolePolicies", "get_role_policy": "GetRolePolicy"})

    def __init__(self, policy_names):
        self.policy_names = policy_names
        self.describe_calls = []

    def can_paginate(self, operation):
        return False

    def list_role_policies(self, **params):
        return {"PolicyNames": self.policy_names, "IsTruncated": False}

    def get_role_policy(self, **params):
        self.describe_calls.append(params)
        return {"RoleName": params["RoleName"], "PolicyName": params["PolicyName"],
                "PolicyDocument": {"Statement": []}}


def test_run_operation_describe_scalar_carries_scope_per_call():
    client = FakeIamDescribeClient(["AdminAccess", "S3ReadOnly", "DenyAll"])
    runner = Runner(default_region="us-east-1")
    key = runner.session_key()
    runner._clients[(key, "iam", False)] = client
    runner._accounts[key] = "123456789012"
    options = Options()
    options.describe = True
    options.stamp_session = True
    out = io.StringIO()
    run_operation(runner, Emitter(stream=out), options, "iam", "list-role-policies",
                   {"RoleName": "my-role"}, key)
    records = [json.loads(line) for line in out.getvalue().splitlines()
               if not line.startswith("ajl:")]
    assert len(client.describe_calls) == 3  # one GetRolePolicy call per policy name
    assert all(call["RoleName"] == "my-role" for call in client.describe_calls)
    assert sorted(r["PolicyName"] for r in records) == ["AdminAccess", "DenyAll", "S3ReadOnly"]
    # RoleName here is the raw GetRolePolicy response field (it echoes the
    # request back); --stamp-session redundantly also puts it in ajl.stamp
    assert all(r["RoleName"] == "my-role" for r in records)


def test_run_operation_describe_no_results_makes_no_describe_calls():
    client = FakeIamDescribeClient([])
    runner = Runner(default_region="us-east-1")
    key = runner.session_key()
    runner._clients[(key, "iam", False)] = client
    runner._accounts[key] = "123456789012"
    options = Options()
    options.describe = True
    out = io.StringIO()
    run_operation(runner, Emitter(stream=out), options, "iam", "list-role-policies",
                   {"RoleName": "empty-role"}, key)
    assert client.describe_calls == []
    assert out.getvalue() == ""


class FakeIamNoEchoDescribeClient:
    """A Get* that doesn't echo back the identifier it was given — like the
    real iam.GetSAMLProvider (only returns metadata, never the ARN you asked
    for). Exercises the scalar-kind id-fallback stamp in run_describe_chain."""

    meta = FakeMeta({"list_saml_providers": "ListSAMLProviders",
                      "get_saml_provider": "GetSAMLProvider"})

    def can_paginate(self, operation):
        return False

    def list_saml_providers(self, **params):
        return {"SAMLProviderList": [{"Arn": "arn:aws:iam::123:saml-provider/one"},
                                      {"Arn": "arn:aws:iam::123:saml-provider/two"}]}

    def get_saml_provider(self, **params):
        return {"CreateDate": "2026-01-01"}  # no Arn/Id back, on purpose


def test_run_operation_describe_scalar_falls_back_to_the_id_it_fetched_with():
    client = FakeIamNoEchoDescribeClient()
    runner = Runner(default_region="us-east-1")
    key = runner.session_key()
    runner._clients[(key, "iam", False)] = client
    runner._accounts[key] = "123456789012"
    options = Options()
    options.describe = True
    out = io.StringIO()
    run_operation(runner, Emitter(stream=out), options, "iam", "list-saml-providers", {}, key)
    records = [json.loads(line) for line in out.getvalue().splitlines()
               if not line.startswith("ajl:")]
    assert sorted(r["ajl"]["arn"] for r in records) == [
        "arn:aws:iam::123:saml-provider/one", "arn:aws:iam::123:saml-provider/two"]
    # id_field="Arn" for this pairing, so the fallback also re-derives id
    # from the arn tail, matching normalize.py's own id-from-arn convention
    assert sorted(r["ajl"]["id"] for r in records) == ["one", "two"]


class FakeEcsFlakyDescribeClient(FakeEcsDescribeClient):
    """describe_clusters raises for one batch — the run must not crash."""

    def describe_clusters(self, **params):
        if "c2" in params["clusters"]:
            raise RuntimeError("Throttling")
        return super().describe_clusters(**params)


def test_run_operation_describe_contains_a_failed_batch(monkeypatch):
    from ajl import modelconfig

    client = FakeEcsFlakyDescribeClient(cluster_count=5)
    runner = Runner(default_region="us-east-1")
    key = runner.session_key()
    runner._clients[(key, "ecs", False)] = client
    runner._accounts[key] = "123456789012"
    cfg = modelconfig.get_operation_config("ecs", "ListClusters")
    monkeypatch.setitem(cfg["output"]["describe"], "batch_size", 1)
    options = Options()
    options.describe = True
    out = io.StringIO()
    run_operation(runner, Emitter(stream=out), options, "ecs", "list-clusters", {}, key)
    records = [json.loads(line) for line in out.getvalue().splitlines()
               if not line.startswith("ajl:")]
    # 5 clusters, batch_size=1 -> 5 calls; c2's call fails, the other 4 still emit
    assert sorted(r["ajl"]["id"] for r in records) == ["c0", "c1", "c3", "c4"]


def test_run_operation_max_items():
    pages = [{"Vpcs": [{"VpcId": f"vpc-{i}", "OwnerId": "1"} for i in range(10)]}]
    client = FakeClient(pages)
    runner, key = make_runner(client)
    options = Options()
    options.max_items = 3
    records = collect_emitted(runner, key, client, options)
    assert len(records) == 3


def test_shape_page_jq_escape_hatch_ec2_instances():
    page = {
        "Reservations": [
            {
                "ReservationId": "r-1",
                "OwnerId": "111122223333",
                "Instances": [
                    {"InstanceId": "i-1", "Tags": [{"Key": "Name", "Value": "web"}]}
                ],
            }
        ]
    }
    from ajl.modelconfig import get_operation_config

    runner = Runner(default_region="us-east-1")
    key = runner.session_key()
    runner._accounts[key] = "111122223333"
    context = {"partition": "aws", "region": "us-east-1", "account": "111122223333"}
    records = list(shape_page(page, get_operation_config("ec2", "DescribeInstances"), context, runner, key))
    assert len(records) == 1
    record = records[0]
    assert record["ajl"]["type"] == "ec2:instance"
    assert record["ajl"]["id"] == "i-1"
    assert record["ajl"]["name"] == "web"
    assert record["ajl"]["arn"] == "arn:aws:ec2:us-east-1:111122223333:instance/i-1"
    assert record["ajl"]["tags"] == {"Name": "web"}
    assert record["Reservation"]["ReservationId"] == "r-1"
    assert "Tags" not in record


def test_shape_page_default_single_list_unwrap():
    runner = Runner(default_region="us-east-1")
    records = list(
        shape_page({"Widgets": [{"A": 1}], "NextToken": "t"}, None, {}, runner, runner.session_key())
    )
    assert records == [{"A": 1}]


def _shape_with_model(service, operation, page):
    from ajl.modelconfig import get_operation_config

    runner = Runner(default_region="us-east-1")
    key = runner.session_key()
    runner._accounts[key] = "111122223333"
    context = {"partition": "aws", "region": "us-east-1", "account": "111122223333"}
    return list(shape_page(page, get_operation_config(service, operation), context, runner, key))


def test_shape_page_sqs_list_queues_jq():
    page = {"QueueUrls": ["https://sqs.us-east-1.amazonaws.com/111122223333/my-queue"]}
    (record,) = _shape_with_model("sqs", "ListQueues", page)
    assert record["ajl"]["type"] == "sqs:queue"
    assert record["ajl"]["id"] == "my-queue"
    assert record["ajl"]["arn"] == "arn:aws:sqs:us-east-1:111122223333:my-queue"
    assert record["QueueUrl"] == page["QueueUrls"][0]


def test_shape_page_route53_hosted_zones_jq():
    page = {"HostedZones": [{"Id": "/hostedzone/Z0432432", "Name": "example.com."}]}
    (record,) = _shape_with_model("route53", "ListHostedZones", page)
    assert record["ajl"]["id"] == "Z0432432"
    assert record["ajl"]["arn"] == "arn:aws:route53:::hostedzone/Z0432432"
    assert record["Id"] == "Z0432432"  # the raw field is stripped, not renamed, and kept


def test_shape_page_ecs_scalar_arn_list():
    page = {"clusterArns": ["arn:aws:ecs:us-east-1:111122223333:cluster/prod"]}
    (record,) = _shape_with_model("ecs", "ListClusters", page)
    assert record["ajl"]["type"] == "ecs:cluster"
    assert record["ajl"]["id"] == "prod"
    assert record["ajl"]["arn"] == page["clusterArns"][0]


def test_jq_emitter_filters_drops_and_explodes():
    from ajl.main import JqEmitter

    out = io.StringIO()
    emitter = JqEmitter(Emitter(stream=out), 'select(.Keep) | del(.Noise)')
    emitter.emit({"Keep": True, "Noise": 1, "Id": "a"})
    emitter.emit({"Keep": False, "Id": "b"})
    emitter.flush()
    records = [json.loads(line) for line in out.getvalue().splitlines()]
    assert records == [{"Keep": True, "Id": "a"}]

    out = io.StringIO()
    emitter = JqEmitter(Emitter(stream=out), '.Items[]')
    emitter.emit({"Items": [{"A": 1}, {"A": 2}]})
    assert len(out.getvalue().splitlines()) == 2


def test_jq_emitter_string_output_prints_raw():
    from ajl.main import JqEmitter

    out = io.StringIO()
    JqEmitter(Emitter(stream=out), '.Key').emit({"Key": "a/b.txt"})
    assert out.getvalue() == "a/b.txt\n"


def test_stamp_emitter_adds_session_fields():
    from ajl.main import StampEmitter

    out = io.StringIO()
    runner = Runner(default_profile="dev", default_region="us-east-1")
    runner._accounts[("dev", "us-east-1")] = "111122223333"
    emitter = StampEmitter(Emitter(stream=out), runner)
    emitter.emit({"ajl": {"id": "x"}}, ("dev", "us-east-1"))
    (record,) = [json.loads(line) for line in out.getvalue().splitlines()]
    stamp = record["ajl"]["stamp"]
    assert stamp["profile"] == "dev"
    assert stamp["region"] == "us-east-1"
    assert stamp["account"] == "111122223333"
    assert record["ajl"]["id"] == "x"  # existing ajl content is preserved, not clobbered


def test_stamp_emitter_creates_ajl_on_a_raw_no_parse_page():
    from ajl.main import StampEmitter

    out = io.StringIO()
    runner = Runner(default_profile="dev", default_region="us-east-1")
    runner._accounts[("dev", "us-east-1")] = "111122223333"
    emitter = StampEmitter(Emitter(stream=out), runner)
    emitter.emit({"SomeRawField": "x"}, ("dev", "us-east-1"))
    (record,) = [json.loads(line) for line in out.getvalue().splitlines()]
    assert list(record) == ["SomeRawField", "ajl"]  # ajl trails even when created fresh
    assert record["ajl"]["stamp"]["profile"] == "dev"


class StampOptions:
    def __init__(self, params_json=None, stamp_session=False, no_stamp_session=False):
        self.params_json = params_json
        self.stamp_session = stamp_session
        self.no_stamp_session = no_stamp_session


def test_should_stamp_session_propagates_through_params_json():
    from ajl.main import should_stamp_session

    # plain single call: no stamp unless asked for
    assert should_stamp_session(StampOptions(), fanning=False) is False
    # --all (or --profiles/--regions) originates the stamp
    assert should_stamp_session(StampOptions(), fanning=True) is True
    # a --params-json stage is always mid-pipeline: keep the stamp flowing
    # even though *this* invocation isn't itself fanning out
    assert should_stamp_session(StampOptions(params_json="-"), fanning=False) is True
    # explicit --stamp-session still works standalone
    assert should_stamp_session(StampOptions(stamp_session=True), fanning=False) is True
    # --no-stamp-session overrides all of the above
    assert should_stamp_session(
        StampOptions(params_json="-", stamp_session=True, no_stamp_session=True),
        fanning=True,
    ) is False


def test_pop_session_fields_flat_lowercase_is_the_convention():
    from ajl.main import pop_session_fields

    line = {"profile": "p1", "region": "eu-west-1", "Bucket": "b"}
    assert pop_session_fields(line) == ("p1", "eu-west-1")
    assert line == {"Bucket": "b"}
    assert pop_session_fields({}) == (None, None)


def test_pop_session_fields_falls_back_to_nested_ajl_stamp():
    # a raw ajl record piped straight back in without a jq reshape
    from ajl.main import pop_session_fields

    line = {"ajl": {"stamp": {"profile": "p3", "region": "us-east-2"}}, "Bucket": "b"}
    assert pop_session_fields(line) == ("p3", "us-east-2")
    assert line == {"Bucket": "b"}  # the whole 'ajl' key is popped, never forwarded to boto3


def test_pop_session_fields_flat_wins_over_nested():
    from ajl.main import pop_session_fields

    line = {"profile": "flat", "ajl": {"stamp": {"profile": "nested"}}}
    assert pop_session_fields(line) == ("flat", None)


def test_shape_page_cloudformation_stack():
    page = {"Stacks": [{
        "StackName": "web-prod",
        "StackId": "arn:aws:cloudformation:us-east-1:111122223333:stack/web-prod/abc-123",
        "Tags": [{"Key": "env", "Value": "prod"}],
    }]}
    (record,) = _shape_with_model("cloudformation", "DescribeStacks", page)
    assert record["ajl"]["type"] == "cloudformation:stack"
    assert record["ajl"]["id"] == "web-prod"
    assert record["ajl"]["arn"].startswith("arn:aws:cloudformation:")
    assert record["ajl"]["tags"] == {"env": "prod"}


def test_shape_page_cloudwatch_metric_and_composite_alarms():
    page = {
        "MetricAlarms": [{"AlarmName": "cpu-high", "AlarmArn": "arn:aws:cloudwatch:us-east-1:1:alarm:cpu-high"}],
        "CompositeAlarms": [{"AlarmName": "svc-degraded", "AlarmArn": "arn:aws:cloudwatch:us-east-1:1:alarm:svc-degraded"}],
    }
    records = _shape_with_model("cloudwatch", "DescribeAlarms", page)
    assert [r["ajl"]["id"] for r in records] == ["cpu-high", "svc-degraded"]
    assert all(r["ajl"]["type"] == "cloudwatch:alarm" for r in records)


def test_shape_page_logs_log_group_arn_format():
    page = {"logGroups": [{"logGroupName": "/ecs/web"}]}
    (record,) = _shape_with_model("logs", "DescribeLogGroups", page)
    assert record["ajl"]["type"] == "logs:log-group"
    assert record["ajl"]["arn"] == "arn:aws:logs:us-east-1:111122223333:log-group:/ecs/web"


def test_shape_page_ecs_task_scalar_arns():
    page = {"taskArns": ["arn:aws:ecs:us-east-1:1:task/prod/abc123"]}
    (record,) = _shape_with_model("ecs", "ListTasks", page)
    assert record["ajl"]["type"] == "ecs:task"
    assert record["ajl"]["id"] == "abc123"


def test_shape_page_eks_nodegroup():
    page = {"nodegroup": {
        "nodegroupName": "workers",
        "nodegroupArn": "arn:aws:eks:us-east-1:1:nodegroup/main/workers/aa-bb",
        "tags": {"team": "platform"},
    }}
    (record,) = _shape_with_model("eks", "DescribeNodegroup", page)
    assert record["ajl"]["type"] == "eks:nodegroup"
    assert record["ajl"]["name"] == "workers"
    assert record["ajl"]["tags"] == {"team": "platform"}


def test_operation_lookup_is_case_insensitive():
    # the CLI cannot reconstruct acronym casing (ID, DB, ACL) from kebab-case
    from ajl.modelconfig import get_operation_config

    assert get_operation_config("iam", "ListOpenIdConnectProviders") is not None
    assert get_operation_config("rds", "DescribeDbInstances") is not None
    assert get_operation_config("wafv2", "ListWebAcls") is not None
    assert get_operation_config("rds", "NoSuchOperation") is None


def test_shape_page_iam_oidc_provider_via_cli_casing():
    page = {"OpenIDConnectProviderList": [
        {"Arn": "arn:aws:iam::111122223333:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/70FF"}
    ]}
    (record,) = _shape_with_model("iam", "ListOpenIdConnectProviders", page)
    assert record["ajl"]["type"] == "iam:oidc-provider"
    assert record["ajl"]["id"] == "70FF"


def test_client_uses_adaptive_retry_for_configured_services():
    runner = Runner(default_region="us-east-1")
    key = runner.session_key()

    ssm_client = runner.client(key, "ssm")
    s3_client = runner.client(key, "s3")
    ec2_client = runner.client(key, "ec2")

    for adaptive_client in (ssm_client, s3_client):
        assert adaptive_client.meta.config.retries["mode"] == "adaptive"
        assert adaptive_client.meta.config.retries["total_max_attempts"] == 11
    assert ec2_client.meta.config.retries["mode"] == "standard"
    assert ec2_client.meta.config.retries["total_max_attempts"] == 4


def test_client_uses_adaptive_retry_for_one_pinned_operation_only():
    runner = Runner(default_region="us-east-1")
    key = runner.session_key()

    # efs's other operations never showed real throttling -- only
    # describe-mount-targets is pinned to adaptive, via
    # ADAPTIVE_RETRY_OPERATIONS, not the whole service.
    mount_targets_client = runner.client(key, "efs", "DescribeMountTargets")
    file_systems_client = runner.client(key, "efs", "DescribeFileSystems")
    bare_efs_client = runner.client(key, "efs")  # no operation given

    assert mount_targets_client.meta.config.retries["mode"] == "adaptive"
    assert mount_targets_client.meta.config.retries["total_max_attempts"] == 11
    assert file_systems_client.meta.config.retries["mode"] == "standard"
    assert bare_efs_client.meta.config.retries["mode"] == "standard"
    # the two non-pinned calls share one client; the pinned one is separate
    assert file_systems_client is bare_efs_client
    assert mount_targets_client is not file_systems_client


class _FakeSTS:
    def __init__(self, account):
        self._account = account
        self.calls = 0

    def get_caller_identity(self):
        self.calls += 1
        return {"Account": self._account}


def test_account_persists_and_is_reused_by_a_fresh_runner(tmp_path, monkeypatch):
    monkeypatch.setenv("AJL_ACCOUNT_CACHE_FILE", str(tmp_path / "accounts.json"))
    runner = Runner(default_profile="prod", default_region="us-east-1")
    key = runner.session_key()
    fake_sts = _FakeSTS("111122223333")
    runner._clients[(key, "sts", False)] = fake_sts

    assert runner.account(key) == "111122223333"
    assert fake_sts.calls == 1

    # A fresh Runner (standing in for the next short-lived `inventory.sh`
    # process) resolves from the persistent cache -- it never needs an sts
    # client of its own for this to succeed.
    runner2 = Runner(default_profile="prod", default_region="us-east-1")
    assert runner2.account(runner2.session_key()) == "111122223333"


def test_account_does_not_persist_for_unnamed_profile(tmp_path, monkeypatch):
    cache_path = tmp_path / "accounts.json"
    monkeypatch.setenv("AJL_ACCOUNT_CACHE_FILE", str(cache_path))
    runner = Runner(default_region="us-east-1")  # no --profile
    key = runner.session_key()
    runner._clients[(key, "sts", False)] = _FakeSTS("111122223333")

    assert runner.account(key) == "111122223333"
    assert not cache_path.exists()
