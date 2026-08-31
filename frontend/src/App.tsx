import { useEffect, useRef, useState } from "react"
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
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { AgentRoster } from "@/features/chat/agent-roster"
import { ConversationWorkspace } from "@/features/chat/conversation-workspace"
import { CredentialDialog } from "@/features/chat/credential-dialog"
import { EvidenceDossier } from "@/features/chat/evidence-dossier"
import {
  useChatController,
  type ChatController,
} from "@/features/chat/use-chat-controller"
import {
  provenanceAnchorId,
  shortenIdentifier,
  type ProvenanceAnchorScope,
} from "@/features/chat/presentation"
import { cn } from "@/lib/utils"

const XL_MEDIA_QUERY = "(min-width: 1280px)"

function App() {
  const controller = useChatController()
  return <EvidenceAtlas controller={controller} />
}

export function EvidenceAtlas({ controller }: { controller: ChatController }) {
  const [credentialOpen, setCredentialOpen] = useState(false)
  const credentialReturnFocus = useRef<HTMLElement | null>(null)
  const [agentOpen, setAgentOpen] = useState(false)
  const [evidenceOpen, setEvidenceOpen] = useState(false)
  const desktopRailsVisible = useMediaQuery(XL_MEDIA_QUERY, true)
  const [sourceTarget, setSourceTarget] = useState<{
    scope: ProvenanceAnchorScope
    sourceId: string
  } | null>(null)
  const { state } = controller
  const result =
    state.request.phase === "completed" ? state.request.result : undefined
  const executions = result?.executions ?? []
  const statuses =
    state.request.phase === "streaming" ? state.request.statuses : []

  useEffect(() => {
    if (typeof window.matchMedia !== "function") return
    const mediaQuery = window.matchMedia(XL_MEDIA_QUERY)
    const closeMobileSheets = (event: MediaQueryListEvent) => {
      if (!event.matches) return
      setAgentOpen(false)
      setEvidenceOpen(false)
    }
    mediaQuery.addEventListener("change", closeMobileSheets)
    return () => mediaQuery.removeEventListener("change", closeMobileSheets)
  }, [])

  useEffect(() => {
    if (!sourceTarget) return
    const frame = window.requestAnimationFrame(() => {
      const target = document.getElementById(
        provenanceAnchorId(sourceTarget.scope, sourceTarget.sourceId),
      )
      if (!target) return
      const reducedMotion =
        window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false
      target.scrollIntoView?.({
        behavior: reducedMotion ? "auto" : "smooth",
        block: "center",
      })
      target.focus({ preventScroll: true })
    })
    return () => window.cancelAnimationFrame(frame)
  }, [evidenceOpen, sourceTarget])

  const revealProvenance = (sourceId: string) => {
    const scope: ProvenanceAnchorScope =
      window.innerWidth >= 1_280 ? "desktop" : "mobile"
    setSourceTarget({ scope, sourceId })
    if (scope === "mobile") setEvidenceOpen(true)
  }

  const openCredentialDialog = (returnFocus?: HTMLElement) => {
    credentialReturnFocus.current =
      returnFocus ??
      (document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null)
    setCredentialOpen(true)
  }

  const handleCredentialOpenChange = (nextOpen: boolean) => {
    setCredentialOpen(nextOpen)
  }

  return (
    <TooltipProvider delayDuration={250}>
      <div className="min-h-svh bg-background">
        <a className="skip-link" href="#workspace">
          Đi đến phần hỏi đáp
        </a>
        <TopBar
          controller={controller}
          onCredentialRequest={openCredentialDialog}
          desktopRailsVisible={desktopRailsVisible}
          agentOpen={agentOpen}
          onAgentOpenChange={setAgentOpen}
          evidenceOpen={evidenceOpen}
          onEvidenceOpenChange={setEvidenceOpen}
          selectedSourceId={
            sourceTarget?.scope === "mobile" ? sourceTarget.sourceId : undefined
          }
        />

        <div className="mx-auto grid w-full max-w-[100rem] gap-2 px-2 pb-2 xl:grid-cols-[17.5rem_minmax(32rem,1fr)_23rem]">
          <main
            id="workspace"
            tabIndex={-1}
            className="min-w-0 xl:col-start-2 xl:row-start-1"
          >
            <ConversationWorkspace
              controller={controller}
              onCredentialRequest={openCredentialDialog}
              onProvenanceRequest={revealProvenance}
            />
          </main>

          {desktopRailsVisible ? (
            <>
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
                aria-label="Chi tiết thực thi"
                className="hidden min-w-0 xl:col-start-3 xl:row-start-1 xl:block"
              >
                <EvidenceDossier
                  state={state}
                  anchorScope="desktop"
                  selectedSourceId={
                    sourceTarget?.scope === "desktop"
                      ? sourceTarget.sourceId
                      : undefined
                  }
                />
              </aside>
            </>
          ) : null}
        </div>

        <CredentialDialog
          open={credentialOpen}
          configured={state.credentialConfigured}
          returnFocusRef={credentialReturnFocus}
          onOpenChange={handleCredentialOpenChange}
          onConfigure={controller.configureCredential}
          onClear={controller.clearCredential}
        />
        <Toaster position="bottom-right" />
      </div>
    </TooltipProvider>
  )
}

function useMediaQuery(query: string, fallback: boolean) {
  const [matches, setMatches] = useState(() => {
    if (typeof window.matchMedia !== "function") return fallback
    return window.matchMedia(query).matches
  })

  useEffect(() => {
    if (typeof window.matchMedia !== "function") return
    const mediaQuery = window.matchMedia(query)
    const handleChange = (event: MediaQueryListEvent) => setMatches(event.matches)
    mediaQuery.addEventListener("change", handleChange)
    return () => mediaQuery.removeEventListener("change", handleChange)
  }, [query])

  return matches
}

function TopBar({
  controller,
  onCredentialRequest,
  desktopRailsVisible,
  agentOpen,
  onAgentOpenChange,
  evidenceOpen,
  onEvidenceOpenChange,
  selectedSourceId,
}: {
  controller: ChatController
  onCredentialRequest(returnFocus?: HTMLElement): void
  desktopRailsVisible: boolean
  agentOpen: boolean
  onAgentOpenChange(open: boolean): void
  evidenceOpen: boolean
  onEvidenceOpenChange(open: boolean): void
  selectedSourceId?: string
}) {
  const { state } = controller
  const result =
    state.request.phase === "completed" ? state.request.result : undefined
  const executions = result?.executions ?? []
  const statuses =
    state.request.phase === "streaming" ? state.request.statuses : []
  const handleMobileSheetCloseAutoFocus = (event: Event) => {
    if (!desktopRailsVisible) return
    event.preventDefault()
    document.getElementById("workspace")?.focus({ preventScroll: true })
  }

  return (
    <header className="px-3 py-3 sm:px-4">
      <div className="mx-auto flex h-14 w-full max-w-[98rem] items-center justify-between gap-3 border-b border-foreground/15">
        <div className="flex min-w-0 items-center gap-3">
          <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-primary text-primary-foreground">
            <Compass aria-hidden="true" className="size-5" />
          </span>
          <div className="min-w-0">
            <p className="truncate font-display text-lg leading-none font-semibold tracking-[-0.02em] sm:text-xl">
              <span className="sm:hidden">Atlas</span>
              <span className="hidden sm:inline">Evidence Atlas</span>
            </p>
            <p className="mt-1 hidden truncate text-xs font-semibold tracking-[0.06em] text-muted-foreground uppercase sm:block">
              Tiki Books · Historical snapshot
            </p>
          </div>
        </div>

        <div className="hidden min-w-0 items-center gap-4 lg:flex">
          <div className="flex items-center gap-2 text-xs">
            {state.online ? (
              <Wifi aria-hidden="true" className="size-3.5 text-success" />
            ) : (
              <WifiOff
                aria-hidden="true"
                className="size-3.5 text-destructive"
              />
            )}
            <span
              className={state.online ? "text-success" : "text-destructive"}
            >
              {state.online ? "Có kết nối mạng" : "Mất kết nối mạng"}
            </span>
          </div>
          <span aria-hidden="true" className="h-5 w-px bg-border" />
          <div className="min-w-0 text-right">
            <p className="text-xs text-muted-foreground">Phiên</p>
            <p
              className="max-w-44 truncate font-mono text-xs"
              title={state.sessionId ?? undefined}
            >
              {state.sessionId
                ? shortenIdentifier(state.sessionId, 28)
                : "phiên mới"}
            </p>
          </div>
        </div>

        <nav
          aria-label="Điều khiển workspace"
          className="flex items-center gap-1.5"
        >
          <Sheet open={agentOpen} onOpenChange={onAgentOpenChange}>
            <Tooltip>
              <TooltipTrigger asChild>
                <SheetTrigger asChild>
                  <Button
                    variant="outline"
                    size="icon"
                    className="size-11 xl:hidden"
                    aria-label="Mở danh sách agent"
                  >
                    <Menu />
                  </Button>
                </SheetTrigger>
              </TooltipTrigger>
              <TooltipContent>Các agent</TooltipContent>
            </Tooltip>
            <SheetContent
              side="left"
              className="overflow-y-auto p-0"
              onCloseAutoFocus={handleMobileSheetCloseAutoFocus}
            >
              <SheetHeader className="sr-only">
                <SheetTitle>Các agent</SheetTitle>
                <SheetDescription>
                  Các agent nghiệp vụ sách và trạng thái thực thi hiện tại.
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

          <Sheet open={evidenceOpen} onOpenChange={onEvidenceOpenChange}>
            <Tooltip>
              <TooltipTrigger asChild>
                <SheetTrigger asChild>
                  <Button
                    variant="outline"
                    size="icon"
                    className="size-11 xl:hidden"
                    aria-label="Mở chi tiết thực thi"
                  >
                    <DatabaseZap />
                  </Button>
                </SheetTrigger>
              </TooltipTrigger>
              <TooltipContent>Chi tiết thực thi</TooltipContent>
            </Tooltip>
            <SheetContent
              side="right"
              className="overflow-y-auto p-0"
              onCloseAutoFocus={handleMobileSheetCloseAutoFocus}
            >
              <SheetHeader className="sr-only">
                <SheetTitle>Chi tiết thực thi</SheetTitle>
                <SheetDescription>
                  Kế hoạch DAG, lần gọi model, nguồn bằng chứng và các ID của
                  yêu cầu.
                </SheetDescription>
              </SheetHeader>
              <EvidenceDossier
                state={state}
                compact
                anchorScope="mobile"
                selectedSourceId={selectedSourceId}
              />
            </SheetContent>
          </Sheet>

          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                aria-label={
                  state.credentialConfigured
                    ? "Thay Gateway API key"
                    : "Thiết lập Gateway API key"
                }
                variant={state.credentialConfigured ? "outline" : "secondary"}
                size="sm"
                className={cn(
                  "h-11 min-w-11 gap-2",
                  state.credentialConfigured &&
                    "border-success/40 text-success",
                )}
                onClick={(event) => onCredentialRequest(event.currentTarget)}
              >
                <KeyRound data-icon="inline-start" />
                <span className="hidden sm:inline">
                  {state.credentialConfigured
                    ? "API key đã thiết lập"
                    : "Thiết lập key"}
                </span>
              </Button>
            </TooltipTrigger>
            <TooltipContent>
              {state.credentialConfigured
                ? "Gateway API key chỉ tồn tại trong tab"
                : "Thiết lập Gateway API key"}
            </TooltipContent>
          </Tooltip>

          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className="size-11"
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
