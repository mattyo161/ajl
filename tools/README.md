# Tools

Some helpful scripts for getting going with `ajl` to extract details for the AWS APIs among other things.

## Get AWS Model Files

Using the AWS Go SDK we can extract the JSON files that describe the output of all the APIs, this is extremely helpful when trying to understand the APIs and create a JSONL wrapper for them.

This command will clone the aws-go-sdk repository into a `.temp` directory and then proceed to extract the desired details from the API files. The `jq`, `yq`, `git` and `python` tools are required to run this process. You can install them with your favorite package manager.

```shell
./aws-model-extraction.sh
```