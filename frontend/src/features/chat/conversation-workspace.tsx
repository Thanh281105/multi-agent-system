import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react"
import {
  AlertCircle,
  ArrowDown,
  ArrowUp,
  Ban,
  BookOpenCheck,
  KeyRound,
  LoaderCircle,
  MapPinned,
  RotateCcw,
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
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupTextarea,
} from "@/components/ui/input-group"
import { Marker, MarkerContent, MarkerIcon } from "@/components/ui/marker"
import { Message, MessageContent, MessageHeader } from "@/components/ui/message"
import {
  MessageScroller,
  MessageScrollerButton,
  MessageScrollerContent,
  MessageScrollerItem,
  MessageScrollerProvider,
  MessageScrollerViewport,
} from "@/components/ui/message-scroller"
import type { ChatController } from "@/features/chat/use-chat-controller"
import { friendlyAgent, friendlyPhase } from "@/features/chat/presentation"
import { cn } from "@/lib/utils"
import type { Provenance } from "@/lib/contracts"

interface ConversationWorkspaceProps {
  controller: ChatController
  onCredentialRequest(): void
  onProvenanceRequest(sourceId: string): void
}

const prompts = [
  "Tìm tai nghe dưới 1 triệu, bán tốt và ít bị phàn nàn.",
  "So sánh đánh giá và rủi ro của các sách bán chạy.",
  "Phân tích xu hướng phân khúc laptop trong dữ liệu hiện có.",
]

export function ConversationWorkspace({
  controller,
  onCredentialRequest,
  onProvenanceRequest,
}: ConversationWorkspaceProps) {
  const [draft, setDraft] = useState("")
  const [draftError, setDraftError] = useState("")
  const composerRef = useRef<HTMLTextAreaElement>(null)
  const cancelRef = useRef<HTMLButtonElement>(null)
  const { state, workspaceState } = controller
  const streaming = state.request.phase === "streaming"
  const wasStreaming = useRef(streaming)
  const statuses =
    state.request.phase === "streaming" ? state.request.statuses : []
  const streamingAnswer =
    state.request.phase === "streaming" ? state.request.answer : ""
  const result =
    state.request.phase === "completed" ? state.request.result : undefined

  useEffect(() => {
    if (streaming && !wasStreaming.current) cancelRef.current?.focus()
    if (!streaming && wasStreaming.current) composerRef.current?.focus()
    wasStreaming.current = streaming
  }, [streaming])

  const send = async (value: string) => {
    if (!state.credentialConfigured) {
      onCredentialRequest()
      return
    }
    const cleaned = value.trim()
    if (!cleaned) {
      setDraftError("Hãy nhập câu hỏi cần điều phối.")
      return
    }
    if (cleaned.length > 2_000) {
      setDraftError("Câu hỏi không được vượt quá 2.000 ký tự.")
      return
    }
    if (!state.online) {
      toast.error("Đang ngoại tuyến. Hãy kiểm tra kết nối mạng.")
      return
    }

    setDraftError("")
    setDraft("")
    const outcome = await controller.sendMessage(cleaned)
    if (outcome === "credential_required") {
      setDraft(cleaned)
      onCredentialRequest()
    } else if (outcome === "offline") {
      setDraft(cleaned)
      toast.error("Đang ngoại tuyến. Hãy thử lại khi kết nối phục hồi.")
    } else if (outcome === "busy") {
      setDraft(cleaned)
      toast.info("Một yêu cầu khác đang được xử lý.")
    } else if (outcome === "invalid_message") {
      setDraft(cleaned)
      setDraftError("Câu hỏi cần từ 1 đến 2.000 ký tự.")
    }
  }

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    void send(draft)
  }

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (
      event.key === "Enter" &&
      !event.shiftKey &&
      !event.nativeEvent.isComposing
    ) {
      event.preventDefault()
      event.currentTarget.form?.requestSubmit()
    }
  }

  const retryLastMessage = () => {
    const lastUserMessage = state.messages.findLast(
      (message) => message.role === "user",
    )
    if (lastUserMessage) void send(lastUserMessage.text)
  }

  return (
    <section
      aria-labelledby="conversation-title"
      aria-busy={streaming}
      className="atlas-panel atlas-elevated flex h-[calc(100svh-5.5rem)] min-h-[36rem] flex-col overflow-hidden"
    >
      <header className="flex flex-wrap items-center justify-between gap-3 border-b bg-card px-4 py-3 sm:px-5">
        <div className="flex min-w-0 items-center gap-3">
          <div className="grid size-9 shrink-0 place-items-center rounded-lg bg-primary text-primary-foreground">
            <BookOpenCheck aria-hidden="true" className="size-4" />
          </div>
          <div className="min-w-0">
            <h1 id="conversation-title" className="truncate text-sm font-bold">
              Bàn điều phối quyết định
            </h1>
            <p className="truncate text-[0.7rem] text-muted-foreground">
              Hội thoại · tuyến thực thi · bằng chứng
            </p>
          </div>
        </div>
        <Badge
          variant="outline"
          className="border-accent/40 bg-accent/5 text-accent"
        >
          Dữ liệu demo học thuật
        </Badge>
      </header>

      <MessageScrollerProvider
        autoScroll={state.messages.length > 0 || streaming}
      >
        <MessageScroller className="min-h-0 flex-1">
          <MessageScrollerViewport
            className="px-4 py-5 sm:px-6"
            aria-label="Hội thoại và trạng thái điều phối"
          >
            <MessageScrollerContent className="mx-auto w-full max-w-3xl gap-5">
              <MessageScrollerItem messageId="evidence-scope">
                <Alert
                  role="note"
                  className="border-accent/25 bg-accent/5 text-foreground"
                >
                  <MapPinned aria-hidden="true" className="text-accent" />
                  <AlertTitle>Phạm vi bằng chứng</AlertTitle>
                  <AlertDescription>
                    Kho hiện tại phục vụ kiểm thử và trình diễn khóa luận; không
                    đại diện toàn bộ thị trường. Mỗi nguồn trong kết quả đều ghi
                    rõ Mẫu hoặc Thật.
                  </AlertDescription>
                </Alert>
              </MessageScrollerItem>

              {state.messages.length === 0 ? (
                <MessageScrollerItem messageId="welcome">
                  <WelcomePanel onPrompt={(prompt) => setDraft(prompt)} />
                </MessageScrollerItem>
              ) : (
                <MessageList
                  messages={state.messages}
                  provenance={result?.provenance ?? []}
                  onProvenanceRequest={onProvenanceRequest}
                />
              )}

              {streaming ? (
                <StreamingMessage answer={streamingAnswer} statuses={statuses} />
              ) : null}

              {result ? (
                <ResultNotice
                  result={result}
                  workspaceState={workspaceState}
                  onRetry={retryLastMessage}
                />
              ) : null}

              {state.request.phase === "failed" ? (
                <MessageScrollerItem messageId="request-failed">
                  <Alert
                    variant="destructive"
                    className="py-3"
                    aria-live="assertive"
                  >
                    <AlertCircle aria-hidden="true" />
                    <AlertTitle>Không thể hoàn tất tuyến này</AlertTitle>
                    <AlertDescription>
                      {state.request.failure.message}
                      {state.request.failure.traceId ? (
                        <span className="mt-1 block font-mono text-[0.68rem]">
                          trace · {state.request.failure.traceId}
                        </span>
                      ) : null}
                    </AlertDescription>
                    <AlertAction>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={
                          state.request.failure.code ===
                          "gateway.authentication_failed"
                            ? onCredentialRequest
                            : retryLastMessage
                        }
                      >
                        {state.request.failure.code ===
                        "gateway.authentication_failed" ? (
                          <KeyRound data-icon="inline-start" />
                        ) : (
                          <RotateCcw data-icon="inline-start" />
                        )}
                        {state.request.failure.code ===
                        "gateway.authentication_failed"
                          ? "Thiết lập key"
                          : "Thử lại"}
                      </Button>
                    </AlertAction>
                  </Alert>
                </MessageScrollerItem>
              ) : null}

              {workspaceState === "cancelled" ? (
                <MessageScrollerItem messageId="request-cancelled">
                  <Alert role="status">
                    <Ban aria-hidden="true" />
                    <AlertTitle>Đã dừng theo yêu cầu</AlertTitle>
                    <AlertDescription>
                      Kết quả đến muộn của tuyến cũ sẽ bị bỏ qua. Bạn có thể gửi
                      một câu hỏi mới ngay bây giờ.
                    </AlertDescription>
                  </Alert>
                </MessageScrollerItem>
              ) : null}

              {!state.online ? (
                <MessageScrollerItem messageId="request-offline">
                  <Alert variant="destructive" aria-live="assertive">
                    <WifiOff aria-hidden="true" />
                    <AlertTitle>Đang ngoại tuyến</AlertTitle>
                    <AlertDescription>
                      Nội dung trong phiên vẫn còn trên tab này; gửi yêu cầu sẽ
                      được mở lại khi mạng phục hồi.
                    </AlertDescription>
                  </Alert>
                </MessageScrollerItem>
              ) : null}

              {!controller.storageAvailable ? (
                <MessageScrollerItem messageId="storage-unavailable">
                  <Alert role="status">
                    <AlertCircle aria-hidden="true" />
                    <AlertTitle>Session storage bị chặn</AlertTitle>
                    <AlertDescription>
                      Phiên và lịch sử chỉ tồn tại cho đến khi trang được đóng
                      hoặc tải lại.
                    </AlertDescription>
                  </Alert>
                </MessageScrollerItem>
              ) : null}
            </MessageScrollerContent>
          </MessageScrollerViewport>
          <MessageScrollerButton
            size="icon"
            className="size-10"
            aria-label="Đi đến tin nhắn mới nhất"
          >
            <ArrowDown />
            <span className="sr-only">Đi đến tin nhắn mới nhất</span>
          </MessageScrollerButton>
        </MessageScroller>
      </MessageScrollerProvider>

      <form
        onSubmit={handleSubmit}
        className="border-t bg-card px-4 py-4 sm:px-5"
      >
        <div className="mx-auto max-w-3xl">
          <FieldGroup>
            <Field
              data-invalid={Boolean(draftError)}
              data-disabled={streaming || undefined}
            >
              <div className="mb-1 flex items-end justify-between gap-4">
                <FieldLabel htmlFor="chat-query">
                  Câu hỏi cần điều phối
                </FieldLabel>
                <span
                  className={cn(
                    "atlas-data text-muted-foreground",
                    draft.length > 1_900 && "text-destructive",
                  )}
                >
                  {draft.length.toLocaleString("vi-VN")} / 2.000
                </span>
              </div>
              <InputGroup className="h-auto rounded-xl bg-background shadow-sm">
                <InputGroupTextarea
                  ref={composerRef}
                  id="chat-query"
                  value={draft}
                  onChange={(event) => {
                    setDraft(event.target.value)
                    if (draftError) setDraftError("")
                  }}
                  onKeyDown={handleKeyDown}
                  maxLength={2_000}
                  rows={2}
                  disabled={streaming}
                  aria-invalid={Boolean(draftError)}
                  aria-describedby="composer-help composer-error"
                  placeholder="Ví dụ: Tìm tai nghe dưới 1 triệu, bán tốt và ít bị khách phàn nàn…"
                  className="max-h-40 min-h-20 py-3"
                />
                <InputGroupAddon align="inline-end" className="self-end pb-2">
                  {streaming ? (
                    <InputGroupButton
                      ref={cancelRef}
                      type="button"
                      size="icon-sm"
                      variant="destructive"
                      className="size-10"
                      onClick={controller.cancelRequest}
                      aria-label="Dừng yêu cầu đang chạy"
                    >
                      <Ban />
                    </InputGroupButton>
                  ) : (
                    <InputGroupButton
                      type="submit"
                      size="icon-sm"
                      variant="default"
                      className="size-10 active:scale-[0.98]"
                      disabled={!state.online}
                      aria-label={
                        state.credentialConfigured
                          ? "Gửi câu hỏi"
                          : "Kết nối Gateway key để gửi"
                      }
                    >
                      {state.credentialConfigured ? <ArrowUp /> : <KeyRound />}
                    </InputGroupButton>
                  )}
                </InputGroupAddon>
              </InputGroup>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <FieldDescription id="composer-help">
                  Enter để gửi · Shift + Enter để xuống dòng
                </FieldDescription>
                {!state.credentialConfigured ? (
                  <button
                    type="button"
                    className="text-xs font-semibold text-accent underline underline-offset-4 focus-visible:rounded-sm focus-visible:ring-2 focus-visible:ring-ring"
                    onClick={onCredentialRequest}
                  >
                    Cần Gateway key
                  </button>
                ) : null}
              </div>
              <FieldError id="composer-error">{draftError}</FieldError>
            </Field>
          </FieldGroup>
        </div>
      </form>
    </section>
  )
}

function WelcomePanel({ onPrompt }: { onPrompt(prompt: string): void }) {
  return (
    <section className="py-3 sm:py-8">
      <div className="max-w-2xl">
        <h2 className="max-w-xl font-display text-4xl leading-[1.08] font-semibold tracking-[-0.025em] text-balance sm:text-5xl">
          Mỗi kết luận đều có một đường về nguồn.
        </h2>
        <p className="mt-4 max-w-xl text-[0.95rem] leading-7 text-muted-foreground">
          Hãy đặt một câu hỏi thương mại điện tử bằng tiếng Việt. Router,
          Planner và các agent miền sẽ để lại tuyến thực thi có thể kiểm tra.
        </p>
      </div>
      <div className="mt-6 grid gap-2 sm:grid-cols-3">
        {prompts.map((prompt) => (
          <Button
            key={prompt}
            type="button"
            variant="outline"
            className="h-auto min-h-16 items-start justify-start whitespace-normal px-3 py-3 text-left leading-5"
            onClick={() => onPrompt(prompt)}
          >
            <span>{prompt}</span>
          </Button>
        ))}
      </div>
    </section>
  )
}

function MessageList({
  messages,
  provenance,
  onProvenanceRequest,
}: {
  messages: ChatController["state"]["messages"]
  provenance: Provenance[]
  onProvenanceRequest(sourceId: string): void
}) {
  return (
    <>
      {messages.map((message) => (
        <MessageScrollerItem
          key={message.id}
          messageId={message.id}
          scrollAnchor={message.role === "user"}
        >
          <Message
            align={message.role === "user" ? "end" : "start"}
            role="article"
            aria-label={
              message.role === "user" ? "Yêu cầu của bạn" : "Kết luận có nguồn"
            }
          >
            <MessageContent>
              <MessageHeader>
                {message.role === "user"
                  ? "Bạn · Yêu cầu"
                  : "Orchestrator · Kết luận có nguồn"}
              </MessageHeader>
              <Bubble
                align={message.role === "user" ? "end" : "start"}
                variant={message.role === "user" ? "default" : "outline"}
                className={cn(
                  "max-w-[92%]",
                  message.role === "assistant" && "w-full max-w-full",
                )}
              >
                <BubbleContent className="whitespace-pre-wrap [overflow-wrap:anywhere] text-[0.94rem] leading-7">
                  {message.role === "assistant"
                    ? renderAnswer(
                        message.text,
                        provenance,
                        onProvenanceRequest,
                      )
                    : message.text}
                </BubbleContent>
              </Bubble>
            </MessageContent>
          </Message>
        </MessageScrollerItem>
      ))}
    </>
  )
}

function StreamingMessage({
  answer,
  statuses,
}: {
  answer: Extract<
    ChatController["state"]["request"],
    { phase: "streaming" }
  >["answer"]
  statuses: Extract<
    ChatController["state"]["request"],
    { phase: "streaming" }
  >["statuses"]
}) {
  const current = statuses.at(-1)
  const liveUpdate = current
    ? `${friendlyPhase(current.phase)}. ${
        current.agent_id ? friendlyAgent(current.agent_id) : current.message
      }`
    : "Đang khởi tạo tuyến"
  return (
    <MessageScrollerItem messageId="streaming-status">
      <span
        className="sr-only"
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >
        {liveUpdate}
      </span>
      <Message role="article" aria-label="Tiến trình điều phối">
        <MessageContent className="max-w-[92%]">
          <Marker variant="border">
            <MarkerIcon>
              <LoaderCircle className="animate-spin text-accent" />
            </MarkerIcon>
            <MarkerContent>Orchestrator · Đang lập tuyến</MarkerContent>
          </Marker>
          <Bubble variant="outline" className="w-full max-w-full">
            <BubbleContent className="w-full px-4 py-3">
              <p className="text-sm font-semibold">
                {current ? friendlyPhase(current.phase) : "Đang khởi tạo tuyến"}
              </p>
              <p className="mt-0.5 text-xs text-muted-foreground">
                {current?.agent_id
                  ? friendlyAgent(current.agent_id)
                  : current?.message}
              </p>
              {answer ? (
                <p className="mt-4 whitespace-pre-wrap text-sm leading-7">
                  {answer}
                  <span
                    aria-hidden="true"
                    className="ml-0.5 inline-block h-4 w-0.5 animate-pulse bg-primary align-[-0.15em]"
                  />
                </p>
              ) : null}
              {statuses.length ? (
                <ol className="mt-3 flex gap-1.5 overflow-x-auto pb-1">
                  {statuses.map((status) => (
                    <li
                      key={`${status.sequence}-${status.phase}-${status.step_id ?? "system"}`}
                      title={status.message}
                    >
                      <Badge
                        variant="secondary"
                        className="shrink-0 font-mono tabular-nums"
                      >
                        {String(status.sequence).padStart(2, "0")} ·{" "}
                        {friendlyPhase(status.phase)}
                      </Badge>
                    </li>
                  ))}
                </ol>
              ) : null}
            </BubbleContent>
          </Bubble>
        </MessageContent>
      </Message>
    </MessageScrollerItem>
  )
}

function ResultNotice({
  result,
  workspaceState,
  onRetry,
}: {
  result: Extract<
    ChatController["state"]["request"],
    { phase: "completed" }
  >["result"]
  workspaceState: ChatController["workspaceState"]
  onRetry(): void
}) {
  if (workspaceState === "success" && result.warnings.length === 0) return null

  const warnings = result.warnings.join(" · ")
  const content =
    workspaceState === "partial"
      ? {
          title: "Kết quả hoàn tất một phần",
          detail:
            warnings ||
            "Một hoặc nhiều bước không hoàn tất; chỉ các kết luận có bằng chứng hợp lệ được hiển thị.",
          destructive: false,
        }
      : workspaceState === "empty"
        ? {
            title: "Chưa có kết luận để hiển thị",
            detail:
              warnings ||
              "Gateway đã hoàn tất nhưng không trả nội dung kết luận. Hãy thử diễn đạt câu hỏi cụ thể hơn.",
            destructive: false,
          }
        : workspaceState === "error"
          ? {
              title: "Tuyến chưa đạt trạng thái hoàn tất",
              detail:
                warnings ||
                `Gateway trả trạng thái ${result.status}; kết quả này chưa được xem là đã kiểm chứng.`,
              destructive: true,
            }
          : {
              title: "Lưu ý thực thi",
              detail: warnings,
              destructive: false,
            }

  return (
    <MessageScrollerItem messageId={`result-${workspaceState}`}>
      <Alert
        variant={content.destructive ? "destructive" : "default"}
        role="status"
      >
        <AlertCircle aria-hidden="true" />
        <AlertTitle>{content.title}</AlertTitle>
        <AlertDescription>{content.detail}</AlertDescription>
        {workspaceState === "empty" || workspaceState === "error" ? (
          <AlertAction>
            <Button size="sm" variant="outline" onClick={onRetry}>
              <RotateCcw data-icon="inline-start" />
              Thử lại
            </Button>
          </AlertAction>
        ) : null}
      </Alert>
    </MessageScrollerItem>
  )
}

function renderAnswer(
  value: string,
  provenance: Provenance[],
  onProvenanceRequest: (sourceId: string) => void,
) {
  const sourceIds = new Set(provenance.map((source) => source.source_id))
  const segments = value.split(/(\[[^\]\n]{1,120}\])/g)
  return segments.map((segment, index) => {
    const citation = /^\[([^\]\n]{1,120})\]$/.exec(segment)
    if (citation && sourceIds.has(citation[1])) {
      const sourceId = citation[1]
      return (
        <Button
          key={`${segment}-${index}`}
          type="button"
          variant="link"
          size="xs"
          className="mx-0.5 h-auto min-h-6 px-1 py-0 align-baseline"
          onClick={() => onProvenanceRequest(sourceId)}
          aria-label={`Mở nguồn ${sourceId}`}
        >
          <code className="font-mono">{segment}</code>
        </Button>
      )
    }
    return citation ? (
      <code
        key={`${segment}-${index}`}
        className="mx-0.5 rounded bg-accent/10 px-1 py-0.5 font-mono text-[0.78em] text-accent"
      >
        {segment}
      </code>
    ) : (
      segment
    )
  })
}
