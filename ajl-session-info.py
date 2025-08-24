import boto3
import json
import datetime
import inspect

import botocore.loaders
from botocore.loaders import Loader
from botocore.session import Session


class JSONEncoder(json.JSONEncoder):

    def default(self, obj):
        if isinstance(obj, datetime.datetime):
            return obj.isoformat()
        return super(JSONEncoder, self).default(obj)

def strip_metadata(response):
    # copy response to new value without the ResponseMetadata
    return {key:value for key, value in response.items() if key != 'ResponseMetadata'}


# https://ben11kehoe.medium.com/boto3-sessions-and-why-you-should-use-them-9b094eb5ca8e
sess = boto3.Session()
sts = sess.client('sts')
s3 = sess.client('s3')
sessionInfo = {
    "partitions": sess.get_available_partitions(),
    "regions": sess.get_available_regions(partition_name="aws", service_name="ec2"),
    "services": sess.get_available_services(),
    "profiles": sess.available_profiles,
    "details": {
        "profile": sess.profile_name,
        "region": sess.region_name,
    },
    "caller_identity": strip_metadata(sts.get_caller_identity())
}

with open('ajl-session-info.json', 'w') as f:
    json.dump(sessionInfo, f, cls=JSONEncoder, indent=2, sort_keys=True)


