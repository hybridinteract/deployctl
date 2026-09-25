"""The panel over HTTP: what a confirmation dialog is able to name.

A host-changing click asked "Update — This changes a running deployment.
Continue?" with no environment in it, while the picker holds staging and
production side by side. The dialog now names the environment, its hosts and
the saved image tag; these tests pin that the page carries those values and that
a Save hands back the new ones.
"""

from __future__ import annotations

import pathlib
import re
import sys

import pytest
from fastapi.testclient import TestClient


from deployctl.webui.panel import create_app, security  # noqa: E402

PANEL_JS = (pathlib.Path(__file__).resolve().parent.parent / "src" / "deployctl") / "webui" / "static" / "panel.js"

CONFIG = """\
MODE=single
PROJECT_NAME=demo
BASE_DOMAIN=demo.test
IMAGE_REPO=ghcr.io/acme/demo
IMAGE_TAG=fb31c25
HOSTS=203.0.113.10
ACME_EMAIL=ops@demo.test
POSTGRES_DB=demo
POSTGRES_USER=demo
"""


@pytest.fixture
def client(write_config):
    write_config("production", CONFIG)
    # Loopback, or the Host check (DNS-rebinding defence) refuses every request.
    return TestClient(create_app(), base_url="http://127.0.0.1")


def test_the_page_carries_what_the_dialog_names(client):
    page = client.get("/?env=production").text
    assert 'data-env="production"' in page
    assert 'data-hosts="203.0.113.10"' in page
    assert 'data-image="demo:fb31c25"' in page


def test_a_save_hands_back_the_new_tag(client):
    saved = client.post(f"/config?env=production&t={security.TOKEN}", data={"IMAGE_TAG": "1234abc"})
    assert saved.status_code == 200, saved.text
    assert 'data-image="demo:1234abc"' in saved.text


def test_the_dialog_names_environment_hosts_and_image():
    js = PANEL_JS.read_text()
    body = js[js.index("function confirmText"):js.index("function runAction")]
    for needed in ("ENV", "d.hosts", "d.image"):
        assert needed in body, f"confirmText no longer names {needed}"
    assert re.search(r"confirm\(confirmText\(label\)\)", js), "runAction must ask confirmText's question"
