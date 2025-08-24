# ajl - AWS JSON Line

Is a wrapper for the AWS Boto3 API that is intended to be able to replicate as best as possible the `aws` cli tool with ability to stream the output as jsonline (nd-json - each line represents a JSON object). This is a very powerful abstraction that can provide some very fast processing of data from the API. On top of that it allows jsonl data to be passed to `ajl` making it possible to stream 100s/1000s of API calls per second, which is extremely useful when dealing with large data sets like S3 buckets, Tags, etc. It is also a great way to inventory an AWS account and taking the streams of JSONL and saving them into OpenSearch, DynamoDB, DocumentDB, or any other JSON schemaless store or even just saving to S3. 

The intent is to also ensure that objects have a consistent interface with the following properties always set `Arn`, `Id`, `Name` and `Tags`. Not all AWS objects have names or ids, in those cases a best attempt will be made to use `tag:Name` or to get the `Id` from the `Arn`. Ideally the `Arn` should be globally unique value across all of AWS, `Id` and `Name` will depend on the API and how the user configured the AWS account.

The `Tags` property will be a map of all the tags for the given resource. Many APIs return the Tags in the response as `{Key:string, Value:string}` pairs which is less useful. So `ajl` will convert them from an array to a map `{Key: Value}` which will allow direct access via `jq` or other json parsers using dot notation like `.Tags.Name` for instance. For APIs that do not return the Tags `ajl` will optionally make calls to the `resourcetaggingapi` to get the tags by `Arn`. These calls will be run in parallel ensuring that responses are fast and efficient.

## Performance

Using `ajl` I have found performance compared to `aws-cli` to be amazingly fast. Performance versus making direct calls to the boto3 is likely comparable, with perhaps some slight overhead due to conversion of the output data to JSON line, however the streaming benefits, the ability to make calls with simple JSON objects, did I mention it will support profiles & regions in your calls so a single command can process many accounts and regions.

My hope is that putting this out into the community there will be opportunities to take this concept much further then the general PoCs that I have been messing with over the years working with AWS.
