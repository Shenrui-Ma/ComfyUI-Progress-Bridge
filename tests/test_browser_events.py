import json

from test_web_extension import WEB, run_state_module


def test_client_events_handle_legacy_progress_disconnect_and_queue_epochs():
    url = (WEB / "progress-client.mjs").as_uri()
    result = run_state_module(f"""
      import {{createClientState, reduceClientEvent}} from {json.dumps(url)};
      let s = createClientState();
      const event = (type, detail, now=100) => s = reduceClientEvent(s, type, detail, now);
      event('status', {{exec_info: {{queue_remaining: 1}}}});
      event('executing', {{prompt_id:'p', node:'1'}});
      event('progress', {{value:5, max:10}}, 200);
      const legacy = s;
      event('executing', {{prompt_id:'p', node:'2'}}, 300);
      const nextNode = s;
      event('execution_error', {{prompt_id:'p'}}, 400);
      event('executing', {{prompt_id:'p', node:null}}, 500);
      const failed = s;
      event('status', {{exec_info: {{queue_remaining: 0}}}}, 600);
      event('status', {{exec_info: {{queue_remaining: 1}}}}, 700);
      const newQueue = s;
      event('status', {{exec_info: {{queue_remaining: -1}}}}, 800);
      event('status', {{exec_info: {{queue_remaining: true}}}}, 900);
      const malformedIgnored = s === newQueue;
      event('reconnecting', null, 1000);
      const disconnected = s;
      event('reconnected', null, 1100);
      console.log(JSON.stringify({{legacy,nextNode,failed,newQueue,malformedIgnored,
        disconnected,reconnected:s}}));
    """)
    assert result["legacy"]["task"]["percent"] == 50
    assert result["legacy"]["updatedAt"] == 200
    assert result["nextNode"]["task"]["percent"] == 0
    assert result["failed"]["task"]["status"] == "error"
    assert result["newQueue"]["task"] is None
    assert result["newQueue"]["queue"] == 1
    assert result["malformedIgnored"]
    assert result["disconnected"]["connection"] == "offline"
    assert result["disconnected"]["queue"] is None
    assert result["disconnected"]["task"] is None
    assert result["reconnected"]["connection"] == "connecting"


def test_client_events_do_not_misattribute_progress_or_regress_terminal_tasks():
    url = (WEB / "progress-client.mjs").as_uri()
    result = run_state_module(f"""
      import {{createClientState, reduceClientEvent as reduce}} from {json.dumps(url)};
      const initial=createClientState();
      const unscoped=reduce(initial,'progress',{{value:1,max:2}},10);
      let s=reduce(initial,'executing',{{prompt_id:'active',node:'1'}},20);
      const mismatch=reduce(s,'execution_success',{{prompt_id:'other'}},30);
      s=reduce(s,'execution_success',{{prompt_id:'active'}},40);
      const terminal=s;
      s=reduce(s,'progress',{{prompt_id:'active',value:1,max:2}},50);
      const nullStatus=reduce(s,'status',null,60);
      console.log(JSON.stringify({{unscoped:unscoped===initial,
        mismatchTask:mismatch.task,late:s===terminal,nullStatus}}));
    """)
    assert result["unscoped"]
    assert result["mismatchTask"]["promptId"] == "active"
    assert result["mismatchTask"]["status"] == "running"
    assert result["late"]
    assert result["nullStatus"]["connection"] == "offline"


def test_comfy_frontend_dispatches_executing_as_node_id_and_null():
    url = (WEB / "progress-client.mjs").as_uri()
    result = run_state_module(f"""
      import {{createClientState, reduceClientEvent as reduce}} from {json.dumps(url)};
      let s=createClientState();
      s=reduce(s,'execution_start',{{prompt_id:'p'}},1);
      s=reduce(s,'executing','4',2);
      s=reduce(s,'progress',{{value:3,max:10}},3);
      const active=s;
      s=reduce(s,'execution_error',{{prompt_id:'p'}},4);
      s=reduce(s,'executing',null,5);
      console.log(JSON.stringify({{active,failed:s}}));
    """)
    assert result["active"]["task"]["nodeId"] == "4"
    assert result["active"]["task"]["percent"] == 30
    assert result["failed"]["task"]["status"] == "error"


def test_initial_status_fetch_uses_counts_only_and_cannot_overwrite_live_events():
    url = (WEB / "progress-client.mjs").as_uri()
    result = run_state_module(f"""
      import {{initializeQueueStatus}} from {json.dumps(url)};
      const requests=[], events=[];
      const api={{async fetchApi(path,options){{requests.push(path);
        if(!options.signal) throw new Error('missing deadline');
        return {{ok:true,async json(){{return {{exec_info:{{queue_remaining:0}}}};}}}};}}}};
      const dispatch=(...args)=>events.push(args);
      await initializeQueueStatus(api,dispatch,()=>true);
      await initializeQueueStatus(api,dispatch,()=>false);
      console.log(JSON.stringify({{requests,events}}));
    """)
    assert result["requests"] == ["/prompt", "/prompt"]
    assert result["events"] == [["status", {"exec_info": {"queue_remaining": 0}}]]
