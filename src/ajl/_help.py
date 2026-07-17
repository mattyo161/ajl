"""Shared help text so subcommand --help surfaces the global ajl flags.

argparse subparsers don't inherit the root parser's options, so `ajl s3 scan
--help` (etc.) would otherwise hide --cache/--workers/--profile/... Each
subparser sets this as its epilog with RawDescriptionHelpFormatter.
"""

GLOBAL_FLAGS = """\
global ajl flags (accepted by every command, parsed before the subcommand):
  --profile P / --region R   target account / region
  --all-profiles/--all-regions/--all   fan out across profiles/regions
  --workers N                parallel requests (default 8)
  --cache TTL                serve+store cached results, e.g. 15m, 2h (AJL_CACHE sets a default)
  --refresh / --recache      with --cache: skip the read, still store fresh
  --rm-after DUR             cache entry lifetime before cleanup (default 7d)
  --no-cache                 disable caching for this run
  --jq PROGRAM               post-shaping jq filter (empty drops, strings raw)
  --stamp-session            add Profile/Region/Account to every record
  --max-items N              stop after N records
  --fetch-tags               batch-fetch missing Tags
  --learn / --no-learn       log the aws-cli equivalent + an audit record
  --verbose                  progress + diagnostics on stderr

encryption (AJL_AGE_*): set AJL_AGE_IDENTITY (an AGE-SECRET-KEY-... or a path;
`ajl cache keygen` makes one) to encrypt the cache and seal SecureStrings;
AJL_AGE_RECIPIENTS / AJL_AGE_PASSPHRASE select the other modes."""
