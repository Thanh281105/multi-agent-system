import { useId, useMemo } from "react"
import {
  Ban,
  Check,
  CircleAlert,
  Clock3,
  Copy,
  Database,
  FileSearch,
  GitFork,
  Hash,
  Microchip,
  Route,
} from "lucide-react"
import { toast } from "sonner"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Separator } from "@/components/ui/separator"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import type { ChatState } from "@/features/chat/chat-state"
import {
  formatDuration,
  friendlyAgent,
  provenanceAnchorId,
  shortenIdentifier,
  statusLabels,
  type ProvenanceAnchorScope,
} from "@/features/chat/presentation"
import { cn } from "@/lib/utils"
import type {
  AgentExecution,
  GatewayChatResponse,
  ModelCall,
  Provenance,
  TaskStatus,
} from "@/lib/contracts"

interface EvidenceDossierProps {
  state: ChatState
  compact?: boolean
  anchorScope: ProvenanceAnchorScope
  selectedSourceId?: string
}

export function EvidenceDossier({
  state,
  compact = false,
  anchorScope,
  selectedSourceId,
}: EvidenceDossierProps) {
  const titleId = useId()
  const result =
    state.request.phase === "completed" ? state.request.result : undefined
  return (
    <section
      aria-labelledby={titleId}
      className={cn(
        "atlas-panel overflow-hidden",
        compact ? "border-0" : "flex flex-col xl:h-[calc(100svh-5.5rem)]",
      )}
    >
      <header className="flex items-end justify-between border-b px-4 py-4">
        <div>
          <h2 id={titleId} className="font-display text-xl font-semibold">
            Chi tiết thực thi
          </h2>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">
            Dữ liệu Tiki Books lịch sử
          </p>
        </div>
        <DossierStatus state={state} />
      </header>

      <ScrollArea className={cn("min-h-0", !compact && "flex-1")}>
        <div className="flex flex-col gap-5 px-4 py-4">
          <DossierSection
            icon={GitFork}
            title="Kế hoạch thực thi (DAG)"
            count={result?.executions.length}
          >
            <ExecutionGraph executions={result?.executions ?? []} />
          </DossierSection>

          <Separator />

          <DossierSection
            icon={Microchip}
            title="Lần gọi model"
            count={result?.model_calls.length}
          >
            <ModelLedger calls={result?.model_calls ?? []} />
          </DossierSection>

          <Separator />

          <DossierSection
            icon={Database}
            title="Nguồn bằng chứng"
            count={result?.provenance.length}
          >
            <ProvenanceIndex
              sources={result?.provenance ?? []}
              anchorScope={anchorScope}
              selectedSourceId={selectedSourceId}
            />
          </DossierSection>

          <Separator />

          <DossierSection icon={Hash} title="Request, trace, session IDs">
            <TraceLedger result={result} sessionId={state.sessionId} />
          </DossierSection>
        </div>
      </ScrollArea>

      <footer className="border-t bg-muted/40 px-4 py-3">
        <div className="flex items-center gap-2 text-xs leading-5 text-muted-foreground">
          <FileSearch aria-hidden="true" className="size-3.5 shrink-0" />
          Không hiển thị nội dung gửi cho model, suy luận ẩn, payload thô hoặc
          thông tin xác thực.
        </div>
      </footer>
    </section>
  )
}

function DossierSection({
  icon: Icon,
  title,
  count,
  children,
}: {
  icon: typeof Route
  title: string
  count?: number
  children: React.ReactNode
}) {
  return (
    <section>
      <div className="mb-3 flex items-center justify-between gap-3">
        <h3 className="flex items-center gap-2 text-xs font-bold">
          <Icon aria-hidden="true" className="size-3.5 text-accent" />
          {title}
        </h3>
        {count !== undefined ? (
          <span className="atlas-data text-muted-foreground">
            {String(count).padStart(2, "0")}
          </span>
        ) : null}
      </div>
      {children}
    </section>
  )
}

function DossierStatus({ state }: { state: ChatState }) {
  const phase = state.request.phase
  if (phase === "streaming") {
    return (
      <Badge variant="accent">
        <span className="size-1.5 animate-pulse rounded-full bg-current" />
        Đang chạy
      </Badge>
    )
  }
  if (phase === "completed") {
    const resultStatus = state.request.result.status
    if (resultStatus === "success") {
      return (
        <Badge variant="success">
          <Check data-icon="inline-start" />
          Hoàn tất
        </Badge>
      )
    }
    if (resultStatus === "partial_success") {
      return (
        <Badge variant="accent">
          <CircleAlert data-icon="inline-start" />
          Một phần
        </Badge>
      )
    }
    if (resultStatus === "failed") {
      return <Badge variant="destructive">Thất bại</Badge>
    }
    return (
      <Badge variant="accent">
        <CircleAlert data-icon="inline-start" />
        {resultStatus === "running" ? "Chưa hoàn tất" : "Đang chờ"}
      </Badge>
    )
  }
  if (phase === "failed") {
    return <Badge variant="destructive">Lỗi</Badge>
  }
  if (phase === "cancelled") {
    return (
      <Badge variant="destructive">
        <Ban data-icon="inline-start" />
        Đã dừng
      </Badge>
    )
  }
  return <Badge variant="outline">Chưa chạy</Badge>
}

function ExecutionGraph({ executions }: { executions: AgentExecution[] }) {
  const layout = useMemo(() => buildDagLayout(executions), [executions])
  if (!executions.length) {
    return (
      <EmptyDossier
        icon={Route}
        title="Chưa có tuyến"
        detail="DAG xuất hiện sau khi Gateway hoàn tất yêu cầu."
      />
    )
  }

  return (
    <div>
      <svg
        viewBox={`0 0 ${layout.width} ${layout.height}`}
        className="h-auto w-full overflow-visible"
        role="img"
        aria-label="Đồ thị phụ thuộc giữa các bước thực thi"
      >
        <title>Tuyến DAG từ trường depends_on của Gateway</title>
        {layout.edges.map((edge) => (
          <path
            key={`${edge.from}-${edge.to}`}
            d={`M ${edge.start.x} ${edge.start.y + 15} C ${edge.start.x} ${edge.midY}, ${edge.end.x} ${edge.midY}, ${edge.end.x} ${edge.end.y - 15}`}
            fill="none"
            stroke={statusColor(edge.status)}
            strokeWidth="2"
            strokeDasharray={
              edge.status === "pending"
                ? "3 5"
                : edge.status === "failed"
                  ? "2 4"
                  : undefined
            }
            className={edge.status === "running" ? "route-active" : undefined}
            vectorEffect="non-scaling-stroke"
          />
        ))}
        {layout.nodes.map((node, index) => (
          <g key={node.execution.step_id}>
            <circle
              cx={node.x}
              cy={node.y}
              r="15"
              fill="var(--card)"
              stroke={statusColor(node.execution.status)}
              strokeWidth="2.25"
            />
            <text
              x={node.x}
              y={node.y + 3.5}
              textAnchor="middle"
              fill="var(--foreground)"
              fontFamily="var(--font-mono)"
              fontSize="9"
              fontWeight="650"
            >
              {String(index + 1).padStart(2, "0")}
            </text>
          </g>
        ))}
      </svg>

      <ol className="mt-2 flex flex-col gap-2">
        {executions.map((execution, index) => (
          <li
            key={execution.step_id}
            className="rounded-lg border bg-muted/35 px-2.5 py-2"
          >
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="truncate text-xs font-bold">
                  <span className="mr-1.5 font-mono text-accent">
                    {String(index + 1).padStart(2, "0")}
                  </span>
                  {friendlyAgent(execution.agent_id)}
                </p>
                <p className="mt-1 truncate font-mono text-xs text-muted-foreground">
                  {execution.action}
                </p>
              </div>
              <StatusDot status={execution.status} />
            </div>
            <div className="mt-2 flex items-center justify-between gap-2 text-xs text-muted-foreground">
              <span className="truncate">
                phụ thuộc ·{" "}
                {execution.depends_on.length
                  ? execution.depends_on.join(", ")
                  : "bắt đầu"}
              </span>
              <span className="shrink-0 font-mono tabular-nums">
                {formatDuration(execution.duration_ms)}
              </span>
            </div>
          </li>
        ))}
      </ol>
    </div>
  )
}

function ModelLedger({ calls }: { calls: ModelCall[] }) {
  if (!calls.length) {
    return (
      <EmptyDossier
        icon={Microchip}
        title="Chưa gọi model"
        detail="Luồng dự phòng theo quy tắc có thể hoàn tất mà không gọi model."
      />
    )
  }
  return (
    <ol className="flex flex-col gap-2">
      {calls.map((call) => {
        const failedRequired = Boolean(call.error_code && !call.fallback_used)
        const outcomeLabel = call.fallback_used
          ? "Fallback theo quy tắc"
          : failedRequired
            ? "Lỗi không có fallback"
            : call.status === "success"
              ? "Thành công"
              : call.status
        return (
          <li
            key={call.call_id}
            className="rounded-lg border bg-card px-2.5 py-2.5"
          >
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <p className="truncate text-xs font-bold">
                  {call.model}
                </p>
                <p className="mt-0.5 truncate font-mono text-xs text-muted-foreground">
                  {call.provider} / {call.stage} /{" "}
                  {friendlyAgent(call.agent_id)}
                </p>
              </div>
              <Badge
                variant={
                  failedRequired
                    ? "destructive"
                    : call.fallback_used
                      ? "accent"
                      : "success"
                }
              >
                {outcomeLabel}
              </Badge>
            </div>
            <div className="mt-2 grid grid-cols-3 gap-2 border-t pt-2 font-mono text-xs text-muted-foreground tabular-nums">
              <span>{call.total_tokens.toLocaleString("vi-VN")} tokens</span>
              <span>{formatDuration(call.duration_ms)}</span>
              <span className="text-right">{call.attempts} lần</span>
            </div>
            {call.fallback_reason ? (
              <p className="mt-2 text-xs leading-5 text-accent">
                {call.fallback_reason}
              </p>
            ) : null}
          </li>
        )
      })}
    </ol>
  )
}

function ProvenanceIndex({
  sources,
  anchorScope,
  selectedSourceId,
}: {
  sources: Provenance[]
  anchorScope: ProvenanceAnchorScope
  selectedSourceId?: string
}) {
  if (!sources.length) {
    return (
      <EmptyDossier
        icon={Database}
        title="Chưa có nguồn"
        detail="Nguồn chỉ xuất hiện khi agent trả bằng chứng hợp lệ."
      />
    )
  }
  return (
    <ol className="flex flex-col gap-2">
      {sources.map((source, index) => (
        <li
          key={`${source.source_type}-${source.source_id}-${index}`}
          id={provenanceAnchorId(anchorScope, source.source_id)}
          tabIndex={-1}
          aria-current={
            selectedSourceId === source.source_id ? "true" : undefined
          }
          className={cn(
            "rounded-lg border bg-card px-3 py-2 outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/40",
            selectedSourceId === source.source_id &&
              "border-accent/50 bg-accent/5",
          )}
        >
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <p className="truncate font-mono text-xs font-semibold">
                {source.source_id}
              </p>
              <p className="mt-0.5 truncate text-xs text-muted-foreground">
                {source.source_type}
              </p>
            </div>
            <Badge variant={source.sample_data ? "accent" : "outline"}>
              {source.sample_data ? "Dữ liệu mẫu" : "Nguồn khác"}
            </Badge>
          </div>
          {source.fields.length ? (
            <p className="mt-1 line-clamp-2 text-xs leading-5 text-muted-foreground">
              trường · {source.fields.join(", ")}
            </p>
          ) : null}
        </li>
      ))}
    </ol>
  )
}

function TraceLedger({
  result,
  sessionId,
}: {
  result?: GatewayChatResponse
  sessionId: string | null
}) {
  const entries = [
    ["request", result?.request_id],
    ["trace", result?.trace_id],
    ["session", result?.session_id ?? sessionId],
  ] as const
  return (
    <dl className="flex flex-col gap-2">
      {entries.map(([label, value]) => (
        <div
          key={label}
          className="grid grid-cols-[3.4rem_minmax(0,1fr)_2.75rem] items-center gap-2 xl:grid-cols-[3.4rem_minmax(0,1fr)_2rem]"
        >
          <dt className="text-xs font-semibold text-muted-foreground">
            {label}
          </dt>
          <dd
            className="truncate font-mono text-xs"
            title={value ?? undefined}
          >
            {value ? shortenIdentifier(value) : "—"}
          </dd>
          <dd>
            <CopyButton label={label} value={value} />
          </dd>
        </div>
      ))}
      {result ? (
        <div className="mt-3 flex items-center justify-between border-t pt-2 text-xs text-muted-foreground">
          <dt className="flex items-center gap-1.5">
            <Clock3 aria-hidden="true" className="size-3" /> tổng thời gian
          </dt>
          <dd className="font-mono tabular-nums">
            {formatDuration(result.duration_ms)}
          </dd>
        </div>
      ) : null}
    </dl>
  )
}

function CopyButton({
  label,
  value,
}: {
  label: string
  value?: string | null
}) {
  const copy = async () => {
    if (!value) return
    try {
      await navigator.clipboard.writeText(value)
      toast.success(`Đã sao chép ${label} ID.`)
    } catch {
      toast.error("Trình duyệt không cho phép sao chép.")
    }
  }
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          type="button"
          size="icon-xs"
          variant="ghost"
          className="size-11 xl:size-8"
          disabled={!value}
          onClick={() => void copy()}
          aria-label={`Sao chép ${label} ID`}
        >
          <Copy />
        </Button>
      </TooltipTrigger>
      <TooltipContent>Sao chép {label} ID</TooltipContent>
    </Tooltip>
  )
}

function StatusDot({ status }: { status: TaskStatus }) {
  return (
    <span className="flex shrink-0 items-center gap-1 text-xs text-muted-foreground">
      <span
        aria-hidden="true"
        className="size-1.5 rounded-full"
        style={{ backgroundColor: statusColor(status) }}
      />
      {statusLabels[status]}
    </span>
  )
}

function EmptyDossier({
  icon: Icon,
  title,
  detail,
}: {
  icon: typeof Route
  title: string
  detail: string
}) {
  return (
    <Empty className="gap-2 rounded-lg border border-dashed bg-muted/25 px-3 py-4">
      <EmptyHeader>
        <EmptyMedia variant="icon">
          <Icon aria-hidden="true" />
        </EmptyMedia>
        <EmptyTitle>{title}</EmptyTitle>
        <EmptyDescription className="text-xs leading-5">
          {detail}
        </EmptyDescription>
      </EmptyHeader>
    </Empty>
  )
}

function statusColor(status: TaskStatus): string {
  if (status === "success") return "var(--success)"
  if (status === "running") return "var(--accent)"
  if (status === "failed") return "var(--destructive)"
  if (status === "partial_success") return "var(--accent)"
  return "var(--muted-foreground)"
}

function buildDagLayout(executions: AgentExecution[]) {
  const width = 320
  const levels = new Map<string, number>()
  for (const execution of executions) {
    const dependencyLevels = execution.depends_on.map(
      (dependency) => levels.get(dependency) ?? -1,
    )
    levels.set(
      execution.step_id,
      dependencyLevels.length ? Math.max(...dependencyLevels) + 1 : 0,
    )
  }
  const maxLevel = Math.max(0, ...levels.values())
  const height = Math.max(76, 48 + maxLevel * 72)
  const groups = new Map<number, AgentExecution[]>()
  for (const execution of executions) {
    const level = levels.get(execution.step_id) ?? 0
    groups.set(level, [...(groups.get(level) ?? []), execution])
  }
  const nodes = executions.map((execution) => {
    const level = levels.get(execution.step_id) ?? 0
    const siblings = groups.get(level) ?? [execution]
    const siblingIndex = siblings.findIndex(
      (item) => item.step_id === execution.step_id,
    )
    return {
      execution,
      x: (width * (siblingIndex + 1)) / (siblings.length + 1),
      y: 25 + level * 72,
    }
  })
  const positions = new Map(nodes.map((node) => [node.execution.step_id, node]))
  const edges = executions.flatMap((execution) => {
    const end = positions.get(execution.step_id)
    if (!end) return []
    return execution.depends_on.flatMap((dependency) => {
      const start = positions.get(dependency)
      if (!start) return []
      return [
        {
          from: dependency,
          to: execution.step_id,
          start,
          end,
          midY: start.y + (end.y - start.y) / 2,
          status: execution.status,
        },
      ]
    })
  })
  return { width, height, nodes, edges }
}
