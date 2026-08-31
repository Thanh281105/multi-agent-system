import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { setNonce } from 'get-nonce'
import './index.css'
import App from './App.tsx'

const cspNonce = document
  .querySelector<HTMLMetaElement>('meta[name="csp-nonce"]')
  ?.content.trim()
if (cspNonce) setNonce(cspNonce)

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
