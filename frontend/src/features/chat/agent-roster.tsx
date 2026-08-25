import {
  ChartNoAxesCombined,
  CircleCheck,
  CircleDashed,
  Compass,
  MessageSquareText,
  PackageSearch,
  ShieldCheck,
  type LucideIcon,
} from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"
import type {
  AgentExecution,
  GatewayStatusEvent,
  TaskStatus,
} from "@/lib/contracts"
import { statusLabels } from "@/features/chat/presentation"

interface AgentRosterProps {
  executions: AgentExecution[]
  statuses: GatewayStatusEvent[]
  requestPhase: "idle" | "streaming" | "completed" | "failed" | "cancelled"
  compact?: boolean
}

interface AgentDefinition {
  id: string
  name: string
  role: string
  index: string
  icon: LucideIcon
}

const agents: AgentDefinition[] = [
  {
    id: "orchestrator",
    name: "Orchestrator",
    role: "Định tuyến · lập DAG · tổng hợp",
    index: "00",
    icon: Compass,
  },
  {
    id: "product_agent",
    name: "Product Agent",
    role: "Danh mục · giá · thuộc tính",
    index: "01",
    icon: PackageSearch,
  },
  {
    id: "review_agent",
    name: "Review Agent",
    role: "Cảm xúc · khía cạnh · tóm tắt",
    index: "02",
    icon: MessageSquareText,
  },
  {
    id: "trust_agent",
    name: "Trust Agent",
    role: "Khiếu nại · rủi ro · độ tin cậy",
    index: "03",
    icon: ShieldCheck,
  },
  {
    id: "market_agent",
    name: "Market Agent",
    role: "Xu hướng · phân khúc · thị trường",
    index: "04",
    icon: ChartNoAxesCombined,
  },
]

export function AgentRoster({
  executions,
  statuses,
  requestPhase,
  compact = false,
}: AgentRosterProps) {
  return (
    <section
      aria-labelledby="agent-roster-title"
      className={cn(
        "atlas-panel overflow-hidden",
        compact ? "border-0" : "flex flex-col xl:h-[calc(100svh-5.5rem)]",
      )}
    >
      <header className="flex items-end justify-between border-b px-4 py-4">
        <div>
          <h2 id="agent-roster-title" className="font-display text-xl font-semibold">
            Trạm chuyên gia
          </h2>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">
            Năng lực được ủy quyền cố định
          </p>
        </div>
        <span className="atlas-data text-muted-foreground">05 TRẠM</span>
      </header>

      <ol className="relative px-3 py-2">
        <span
          aria-hidden="true"
          className="absolute top-9 bottom-9 left-[2.08rem] w-px bg-border"
        />
        {agents.map((agent) => {
          const status = resolveAgentStatus(
            agent.id,
            executions,
            statuses,
            requestPhase,
          )
          const Icon = agent.icon
          const active = status === "running"
          return (
            <li key={agent.id} className="relative">
              <div
                className={cn(
                  "group flex min-h-20 items-center gap-3 rounded-lg px-2 py-2 transition-colors",
                  active && "bg-accent/8",
                )}
              >
                <span
                  className={cn(
                    "relative z-10 grid size-9 shrink-0 place-items-center rounded-full border bg-card text-muted-foreground",
                    active && "border-accent bg-accent text-accent-foreground",
                    status === "success" &&
                      "border-success bg-success text-success-foreground",
                    status === "partial_success" && "border-accent text-accent",
                    status === "failed" &&
                      "border-destructive bg-destructive text-destructive-foreground",
                  )}
                >
                  <Icon aria-hidden="true" className="size-4" />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="flex items-center justify-between gap-2">
                    <span className="truncate text-sm font-semibold">{agent.name}</span>
                    <span className="atlas-data text-muted-foreground">
                      {agent.index}
                    </span>
                  </span>
                  <span className="mt-0.5 block truncate text-[0.72rem] text-muted-foreground">
                    {agent.role}
                  </span>
                </span>
              </div>
              <div className="absolute top-[3.35rem] left-[3.2rem] flex items-center gap-1.5">
                {status === "success" ? (
                  <CircleCheck aria-hidden="true" className="size-3 text-success" />
                ) : (
                  <CircleDashed
                    aria-hidden="true"
                    className={cn("size-3 text-muted-foreground", active && "animate-spin text-accent")}
                  />
                )}
                <span className="text-[0.64rem] font-medium text-muted-foreground">
                  {status === "pending" ? "Sẵn tuyến" : statusLabels[status]}
                </span>
              </div>
            </li>
          )
        })}
      </ol>

      <footer className="mt-auto border-t bg-muted/40 px-4 py-3">
        <Badge variant="outline" className="border-success/40 text-success">
          <CircleCheck data-icon="inline-start" />
          Action allowlist bật
        </Badge>
        <p className="mt-2 text-[0.7rem] leading-5 text-muted-foreground">
          Model đề xuất; code kiểm tra DAG, action và nguồn dữ liệu trước khi chạy.
        </p>
      </footer>
    </section>
  )
}

function resolveAgentStatus(
  agentId: string,
  executions: AgentExecution[],
  statuses: GatewayStatusEvent[],
  requestPhase: AgentRosterProps["requestPhase"],
): TaskStatus {
  if (agentId === "orchestrator") {
    if (requestPhase === "streaming") return "running"
    if (requestPhase === "completed") return "success"
    if (requestPhase === "failed") return "failed"
    return "pending"
  }

  const execution = executions.find((item) => item.agent_id === agentId)
  if (execution) return execution.status

  const progress = statuses.findLast((item) => item.agent_id === agentId)
  return progress?.status ?? "pending"
}
