## Progress
- go - uilive - https://github.com/gosuri/uilive
- python - alive-progress - https://github.com/rsalmei/alive-progress
  - tqdm - https://github.com/tqdm/tqdm
  - https://builtin.com/software-engineering-perspectives/python-progress-bar
  - https://www.datacamp.com/tutorial/progress-bars-in-python
- cli - https://cli.r-lib.org/reference/index.html
- command completion - https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-completion.html
- pandas - https://github.com/aws/aws-sdk-pandas
- go-sdk - https://github.com/aws/aws-sdk-go.git (api reference)
- yaml - https://yaml.org/spec/1.2.2/
  - https://www.cloudbees.com/blog/yaml-tutorial-everything-you-need-get-started
## Package Management

### uv
- https://github.com/astral-sh/uv

```shell
uv init
```

```shell
uv add ruff
uv add tqdm
uv add joblib
uv add boto3
```

### Get JSON config files
```shell
[[ ! -d aws-sdk-go ]] && git clone https://github.com/aws/aws-sdk-go.git
find aws-sdk-go/models -name "*-*.json" -exec jq '
  {
    file: input_filename,
    keys: (.|keys)
  } | . * (
    .file | split("/") | {
        source: .[-3], 
        api_version: .[-2], 
        filename: .[-1], 
        file_type: (.[-1]|split("-")[0:-1]|join("-"))
      }
    )
' {} \; | jq -s '. | sort_by(.file)' \
> aws-sdk-go-files.json
```


```shell
cat aws-sdk-go/models/apis/ec2/2016-11-15/api-2.json \
| jq -r '
  # $sorted is a list of [{key, value, $depends_on[]},..] objects in dependency order 
  def topological_sort($sorted; $depends_on):
    debug("length of input \(. | length); length of sorted \($sorted | length); field=\($depends_on)") | 
    [ .[] |
      # remove any keys from the sorted list from object dependencies
      .[$depends_on] - $sorted[].key
    ] | ($sorted + [.[] | select(.[$depends_on] | length == 0)]) as $sorted |
    debug("length of $sorted = \($sorted | length) depends_on = \([.[].[$depends_on]])") |
    [ .[] | select([.key] | in($sorted[].key) | not) ] |
    if length > 0 then (. | topological_sort($sorted; $depends_on))
    else $sorted
    end
  ;
  def dependency_sort: reduce .value.requires[] as $r ({(.key): false}; .[$r] = true)
  ;
  # make sure that shapes are defined before they are used
  {version, metadata, shapes} * . | .shapes |= (
    [ to_entries[] | 
      .depends_on = ([.value | .. | .shape?]|unique|.-[null])
#      | debug("depends_on = \(.depends_on)")
    ] | 
    topological_sort([]; "depends_on") |
    [ .[] |
      {
        "&\(.key)": (
          .value + {depends_on} # |= . + {requires: ([.. | .shape?]|unique|.-[null])}
        )
      }
    ]
  )
' \
| yq --input-format json --output-format yaml \
| jq -Rr --arg sq "'" '
    gsub("- \($sq)(?<anchor>&.+)\($sq):"; "- \(.anchor)") |
    gsub("shape: (?<anchor>.+)$"; "<<: *\(.anchor)")
' \
| tee ec2-api.yaml
```


```shell
[[ ! -d api-models-aws ]] && git clone https://github.com/aws/api-models-aws.git
find api-models-aws/models -name "*-*.json" -exec jq '
  .shapes | to_entries[] |
  select(.value.type == "service") |
  {
    file: input_filename,
    shapeId: .key,
    sdkId: .value.traits."aws.api#service".sdkId,
    title: .value.traits."smithy.api#title"
  }
' {} \; | jq -s '. | sort_by(.file)' \
> api-models-aws-result.json
```

```shell
python ajl-service-mappings.py \
> ajl-service-mappings.json 

```