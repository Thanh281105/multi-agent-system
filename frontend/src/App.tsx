import { useState } from "react"
import {
  Compass,
  DatabaseZap,
  KeyRound,
  Menu,
  Plus,
  Wifi,
  WifiOff,
} from "lucide-react"

import { Button } from "@/components/ui/button"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet"
import { Toaster } from "@/components/ui/sonner"
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip"
import { AgentRoster } from "@/features/chat/agent-roster"
import { ConversationWorkspace } from "@/features/chat/conversation-workspace"
import { CredentialDialog } from "@/features/chat/credential-dialog"
import { EvidenceDossier } from "@/features/chat/evidence-dossier"
import {
  useChatController,
  type ChatController,
} from "@/features/chat/use-chat-controller"
import { shortenIdentifier } from "@/features/chat/presentation"
import { cn } from "@/lib/utils"

function App() {
  const controller = useChatController()
  return <EvidenceAtlas controller={controller} />
}

export function EvidenceAtlas({ controller }: { controller: ChatController }) {
  const [credentialOpen, setCredentialOpen] = useState(false)
  const { state } = controller
  const result = state.request.phase === "completed" ? state.request.result : undefined
  const executions = result?.executions ?? []
  const statuses =
    state.request.phase === "streaming" ? state.request.statuses : []

  return (
    <TooltipProvider delayDuration={250}>
      <div className="min-h-svh bg-background">
        <a className="skip-link" href="#workspace">
          Đi đến bàn điều phối
        </a>
        <TopBar
          controller={controller}
          onCredentialRequest={() => setCredentialOpen(true)}
        />

        <div className="mx-auto grid w-full max-w-[100rem] gap-2 px-2 pb-2 xl:grid-cols-[17.5rem_minmax(32rem,1fr)_23rem]">
          <main id="workspace" className="min-w-0 xl:col-start-2 xl:row-start-1">
            <ConversationWorkspace
              controller={controller}
              onCredentialRequest={() => setCredentialOpen(true)}
            />
          </main>

          <aside
            aria-label="Danh sách agent"
            className="hidden min-w-0 xl:col-start-1 xl:row-start-1 xl:block"
          >
            <AgentRoster
              executions={executions}
              statuses={statuses}
              requestPhase={state.request.phase}
            />
          </aside>

          <aside
            aria-label="Hồ sơ thực thi"
            className="hidden min-w-0 xl:col-start-3 xl:row-start-1 xl:block"
          >
            <EvidenceDossier state={state} />
          </aside>
        </div>

        <CredentialDialog
          open={credentialOpen}
          configured={state.credentialConfigured}
          onOpenChange={setCredentialOpen}
          onConfigure={controller.configureCredential}
          onClear={controller.clearCredential}
        />
        <Toaster position="bottom-right" />
      </div>
    </TooltipProvider>
  )
}

function TopBar({
  controller,
  onCredentialRequest,
}: {
  controller: ChatController
  onCredentialRequest(): void
}) {
  const { state } = controller
  const result = state.request.phase === "completed" ? state.request.result : undefined
  const executions = result?.executions ?? []
  const statuses =
    state.request.phase === "streaming" ? state.request.statuses : []

  return (
    <header className="px-3 py-3 sm:px-4">
      <div className="mx-auto flex h-14 w-full max-w-[98rem] items-center justify-between gap-3 border-b border-foreground/15">
        <div className="flex min-w-0 items-center gap-3">
          <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-primary text-primary-foreground">
            <Compass aria-hidden="true" className="size-5" />
          </span>
          <div className="min-w-0">
            <p className="truncate font-display text-xl leading-none font-semibold tracking-[-0.02em]">
              Thương Trí
            </p>
            <p className="mt-1 hidden truncate text-[0.65rem] font-semibold tracking-[0.08em] text-muted-foreground uppercase sm:block">
              Evidence Atlas · Multi-Agent Commerce
            </p>
          </div>
        </div>

        <div className="hidden min-w-0 items-center gap-4 lg:flex">
          <div className="flex items-center gap-2 text-xs">
            {state.online ? (
              <Wifi aria-hidden="true" className="size-3.5 text-success" />
            ) : (
              <WifiOff aria-hidden="true" className="size-3.5 text-destructive" />
            )}
            <span className={state.online ? "text-success" : "text-destructive"}>
              {state.online ? "Trình duyệt online" : "Trình duyệt ngoại tuyến"}
            </span>
          </div>
          <span aria-hidden="true" className="h-5 w-px bg-border" />
          <div className="min-w-0 text-right">
            <p className="text-[0.62rem] text-muted-foreground">session</p>
            <p
              className="max-w-44 truncate font-mono text-[0.65rem]"
              title={state.sessionId ?? undefined}
            >
              {state.sessionId ? shortenIdentifier(state.sessionId, 28) : "phiên mới"}
            </p>
          </div>
        </div>

        <nav aria-label="Điều khiển workspace" className="flex items-center gap-1.5">
          <Sheet>
            <Tooltip>
              <TooltipTrigger asChild>
                <SheetTrigger asChild>
                  <Button
                    variant="outline"
                    size="icon"
                    className="xl:hidden"
                    aria-label="Mở danh sách agent"
                  >
                    <Menu />
                  </Button>
                </SheetTrigger>
              </TooltipTrigger>
              <TooltipContent>Danh sách agent</TooltipContent>
            </Tooltip>
            <SheetContent side="left" className="w-[20rem] overflow-y-auto p-0 sm:max-w-sm">
              <SheetHeader className="sr-only">
                <SheetTitle>Trạm chuyên gia</SheetTitle>
                <SheetDescription>
                  Danh sách agent miền và trạng thái thực thi hiện tại.
                </SheetDescription>
              </SheetHeader>
              <AgentRoster
                executions={executions}
                statuses={statuses}
                requestPhase={state.request.phase}
                compact
              />
            </SheetContent>
          </Sheet>

          <Sheet>
            <Tooltip>
              <TooltipTrigger asChild>
                <SheetTrigger asChild>
                  <Button
                    variant="outline"
                    size="icon"
                    className="xl:hidden"
                    aria-label="Mở hồ sơ thực thi"
                  >
                    <DatabaseZap />
                  </Button>
                </SheetTrigger>
              </TooltipTrigger>
              <TooltipContent>Hồ sơ thực thi</TooltipContent>
            </Tooltip>
            <SheetContent side="right" className="w-[22rem] overflow-y-auto p-0 sm:max-w-md">
              <SheetHeader className="sr-only">
                <SheetTitle>Hồ sơ thực thi</SheetTitle>
                <SheetDescription>
                  DAG, model call, provenance và định danh trace đã được làm sạch.
                </SheetDescription>
              </SheetHeader>
              <EvidenceDossier state={state} compact />
            </SheetContent>
          </Sheet>

          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                aria-label={
                  state.credentialConfigured
                    ? "Thay Gateway key"
                    : "Kết nối Gateway key"
                }
                variant={state.credentialConfigured ? "outline" : "secondary"}
                size="sm"
                className={cn(
                  "h-9 gap-2",
                  state.credentialConfigured && "border-success/40 text-success",
                )}
                onClick={onCredentialRequest}
              >
                <KeyRound data-icon="inline-start" />
                <span className="hidden sm:inline">
                  {state.credentialConfigured ? "Key trong bộ nhớ" : "Kết nối key"}
                </span>
              </Button>
            </TooltipTrigger>
            <TooltipContent>
              {state.credentialConfigured
                ? "Gateway key chỉ tồn tại trong tab"
                : "Thiết lập Gateway API key"}
            </TooltipContent>
          </Tooltip>

          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                onClick={controller.resetSession}
                aria-label="Tạo phiên mới"
              >
                <Plus />
              </Button>
            </TooltipTrigger>
            <TooltipContent>Tạo phiên mới</TooltipContent>
          </Tooltip>
        </nav>
      </div>
    </header>
  )
}

export default App
