"""설정 오류의 실제 템플릿 JS를 Node DOM 대역으로 실행한다."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ddak.web.routes.setup import _public_form


@pytest.mark.parametrize("response_kind", [400, 409, 502, "network"])
@pytest.mark.parametrize(
    "payload",
    [
        "public",
        "password",
        "nested",
        "url",
        "malformed",
        "unfinished",
        "single",
        "empty-user",
        "slash",
    ],
)
def test_inventory_error_keeps_only_server_redacted_input(response_kind, payload):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node 런타임 없음")
    template = Path(__file__).resolve().parents[3] / "src/ddak/web/templates/setup.html"
    script = re.search(r"<script>(.*?)</script>", template.read_text(), re.S).group(1)
    private = "fixture-private-inventory-value"
    raw = {
        "public": '{  "mode": "container", "tiers": {"was": {"port": 8080}}  }',
        "password": json.dumps({"mode": "container", "password": private}),
        "nested": json.dumps({"mode": "container", "tiers": {"was": {"api_key": private}}}),
        "url": json.dumps({"mode": "container", "url": "postgres://human:" + private + "@db/app"}),
        "malformed": json.dumps({"mode": "container", "password": private})[:-1],
        "unfinished": json.dumps({"mode": "container", "password": private})[:-2],
        "single": str({"mode": "container", "password": private}),
        "empty-user": json.dumps({"mode": "container", "url": "redis://:" + private + "@db/0"}),
        "slash": json.dumps({"mode": "container", "password": private})[:-2] + chr(92),
    }[payload]
    public = _public_form("inventory", {"inventory": raw})["inventory"]
    assert private not in public and "container" in public
    fixture = r"""
const vm = require('node:vm');
const data = JSON.parse(process.argv[1]);
const inventory = {value: data.raw};
const box = {id:'setup-feedback-inventory', hidden:true, childNodes:[],
  focus(){}, replaceChildren(...nodes){this.childNodes=nodes;}};
const button = {textContent:'저장', disabled:false, setAttribute(){}, removeAttribute(){}};
const form = {action:'/setup/inventory',dataset:{},elements:{namedItem(){return inventory;}},
  addEventListener(_, fn){this.submit=fn;},
  querySelector(selector){return selector.includes('button') ? button : box;}};
let submitted;
const context = {window:{fetch:true}, URLSearchParams,
  FormData: class {
    constructor(){this.entries=[['inventory',inventory.value]];}
    [Symbol.iterator](){return this.entries[Symbol.iterator]();}
  },
  document:{querySelectorAll(selector){return selector.startsWith('form') ? [form] : [];}},
  DOMParser: class {parseFromString(){return {
    getElementById(){return {hidden:false,childNodes:['오류']};},
    querySelector(){return {value:data.public};}
  };}},
  fetch:async (_, request)=>{
    submitted=request.body.get('inventory');
    if(data.kind==='network') throw Error('fixture offline');
    return {ok:false,status:data.kind,headers:{get(){return 'text/html';}},text:async()=>''};
  }
};
vm.runInNewContext(data.script,context);
form.submit({preventDefault(){}}).then(()=>console.log(JSON.stringify({
  value:inventory.value, sent:submitted, inline:!box.hidden, enabled:!button.disabled
}))).catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run(
        [
            node,
            "-e",
            fixture,
            json.dumps({"script": script, "raw": raw, "public": public, "kind": response_kind}),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    state = json.loads(result.stdout)
    assert state["sent"] == raw
    assert private not in state["value"]
    assert state["value"] == public
    assert state["inline"] and state["enabled"]
