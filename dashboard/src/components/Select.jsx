import { useEffect, useId, useRef, useState } from 'react'

// Accessible custom dropdown replacing the native <select>, so the open menu is
// styled by the app (both themes) instead of the OS. Keyboard: Up/Down/Home/End
// move the active option, Enter/Space open then commit, Esc closes, printable
// keys type-ahead. Focus stays on the trigger; the active option is exposed via
// aria-activedescendant (the listbox pattern). `options` is [{ value, label,
// disabled }]; onChange receives the chosen value string.
//
// `className` styles the trigger button (callers pass their own border/bg/size so
// each dropdown keeps matching its surroundings); the menu panel is always the
// app surface so options stay legible even under a colored trigger (e.g. the
// white-on-blue sidebar switcher).

function nextEnabled(options, from, dir) {
  const n = options.length
  if (!n) return -1
  let i = from
  for (let c = 0; c < n; c++) {
    i = (i + dir + n) % n
    if (!options[i]?.disabled) return i
  }
  return from >= 0 && !options[from]?.disabled ? from : -1
}

export default function Select({
  value,
  onChange,
  options = [],
  disabled = false,
  ariaLabel,
  id,
  placeholder = 'Select…',
  className = '',
  menuClassName = '',
  wrapperClassName = 'relative',
}) {
  const [open, setOpen] = useState(false)
  const [openUp, setOpenUp] = useState(false)
  const [activeIndex, setActiveIndex] = useState(-1)
  const rootRef = useRef(null)
  const btnRef = useRef(null)
  const listRef = useRef(null)
  const typeahead = useRef({ str: '', at: 0 })
  const autoId = useId()
  const listId = `${id || autoId}-listbox`

  const selectedIndex = options.findIndex(o => o.value === value)
  const selected = selectedIndex >= 0 ? options[selectedIndex] : null

  // Close when focus/click leaves the component.
  useEffect(() => {
    if (!open) return
    const onDown = e => { if (rootRef.current && !rootRef.current.contains(e.target)) setOpen(false) }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('touchstart', onDown)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('touchstart', onDown)
    }
  }, [open])

  // Keep the active option visible while navigating.
  useEffect(() => {
    if (open && activeIndex >= 0) {
      listRef.current?.querySelector(`[data-index="${activeIndex}"]`)?.scrollIntoView({ block: 'nearest' })
    }
  }, [open, activeIndex])

  const openMenu = () => {
    if (disabled) return
    const rect = btnRef.current?.getBoundingClientRect()
    if (rect) {
      const below = window.innerHeight - rect.bottom
      // Flip above only when there is genuinely more room up than down.
      setOpenUp(below < 260 && rect.top > below)
    }
    setActiveIndex(selectedIndex >= 0 ? selectedIndex : nextEnabled(options, -1, 1))
    setOpen(true)
  }

  const commit = i => {
    const opt = options[i]
    if (!opt || opt.disabled) return
    if (opt.value !== value) onChange(opt.value)
    setOpen(false)
    btnRef.current?.focus()
  }

  const onKeyDown = e => {
    if (disabled) return
    if (!open) {
      if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(e.key)) { e.preventDefault(); openMenu() }
      return
    }
    switch (e.key) {
      case 'Escape': e.preventDefault(); setOpen(false); btnRef.current?.focus(); break
      case 'ArrowDown': e.preventDefault(); setActiveIndex(i => nextEnabled(options, i, 1)); break
      case 'ArrowUp': e.preventDefault(); setActiveIndex(i => nextEnabled(options, i, -1)); break
      case 'Home': e.preventDefault(); setActiveIndex(nextEnabled(options, -1, 1)); break
      case 'End': e.preventDefault(); setActiveIndex(nextEnabled(options, 0, -1)); break
      case 'Enter': case ' ': e.preventDefault(); commit(activeIndex); break
      case 'Tab': setOpen(false); break
      default:
        if (e.key.length === 1 && !e.metaKey && !e.ctrlKey && !e.altKey) {
          const now = Date.now()
          const t = typeahead.current
          t.str = now - t.at > 600 ? e.key : t.str + e.key
          t.at = now
          const q = t.str.toLowerCase()
          const match = options.findIndex(o => !o.disabled && String(o.label).toLowerCase().startsWith(q))
          if (match >= 0) setActiveIndex(match)
        }
    }
  }

  return (
    <div ref={rootRef} className={wrapperClassName}>
      <button
        type="button"
        ref={btnRef}
        id={id}
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-activedescendant={open && activeIndex >= 0 ? `${listId}-opt-${activeIndex}` : undefined}
        aria-label={ariaLabel}
        onClick={() => (open ? setOpen(false) : openMenu())}
        onKeyDown={onKeyDown}
        className={`flex items-center justify-between gap-2 text-left ${className}`}
      >
        <span className={`truncate ${selected ? '' : 'opacity-60'}`}>{selected ? selected.label : placeholder}</span>
        <svg
          width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"
          className={`shrink-0 opacity-70 transition-transform duration-150 ${open ? 'rotate-180' : ''}`}
        >
          <path d="M6 9l6 6 6-6" />
        </svg>
      </button>
      {open && (
        <ul
          ref={listRef}
          role="listbox"
          id={listId}
          aria-label={ariaLabel}
          className={`absolute left-0 z-50 max-h-60 w-max min-w-full max-w-[min(22rem,90vw)] overflow-y-auto rounded-xl border border-brand-border bg-white p-1 shadow-lg dark:border-brand-dark-border dark:bg-brand-dark-surface ${openUp ? 'bottom-full mb-1' : 'top-full mt-1'} ${menuClassName}`}
        >
          {options.map((opt, i) => (
            <li
              key={`${opt.value}-${i}`}
              id={`${listId}-opt-${i}`}
              role="option"
              aria-selected={opt.value === value}
              data-index={i}
              onMouseEnter={() => !opt.disabled && setActiveIndex(i)}
              onMouseDown={e => e.preventDefault()}
              onClick={() => commit(i)}
              className={`flex items-center justify-between gap-3 rounded-md px-3 py-1.5 text-sm ${
                opt.disabled
                  ? 'cursor-not-allowed opacity-40'
                  : i === activeIndex
                    ? 'cursor-pointer bg-brand-blue text-white'
                    : 'cursor-pointer text-brand-navy dark:text-brand-dark-navy'
              }`}
            >
              <span className="truncate">{opt.label}</span>
              {opt.value === value && (
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="shrink-0">
                  <path d="M20 6L9 17l-5-5" />
                </svg>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
