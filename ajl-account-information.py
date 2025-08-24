import boto3
import orjson

# class JSONEncoder(json.JSONEncoder):
#     def default(self, obj):
#         if isinstance(obj, datetime.datetime):
#             return obj.isoformat()
#         return super(JSONEncoder, self).default(obj)

def strip_metadata(response):
    # copy response to new value without the ResponseMetadata
    return {key:value for key, value in response.items() if key != 'ResponseMetadata'}

sess = boto3.Session()
account = sess.client('account')

accountInfo = {
    "regions": account.list_regions(MaxResults=50)["Regions"],
    "contact": strip_metadata(account.get_contact_information()),
    "info": strip_metadata(account.get_account_information())
}


print(orjson.dumps(accountInfo, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS).decode())

