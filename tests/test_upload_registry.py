"""Registry discovery must separate R2 uploads from public downloads."""
import pytest
import cli
from lib.upload import _derive_public_url


@pytest.mark.parametrize("url_format", [
    {"pattern": "https://files.example.com/cdn/{id}"},
    {"pattern": "https://files.example.com/cdn/{base58id}"},
    "https://files.example.com/cdn/{id}",
])
def test_public_download_uses_manifest_host(url_format):
    assert _derive_public_url(
        "https://example.r2.cloudflarestorage.com/bucket/cdn/sample?signature=private",
        "cdn/sample", None, {"publicUrlFormat": url_format},
    ) == "https://files.example.com/cdn/sample"


def test_upload_defaults_to_registry_storage():
    args = cli.build_parser().parse_args(["upload", "report.txt"])
    assert args.server == "bsv-storage"
    assert args.retention == 525600


def test_upload_keeps_explicit_server_and_retention():
    args = cli.build_parser().parse_args(["upload", "report.txt", "https://other.example", "180"])
    assert args.server == "https://other.example"
    assert args.retention == 180
