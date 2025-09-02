#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# SDK_DIR is where any SDK files will be checked out
SDK_DIR="${SCRIPT_DIR}/../.temp/aws-sdks"
[[ ! -d "${SDK_DIR}" ]] && mkdir -p "${SDK_DIR}"

if [[ -d "${SDK_DIR}/aws-sdk-go" ]]; then
  (cd "${SDK_DIR}/aws-sdk-go" && git pull)
else
  (cd "${SDK_DIR}" && git clone https://github.com/aws/aws-sdk-go.git)
fi

# TMP_MODELS_DIR is where all raw json api models will be extracted to for analysis
TMP_MODELS_DIR="${SCRIPT_DIR}/../.temp/aws-models"
[[ ! -d "${TMP_MODELS_DIR}" ]] && mkdir -p "${TMP_MODELS_DIR}"


# Build a list of API json files to parse
apis=(
#  $(find "${SDK_DIR}/aws-sdk-go/models/apis/" -type f -name "api*.json" | sort)
  # Uncomment if there are just a few that you want to test with
#  $(find "${SDK_DIR}/aws-sdk-go/models/apis/ec2" -type f -name "api*.json" | sort)
#  $(find "${SDK_DIR}/aws-sdk-go/models/apis/rds" -type f -name "api*.json" | sort)
#  $(find "${SDK_DIR}/aws-sdk-go/models/apis/s3" -type f -name "api*.json" | sort)
  $(find "${SDK_DIR}/aws-sdk-go/models/apis/ssm" -type f -name "api*.json" | sort)
  $(find "${SDK_DIR}/aws-sdk-go/models/apis/dynamodb" -type f -name "api*.json" | sort)
)

function main() {
  for api in "${apis[@]}"; do
    # we want to get the 3rd folder from the right, to do that we reverse the string, cut the folder and rev back
    export api_client="$(rev <<< "${api}" | cut -d / -f 3 | rev)"
    api_dir="${TMP_MODELS_DIR}/${api_client}"
    [[ ! -d "${api_dir}" ]] && mkdir -p "${api_dir}"
    # save original api file
    cat "${api}" \
      | tee "${api_dir}/${api_client}-api-orig.json" \
      | json2yaml > "${api_dir}/${api_client}-api-orig.yaml"
    # generate topological sorted yaml version
    cat "${api}" \
    | jq -r '
      # $sorted is a list of [{key, value, $depends_on[]},..] objects in dependency order
      def topological_sort($sorted; $depends_on):
        ($sorted + [.[] | select(.[$depends_on] | length == 0)]) as $sorted |
        debug("\($ENV["api_client"]) length of input \(. | length); length of sorted \($sorted | length); field=\($depends_on)") |
        [ .[] | select(.[$depends_on] | length > 0) |
          # remove any keys from the sorted list from object dependencies
          .[$depends_on] |= (. - [$sorted[].key])
        ] |
        #  debug("length of input \(. | length); length of sorted \($sorted | length); length no dependencies \([.[] | select(.[$depends_on] | length == 0)] | length)") |
        if length > 0 then (
          # Check to make sure that there are some dependencies that have been cleared
          if ([.[] | select(.[$depends_on] | length == 0)] | length == 0) then (
            debug("### ERROR (cyclical) - \($ENV["api_client"]) length of input \(. | length); length of sorted \($sorted | length); no additional dependencies cleared") |
            # return the result as is
            $sorted + .
          ) else (
            . | topological_sort($sorted; $depends_on)
          ) end
        ) else (
          $sorted
        ) end
      ;
      def dependency_sort: reduce .value.requires[] as $r ({(.key): false}; .[$r] = true)
      ;
      # Do some clean up to fix issues with circular references
# ---
#more to clean up
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:34 .temp/aws-models/amplifyuibuilder/amplifyuibuilder-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:35 .temp/aws-models/appsync/appsync-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:35 .temp/aws-models/athena/athena-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:35 .temp/aws-models/bedrock-agent-runtime/bedrock-agent-runtime-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:35 .temp/aws-models/ce/ce-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:36 .temp/aws-models/connect/connect-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:36 .temp/aws-models/connectcases/connectcases-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:36 .temp/aws-models/datazone/datazone-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:36 .temp/aws-models/deadline/deadline-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:36 .temp/aws-models/dynamodb/dynamodb-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:36 .temp/aws-models/elasticmapreduce/elasticmapreduce-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:36 .temp/aws-models/emr-containers/emr-containers-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:36 .temp/aws-models/emr-serverless/emr-serverless-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:36 .temp/aws-models/freetier/freetier-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:36 .temp/aws-models/fsx/fsx-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:36 .temp/aws-models/glue/glue-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:37 .temp/aws-models/iotfleetwise/iotfleetwise-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:37 .temp/aws-models/iotsitewise/iotsitewise-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:37 .temp/aws-models/iottwinmaker/iottwinmaker-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:37 .temp/aws-models/kendra/kendra-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:37 .temp/aws-models/mediaconvert/mediaconvert-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:37 .temp/aws-models/models.lex.v2/models.lex.v2-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:37 .temp/aws-models/organizations/organizations-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:37 .temp/aws-models/pi/pi-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:38 .temp/aws-models/qapps/qapps-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:38 .temp/aws-models/qbusiness/qbusiness-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:38 .temp/aws-models/qconnect/qconnect-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:38 .temp/aws-models/quicksight/quicksight-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:38 .temp/aws-models/rds-data/rds-data-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:38 .temp/aws-models/runtime.lex.v2/runtime.lex.v2-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:38 .temp/aws-models/sagemaker/sagemaker-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:38 .temp/aws-models/ssm/ssm-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:38 .temp/aws-models/streams.dynamodb/streams.dynamodb-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:38 .temp/aws-models/timestream-query/timestream-query-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:38 .temp/aws-models/verifiedpermissions/verifiedpermissions-api.json
#-rw-r--r--@ 1 matt  staff         0 Aug 24 15:38 .temp/aws-models/wafv2/wafv2-api.json
# ---
      if ($ENV["api_client"] == "ssm") then (
        # remove circular reference
        del(.shapes.OpsAggregator.members.Aggregators, .shapes.InventoryAggregator.members.Aggregators)
      ) elif ($ENV["api_client"] == "dynamodb") then (
        # remove circular reference
        del(.shapes.AttributeValue.members.M, .shapes.AttributeValue.members.L)
      ) end |
      # make sure that shapes are defined before they are used
      {version, metadata, shapes} * . | .shapes |= (
        [ to_entries[] |
          .depends_on = ([.value | .. | .shape?]|unique|.-[null])
        ] |
        topological_sort([]; "depends_on") |
        [ .[] |
          {
            "&\(.key)": (
              # keep a reference to the shape for lookup
              {shape_name: .key} +
              .value
            )
          }
        ]
      ) |
      walk(if (type == "object" and has("locationName")) then del(.locationName) else . end)
    ' \
    | yq --input-format json --output-format yaml \
    | jq -Rr --arg sq "'" '
        # fix anchor syntax
        gsub("- \($sq)(?<anchor>&.+)\($sq):"; "- \(.anchor)") |
        gsub("shape: (?<anchor>.+)$"; "<<: *\(.anchor)")
    ' \
    | tee "${api_dir}/${api_client}-api.yaml" \
    | yaml2json \
    > "${api_dir}/${api_client}-api.json"

    # generate shapes
    [[ ! -d "${api_dir}/shapes" ]] && mkdir -p "${api_dir}/shapes"
    cat "${api_dir}/${api_client}-api.json" \
    | jq -rc '
      .shapes[] |
      # output json file
      {filename: "json/\(.shape_name).json", content: .},
      # output yaml file
      {filename: "yaml/\(.shape_name).yaml", content: .}
    ' > "${api_dir}/shapes/_api-shapes.jsonl"
    python "${SCRIPT_DIR}/jql-files.py" --file "${api_dir}/shapes/_api-shapes.jsonl"

    # generate operations
    [[ ! -d "${api_dir}/operations" ]] && mkdir -p "${api_dir}/operations"
    cat "${api_dir}/${api_client}-api.json" \
    | jq -rc '
      .operations | to_entries[] |
      # output json file
      {filename: "json/\(.key).json", content: ({operation_name: .key} + .value)},
      # output yaml file
      {filename: "yaml/\(.key).yaml", content: ({operation_name: .key} + .value)}
    ' > "${api_dir}/operations/_api-operations.jsonl"
    python "${SCRIPT_DIR}/jql-files.py" --file "${api_dir}/operations/_api-operations.jsonl"
  done
}

function json2yaml() {
  yq --input-format json --output-format yaml
}

function yaml2json() {
  yq --input-format yaml --output-format json
}

main