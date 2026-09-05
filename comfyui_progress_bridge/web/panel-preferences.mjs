export const PANEL_STORAGE_KEY = "comfy-progress-bridge-browser-settings-v1";

export const DEFAULT_PANEL_PREFERENCES = Object.freeze({
  language: "auto",
  theme: "system",
  opacity: 92,
  scale: 100,
  collapsed: false,
  position: null,
});

const THEMES = new Set(["system", "dark", "light"]);

function boundedNumber(value, minimum, maximum, fallback) {
  return Number.isFinite(value) && value >= minimum && value <= maximum
    ? Math.round(value)
    : fallback;
}

export function sanitizePanelPreferences(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return { ...DEFAULT_PANEL_PREFERENCES };
  }
  const position = value.position;
  const validPosition = position
    && typeof position === "object"
    && !Array.isArray(position)
    && Number.isFinite(position.x)
    && Number.isFinite(position.y);
  return {
    language: ["auto", "en-US", "zh-CN", "ja-JP", "ko-KR"].includes(value.language)
      ? value.language : "auto",
    theme: THEMES.has(value.theme) ? value.theme : DEFAULT_PANEL_PREFERENCES.theme,
    opacity: boundedNumber(value.opacity, 55, 100, DEFAULT_PANEL_PREFERENCES.opacity),
    scale: boundedNumber(value.scale, 80, 125, DEFAULT_PANEL_PREFERENCES.scale),
    collapsed: typeof value.collapsed === "boolean"
      ? value.collapsed
      : DEFAULT_PANEL_PREFERENCES.collapsed,
    position: validPosition
      ? { x: Math.round(position.x), y: Math.round(position.y) }
      : null,
  };
}

export function readPanelPreferences(storage) {
  try {
    const raw = storage?.getItem?.(PANEL_STORAGE_KEY);
    if (raw && raw.length > 4096) return { ...DEFAULT_PANEL_PREFERENCES };
    return raw ? sanitizePanelPreferences(JSON.parse(raw)) : {
      ...DEFAULT_PANEL_PREFERENCES,
      collapsed: storage?.getItem?.("comfy-progress-bridge-collapsed") === "1",
    };
  } catch {
    return { ...DEFAULT_PANEL_PREFERENCES };
  }
}

// Calculate untransformed dimensions first so scale never pushes reset/default offscreen.
export function panelLayout(position, percent, viewport) {
  const scale = boundedNumber(percent, 80, 125, 100) / 100;
  const width = Math.max(1, Math.min(310, (viewport.width - 16) / scale));
  const maxHeight = Math.max(1, (viewport.height - 16) / scale);
  const preferred = position ?? { x: viewport.width - width * scale - 16, y: 58 };
  const point = clampPanelPosition(preferred, {width: width * scale, height: 0}, viewport);
  const y = Math.min(point.y, Math.max(8, viewport.height - 48));
  return { x: point.x, y, width, scale,
    maxHeight: Math.min(maxHeight, Math.max(1, (viewport.height - y - 8) / scale)) };
}

export function writePanelPreferences(storage, preferences) {
  const safe = sanitizePanelPreferences(preferences);
  try {
    storage?.setItem?.(PANEL_STORAGE_KEY, JSON.stringify(safe));
  } catch {
    // Restricted storage must not prevent the panel from working.
  }
  return safe;
}

export function clampPanelPosition(position, panelSize, viewportSize, margin = 8) {
  const safeMargin = Number.isFinite(margin) && margin >= 0 ? margin : 8;
  const panelWidth = Number.isFinite(panelSize?.width) && panelSize.width >= 0
    ? panelSize.width
    : 0;
  const panelHeight = Number.isFinite(panelSize?.height) && panelSize.height >= 0
    ? panelSize.height
    : 0;
  const viewportWidth = Number.isFinite(viewportSize?.width) && viewportSize.width >= 0
    ? viewportSize.width
    : 0;
  const viewportHeight = Number.isFinite(viewportSize?.height) && viewportSize.height >= 0
    ? viewportSize.height
    : 0;
  const maximumX = Math.max(safeMargin, viewportWidth - panelWidth - safeMargin);
  const maximumY = Math.max(safeMargin, viewportHeight - panelHeight - safeMargin);
  const x = Number.isFinite(position?.x) ? position.x : safeMargin;
  const y = Number.isFinite(position?.y) ? position.y : safeMargin;
  return {
    x: Math.round(Math.min(maximumX, Math.max(safeMargin, x))),
    y: Math.round(Math.min(maximumY, Math.max(safeMargin, y))),
  };
}
