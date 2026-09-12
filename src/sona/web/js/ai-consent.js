import { confirmAction } from "./dialog.js";

export async function confirmAIUsage(isCurrent = () => true) {
  const required = await window.pywebview.api.ai_usage_notice_required();
  if (!isCurrent()) return false;
  if (!required) return true;
  return confirmAction({
    title: "使用 AI 整理？",
    message: "转录文字会发送给你设置的默认 AI 模型，可能产生费用。确认后，后续整理与重试不再重复提示。",
    confirmLabel: "开始整理",
  });
}
