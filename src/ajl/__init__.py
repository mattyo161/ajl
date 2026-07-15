"""ajl - AWS JSON Line: a streaming JSONL-native CLI for the AWS API."""

try:
    from ._version import __version__
except ImportError:  # running from a source tree without a build
    __version__ = "0.0.0.dev0"
