import boto3
# import botocore
import orjson
import datetime

from botocore.serialize import JSONSerializer


# class JSONEncoder(json.JSONEncoder):
#     def default(self, obj):
#         if isinstance(obj, datetime.datetime):
#             return obj.isoformat()
#         return super(JSONEncoder, self).default(obj)

sess = boto3.Session()
clients = sess.get_available_services()
client_api_mapping = {}
for client in clients:
    c = sess.client(client)
    client_api_mapping[client] = {
        "api_mapping": c.meta.method_to_api_mapping,
        "waiters": [] if not(hasattr(c, "waiter_names")) else c.waiter_names,
        "partition": c.meta.partition,
        "region": c.meta.region_name,
        "config": c.meta.config.__dict__,
        "metadata": c.meta.service_model.metadata,
        "operations": c.meta.service_model.operation_names
    }

    # can get doc by accessing each client and inspecting `__doc__`
    # getattr(c, "<cmd>").__doc__
    # eg. getattr(s3, "list_buckets").__doc__

print(orjson.dumps(client_api_mapping, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS).decode())

