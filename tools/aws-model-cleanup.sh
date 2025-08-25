#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# SDK_DIR is where any SDK files will be checked out
SDK_DIR="${SCRIPT_DIR}/../.temp/aws-sdks"
[[ ! -d "${SDK_DIR}" ]] && mkdir -p "${SDK_DIR}"

# MODELS_DIR is where all raw json api models will be extracted to for analysis
TMP_MODELS_DIR="${SCRIPT_DIR}/../.temp/aws-models"
[[ ! -d "${TMP_MODELS_DIR}" ]] && mkdir -p "${TMP_MODELS_DIR}"

MODELS_DIR="${SCRIPT_DIR}/../models"
[[ ! -d "${MODELS_DIR}" ]] && mkdir -p "${MODELS_DIR}"

# Build a list of API json files to parse
apis=(
#  $(ls "${TMP_MODELS_DIR}/*/*-api.json")
  # Uncomment if there are just a few that you want to test with
   $(ls "${TMP_MODELS_DIR}/ec2/"*-api.json)
   $(ls "${TMP_MODELS_DIR}/rds/"*-api.json)
   $(ls "${TMP_MODELS_DIR}/s3/"*-api.json)
   $(ls "${TMP_MODELS_DIR}/ssm/"*-api.json)
   $(ls "${TMP_MODELS_DIR}/dynamodb/"*-api.json)
)

function main() {
  for api in "${apis[@]}"; do
    # we want to get the 2nd folder from the right, to do that we reverse the string, cut the folder and rev back
    export api_client="$(rev <<< "${api}" | cut -d / -f 2 | rev)"
    cat "${api}" \
    | jq -r --sort-keys '
      {
        name: $ENV["api_client"],
        metadata,
        operations: ([
            .operations |
            to_entries[] |
            {
                key,
                value: {
                    name: .key,
                    input: {
                        required: .value.input.required,
                        members: (
                            # Make sure that `null` input is supported as empty map
                            .value.input.members//{} |
                            [
                                to_entries[] |
                                {
                                    key,
                                    value: {
                                        type: .value.type,
                                        name: .key,
                                        shape_name: .value.shape_name
                                    }
                                }
                            ] | from_entries
                        ),
                        # Get the fields that contain *Marker(s) and Next* as these are fields that indicate paginated values
                        markers: (.value.input.members//{} | keys | [.[] | select(test("^Next|(Marker|Token)s?$"))])
                    },
                    output: {
                        members: (
                            # <- Make sure that `null` input is supported as empty map
                            .value.output.members//{} |
                            [
                                to_entries[] |
                                {
                                    key,
                                    value: {
                                        type: .value.type,
                                        name: .key,
                                        shape_name: .value.shape_name
                                    }
                                }
                            ] | from_entries
                        ),
                        # Get the fields that contain *Marker(s) and Next* as these are fields that indicate paginated values
                        markers: (.value.output.members//{} | keys | [.[] | select(test("^Next|(Marker|Token)s?$"))]),
                        # Get lists, this will be used by AJL to generate the jsonl output
                        lists: (.value.output.members//{} | [ to_entries [] |
                            select(.value.type == "list") |
                            {
                              key,
                              value: (
                                {
                                  id_fields: (.value.member.members//{} | [keys[] | select(test("(Id|Identifier)$"))]),
                                  arn_fields: (.value.member.members//{} | [keys[] | select(test("(Arn)$"))]),
                                  name_fields: (.value.member.members//{} | [keys[] | select(test("(Name)$"))]),
                                  all_fields: (.value.member.members//{} | keys)
                                }
                              )
                            }

                          ] | from_entries
                        )
                    }
                }
            }
        ] | from_entries
      )}
      ' \
      | tee "${MODELS_DIR}/${api_client}.json" \
      | json2yaml > "${MODELS_DIR}/${api_client}.yaml"

  done
}

function json2yaml() {
  yq --input-format json --output-format yaml
}

function yaml2json() {
  yq --input-format yaml --output-format json
}

main