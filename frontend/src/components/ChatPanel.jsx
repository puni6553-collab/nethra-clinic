import { useRef, useState } from 'react'
import { api } from '../api.js'

// Iteration 4: the chat is a booking agent. Every message runs through the
// red-flag check first, the agent finds free times and books/cancels/moves
// through the clinic API, and nothing changes until the patient presses
// Confirm on the card. The browser holds only the thread_id and the new
// message — the server keeps the conversation (last 20 messages reach the
// model). "New chat" (or logging out) starts a fresh thread.
const MAX_CHARS = 500

const KIND_LABEL = {
  answer: null,
  idk: null,
  emergency: 'Emergency',
  check_failed: 'Notice',
  unavailable: 'Notice',
}

function newThreadId() {
  if (globalThis.crypto?.randomUUID) return crypto.randomUUID()
  return `t-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
}

// Gemini spark — the four-point star mark, inline SVG like the rest of the
// app's branding (there are no image assets in this project).
function GeminiSpark({ className = '' }) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      width="24"
      height="24"
      aria-hidden="true"
      focusable="false"
    >
      <path
        fill="url(#gemini-grad)"
        d="M12 0c.34 4.9 2.76 9.1 7.17 11.5 0 0-1.2 4.9-7.17 12.5C6.03 16.4 4.83 11.5 4.83 11.5 9.24 9.1 11.66 4.9 12 0z"
      />
      <defs>
        <linearGradient id="gemini-grad" x1="4.8" y1="20" x2="19.2" y2="4" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#4285F4" />
          <stop offset="0.5" stopColor="#9B72CB" />
          <stop offset="1" stopColor="#D96570" />
        </linearGradient>
      </defs>
    </svg>
  )
}

export default function ChatPanel() {
  const [threadId, setThreadId] = useState(newThreadId)
  const [messages, setMessages] = useState([]) // {role, text, sources?, kind?}
  const [pending, setPending] = useState(null) // confirmation-card summary
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [open, setOpen] = useState(false) // floating widget: starts closed
  const endRef = useRef(null)

  function newChat() {
    setThreadId(newThreadId())
    setMessages([])
    setPending(null)
    setInput('')
  }

  function append(res) {
    setMessages((cur) => [
      ...cur,
      {
        role: 'assistant',
        text: res.message || '',
        sources: res.sources || [],
        skills: res.skills || [],
        kind: res.type,
      },
    ])
    setPending(res.pending_action || null)
  }

  async function post(payload, userText) {
    setBusy(true)
    if (userText) {
      setMessages((cur) => [...cur, { role: 'user', text: userText }])
      setInput('')
    }
    try {
      const res = await api.chat({ thread_id: threadId, ...payload })
      append(res)
    } catch (err) {
      setMessages((cur) => [
        ...cur,
        {
          role: 'assistant',
          text:
            err.message ||
            'Sorry, I can’t answer just now. Please try again, or ask Staff at the front desk.',
          kind: 'unavailable',
        },
      ])
      setPending(null)
    } finally {
      setBusy(false)
      requestAnimationFrame(() => endRef.current?.scrollIntoView({ behavior: 'smooth' }))
    }
  }

  // A typed message while a card is waiting counts as No — the server
  // declines the action and handles the new message in the same request.
  function send() {
    const text = input.trim()
    if (!text || busy) return
    post({ message: text }, text)
  }

  function confirmAction() {
    if (busy) return
    post({ confirm: true })
  }

  function declineAction() {
    if (busy) return
    post({ confirm: false })
  }

  const over = input.length > MAX_CHARS

  if (!open) {
    return (
      <div className="chat-fab-wrap">
        <button
          className="chat-fab"
          onClick={() => setOpen(true)}
          aria-label="Open chat"
          title="Ask NetraCare"
        >
          <GeminiSpark className="chat-fab-spark" />
        </button>
      </div>
    )
  }

  return (
    <div className="chat-fab-wrap">
      <section className="card chat-card chat-panel">
        <div className="fact-head">
          <h2 className="section-title" style={{ marginBottom: 0 }}>
            <GeminiSpark className="chat-header-spark" /> Ask NetraCare
          </h2>
          <button className="btn btn-small" onClick={newChat} disabled={busy || messages.length === 0}>
            New chat
          </button>
        </div>
        <p className="muted small">
          Ask for free times in plain words — the assistant books, cancels and reschedules
          after you confirm each change. It can’t diagnose or prescribe.
        </p>

      <div className="chat-log" role="log" aria-live="polite" aria-label="Chat messages">
        {messages.length === 0 && (
          <p className="muted small">
            Try: “Book me a retina follow-up with Dr. Shah next Friday morning.”
          </p>
        )}
        {messages.map((m, i) => {
          const emergency = m.kind === 'emergency'
          const notice = KIND_LABEL[m.kind] && m.kind !== 'answer' && m.kind !== 'idk'
          if (!m.text && m.role === 'assistant') return null
          return (
            <div key={i} className={`chat-row ${m.role}`}>
              <div
                className={
                  `chat-bubble ${m.role}` +
                  (emergency ? ' chat-emergency' : '') +
                  (notice ? ' chat-notice' : '')
                }
              >
                {emergency && <strong className="chat-tag">Emergency</strong>}
                <span className="chat-text">{m.text}</span>
                {m.sources && m.sources.length > 0 && (
                  <span className="chat-sources">
                    Sources: {m.sources.join(' · ')}
                  </span>
                )}
                {m.skills && m.skills.length > 0 && (
                  <span className="chat-sources">
                    Skill: {m.skills.join(' · ')}
                  </span>
                )}
              </div>
            </div>
          )
        })}

        {pending && (
          <div className="chat-row assistant">
            <div className="chat-confirm">
              <span className="chat-confirm-text">{pending}</span>
              <div className="chat-confirm-actions">
                <button
                  className="btn btn-primary btn-small"
                  onClick={confirmAction}
                  disabled={busy}
                >
                  Confirm
                </button>
                <button className="btn btn-small" onClick={declineAction} disabled={busy}>
                  No
                </button>
              </div>
            </div>
          </div>
        )}

        {busy && (
          <div className="chat-row assistant">
            <div className="chat-bubble assistant chat-thinking">
              <span className="spinner" aria-hidden="true" /> Thinking…
            </div>
          </div>
        )}
        <div ref={endRef} />
      </div>

      <div className="chat-input-row">
        <textarea
          className="chat-input"
          rows={2}
          maxLength={MAX_CHARS}
          aria-label="Message the NetraCare assistant"
          placeholder="Ask for free times, book, cancel or reschedule…"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              send()
            }
          }}
        />
        <div className="chat-side">
          <span className={`hint ${over ? 'hint-bad' : ''}`}>{input.length}/{MAX_CHARS}</span>
          <button className="btn" onClick={send} disabled={busy || !input.trim() || over}>
            Send
          </button>
        </div>
      </div>
      </section>
      <button
        className="chat-fab"
        onClick={() => setOpen(false)}
        aria-label="Close chat"
        title="Close chat"
      >
        <span className="chat-fab-close" aria-hidden="true">✕</span>
      </button>
    </div>
  )
}
