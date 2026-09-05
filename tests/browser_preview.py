"""Loopback-only browser test fixture. No ComfyUI, models or third-party packages.

Run: python tests/browser_preview.py
Open http://127.0.0.1:18089. The page loads the real shipped extension unchanged.
"""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "comfyui_progress_bridge" / "web"
HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Progress Bridge isolated browser tests</title>
<style>body{background:#171a21;color:#f4f6fc;font:16px system-ui;margin:24px}
button{padding:8px;margin:4px} #results{white-space:pre-wrap} iframe{border:1px solid #888}</style>
</head><body><h1>Progress Bridge browser tests</h1>
<p>Simulated events; no inference or network messages.</p>
<button id="start">Run legacy progress</button><button id="node">Next node</button>
<button id="fail">Failure then terminal null</button><button id="finish">Success</button>
<button id="offline">Disconnect</button><button id="online">Reconnect</button>
<button id="small">Small viewport</button><button id="test">Run assertions</button>
<pre id="results"></pre>
<script type="module">
import {api} from '/scripts/api.js';
if(location.search.includes('blocked')) Object.defineProperty(window,'localStorage',{
  get(){throw new Error('storage blocked');}});
await import('/extensions/progress/progress-panel.js');
const send=(type,detail)=>api.dispatchEvent(new CustomEvent(type,{detail}));
const start=()=>{send('status',{exec_info:{queue_remaining:0}});
send('status',{exec_info:{queue_remaining:1}});send('execution_start',{prompt_id:'demo'});
send('executing','1');
send('progress',{value:5,max:10});};
document.querySelector('#start').onclick=start;
document.querySelector('#node').onclick=()=>send('executing','2');
document.querySelector('#fail').onclick=()=>{send('execution_error',{prompt_id:'demo'});
send('executing',null);send('status',{exec_info:{queue_remaining:0}});};
document.querySelector('#finish').onclick=()=>{send('execution_success',{prompt_id:'demo'});
send('status',{exec_info:{queue_remaining:0}});};
document.querySelector('#offline').onclick=()=>send('reconnecting');
document.querySelector('#online').onclick=()=>{send('reconnected');
send('status',{exec_info:{queue_remaining:0}});};
document.querySelector('#small').onclick=()=>{const f=document.createElement('iframe');
f.src='/?small';f.width='280';f.height='230';document.body.append(f);};
send('status',{exec_info:{queue_remaining:0}});
document.querySelector('#test').onclick=()=>{
 const report=[]; const check=(name,ok)=>{report.push((ok?'PASS ':'FAIL ')+name);};
 const p=document.querySelector('#comfy-progress-bridge-panel');
 check('one panel',document.querySelectorAll('#comfy-progress-bridge-panel').length===1);
 let leakedKey=false;
 const keyboard=()=>{leakedKey=true;};
 document.addEventListener('keydown',keyboard);
 p.querySelector('.cpb-drag-handle').dispatchEvent(new KeyboardEvent('keydown',{
   key:'ArrowLeft',bubbles:true,cancelable:true}));
 document.removeEventListener('keydown',keyboard);
 check('panel keys do not reach canvas',!leakedKey);
 const header=p.querySelector('.cpb-header');
 const before=p.getBoundingClientRect();
 header.dispatchEvent(new PointerEvent('pointerdown',{
   pointerId:99,button:0,isPrimary:true,clientX:before.x+40,clientY:before.y+20,bubbles:true}));
 window.dispatchEvent(new PointerEvent('pointermove',{
   pointerId:99,clientX:before.x+10,clientY:before.y+10}));
 const moved=p.getBoundingClientRect();
 window.dispatchEvent(new PointerEvent('pointercancel',{pointerId:99}));
 check('touch cancellation clears drag',!p.classList.contains('cpb-dragging'));
 check('pointer movement works without capture',moved.x!==before.x||moved.y!==before.y);
 start();check('legacy progress',p.dataset.status==='running'&&p.textContent.includes('50%'));
 document.querySelector('#node').click();
 check('node progress resets',p.querySelector('.cpb-fill').style.width==='0%');
 document.querySelector('#fail').click();check('error preserved',p.dataset.status==='error');
 document.querySelector('#offline').click();check('disconnect',p.dataset.status==='offline');
 document.querySelector('#online').click();check('reconnect',p.dataset.status==='idle');
 const settings=p.querySelector('.cpb-settings-button');settings.click();
 check('settings opens',!p.querySelector('.cpb-settings').hidden);
 for(const language of ['en-US','zh-CN','ja-JP','ko-KR']){
   const select=p.querySelector('#cpb-language');select.value=language;
   select.dispatchEvent(new Event('change',{bubbles:true}));
   check('language '+language,p.querySelector('.cpb-title').textContent.length>0);
 }
 p.querySelector('#cpb-language').value='en-US';
 p.querySelector('#cpb-language').dispatchEvent(new Event('change',{bubbles:true}));
 p.querySelector('#cpb-scale').value='125';
 p.querySelector('#cpb-scale').dispatchEvent(new Event('input',{bubbles:true}));
 p.querySelector('.cpb-reset-position').click();
 const r=p.getBoundingClientRect();
 check('125% reset fits viewport',r.left>=0&&r.top>=0
   &&r.right<=innerWidth+1&&r.bottom<=innerHeight+1);
 p.querySelector('#cpb-scale').value='100';
 p.querySelector('#cpb-scale').dispatchEvent(new Event('input',{bubbles:true}));
 settings.click();document.querySelector('#results').textContent=report.join('\\n');
};
</script></body></html>"""
APP = """export const app = {
  graph: {getNodeById(id){return {title:id==='1'?'KSampler':'VAE Decode'};}},
  registerExtension(extension){extension.setup();}
};"""
API = "export const api = new EventTarget();"


class PreviewHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/":
            body, kind = HTML.encode(), "text/html"
        elif path == "/scripts/app.js":
            body, kind = APP.encode(), "text/javascript"
        elif path == "/scripts/api.js":
            body, kind = API.encode(), "text/javascript"
        elif path.startswith("/extensions/progress/"):
            name = path.removeprefix("/extensions/progress/")
            if "/" in name or name not in {p.name for p in WEB.iterdir()}:
                self.send_error(404)
                return
            body, kind = (WEB / name).read_bytes(), "text/javascript"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18089)
    arguments = parser.parse_args()
    ThreadingHTTPServer(("127.0.0.1", arguments.port), PreviewHandler).serve_forever()
