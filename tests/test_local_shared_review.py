from __future__ import annotations

import importlib.util
import json
import multiprocessing
import shutil
import subprocess
import threading
from collections.abc import Iterator
from http.client import HTTPConnection
from pathlib import Path
from types import ModuleType
from typing import Protocol

import pytest

ROOT = Path(__file__).parents[1]
SERVER_PATH = ROOT / "docs" / "mockups" / "serve_local_shared_review.py"
PAGE_PATH = ROOT / "docs" / "mockups" / "local-shared-reconcile-audit.html"


def load_server() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "serve_local_shared_review", SERVER_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Barrier(Protocol):
    def wait(self) -> int: ...


def append_feedback_worker(path: str, prefix: str, barrier: Barrier) -> None:
    module = load_server()
    barrier.wait()
    for index in range(20):
        module.append_feedback(
            Path(path),
            {
                "id": f"{prefix}-{index}",
                "section": "summary",
                "note": "concurrent note",
                "author": prefix,
                "created_at": "2026-08-22T00:00:00+00:00",
            },
        )


@pytest.fixture
def review_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[str, int, Path]]:
    module = load_server()
    feedback = tmp_path / "private" / "feedback.json"
    monkeypatch.setenv("SETFORGE_REVIEW_FEEDBACK_FILE", str(feedback))
    server = module.ReviewServer(("127.0.0.1", 0), module.ReviewHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        host = server.server_address[0]
        port = server.server_address[1]
        yield str(host), int(port), feedback
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def request(
    server: tuple[str, int, Path],
    method: str,
    path: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    host, port, _ = server
    connection = HTTPConnection(host, port, timeout=2)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    payload = response.read()
    response_headers = dict(response.getheaders())
    connection.close()
    return response.status, response_headers, payload


def test_page_has_unique_review_sections() -> None:
    from html.parser import HTMLParser

    class AuditParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.ids: list[str] = []
            self.review_sections: list[str] = []

        def handle_starttag(
            self, tag: str, attrs: list[tuple[str, str | None]]
        ) -> None:
            values = dict(attrs)
            if identifier := values.get("id"):
                self.ids.append(identifier)
            if tag == "section" and (review_id := values.get("data-review-id")):
                self.review_sections.append(review_id)

    parser = AuditParser()
    parser.feed(PAGE_PATH.read_text(encoding="utf-8"))
    assert parser.ids == list(dict.fromkeys(parser.ids))
    assert set(parser.review_sections) == {
        "summary",
        "reconciliation",
        "workflow",
        "adoption",
        "feedback",
        "remaining",
    }


def test_health_redirect_and_static_page(review_server: tuple[str, int, Path]) -> None:
    status, _, body = request(review_server, "GET", "/api/health")
    assert status == 200
    assert json.loads(body) == {"status": "ok"}

    status, headers, _ = request(review_server, "GET", "/")
    assert status == 302
    assert headers["Location"] == "/local-shared-reconcile-audit.html"

    status, _, body = request(
        review_server, "GET", "/local-shared-reconcile-audit.html"
    )
    assert status == 200
    assert b"SetForge convergence audit" in body


def test_feedback_round_trip_is_persisted_privately(
    review_server: tuple[str, int, Path],
) -> None:
    payload = json.dumps(
        {"section": "summary", "note": "Clear and accurate", "author": "Raul"}
    ).encode()
    status, _, body = request(
        review_server,
        "POST",
        "/api/feedback",
        payload,
        {"Content-Type": "application/json", "Content-Length": str(len(payload))},
    )
    assert status == 201
    created: dict[str, object] = json.loads(body)["feedback"]
    assert created["section"] == "summary"

    status, headers, body = request(review_server, "GET", "/api/feedback")
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    assert json.loads(body)["feedback"] == [created]
    assert json.loads(review_server[2].read_text(encoding="utf-8")) == [created]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"not-json", "invalid JSON"),
        (b"[]", "feedback must be an object"),
        (b'{"section":"summary"}', "feedback fields must be strings"),
        (b'{"section":"","note":"x","author":"r"}', "section and note are required"),
    ],
)
def test_invalid_feedback_is_rejected(
    review_server: tuple[str, int, Path], body: bytes, expected: str
) -> None:
    status, _, payload = request(
        review_server,
        "POST",
        "/api/feedback",
        body,
        {"Content-Type": "application/json", "Content-Length": str(len(body))},
    )
    assert status == 400
    assert json.loads(payload) == {"error": expected}
    assert not review_server[2].exists()


def test_oversized_feedback_is_rejected(review_server: tuple[str, int, Path]) -> None:
    status, _, payload = request(
        review_server,
        "POST",
        "/api/feedback",
        b"",
        {"Content-Type": "application/json", "Content-Length": "32769"},
    )
    assert status == 400
    assert json.loads(payload) == {"error": "feedback body is empty or too large"}


def test_corrupt_feedback_is_preserved_and_refused(
    review_server: tuple[str, int, Path],
) -> None:
    corrupt = b'{"truncated":'
    review_server[2].parent.mkdir(parents=True)
    review_server[2].write_bytes(corrupt)
    body = b'{"section":"summary","note":"new","author":"r"}'
    status, _, payload = request(
        review_server,
        "POST",
        "/api/feedback",
        body,
        {"Content-Type": "application/json", "Content-Length": str(len(body))},
    )
    assert status == 409
    assert json.loads(payload) == {
        "error": "persisted feedback is unreadable or invalid"
    }
    assert review_server[2].read_bytes() == corrupt


def test_structurally_invalid_feedback_is_preserved_and_refused(
    review_server: tuple[str, int, Path],
) -> None:
    invalid = b"[null]\n"
    review_server[2].parent.mkdir(parents=True)
    review_server[2].write_bytes(invalid)
    body = b'{"section":"summary","note":"new","author":"r"}'
    status, _, payload = request(
        review_server,
        "POST",
        "/api/feedback",
        body,
        {"Content-Type": "application/json", "Content-Length": str(len(body))},
    )
    assert status == 409
    assert json.loads(payload) == {
        "error": "persisted feedback contains an invalid entry"
    }
    assert review_server[2].read_bytes() == invalid


def test_invalid_utf8_feedback_returns_conflict_and_preserves_bytes(
    review_server: tuple[str, int, Path],
) -> None:
    invalid = b"[\xff]\n"
    review_server[2].parent.mkdir(parents=True)
    review_server[2].write_bytes(invalid)
    status, _, payload = request(review_server, "GET", "/api/feedback")
    assert status == 409
    assert json.loads(payload) == {
        "error": "persisted feedback is unreadable or invalid"
    }
    assert review_server[2].read_bytes() == invalid


def test_unknown_review_section_is_rejected(
    review_server: tuple[str, int, Path],
) -> None:
    body = b'{"section":"typo","note":"new","author":"r"}'
    status, _, payload = request(
        review_server,
        "POST",
        "/api/feedback",
        body,
        {"Content-Type": "application/json", "Content-Length": str(len(body))},
    )
    assert status == 400
    assert json.loads(payload) == {"error": "unknown review section"}
    assert not review_server[2].exists()


def test_cross_origin_and_non_json_feedback_are_rejected(
    review_server: tuple[str, int, Path],
) -> None:
    body = b'{"section":"summary","note":"new","author":"r"}'
    status, _, _ = request(
        review_server,
        "POST",
        "/api/feedback",
        body,
        {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "Origin": "https://attacker.example",
        },
    )
    assert status == 403
    status, _, _ = request(
        review_server,
        "POST",
        "/api/feedback",
        body,
        {"Content-Type": "text/plain", "Content-Length": str(len(body))},
    )
    assert status == 415
    assert not review_server[2].exists()


def test_server_accepts_loopback_hosts_only() -> None:
    module = load_server()
    assert module.host_is_loopback("127.0.0.1")
    assert module.host_is_loopback("::1")
    assert not module.host_is_loopback("0.0.0.0")


def test_feedback_appends_are_serialized_across_processes(tmp_path: Path) -> None:
    feedback = tmp_path / "feedback.json"
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(4)
    processes = [
        context.Process(
            target=append_feedback_worker,
            args=(str(feedback), str(index), barrier),
        )
        for index in range(4)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    entries = json.loads(feedback.read_text(encoding="utf-8"))
    assert len(entries) == 80
    assert len({entry["id"] for entry in entries}) == 80


def test_inline_annotation_ui_executes(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable for executable inline-JavaScript verification")
    page = PAGE_PATH.read_text(encoding="utf-8")
    script = page.rsplit("<script>", 1)[1].split("</script>", 1)[0]
    harness = r"""
class Element {
  constructor(id='') {
    this.id=id;
    this.dataset={};
    this.children=[];
    this.listeners={};
    this.textContent='';
    this.className='';
  }
  addEventListener(name, callback) { this.listeners[name]=callback; }
  append(child) { this.children.push(child); }
  replaceChildren() { this.children=[]; }
  querySelector(selector) { return selector === '.notes' ? this.notes : null; }
  closest() { return this.section; }
}
const notes = new Element('notes');
const section = new Element('summary');
section.dataset.reviewId='summary';
section.notes=notes;
const button = new Element('button');
button.section=section;
const fakeDialog = new Element('dialog');
fakeDialog.open=false;
fakeDialog.showModal=()=>fakeDialog.open=true;
fakeDialog.close=()=>fakeDialog.open=false;
const fakeError = new Element('error');
const fakeForm = new Element('form');
fakeForm.elements={section:{value:''}};
fakeForm.reset=()=>{fakeForm.elements.section.value='';};
const selected = {
  '#review-dialog':fakeDialog,
  '#review-form':fakeForm,
  '#review-error':fakeError,
};
global.document = {
  querySelector: selector => selected[selector],
  querySelectorAll: selector => {
    if (selector === '[data-review-id]') return [section];
    if (selector === '.review-btn') return [button];
    if (selector === '.notes') return [notes];
    return [];
  },
  createElement: () => new Element(),
};
global.FormData = class {
  *[Symbol.iterator]() {
    yield ['section','summary'];
    yield ['note','saved'];
    yield ['author','Reviewer'];
  }
};
let responses = [{ok:true,json:async()=>({feedback:[]})}];
global.fetch = async () => responses.shift();
"""
    assertions = r"""
;(async () => {
  await new Promise(resolve => setTimeout(resolve, 0));
  button.listeners.click();
  if (!fakeDialog.open || fakeForm.elements.section.value !== 'summary') {
    throw new Error('annotate wiring failed');
  }
  responses.push(
    {ok:true,json:async()=>({feedback:{}})},
    {ok:true,json:async()=>({
      feedback:[{section:'summary',author:'Reviewer',note:'saved'}],
    })},
  );
  await fakeForm.listeners.submit({
    preventDefault(){},
    submitter:{value:'submit'},
  });
  const rendered = notes.children[0]?.textContent.includes('saved');
  if (fakeDialog.open || notes.children.length !== 1 || !rendered) {
    throw new Error('save/render failed');
  }
  button.listeners.click();
  responses.push({ok:false,json:async()=>({error:'refused'})});
  await fakeForm.listeners.submit({
    preventDefault(){},
    submitter:{value:'submit'},
  });
  if (fakeError.textContent !== 'refused') {
    throw new Error('error display failed');
  }
})().catch(failure => {
  console.error(failure);
  process.exitCode=1;
});
"""
    program = tmp_path / "annotation-ui.mjs"
    program.write_text(harness + script + assertions, encoding="utf-8")
    subprocess.run([node, "--check", str(program)], check=True)
    subprocess.run([node, str(program)], check=True)
