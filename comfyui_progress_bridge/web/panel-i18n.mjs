const keys = ["title", "move", "settings", "collapse", "expand", "theme", "system", "dark",
  "light", "opacity", "scale", "reset", "language", "auto", "status", "server", "node",
  "queue", "updated", "idle", "running", "success", "error", "interrupted", "offline",
  "connecting", "queued", "note"];
const translations = {
  "en-US": ["ComfyUI Progress", "Move progress panel", "Progress panel settings",
    "Collapse progress panel", "Expand progress panel", "Theme", "System", "Dark", "Light",
    "Opacity", "Scale", "Reset position", "Language", "Automatic", "Status", "Server", "Node",
    "Queue", "Updated", "Idle", "Running", "Complete", "Failed", "Interrupted", "Offline",
    "Connecting", "Queue busy",
    "Browser appearance only. Telegram/Weixin notifications are configured on the ComfyUI host."],
  "zh-CN": ["ComfyUI 进度", "移动进度面板", "进度面板设置", "折叠进度面板", "展开进度面板",
    "主题", "跟随系统", "深色", "浅色", "不透明度", "缩放", "重置位置", "语言", "自动",
    "状态", "服务", "节点", "队列", "更新", "空闲", "运行中", "已完成", "失败", "已中断",
    "已断开", "连接中", "队列忙碌", "此处仅设置浏览器外观。Telegram/微信通知需在 ComfyUI 主机上配置。"],
  "ja-JP": ["ComfyUI 進捗", "進捗パネルを移動", "進捗パネル設定", "折りたたむ", "展開",
    "テーマ", "システム", "ダーク", "ライト", "不透明度", "拡大率", "位置をリセット", "言語",
    "自動", "状態", "サーバー", "ノード", "キュー", "更新", "待機", "実行中", "完了", "失敗",
    "中断", "オフライン", "接続中", "キュー実行中",
    "ブラウザーの外観設定です。Telegram/Weixin 通知は ComfyUI ホストで設定します。"],
  "ko-KR": ["ComfyUI 진행률", "진행 패널 이동", "진행 패널 설정", "접기", "펼치기",
    "테마", "시스템", "어둡게", "밝게", "불투명도", "크기", "위치 초기화", "언어", "자동",
    "상태", "서버", "노드", "대기열", "업데이트", "대기", "실행 중", "완료", "실패", "중단",
    "오프라인", "연결 중", "대기열 실행 중",
    "브라우저 외관 설정입니다. Telegram/Weixin 알림은 ComfyUI 호스트에서 설정하세요."],
};

export function panelLanguage(preference, browserLanguage = "en-US") {
  if (preference !== "auto" && Object.hasOwn(translations, preference)) return preference;
  const prefix = String(browserLanguage).toLowerCase().split("-")[0];
  return {zh:"zh-CN",ja:"ja-JP",ko:"ko-KR"}[prefix] || "en-US";
}

export function panelText(language, key) {
  const index = keys.indexOf(key);
  return (translations[language] || translations["en-US"])[index] ?? key;
}
