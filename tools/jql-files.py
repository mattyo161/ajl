import json
import os
import sys
import argparse
import jsonlines
import yaml
import datetime

class JSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime.datetime):
            return obj.isoformat()
        return super(JSONEncoder, self).default(obj)


arg_parser = argparse.ArgumentParser()
arg_parser.add_argument('--file', type=str)
args, extra_args = arg_parser.parse_known_args()

print(f"args={args}; extra_args={extra_args}", file=sys.stderr)
if args.file:
    if os.path.exists(args.file) and os.path.isfile(args.file):
        filepath = os.path.abspath(args.file)
        filedir = os.path.dirname(filepath)
        filename, fileext = os.path.splitext(os.path.basename(filepath))
        print(f"filename={filename}; fileext={fileext}; filedir={filedir};", file=sys.stderr)

    # process using jsonl
    if fileext and fileext == ".jsonl":
        with jsonlines.open(filepath) as reader:
            for obj in reader:
                out_filename, out_fileext = os.path.splitext(obj["filename"])
                if out_filename[0] == "/":
                    out_filepath = os.path.abspath(out_filename + out_fileext)
                else:
                    out_filepath = os.path.join(filedir, out_filename + out_fileext)
                out_filedir = os.path.dirname(out_filepath)
                if not os.path.isdir(out_filedir):
                    os.mkdir(out_filedir)
                content = obj["content"]
                # print(f"out_filepath={out_filepath}; out_filename={out_filename}; out_filename={out_fileext}; content_length:{len(json.dumps(content))}", file=sys.stderr)
                if isinstance(obj, str):
                    # write string to the filename
                    with open(out_filepath, "w") as out_file:
                        out_file.write(content)
                else:
                    if out_fileext == ".jsonl":
                        with jsonlines.open(out_filepath, mode='w') as out_file:
                            out_file.write(content)
                    elif out_fileext == ".json":
                        with open(out_filepath, "w") as out_file:
                            out_file.write(json.dumps(content, indent=2, cls=JSONEncoder))
                    elif out_fileext == ".yaml" or out_fileext == ".yml":
                        with open(out_filepath, "w") as out_file:
                            out_file.write(yaml.dump(content))



