import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
WEB = ROOT / "comfyui_progress_bridge" / "web"


def run_state_module(script: str) -> dict:
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_comfyui_entrypoints_export_the_frontend_directory():
    package_source = (ROOT / "comfyui_progress_bridge" / "__init__.py").read_text()
    root_source = (ROOT / "__init__.py").read_text()
    assert 'WEB_DIRECTORY = "./web"' in package_source
    assert 'WEB_DIRECTORY = "./comfyui_progress_bridge/web"' in root_source


def test_browser_extension_registers_and_listens_for_bridge_events():
    source = (WEB / "progress-panel.js").read_text()
    assert "app.registerExtension({" in source
    assert 'name: "Comfy.ProgressBridge.Panel"' in source
    for event_type in (
        "executing",
        "progress",
        "execution_error",
        "execution_interrupted",
        "execution_success",
        "status",
    ):
        assert f'api.addEventListener("{event_type}"' in source
    assert "function readCollapsed()" in source
    assert "function writeCollapsed(collapsed)" in source
    assert ".innerHTML" not in source


def test_browser_state_reduces_progress_and_terminal_events():
    module_url = (WEB / "progress-state.mjs").as_uri()
    output = run_state_module(
        f"""
        import {{ createState, reduceEvent, viewModel }} from {json.dumps(module_url)};
        let state = createState();
        state = reduceEvent(state, {{
          type: "progress",
          data: {{prompt_id: "abc", node: "7", value: 3, max: 10}}
        }});
        const running = viewModel(state);
        state = reduceEvent(state, {{
          type: "execution_success", data: {{prompt_id: "abc"}}
        }});
        console.log(JSON.stringify({{running, finished: viewModel(state)}}));
        """
    )

    assert output["running"] == {
        "status": "running",
        "promptId": "abc",
        "nodeId": "7",
        "value": 3,
        "max": 10,
        "percent": 30,
    }
    assert output["finished"]["status"] == "success"
    assert output["finished"]["percent"] == 100


def test_browser_state_ignores_invalid_and_out_of_order_events():
    module_url = (WEB / "progress-state.mjs").as_uri()
    output = run_state_module(
        f"""
        import {{ createState, reduceEnvelope, viewModel }} from {json.dumps(module_url)};
        let state = createState();
        state = reduceEnvelope(state, {{schema: 2, instance_id: "one", sequence: 2,
          type: "progress", data: {{prompt_id: "abc", value: 8, max: 10}}}});
        state = reduceEnvelope(state, {{schema: 2, instance_id: "one", sequence: 1,
          type: "progress", data: {{prompt_id: "abc", value: 1, max: 10}}}});
        state = reduceEnvelope(state, {{schema: 99, instance_id: "one", sequence: 3,
          type: "progress", data: {{prompt_id: "abc", value: 0, max: 10}}}});
        console.log(JSON.stringify(viewModel(state)));
        """
    )

    assert output["value"] == 8
    assert output["percent"] == 80


def test_browser_state_reconciles_idle_and_does_not_retain_prompt_history():
    module_url = (WEB / "progress-state.mjs").as_uri()
    output = run_state_module(
        f"""
        import {{ createState, reduceEnvelope }} from {json.dumps(module_url)};
        let state = createState();
        for (let i = 1; i <= 50; i++) {{
          state = reduceEnvelope(state, {{schema: 2, instance_id: `srv-${{i}}`, sequence: 1,
            type: "progress", data: {{prompt_id: `p-${{i}}`, node: `${{i}}`, value: 1, max: 2}}}});
        }}
        state = reduceEnvelope(state, {{schema: 2, instance_id: "browser", sequence: 1,
          type: "idle", data: {{}}}});
        console.log(JSON.stringify(state));
        """
    )

    assert output["task"] is None
    assert output["instanceId"] == "browser"
    assert "tasks" not in output
    assert "latestSequenceByInstance" not in output