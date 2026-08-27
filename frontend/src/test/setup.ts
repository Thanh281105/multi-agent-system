import "@testing-library/jest-dom/vitest"

import { cleanup } from "@testing-library/react"
import { afterEach } from "vitest"

class ResizeObserverMock implements ResizeObserver {
  disconnect() {}
  observe() {}
  unobserve() {}
}

Object.defineProperty(globalThis, "ResizeObserver", {
  configurable: true,
  value: ResizeObserverMock,
})

Object.defineProperty(Element.prototype, "scrollIntoView", {
  configurable: true,
  value: () => undefined,
})

afterEach(() => cleanup())
