import { Component } from 'react'

/**
 * Keep one broken panel from taking the whole dashboard down with it.
 *
 * `main.jsx` renders <App /> straight into the root and nothing else in the SPA
 * implements `getDerivedStateFromError`, so any render throw inside a tab used to
 * unmount the entire tree — header, nav and all — leaving a blank page that only a
 * hard reload recovered. This catches the throw, keeps the rest of the shell
 * mounted, and offers an in-place retry.
 */
export default class PanelErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { failed: false }
    this.retry = () => this.setState({ failed: false })
  }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  componentDidCatch(error) {
    // Message only, and locally: panel props carry customer usage and billing
    // figures, so this must not become a new egress path for them.
    console.error('dashboard panel failed to render:', error?.message || 'unknown error')
  }

  render() {
    if (!this.state.failed) return this.props.children
    return (
      <div role="alert" className="rounded-2xl border border-red-200 dark:border-red-900/40 p-6">
        <p className="font-serif text-2xl text-brand-navy dark:text-brand-dark-navy mb-2">This section could not be displayed.</p>
        <p className="annotation">// the rest of the dashboard is unaffected</p>
        <button
          type="button"
          onClick={this.retry}
          className="annotation mt-4 hover:text-brand-blue"
        >
          reload this section
        </button>
      </div>
    )
  }
}
