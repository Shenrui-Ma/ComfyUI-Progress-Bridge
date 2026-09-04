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
    assert 'className = "cpb-settings-button"' in source
    assert 'className = "cpb-drag-handle"' in source
    assert 'header.addEventListener("pointerdown"' in source
    assert 'window.addEventListener("pointermove"' in source
    assert 'window.addEventListener("pointerup"' in source
    assert 'window.addEventListener("resize"' in source
    assert "Reset position" in source
    assert ".innerHTML" not in source


def test_browser_panel_preferences_are_bounded_and_fail_closed():
    module_url = (WEB / "panel-preferences.mjs").as_uri()
    output = run_state_module(
        f"""
        import {{
          DEFAULT_PANEL_PREFERENCES,
          readPanelPreferences,
          sanitizePanelPreferences,
          writePanelPreferences,
        }} from {json.dumps(module_url)};
        const hostileStorage = {{
          getItem() {{ throw new Error("blocked"); }},
          setItem() {{ throw new Error("blocked"); }},
        }};
        const invalid = sanitizePanelPreferences({{
          theme: "neon",
          opacity: 1000,
          scale: -5,
          collapsed: "yes",
          position: {{x: "1", y: Infinity}},
        }});
        const written = [];
        const storage = {{
          getItem() {{ return JSON.stringify({{
            theme: "light", opacity: 63, scale: 115, collapsed: true,
            position: {{x: 20.2, y: 40.8}},
          }}); }},
          setItem(key, value) {{ written.push([key, JSON.parse(value)]); }},
        }};
        const loaded = readPanelPreferences(storage);
        writePanelPreferences(storage, loaded);
        console.log(JSON.stringify({{
          defaults: DEFAULT_PANEL_PREFERENCES,
          hostile: readPanelPreferences(hostileStorage),
          invalid,
          loaded,
          written,
        }}));
        """
    )

    expected_defaults = {
        "theme": "system",
        "opacity": 92,
        "scale": 100,
        "collapsed": False,
        "position": None,
    }
    assert output["defaults"] == expected_defaults
    assert output["hostile"] == expected_defaults
    assert output["invalid"] == expected_defaults
    assert output["loaded"] == {
        "theme": "light",
        "opacity": 63,
        "scale": 115,
        "collapsed": True,
        "position": {"x": 20, "y": 41},
    }
    assert output["written"][0][0] == "comfy-progress-bridge-browser-settings-v1"
    assert output["written"][0][1] == output["loaded"]


def test_browser_panel_position_is_clamped_inside_viewport():
    module_url = (WEB / "panel-preferences.mjs").as_uri()
    output = run_state_module(
        f"""
        import {{ clampPanelPosition }} from {json.dumps(module_url)};
        console.log(JSON.stringify({{
          ordinary: clampPanelPosition({{x: 900, y: -40}}, {{width: 300, height: 220}},
            {{width: 1024, height: 768}}),
          tiny: clampPanelPosition({{x: 50, y: 80}}, {{width: 400, height: 300}},
            {{width: 240, height: 180}}),
        }}));
        """
    )

    assert output == {
        "ordinary": {"x": 716, "y": 8},
        "tiny": {"x": 8, "y": 8},
    }


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
