import { useState, type FormEvent, type RefObject } from "react"
import { KeyRound, ShieldCheck } from "lucide-react"

import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"

interface CredentialDialogProps {
  open: boolean
  configured: boolean
  returnFocusRef: RefObject<HTMLElement | null>
  onOpenChange(open: boolean): void
  onConfigure(value: string): boolean
  onClear(): void
}

export function CredentialDialog({
  open,
  configured,
  returnFocusRef,
  onOpenChange,
  onConfigure,
  onClear,
}: CredentialDialogProps) {
  const [value, setValue] = useState("")
  const [error, setError] = useState("")

  const handleOpenChange = (nextOpen: boolean) => {
    if (!nextOpen) {
      setValue("")
      setError("")
    }
    onOpenChange(nextOpen)
  }

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!onConfigure(value)) {
      setError("Gateway API key cần ít nhất 8 ký tự.")
      return
    }
    setValue("")
    setError("")
    onOpenChange(false)
  }

  const handleClear = () => {
    onClear()
    setValue("")
    setError("")
    onOpenChange(false)
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        className="sm:max-w-md"
        onCloseAutoFocus={(event) => {
          const returnFocus = returnFocusRef.current
          if (
            !returnFocus ||
            returnFocus === document.body ||
            !returnFocus.isConnected
          ) {
            return
          }
          event.preventDefault()
          returnFocus.focus()
        }}
      >
        <DialogHeader>
          <div className="mb-2 grid size-10 place-items-center rounded-lg bg-primary text-primary-foreground">
            <KeyRound aria-hidden="true" className="size-5" />
          </div>
          <DialogTitle className="font-display text-2xl">
            Thiết lập Gateway API key
          </DialogTitle>
          <DialogDescription className="leading-6">
            Nhập khóa <code className="font-mono text-xs">X-API-Key</code> do
            Agent Gateway cấp. Đây không phải API key của nhà cung cấp model.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit}>
          <FieldGroup>
            <Field data-invalid={Boolean(error)}>
              <FieldLabel htmlFor="gateway-key">Gateway API key</FieldLabel>
              <Input
                id="gateway-key"
                type="password"
                value={value}
                onChange={(event) => {
                  setValue(event.target.value)
                  if (error) setError("")
                }}
                autoComplete="off"
                spellCheck={false}
                aria-invalid={Boolean(error)}
                aria-describedby={
                  error
                    ? "gateway-key-description gateway-key-error"
                    : "gateway-key-description"
                }
                className="h-11 font-mono"
                placeholder="Nhập khóa truy cập Gateway"
              />
              <FieldDescription
                id="gateway-key-description"
                className="flex gap-2"
              >
                <ShieldCheck
                  aria-hidden="true"
                  className="mt-0.5 size-4 shrink-0 text-success"
                />
                Chỉ giữ trong bộ nhớ của tab hiện tại; không ghi vào browser
                storage, URL, log hoặc lịch sử hội thoại.
              </FieldDescription>
              <FieldError id="gateway-key-error">{error}</FieldError>
            </Field>
          </FieldGroup>

          <DialogFooter className="mt-6 gap-2 sm:justify-between">
            {configured ? (
              <Button
                type="button"
                variant="destructive"
                className="h-11 sm:h-9"
                onClick={handleClear}
              >
                Xóa API key khỏi bộ nhớ
              </Button>
            ) : (
              <span />
            )}
            <div className="flex gap-2">
              <Button
                type="button"
                variant="outline"
                className="h-11 sm:h-9"
                onClick={() => handleOpenChange(false)}
              >
                Đóng
              </Button>
              <Button type="submit" className="h-11 min-w-24 sm:h-9">
                Dùng API key
              </Button>
            </div>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
