import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react"
import {
  AlertCircle,
  ArrowUp,
  Ban,
  BookOpenCheck,
  KeyRound,
  LoaderCircle,
  RotateCcw,
  Sparkles,
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
import { Button } from "@/components/ui/button"
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import { Textarea } from "@/components/ui/textarea"
import type { ChatController } from "@/features/chat/use-chat-controller"
import { friendlyAgent, friendlyPhase } from "@/features/chat/presentation"
import { cn } from "@/lib/utils"

interface ConversationWorkspaceProps {
  controller: ChatController
  onCredentialRequest(): void
}

const prompts = [
  "Tìm tai nghe dưới 1 triệu, bán tốt và ít bị phàn nàn.",
  "So sánh đánh giá và rủi ro của các sách bán chạy.",
  "Phân tích xu hướng phân khúc laptop trong dữ liệu hiện có.",
]

export function ConversationWorkspace({
  controller,
  onCredentialRequest,
}: ConversationWorkspaceProps) {
  const [draft, setDraft] = useState("")
  const [draftError, setDraftError] = useState("")
  const transcriptRef = useRef<HTMLDivElement>(null)
  const { state, workspaceState } = controller
  const streaming = state.request.phase === "streaming"
  const statuses =
    state.request.phase === "streaming" ? state.request.statuses : []

  useEffect(() => {
    const transcript = transcriptRef.current
    if (!transcript) return
    if (typeof transcript.scrollTo === "function") {
      transcript.scrollTo({ top: transcript.scrollHeight, behavior: "smooth" })
    } else {
      transcript.scrollTop = transcript.scrollHeight
    }
  }, [state.messages, state.request.phase, statuses.length])

  const send = async (value: string) => {
    const cleaned = value.trim()
    if (!cleaned) {
      setDraftError("Hãy nhập câu hỏi cần điều phối.")
      return
    }
    if (cleaned.length > 2_000) {
      setDraftError("Câu hỏi không được vượt quá 2.000 ký tự.")
      return
    }
    if (!state.credentialConfigured) {
      onCredentialRequest()
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
      className="atlas-panel atlas-elevated flex min-h-[42rem] flex-col overflow-hidden xl:h-[calc(100svh-5.5rem)]"
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

      <div
        ref={transcriptRef}
        className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 py-5 sm:px-6"
        aria-live="off"
      >
        <div className="mx-auto flex w-full max-w-3xl flex-col gap-5">
          <Alert
            role="note"
            className="border-accent/25 bg-accent/5 text-foreground"
          >
            <Sparkles aria-hidden="true" className="text-accent" />
            <AlertTitle>Phạm vi bằng chứng</AlertTitle>
            <AlertDescription>
              Kho hiện tại phục vụ kiểm thử và trình diễn khóa luận; không đại
              diện toàn bộ thị trường. Mỗi nguồn trong kết quả đều ghi rõ Mẫu
              hoặc Thật.
            </AlertDescription>
          </Alert>

          {state.messages.length === 0 ? (
            <WelcomePanel onPrompt={(prompt) => setDraft(prompt)} />
          ) : (
            <MessageList messages={state.messages} />
          )}

          {streaming ? <StreamingMessage statuses={statuses} /> : null}

          {state.request.phase === "failed" ? (
            <Alert variant="destructive" className="py-3" aria-live="assertive">
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
                    state.request.failure.code === "gateway.authentication_failed"
                      ? onCredentialRequest
                      : retryLastMessage
                  }
                >
                  {state.request.failure.code === "gateway.authentication_failed" ? (
                    <KeyRound data-icon="inline-start" />
                  ) : (
                    <RotateCcw data-icon="inline-start" />
                  )}
                  {state.request.failure.code === "gateway.authentication_failed"
                    ? "Thiết lập key"
                    : "Thử lại"}
                </Button>
              </AlertAction>
            </Alert>
          ) : null}

          {workspaceState === "cancelled" ? (
            <Alert role="status">
              <Ban aria-hidden="true" />
              <AlertTitle>Đã dừng theo yêu cầu</AlertTitle>
              <AlertDescription>
                Kết quả đến muộn của tuyến cũ sẽ bị bỏ qua. Bạn có thể gửi một
                câu hỏi mới ngay bây giờ.
              </AlertDescription>
            </Alert>
          ) : null}

          {!state.online ? (
            <Alert variant="destructive" aria-live="assertive">
              <WifiOff aria-hidden="true" />
              <AlertTitle>Đang ngoại tuyến</AlertTitle>
              <AlertDescription>
                Nội dung trong phiên vẫn còn trên tab này; gửi yêu cầu sẽ được
                mở lại khi mạng phục hồi.
              </AlertDescription>
            </Alert>
          ) : null}

          {!controller.storageAvailable ? (
            <Alert role="status">
              <AlertCircle aria-hidden="true" />
              <AlertTitle>Session storage bị chặn</AlertTitle>
              <AlertDescription>
                Phiên và lịch sử chỉ tồn tại cho đến khi trang được đóng hoặc tải lại.
              </AlertDescription>
            </Alert>
          ) : null}
        </div>
      </div>

      <form
        onSubmit={handleSubmit}
        className="border-t bg-card px-4 py-4 sm:px-5"
      >
        <div className="mx-auto max-w-3xl">
          <FieldGroup>
            <Field data-invalid={Boolean(draftError)}>
              <div className="mb-1 flex items-end justify-between gap-4">
                <FieldLabel htmlFor="chat-query">Câu hỏi cần điều phối</FieldLabel>
                <span
                  className={cn(
                    "atlas-data text-muted-foreground",
                    draft.length > 1_900 && "text-destructive",
                  )}
                >
                  {draft.length.toLocaleString("vi-VN")} / 2.000
                </span>
              </div>
              <div className="relative rounded-xl border bg-background p-1.5 shadow-sm transition-[border-color,box-shadow] focus-within:border-ring focus-within:ring-3 focus-within:ring-ring/20">
                <Textarea
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
                  className="max-h-40 min-h-16 resize-none border-0 bg-transparent pr-14 shadow-none focus-visible:ring-0"
                />
                {streaming ? (
                  <Button
                    type="button"
                    size="icon"
                    variant="destructive"
                    className="absolute right-2 bottom-2 size-10"
                    onClick={controller.cancelRequest}
                    aria-label="Dừng yêu cầu đang chạy"
                  >
                    <Ban />
                  </Button>
                ) : (
                  <Button
                    type="submit"
                    size="icon"
                    className="absolute right-2 bottom-2 size-10 active:scale-[0.98]"
                    disabled={!state.online || !draft.trim()}
                    aria-label={
                      state.credentialConfigured
                        ? "Gửi câu hỏi"
                        : "Kết nối Gateway key để gửi"
                    }
                  >
                    {state.credentialConfigured ? <ArrowUp /> : <KeyRound />}
                  </Button>
                )}
              </div>
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
    <section className="py-6 sm:py-10">
      <div className="max-w-2xl">
        <span className="atlas-data text-accent">ATLAS NOTE / 001</span>
        <h2 className="mt-3 max-w-xl font-display text-4xl leading-[1.08] font-semibold tracking-[-0.025em] text-balance sm:text-5xl">
          Mỗi kết luận đều có một đường về nguồn.
        </h2>
        <p className="mt-4 max-w-xl text-[0.95rem] leading-7 text-muted-foreground">
          Hãy đặt một câu hỏi thương mại điện tử bằng tiếng Việt. Router,
          Planner và các agent miền sẽ để lại tuyến thực thi có thể kiểm tra.
        </p>
      </div>
      <div className="mt-7 grid gap-2 sm:grid-cols-3">
        {prompts.map((prompt, index) => (
          <Button
            key={prompt}
            type="button"
            variant="outline"
            className="h-auto min-h-20 items-start justify-start whitespace-normal px-3 py-3 text-left leading-5"
            onClick={() => onPrompt(prompt)}
          >
            <span className="mr-1 font-mono text-[0.65rem] text-accent">
              0{index + 1}
            </span>
            <span>{prompt}</span>
          </Button>
        ))}
      </div>
    </section>
  )
}

function MessageList({
  messages,
}: {
  messages: ChatController["state"]["messages"]
}) {
  return (
    <div className="flex flex-col gap-5">
      {messages.map((message) => (
        <article
          key={message.id}
          className={cn(
            "max-w-[92%]",
            message.role === "user" ? "ml-auto" : "mr-auto w-full",
          )}
        >
          <div className="mb-1.5 flex items-center gap-2 text-[0.68rem] font-bold text-muted-foreground">
            <span>{message.role === "user" ? "Bạn" : "Orchestrator"}</span>
            <span aria-hidden="true">·</span>
            <span>{message.role === "user" ? "Yêu cầu" : "Kết luận có nguồn"}</span>
          </div>
          <div
            className={cn(
              "rounded-xl px-4 py-3 text-[0.94rem] leading-7",
              message.role === "user"
                ? "rounded-tr-sm bg-primary text-primary-foreground"
                : "rounded-tl-sm border bg-card",
            )}
          >
            {message.role === "assistant"
              ? renderAnswer(message.text)
              : message.text}
          </div>
        </article>
      ))}
    </div>
  )
}

function StreamingMessage({
  statuses,
}: {
  statuses: Extract<
    ChatController["state"]["request"],
    { phase: "streaming" }
  >["statuses"]
}) {
  const current = statuses.at(-1)
  return (
    <article className="mr-auto w-full max-w-[92%]" aria-live="polite">
      <div className="mb-1.5 text-[0.68rem] font-bold text-muted-foreground">
        Orchestrator · Đang lập tuyến
      </div>
      <div className="rounded-xl rounded-tl-sm border bg-card px-4 py-3">
        <div className="flex items-center gap-3">
          <LoaderCircle aria-hidden="true" className="size-4 animate-spin text-accent" />
          <div>
            <p className="text-sm font-semibold">
              {current ? friendlyPhase(current.phase) : "Đang khởi tạo tuyến"}
            </p>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {current?.agent_id ? friendlyAgent(current.agent_id) : current?.message}
            </p>
          </div>
        </div>
        {statuses.length ? (
          <ol className="mt-3 flex gap-1.5 overflow-x-auto pb-1">
            {statuses.map((status) => (
              <li
                key={`${status.sequence}-${status.phase}-${status.step_id ?? "system"}`}
                className="shrink-0 rounded-md bg-muted px-2 py-1 font-mono text-[0.62rem] text-muted-foreground"
                title={status.message}
              >
                {String(status.sequence).padStart(2, "0")} · {friendlyPhase(status.phase)}
              </li>
            ))}
          </ol>
        ) : null}
      </div>
    </article>
  )
}

function renderAnswer(value: string) {
  const segments = value.split(/(\[[^\]\n]{1,120}\])/g)
  return segments.map((segment, index) =>
    /^\[[^\]\n]{1,120}\]$/.test(segment) ? (
      <code
        key={`${segment}-${index}`}
        className="mx-0.5 rounded bg-accent/10 px-1 py-0.5 font-mono text-[0.78em] text-accent"
      >
        {segment}
      </code>
    ) : (
      segment
    ),
  )
}
