import type { TaskStatus } from "@/lib/contracts"

export const agentLabels: Record<string, string> = {
  orchestrator: "Orchestrator",
  product_agent: "Danh mục sách",
  review_agent: "Đánh giá độc giả",
  trust_agent: "Trust signals",
  market_agent: "Snapshot stats",
}

export const phaseLabels: Record<string, string> = {
  "request.accepted": "Đã tiếp nhận",
  "routing.completed": "Đã định tuyến",
  "planning.completed": "Đã lập kế hoạch",
  "model.routing.completed": "Model định tuyến xong",
  "model.planning.completed": "Model lập kế hoạch xong",
  "agent.started": "Agent bắt đầu",
  "agent.completed": "Agent hoàn tất",
  "aggregation.completed": "Đã tổng hợp",
  "model.aggregation.completed": "Model tổng hợp xong",
}

export const statusLabels: Record<TaskStatus, string> = {
  pending: "Chờ",
  running: "Đang chạy",
  success: "Hoàn tất",
  partial_success: "Một phần",
  failed: "Thất bại",
}

export function friendlyAgent(agentId: string | null): string {
  if (!agentId) return "Toàn hệ thống"
  return agentLabels[agentId] ?? agentId
}

export function friendlyPhase(phase: string): string {
  return phaseLabels[phase] ?? phase
}

export function formatDuration(value: number): string {
  if (value < 1_000) return `${Math.round(value)} ms`
  return `${(value / 1_000).toFixed(2)} s`
}

export function shortenIdentifier(value: string, maxLength = 24): string {
  if (value.length <= maxLength) return value
  const visible = Math.max(6, Math.floor((maxLength - 1) / 2))
  return `${value.slice(0, visible)}…${value.slice(-visible)}`
}

export type ProvenanceAnchorScope = "desktop" | "mobile"

export function provenanceAnchorId(
  scope: ProvenanceAnchorScope,
  sourceId: string,
): string {
  return `provenance-${scope}-${encodeURIComponent(sourceId)}`
}
