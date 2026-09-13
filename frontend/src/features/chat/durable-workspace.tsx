import { useMemo, useState, type FormEvent } from "react"
import {
  AlertCircle,
  Ban,
  Check,
  ChevronDown,
  CircleAlert,
  Database,
  LoaderCircle,
  MemoryStick,
  Plus,
  RefreshCw,
  RotateCcw,
  Send,
  Trash2,
  WifiOff,
} from "lucide-react"
import { toast } from "sonner"

import {
  Alert,
  AlertAction,
  AlertDescription,
  AlertTitle,
} from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Bubble, BubbleContent } from "@/components/ui/bubble"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Message,
  MessageContent,
  MessageHeader,
} from "@/components/ui/message"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Separator } from "@/components/ui/separator"
import { Textarea } from "@/components/ui/textarea"
import {
  actionStatusLabels,
  formatArtifactLabel,
  formatVnd,
  projectActionCard,
  selectConversationTurnViews,
  type ActionCardView,
  type ArtifactView,
  type CitationTargetView,
  type ConversationTurnView,
} from "@/features/chat/presentation"
import type { ChatController } from "@/features/chat/use-chat-controller"
import { cn } from "@/lib/utils"
import type {
  ConversationMode,
  PreferenceKind,
  PreferenceValue,
} from "@/lib/v2-contracts"

interface DurableWorkspaceProps {
  controller: ChatController & { durable: NonNullable<ChatController["durable"]> }
  onCredentialRequest(returnFocus?: HTMLElement): void
  onProvenanceRequest(evidenceId: string): void
  selectedTurnId?: string
  onTurnSelect(turnId: string): void
}

const modeLabels: Record<ConversationMode, string> = {
  shopper: "Người mua",
  merchant: "Nhà bán hàng",
}

const preferenceLabels: Record<PreferenceKind, string> = {
  genre: "Thể loại",
  author: "Tác giả",
  language: "Ngôn ngữ",
  max_budget_vnd: "Ngân sách tối đa",
}

export function DurableWorkspace({
  controller,
  onCredentialRequest,
  onProvenanceRequest,
  selectedTurnId,
  onTurnSelect,
}: DurableWorkspaceProps) {
  const { durable, state } = controller
  const [draft, setDraft] = useState("")
  const [busy, setBusy] = useState(false)
  const [action, setAction] = useState<ActionCardView | null>(null)
  const turns = selectConversationTurnViews(state)
  const activeBusy =
    state.durable.activeTurn?.status === "pending" ||
    state.durable.activeTurn?.status === "running"

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const message = draft.trim()
    if (!state.credentialConfigured) {
      onCredentialRequest(event.currentTarget)
      return
    }
    if (!message) return
    setBusy(true)
    const outcome = await durable.sendMessage(message)
    setBusy(false)
    if (outcome === "completed") setDraft("")
    else if (outcome === "busy") toast.info("Một lượt khác đang chạy.")
    else if (outcome === "offline") toast.error("Đang ngoại tuyến.")
    else if (outcome === "not_ready") toast.error("Hội thoại chưa sẵn sàng.")
  }

  const createConversation = async () => {
    setBusy(true)
    const conversation = await durable.createConversation()
    setBusy(false)
    if (conversation) toast.success("Đã tạo hội thoại mới.")
  }

  const deleteConversation = async () => {
    const conversation = state.durable.activeConversation
    if (!conversation || activeBusy) return
    setBusy(true)
    const outcome = await durable.deleteConversation(conversation.conversationId)
    setBusy(false)
    if (outcome === "completed") toast.success("Đã xoá hội thoại.")
  }

  const openAction = async (candidate: ActionCardView) => {
    const readback = await durable.getAction(candidate.actionId)
    if (!readback) {
      toast.error("Không thể làm mới đề xuất.")
      return
    }
    const latest = projectActionCard(readback.action)
    if (latest.status !== "proposed") {
      toast.info(`Đề xuất hiện ở trạng thái ${latest.statusLabel.toLowerCase()}.`)
    }
    setAction(latest)
  }

  const decideAction = async (decision: "confirm" | "reject") => {
    if (!action) return
    setBusy(true)
    const response =
      decision === "confirm"
        ? await durable.confirmAction(action.actionId, action.proposalVersion)
        : await durable.rejectAction(action.actionId, action.proposalVersion)
    setBusy(false)
    if (!response) {
      toast.error("Không thể xác nhận trạng thái đề xuất.")
      return
    }
    setAction(projectActionCard(response.action))
    toast.success(
      decision === "confirm"
        ? "Đã ghi nhận kết quả hành động."
        : "Đã từ chối đề xuất.",
    )
  }

  return (
    <section
      aria-labelledby="durable-conversation-title"
      aria-busy={activeBusy || busy}
      className="atlas-panel atlas-elevated flex h-[calc(100svh-5.5rem)] min-h-[36rem] flex-col overflow-hidden"
    >
      <DurableHeader
        controller={controller}
        busy={busy}
        activeBusy={activeBusy}
        onCreate={() => void createConversation()}
        onDelete={() => void deleteConversation()}
      />

      <ScrollArea className="min-h-0 flex-1">
        <div className="mx-auto flex w-full max-w-3xl flex-col gap-5 px-4 py-5 sm:px-6">
          {!state.online ? (
            <Alert variant="destructive">
              <WifiOff />
              <AlertTitle>Đang ngoại tuyến</AlertTitle>
              <AlertDescription>
                Lịch sử đã tải vẫn xem được; không gửi hay quyết định hành động mới.
              </AlertDescription>
            </Alert>
          ) : null}
          {durable.phase === "loading" || state.durable.historyState === "loading" ? (
            <LoadingPanel />
          ) : durable.phase === "failed" ? (
            <Alert variant="destructive">
              <AlertCircle />
              <AlertTitle>Không tải được workspace v2</AlertTitle>
              <AlertDescription>
                {durable.failure?.message ?? "Hãy kiểm tra quyền truy cập và thử lại."}
              </AlertDescription>
            </Alert>
          ) : turns.length ? (
            <ol className="flex flex-col gap-5" aria-label="Lịch sử hội thoại">
              {turns.map((turn) => (
                <TurnCard
                  key={turn.key}
                  turn={turn}
                  selected={
                    selectedTurnId === turn.turnId ||
                    selectedTurnId === turn.clientTurnId
                  }
                  onSelect={onTurnSelect}
                  onEvidence={onProvenanceRequest}
                  onAction={(candidate) => void openAction(candidate)}
                  onRetry={() => void durable.retryPendingTurn()}
                />
              ))}
            </ol>
          ) : (
            <Empty className="border border-dashed bg-muted/20 py-12">
              <EmptyHeader>
                <EmptyMedia variant="icon"><Database /></EmptyMedia>
                <EmptyTitle>Hội thoại chưa có lượt</EmptyTitle>
                <EmptyDescription>
                  Gửi câu hỏi đầu tiên; mỗi kết quả, nguồn và đề xuất sẽ gắn với đúng turn.
                </EmptyDescription>
              </EmptyHeader>
            </Empty>
          )}

          <MemoryPanel controller={controller} turns={turns} disabled={busy || activeBusy} />
        </div>
      </ScrollArea>

      <form onSubmit={submit} className="border-t bg-card px-4 py-4 sm:px-5">
        <div className="mx-auto flex max-w-3xl items-end gap-2">
          <div className="min-w-0 flex-1">
            <Label htmlFor="durable-chat-query" className="mb-1.5 block">
              Tin nhắn cho hội thoại hiện tại
            </Label>
            <Textarea
              id="durable-chat-query"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              maxLength={2_000}
              rows={2}
              disabled={activeBusy || busy || durable.phase !== "ready"}
              placeholder="Hỏi về danh mục, đánh giá hoặc một hành động sandbox…"
              className="min-h-20 resize-none"
            />
          </div>
          {activeBusy ? (
            <Button
              type="button"
              variant="destructive"
              size="icon"
              className="size-11"
              onClick={() => void durable.cancelRequest()}
              aria-label="Dừng turn đang chạy"
            >
              <Ban />
            </Button>
          ) : (
            <Button
              type="submit"
              size="icon"
              className="size-11 active:scale-[0.98]"
              disabled={busy || durable.phase !== "ready" || !state.online}
              aria-label="Gửi tin nhắn v2"
            >
              <Send />
            </Button>
          )}
        </div>
      </form>

      <ActionDecisionDialog
        action={action}
        busy={busy}
        onOpenChange={(open) => { if (!open) setAction(null) }}
        onConfirm={() => void decideAction("confirm")}
        onReject={() => void decideAction("reject")}
      />
    </section>
  )
}

function DurableHeader({
  controller,
  busy,
  activeBusy,
  onCreate,
  onDelete,
}: {
  controller: DurableWorkspaceProps["controller"]
  busy: boolean
  activeBusy: boolean
  onCreate(): void
  onDelete(): void
}) {
  const { durable, state } = controller
  const conversation = state.durable.activeConversation
  return (
    <header className="border-b bg-card px-4 py-3 sm:px-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h1 id="durable-conversation-title" className="truncate font-display text-xl font-semibold">
            {conversation?.title ?? "Evidence Atlas v2"}
          </h1>
          <p className="mt-1 text-xs text-muted-foreground">
            {conversation ? `${modeLabels[conversation.mode]} · lịch sử bền vững` : "Đang chuẩn bị hội thoại"}
          </p>
        </div>
        <div className="flex items-center gap-1.5">
          <Button type="button" variant="outline" size="icon" className="size-11" onClick={onCreate} disabled={busy || !durable.selectedMode} aria-label="Tạo hội thoại mới">
            <Plus />
          </Button>
          <Button type="button" variant="ghost" size="icon" className="size-11" onClick={onDelete} disabled={busy || activeBusy || !conversation} aria-label="Xoá hội thoại hiện tại">
            <Trash2 />
          </Button>
        </div>
      </div>
      <div className="mt-3 grid gap-2 sm:grid-cols-2">
        <label className="text-xs font-semibold text-muted-foreground">
          Mode
          <span className="relative mt-1 block">
            <select
              value={durable.selectedMode ?? ""}
              onChange={(event) => void durable.changeMode(event.target.value as ConversationMode)}
              disabled={busy || activeBusy || !durable.allowedModes.length}
              className="h-11 w-full appearance-none rounded-lg border bg-background px-3 pe-9 text-sm text-foreground outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/40"
            >
              {!durable.selectedMode ? <option value="">Chọn mode</option> : null}
              {durable.allowedModes.map((mode) => <option key={mode} value={mode}>{modeLabels[mode]}</option>)}
            </select>
            <ChevronDown className="pointer-events-none absolute top-3.5 right-3 size-4" />
          </span>
        </label>
        <label className="text-xs font-semibold text-muted-foreground">
          Hội thoại
          <span className="relative mt-1 block">
            <select
              value={conversation?.conversationId ?? ""}
              onChange={(event) => void durable.switchConversation(event.target.value)}
              disabled={busy || activeBusy || !durable.conversations.length}
              className="h-11 w-full appearance-none rounded-lg border bg-background px-3 pe-9 text-sm text-foreground outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/40"
            >
              {!conversation ? <option value="">Chọn hội thoại</option> : null}
              {durable.conversations.map((item) => (
                <option key={item.conversationId} value={item.conversationId}>
                  {item.title ?? item.conversationId}
                </option>
              ))}
            </select>
            <ChevronDown className="pointer-events-none absolute top-3.5 right-3 size-4" />
          </span>
        </label>
      </div>
    </header>
  )
}

function LoadingPanel() {
  return (
    <div className="flex min-h-48 items-center justify-center rounded-xl border border-dashed bg-muted/20" role="status">
      <LoaderCircle className="mr-2 size-4 animate-spin text-accent" />
      <span className="text-sm text-muted-foreground">Đang tải lịch sử đã xác thực…</span>
    </div>
  )
}

function TurnCard({ turn, selected, onSelect, onEvidence, onAction, onRetry }: {
  turn: ConversationTurnView
  selected: boolean
  onSelect(turnId: string): void
  onEvidence(evidenceId: string): void
  onAction(action: ActionCardView): void
  onRetry(): void
}) {
  const turnId = turn.turnId ?? turn.clientTurnId
  return (
    <li>
      <article className={cn("rounded-xl border bg-card p-3 transition-colors", selected && "border-accent/50 bg-accent/5")}>
        <button type="button" className="mb-3 flex min-h-11 w-full items-center justify-between gap-3 rounded-lg px-1 text-left outline-none focus-visible:ring-3 focus-visible:ring-ring/40" onClick={() => turnId && onSelect(turnId)} disabled={!turnId}>
          <span className="font-mono text-xs text-muted-foreground">{turn.turnId ?? turn.clientTurnId}</span>
          <span className="flex items-center gap-2">
            {turn.outcomeLabel ? <Badge variant="outline">{turn.outcomeLabel}</Badge> : null}
            <Badge variant={turn.status === "completed" ? "success" : turn.status === "failed" || turn.status === "cancelled" ? "destructive" : "accent"}>{turn.statusLabel}</Badge>
          </span>
        </button>
        {turn.userMessage ? (
          <Message align="end"><MessageContent><MessageHeader>Bạn</MessageHeader><Bubble align="end"><BubbleContent className="whitespace-pre-wrap">{turn.userMessage}</BubbleContent></Bubble></MessageContent></Message>
        ) : null}
        {turn.assistant ? (
          <div className="mt-4 space-y-3">
            <Message><MessageContent><MessageHeader>Evidence Atlas · câu trả lời có nguồn</MessageHeader><Bubble variant="outline" className="w-full max-w-full"><BubbleContent className="whitespace-pre-wrap leading-7">{renderCitedAnswer(turn.assistant.text, turn.assistant.citations, onEvidence)}</BubbleContent></Bubble></MessageContent></Message>
            {turn.assistant.warnings.length ? <Alert><CircleAlert /><AlertTitle>Lưu ý</AlertTitle><AlertDescription>{turn.assistant.warnings.join(" · ")}</AlertDescription></Alert> : null}
            {turn.assistant.artifacts.length ? <ArtifactGrid artifacts={turn.assistant.artifacts} /> : null}
            {turn.assistant.actions.length ? <div className="grid gap-2">{turn.assistant.actions.map((item) => <ActionSummary key={item.actionId} action={item} onOpen={onAction} />)}</div> : null}
          </div>
        ) : turn.isStreaming ? (
          <div className="mt-3 flex items-center gap-2 text-sm text-muted-foreground" role="status"><LoaderCircle className="size-4 animate-spin text-accent" />Đang xử lý; tiến trình sẽ được nối lại nếu mất kết nối.</div>
        ) : null}
        {turn.error ? (
          <Alert variant="destructive" className="mt-3"><AlertCircle /><AlertTitle>{turn.error.code}</AlertTitle><AlertDescription>{turn.error.message}</AlertDescription>{turn.error.retryable ? <AlertAction><Button type="button" size="sm" variant="outline" onClick={onRetry}><RotateCcw />Thử nối lại</Button></AlertAction> : null}</Alert>
        ) : turn.status === "interrupted" ? (
          <Button type="button" variant="outline" size="sm" className="mt-3" onClick={onRetry}><RotateCcw />Nối lại turn</Button>
        ) : null}
      </article>
    </li>
  )
}

function renderCitedAnswer(value: string, citations: CitationTargetView[], onEvidence: (id: string) => void) {
  const byLabel = new Map(citations.map((citation) => [citation.displayLabel, citation]))
  return value.split(/(\[[^\]\n]{1,120}\])/g).map((segment, index) => {
    const match = /^\[([^\]\n]{1,120})\]$/.exec(segment)
    const citation = match
      ? (byLabel.get(match[1]) ?? byLabel.get(segment))
      : undefined
    if (!citation) return match ? <code key={`${segment}-${index}`} className="mx-0.5 rounded bg-muted px-1 py-0.5 font-mono text-[0.78em]">{segment}</code> : segment
    return <Button key={`${citation.citationId}-${index}`} type="button" variant="link" size="xs" className="mx-0.5 h-auto min-h-6 px-1 py-0 align-baseline" onClick={() => onEvidence(citation.evidenceId)} aria-label={`Mở nguồn ${citation.displayLabel}`}><code>{segment}</code></Button>
  })
}

function ArtifactGrid({ artifacts }: { artifacts: ArtifactView[] }) {
  return <div className="grid gap-2 sm:grid-cols-2" aria-label="Artifacts">{artifacts.map((artifact) => <div key={artifact.artifactId} className="rounded-lg border bg-muted/20 p-3"><p className="text-sm font-semibold">{formatArtifactLabel(artifact)}</p><p className="mt-1 font-mono text-xs text-muted-foreground">v{artifact.resourceVersion} · {artifact.resourceId}</p><ArtifactBody artifact={artifact} /></div>)}</div>
}

function ArtifactBody({ artifact }: { artifact: ArtifactView }) {
  if (artifact.kind === "product_comparison") return <p className="mt-2 text-xs text-muted-foreground">{artifact.products.length} sản phẩm · {artifact.status === "partial" ? "dữ liệu một phần" : "sẵn sàng"}</p>
  if (artifact.kind === "action") return <p className="mt-2 text-xs text-muted-foreground">{actionStatusLabels[artifact.actionStatus]} · proposal v{artifact.proposalVersion}</p>
  return <p className="mt-2 text-xs text-muted-foreground">{artifact.items.length} dòng · <span className="font-mono tabular-nums">{formatVnd(artifact.totalPriceVnd)}</span></p>
}

function ActionSummary({ action, onOpen }: { action: ActionCardView; onOpen(action: ActionCardView): void }) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-lg border border-accent/25 bg-accent/5 p-3">
      <div className="min-w-0"><p className="truncate text-sm font-semibold">{action.title}</p><p className="mt-1 text-xs text-muted-foreground">{action.kindLabel} · proposal v{action.proposalVersion} · {action.statusLabel}</p></div>
      <Button type="button" variant="outline" size="sm" onClick={() => onOpen(action)}>Xem đề xuất</Button>
    </div>
  )
}

function ActionDecisionDialog({ action, busy, onOpenChange, onConfirm, onReject }: {
  action: ActionCardView | null
  busy: boolean
  onOpenChange(open: boolean): void
  onConfirm(): void
  onReject(): void
}) {
  const actionable = action?.status === "proposed"
  return (
    <Dialog open={Boolean(action)} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader><DialogTitle>{action?.title ?? "Chi tiết đề xuất"}</DialogTitle><DialogDescription>Kiểm tra đúng thay đổi, version, hạn và quyền trước khi quyết định.</DialogDescription></DialogHeader>
        {action ? <div className="space-y-3"><div className="grid grid-cols-2 gap-2 text-xs"><Metadata label="Trạng thái" value={action.statusLabel} /><Metadata label="Proposal version" value={String(action.proposalVersion)} /><Metadata label="Resource version" value={String(action.expectedResourceVersion)} /><Metadata label="Quyền bắt buộc" value={action.requiredPermission} /><Metadata label="Hết hạn" value={new Date(action.expiresAt).toLocaleString("vi-VN")} /><Metadata label="Resource" value={`${action.resourceType} · ${action.resourceId}`} /></div><Separator /><ol className="space-y-2">{action.changes.map((change, index) => <li key={`${change.resourceId}-${change.field}-${index}`} className="rounded-lg border bg-muted/30 p-2 text-xs"><p className="font-mono">{change.resourceType} · {change.resourceId}</p><p className="mt-1">{change.field}: <strong>{String(change.beforeInteger ?? change.beforeText ?? "—")}</strong> → <strong>{String(change.afterInteger ?? change.afterText ?? "—")}</strong></p></li>)}</ol></div> : null}
        <DialogFooter><DialogClose asChild><Button variant="outline">Đóng</Button></DialogClose><Button variant="destructive" onClick={onReject} disabled={!actionable || busy}>Từ chối</Button><Button onClick={onConfirm} disabled={!actionable || busy}>{busy ? <LoaderCircle className="animate-spin" /> : <Check />}Xác nhận</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function Metadata({ label, value }: { label: string; value: string }) {
  return <div className="rounded-lg border bg-card p-2"><p className="text-muted-foreground">{label}</p><p className="mt-1 break-words font-mono font-semibold">{value}</p></div>
}

function MemoryPanel({ controller, turns, disabled }: { controller: DurableWorkspaceProps["controller"]; turns: ConversationTurnView[]; disabled: boolean }) {
  const { durable, state } = controller
  const [kind, setKind] = useState<PreferenceKind>("genre")
  const [value, setValue] = useState("")
  const [sourceTurnId, setSourceTurnId] = useState("")
  const completedTurns = useMemo(() => turns.filter((turn) => turn.turnId && turn.status === "completed"), [turns])
  const preferences = Object.values(state.durable.resources.preferences)
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!sourceTurnId || !value.trim()) return
    const preference: PreferenceValue = kind === "max_budget_vnd" ? { kind, value: Number.parseInt(value, 10) } : { kind, value: value.trim() }
    const saved = await durable.putPreference(sourceTurnId, preference)
    if (saved) { setValue(""); toast.success("Đã lưu memory có nguồn turn.") }
    else toast.error("Memory chỉ được tạo từ turn đã hoàn tất.")
  }
  return (
    <details className="rounded-xl border bg-card p-3">
      <summary className="flex min-h-11 cursor-pointer list-none items-center justify-between gap-3 rounded-lg outline-none focus-visible:ring-3 focus-visible:ring-ring/40"><span className="flex items-center gap-2 text-sm font-semibold"><MemoryStick className="size-4 text-accent" />Memory minh bạch</span><Badge variant="outline">{preferences.length}</Badge></summary>
      <div className="mt-3 space-y-3 border-t pt-3">
        <div className="flex items-center justify-between gap-3"><p className="text-xs leading-5 text-muted-foreground">Mỗi preference phải chỉ rõ turn đã hoàn tất làm nguồn.</p><Button type="button" variant="ghost" size="icon-sm" onClick={() => void durable.listPreferences()} disabled={disabled} aria-label="Làm mới memory"><RefreshCw /></Button></div>
        {preferences.length ? <ul className="space-y-2">{preferences.map((record) => <li key={record.preferenceId} className="flex items-center justify-between gap-2 rounded-lg border bg-muted/20 p-2"><div><p className="text-xs font-semibold">{preferenceLabels[record.preference.kind]} · {String(record.preference.value)}</p><p className="mt-1 font-mono text-xs text-muted-foreground">nguồn · {record.sourceTurnId}</p></div><Button type="button" variant="ghost" size="icon-sm" onClick={() => void durable.deletePreference(record.preferenceId)} disabled={disabled} aria-label={`Xoá memory ${record.preferenceId}`}><Trash2 /></Button></li>)}</ul> : <p className="text-xs text-muted-foreground">Chưa lưu preference nào.</p>}
        <form onSubmit={submit} className="grid gap-2 sm:grid-cols-[8rem_minmax(0,1fr)]">
          <label className="text-xs font-semibold text-muted-foreground">Loại<select value={kind} onChange={(event) => setKind(event.target.value as PreferenceKind)} className="mt-1 h-11 w-full rounded-lg border bg-background px-2 text-sm text-foreground outline-none focus-visible:ring-3 focus-visible:ring-ring/40">{Object.entries(preferenceLabels).map(([entry, label]) => <option key={entry} value={entry}>{label}</option>)}</select></label>
          <label className="text-xs font-semibold text-muted-foreground">Turn nguồn<select value={sourceTurnId} onChange={(event) => setSourceTurnId(event.target.value)} className="mt-1 h-11 w-full rounded-lg border bg-background px-2 text-sm text-foreground outline-none focus-visible:ring-3 focus-visible:ring-ring/40"><option value="">Chọn turn đã hoàn tất</option>{completedTurns.map((turn) => <option key={turn.turnId!} value={turn.turnId!}>{turn.turnId}</option>)}</select></label>
          <div className="sm:col-span-2 flex items-end gap-2"><label className="min-w-0 flex-1 text-xs font-semibold text-muted-foreground">Giá trị<Input value={value} onChange={(event) => setValue(event.target.value)} type={kind === "max_budget_vnd" ? "number" : "text"} min={kind === "max_budget_vnd" ? 0 : undefined} className="mt-1" /></label><Button type="submit" disabled={disabled || !sourceTurnId || !value.trim()}><Plus />Lưu</Button></div>
        </form>
      </div>
    </details>
  )
}
