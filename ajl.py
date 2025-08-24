import sys
import boto3
import json
import datetime
import orjson
import jsonlines
import os
import argparse
import caseconverter

import botocore.loaders
from botocore.loaders import Loader
from botocore.session import Session


def json_encoder(obj):
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    return json.dumps(obj)


class JSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime.datetime):
            return obj.isoformat()
        return super(JSONEncoder, self).default(obj)

def strip_metadata(response):
    # copy response to new value without the ResponseMetadata
    return {key:value for key, value in response.items() if key != 'ResponseMetadata'}


def process_response(response: object, client: str, operation: str):
    operation_kebab = caseconverter.kebabcase(operation)
    operation_pascal = caseconverter.pascalcase(operation)
    operation_filepath = os.path.join("models",client,"operations","json",operation_pascal + ".json")
    if operation_kebab.startswith("describe-") or operation_kebab.startswith("list-"):
        print(f"reading config file {operation_filepath}", file=sys.stderr)
        list_objects = [operation_pascal.replace("Describe", "").replace("List", "")]
        if os.path.exists(operation_filepath):
            with open(operation_filepath, 'r') as operation_file:
                operation_config = json.load(operation_file)
            for property, shape in operation_config["output"]["members"].items():
                if shape["type"] == "list":
                    list_objects.append(property)
        print(f"list_objects={list_objects}; response_keys={response.keys()}", file=sys.stderr)
        for property in set(list_objects):
            print(f"Getting {property} from response", file=sys.stderr)
            if property in response:
                print(f"outputing property {property}", file=sys.stderr)
                for entry in response[property]:
                    print(json.dumps(entry, indent=None, cls=JSONEncoder))



PROFILE = os.environ.get("AWS_PROFILE", os.environ.get("AWS_DEFAULT_PROFILE", None))
REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))

generic_parser = argparse.ArgumentParser(add_help=False)
generic_parser.add_argument('--profile', type=str)
generic_parser.add_argument('--region', type=str)
generic_parser.add_argument('--params-json', type=str)

ajl_parser = argparse.ArgumentParser(parents=[generic_parser])

args = ajl_parser.parse_known_args()
options = args[0]
extra_params = args[1]
profile = options.profile or PROFILE
region = options.region or REGION
print(f"profile={profile}; region={region}; args={args}")


# https://ben11kehoe.medium.com/boto3-sessions-and-why-you-should-use-them-9b094eb5ca8e
sess = boto3.Session(profile_name=profile, region_name=region)
sts = sess.client('sts')
s3 = sess.client('s3')
extra_params = args[1]

client_name = None
operation_name = None
if len(extra_params) > 0 and extra_params[0][0] != '-':
    client_name = extra_params.pop(0)
if len(extra_params) > 0 and extra_params[0][0] != '-':
    operation_name = caseconverter.snakecase(extra_params.pop(0))

reader_pointer = None
if options.params_json:
    if options.params_json == "-":
        reader_pointer = sys.stdin
if reader_pointer:
    print("in reader_pointer", file=sys.stderr)
    try:
        # Create a jsonlines.Reader instance, passing the given file-like object
        reader = jsonlines.Reader(reader_pointer)

        # Iterate over the JSON objects in the input stream
        for json_params in reader:
            # Process each JSON object
            if "client" in json_params:
                client_name = json_params["client"]
                del (json_params["client"])
            if "operation" in json_params:
                operation_name = caseconverter.snakecase(json_params["operation"])
                del (json_params["operation"])
            client = sess.client(client_name)
            cmd = getattr(client, operation_name)
            print(f"json_params={json_params}", file=sys.stderr)
            resp = cmd(**json_params)
            # for item in strip_metadata(resp)["Buckets"]:
            process_response(response=resp, client=client_name, operation=operation_name)

    except jsonlines.InvalidLineError as e:
        print(f"Error: Invalid JSON Lines format in stdin: {e}", file=sys.stderr)
    except Exception as e:
        print(f"An unexpected error occurred: {e}", file=sys.stderr)
    finally:
        # Close the reader (which also closes sys.stdin)
        if 'reader' in locals() and reader:
            reader.close()

else:
    print(f"service={client_name}; operation={operation_name}")
    client = sess.client(client_name)
    cmd = getattr(client, operation_name)
    ## Need to figure out how to get the rest of the params to be passed as JSON
    ## This is where the kebabcase `--input-param-name` needs to be converted to the `InputParamName` that the API requires
    resp = cmd()
    process_response(response=resp, client=client_name, operation=operation_name)
    print(json.dumps(strip_metadata(resp), cls=JSONEncoder, indent=2, sort_keys=True))

