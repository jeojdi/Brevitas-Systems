
function WelcomePage() {
  const params = new URLSearchParams(window.location.search);
  const email  = params.get('email') || '';
  const name   = email ? email.split('@')[0].replace(/[._\-+]/g, ' ').replace(/\b\w/g, c => c.toUpperCase()).split(' ')[0] : '';

  return (
    <div className="wrap">
      <a href="/" className="logo">
        <span className="logo-name">Brevitas</span>
        <span className="logo-tag">Systems</span>
      </a>

      <div className="card">
        <div className="beta-pill">EARLY ACCESS · BETA</div>

        <h1 className="heading">
          {name ? `Hello, ${name}.` : 'Welcome aboard.'}
        </h1>

        <p className="body">
          Thanks for signing up. Check your email to confirm your address, then sign in and you're good to go.
        </p>

        <p className="body" style={{ marginBottom: 0 }}>
          Brevitas is in private beta right now — a small group of teams running real agent pipelines in production. You're getting in early, which means you'll see things that are still rough. We want to hear about them.
        </p>

        <hr className="divider" />

        <div className="detail-row">
          <span className="detail-label">STATUS</span>
          <span className="detail-value">Private beta — active development</span>
        </div>
        <div className="detail-row">
          <span className="detail-label">CONTACT</span>
          <span className="detail-value"><a href="mailto:info@brevitassystems.com">info@brevitassystems.com</a></span>
        </div>
        <div className="detail-row">
          <span className="detail-label">BUGS</span>
          <span className="detail-value">Email us directly — we read everything</span>
        </div>
        {email && (
          <div className="detail-row">
            <span className="detail-label">SIGNED UP</span>
            <span className="detail-value">{email}</span>
          </div>
        )}

        <div className="cta-row">
          <a href="/login" className="btn-primary">Sign in →</a>
          <a href="/" className="btn-ghost">Back to homepage</a>
        </div>
      </div>

      <a href="/" className="back-link">← brevitassystems.com</a>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('app')).render(<WelcomePage />);
