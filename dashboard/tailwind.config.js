export default {
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{jsx,js}'],
  theme: {
    fontFamily: {
      serif: ['Cormorant Garamond', 'Georgia', 'serif'],
      sans:  ['Inter', 'system-ui', 'sans-serif'],
      mono:  ['JetBrains Mono', 'Menlo', 'monospace'],
    },
    extend: {
      colors: {
        brand: {
          // ── light mode ──────────────────────────────
          bg:               '#eef0f7',
          surface:          '#ffffff',
          navy:             '#0d1530',
          'navy-mid':       '#2d3561',
          blue:             '#4f5fc4',
          'blue-dim':       '#eceeff',
          teal:             '#2d8a6e',
          'teal-dim':       '#e4f4ef',
          muted:            '#8b93b8',
          'muted-dim':      '#c4c8e2',
          border:           '#e2e4f0',
          'border-mid':     '#ccd0e8',

          // ── dark mode — pure near-black editorial ──
          'dark-bg':          '#0c0c0c',   // page background
          'dark-surface':     '#141414',   // card / nav background
          'dark-elevated':    '#1c1c1c',   // hover / input / elevated elements
          'dark-navy':        '#eeeeee',   // primary text
          'dark-navy-mid':    '#aaaaaa',   // secondary text
          'dark-blue-dim':    '#181f44',   // blue selection tint
          'dark-teal':        '#3ecba8',   // vibrant teal accent
          'dark-teal-dim':    '#0d2820',   // teal bg tint
          'dark-muted':       '#5a5a5a',   // annotation / label text
          'dark-muted-dim':   '#242424',   // very dim bg
          'dark-border':      '#1e1e1e',   // default border
          'dark-border-mid':  '#2a2a2a',   // slightly more visible border
        },
      },
      letterSpacing: {
        widest: '0.2em',
      },
    },
  },
  plugins: [],
}
