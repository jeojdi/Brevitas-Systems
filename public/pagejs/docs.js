(function(){
const BASE_URL = "https://brevitassystems.com";
const TOC = [
  { group: "GETTING STARTED", items: [
    { id: "overview", label: "How it works" },
    { id: "verifying-savings", label: "Verifying savings" },
    { id: "requirements", label: "Requirements" }
  ] },
  { group: "INSTALL", items: [
    { id: "install", label: "Install" },
    { id: "setup", label: "First-time setup" }
  ] },
  { group: "MANAGE", items: [
    { id: "verify", label: "Verify it works" },
    { id: "service", label: "Background service" },
    { id: "update", label: "Updating" },
    { id: "uninstall", label: "Uninstalling" }
  ] },
  { group: "REFERENCE", items: [
    { id: "commands", label: "Command reference" },
    { id: "troubleshooting", label: "Troubleshooting" }
  ] }
];
function DocsPage() {
  const [active, setActive] = useState("overview");
  useEffect(() => {
    const obs = new IntersectionObserver((entries) => {
      const visible = entries.filter((e) => e.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
      if (visible[0]) setActive(visible[0].target.id);
    }, { rootMargin: "-20% 0px -60% 0px" });
    document.querySelectorAll("[data-doc-section]").forEach((el) => obs.observe(el));
    return () => obs.disconnect();
  }, []);
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(Nav, { current: "docs" }), /* @__PURE__ */ React.createElement("div", { className: "docs-page", style: { paddingTop: 80 } }, /* @__PURE__ */ React.createElement("div", { className: "container docs-shell" }, /* @__PURE__ */ React.createElement("aside", { className: "docs-side" }, /* @__PURE__ */ React.createElement("div", { className: "docs-side-inner" }, /* @__PURE__ */ React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 10, marginBottom: 28 } }, /* @__PURE__ */ React.createElement("span", { className: "t-mono", style: { color: "var(--bronze)", fontSize: 11 } }, "INSTALL GUIDE"), /* @__PURE__ */ React.createElement("span", { className: "t-mono", style: { color: "var(--stone)", fontSize: 11 } }, "bvx")), TOC.map((group) => /* @__PURE__ */ React.createElement("div", { key: group.group, style: { marginBottom: 28 } }, /* @__PURE__ */ React.createElement("div", { className: "t-mono", style: { color: "var(--stone)", fontSize: 10, marginBottom: 10 } }, group.group), /* @__PURE__ */ React.createElement("ul", { style: { listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 4 } }, group.items.map((item) => /* @__PURE__ */ React.createElement("li", { key: item.id }, /* @__PURE__ */ React.createElement(
    "a",
    {
      href: `#${item.id}`,
      className: `docs-toc-link ${active === item.id ? "active" : ""}`
    },
    item.label
  )))))), /* @__PURE__ */ React.createElement("div", { style: { marginTop: 40, padding: 16, border: "1px solid var(--line)", background: "var(--ink-2)", borderRadius: 2 } }, /* @__PURE__ */ React.createElement("div", { className: "t-mono", style: { color: "var(--bronze)", fontSize: 11, marginBottom: 8 } }, "DASHBOARD"), /* @__PURE__ */ React.createElement("div", { className: "t-body", style: { fontSize: 13, margin: 0 } }, "Manage keys, run live requests, and view token savings in the dashboard."), /* @__PURE__ */ React.createElement("a", { href: "/dashboard", className: "btn-inline", style: { marginTop: 12, display: "inline-block", fontSize: 13, color: "var(--bronze)" } }, "Open dashboard \u2192")))), /* @__PURE__ */ React.createElement("main", { className: "docs-main" }, /* @__PURE__ */ React.createElement(Section, { id: "overview", kicker: "GETTING STARTED \xB7 01", title: /* @__PURE__ */ React.createElement(React.Fragment, null, "How it works.") }, /* @__PURE__ */ React.createElement("p", { className: "t-body-lg" }, "Brevitas is middleware that sits between your AI coding assistants and the LLM provider, trimming tokens on every request. The ", /* @__PURE__ */ React.createElement("span", { className: "mono bronze" }, "bvx"), " CLI installs it, points each supported tool at a local proxy, and supervises the background service."), /* @__PURE__ */ React.createElement("div", { style: { background: "var(--ink-2)", border: "1px solid var(--line)", borderRadius: 2, padding: "20px 24px", fontFamily: "JetBrains Mono, monospace", fontSize: 12, color: "var(--stone-2)", lineHeight: 1.7 } }, /* @__PURE__ */ React.createElement("div", { style: { color: "var(--stone)", marginBottom: 8 } }, "// request path"), /* @__PURE__ */ React.createElement("pre", { style: { margin: 0, whiteSpace: "pre", overflowX: "auto" } }, `AI Tool  \u2500\u25B6  Brevitas Local Proxy  \u2500\u25B6  brevitas-systems  \u2500\u25B6  LLM Provider  \u2500\u25B6  Response
             (127.0.0.1:8080)          (optimization,
                                         local socket)`)), /* @__PURE__ */ React.createElement("p", { className: "t-body" }, "There are three moving parts:"), /* @__PURE__ */ React.createElement("table", { className: "docs-table" }, /* @__PURE__ */ React.createElement("thead", null, /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("th", null, "Piece"), /* @__PURE__ */ React.createElement("th", null, "What it is"), /* @__PURE__ */ React.createElement("th", null, "Who manages it"))), /* @__PURE__ */ React.createElement("tbody", null, /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("td", null, /* @__PURE__ */ React.createElement("span", { className: "mono bronze" }, "bvx")), /* @__PURE__ */ React.createElement("td", null, "The installer/manager CLI (written in Go). Detects your AI tools, stores one API key, points each tool at the local proxy, and runs the background service."), /* @__PURE__ */ React.createElement("td", null, "You \u2014 ", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "brew"), " / ", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "install.ps1"))), /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("td", null, /* @__PURE__ */ React.createElement("span", { className: "mono" }, "Proxy service")), /* @__PURE__ */ React.createElement("td", null, "A local HTTP proxy on ", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "127.0.0.1:8080"), " that every configured tool routes through. Runs in the background (", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "bvx serve"), ")."), /* @__PURE__ */ React.createElement("td", null, /* @__PURE__ */ React.createElement("span", { className: "mono" }, "bvx"), " \u2014 installs + supervises it")), /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("td", null, /* @__PURE__ */ React.createElement("span", { className: "mono" }, "brevitas-systems")), /* @__PURE__ */ React.createElement("td", null, "The Python package holding the optimization logic. ", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "bvx"), " talks to it over a local socket. Not bundled \u2014 installed and pinned via ", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "pip"), "."), /* @__PURE__ */ React.createElement("td", null, /* @__PURE__ */ React.createElement("span", { className: "mono" }, "bvx install"), " / ", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "update"))))), /* @__PURE__ */ React.createElement("p", { className: "t-body" }, /* @__PURE__ */ React.createElement("span", { className: "mono bronze" }, "bvx"), " never bundles the optimizer and never edits a tool config you haven't approved. Every config change is backed up before it's rewritten.")), /* @__PURE__ */ React.createElement(Section, { id: "verifying-savings", kicker: "GETTING STARTED \xB7 02", title: /* @__PURE__ */ React.createElement(React.Fragment, null, "Verifying savings.") }, /* @__PURE__ */ React.createElement("p", { className: "t-body-lg" }, "Brevitas calculates savings from what the provider ", /* @__PURE__ */ React.createElement("strong", { style: { color: "var(--fg)" } }, "actually billed"), ", not what the caller requested \u2014 the provider's own response and pricing data are the source of truth."), /* @__PURE__ */ React.createElement("ul", { style: { margin: 0, paddingLeft: 18, display: "flex", flexDirection: "column", gap: 12 } }, /* @__PURE__ */ React.createElement("li", { className: "t-body" }, /* @__PURE__ */ React.createElement("strong", { style: { color: "var(--fg)" } }, "Model."), " The model name is taken from the provider's ", /* @__PURE__ */ React.createElement("em", null, "response"), ", not the request, so pricing uses the actual model and version that ran."), /* @__PURE__ */ React.createElement("li", { className: "t-body" }, /* @__PURE__ */ React.createElement("strong", { style: { color: "var(--fg)" } }, "Cost."), " Each model has separate rates for fresh input, cached input, cache writes, and output. Cost = tokens \xD7 the appropriate rate for each bucket."), /* @__PURE__ */ React.createElement("li", { className: "t-body" }, /* @__PURE__ */ React.createElement("strong", { style: { color: "var(--fg)" } }, "Savings."), " Brevitas doesn't need to reduce token count. If it causes tokens to be served from a cheaper cache bucket, the difference between the all-fresh baseline and the actual provider cost is the saving."), /* @__PURE__ */ React.createElement("li", { className: "t-body" }, /* @__PURE__ */ React.createElement("strong", { style: { color: "var(--fg)" } }, "No double-counting."), " If the provider's cache would have hit without Brevitas, that discount is excluded. Brevitas is only credited for savings it actually caused."), /* @__PURE__ */ React.createElement("li", { className: "t-body" }, /* @__PURE__ */ React.createElement("strong", { style: { color: "var(--fg)" } }, "No fake savings."), " If Brevitas changes nothing, baseline = actual cost \u2192 ", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "$0"), " savings \u2192 ", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "$0"), " fee."), /* @__PURE__ */ React.createElement("li", { className: "t-body" }, /* @__PURE__ */ React.createElement("strong", { style: { color: "var(--fg)" } }, "Safety."), " If the real model can't be identified or the cache hit can't be attributed to Brevitas, the row is recorded ", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "unpriced / $0"), " rather than guessed.")), /* @__PURE__ */ React.createElement("div", { className: "t-mono", style: { color: "var(--stone)", fontSize: 11, marginTop: 8 } }, "// worked example \u2014 100k-token Opus prompt, 90k served from cache"), /* @__PURE__ */ React.createElement("table", { className: "docs-table" }, /* @__PURE__ */ React.createElement("thead", null, /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("th", null, "Leg"), /* @__PURE__ */ React.createElement("th", null, "Tokens"), /* @__PURE__ */ React.createElement("th", null, "Rate (per 1M)"), /* @__PURE__ */ React.createElement("th", null, "Cost"))), /* @__PURE__ */ React.createElement("tbody", null, /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("td", null, "Baseline ", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "(all fresh)")), /* @__PURE__ */ React.createElement("td", null, "100k input"), /* @__PURE__ */ React.createElement("td", null, /* @__PURE__ */ React.createElement("span", { className: "mono" }, "$5.00")), /* @__PURE__ */ React.createElement("td", null, /* @__PURE__ */ React.createElement("span", { className: "mono" }, "$0.500"))), /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("td", null, "Actual ", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "(90k cache-read + 10k fresh)")), /* @__PURE__ */ React.createElement("td", null, "90k @ cached \xB7 10k @ fresh"), /* @__PURE__ */ React.createElement("td", null, /* @__PURE__ */ React.createElement("span", { className: "mono" }, "$0.50 / $5.00")), /* @__PURE__ */ React.createElement("td", null, /* @__PURE__ */ React.createElement("span", { className: "mono" }, "$0.095"))), /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("td", null, /* @__PURE__ */ React.createElement("strong", { style: { color: "var(--fg)" } }, "Verified savings")), /* @__PURE__ */ React.createElement("td", null, "\u2014"), /* @__PURE__ */ React.createElement("td", null, "\u2014"), /* @__PURE__ */ React.createElement("td", null, /* @__PURE__ */ React.createElement("span", { className: "mono bronze" }, "$0.405"))), /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("td", null, "Brevitas fee ", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "(20%)")), /* @__PURE__ */ React.createElement("td", null, "\u2014"), /* @__PURE__ */ React.createElement("td", null, "\u2014"), /* @__PURE__ */ React.createElement("td", null, /* @__PURE__ */ React.createElement("span", { className: "mono" }, "~$0.081"))))), /* @__PURE__ */ React.createElement("div", { style: { background: "var(--ink-2)", border: "1px solid var(--line)", borderLeft: "2px solid var(--bronze)", borderRadius: 2, padding: "16px 20px", marginTop: 4 } }, /* @__PURE__ */ React.createElement("p", { className: "t-body", style: { margin: 0 } }, /* @__PURE__ */ React.createElement("strong", { style: { color: "var(--fg)" } }, "In one sentence."), " Brevitas charges a percentage of verified, attributable savings between the provider's full-price baseline and its actual billed cost \u2014 using the provider's own response and pricing data as the source of truth."))), /* @__PURE__ */ React.createElement(Section, { id: "requirements", kicker: "GETTING STARTED \xB7 03", title: /* @__PURE__ */ React.createElement(React.Fragment, null, "Requirements.") }, /* @__PURE__ */ React.createElement("ul", { style: { margin: 0, paddingLeft: 18, display: "flex", flexDirection: "column", gap: 12 } }, /* @__PURE__ */ React.createElement("li", { className: "t-body" }, /* @__PURE__ */ React.createElement("strong", { style: { color: "var(--fg)" } }, "macOS, Linux, or Windows"), " (x86-64 or ARM64)."), /* @__PURE__ */ React.createElement("li", { className: "t-body" }, /* @__PURE__ */ React.createElement("strong", { style: { color: "var(--fg)" } }, "Python 3.13+"), " \u2014 required by ", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "brevitas-systems"), ". Homebrew installs it as a dependency automatically; on Windows install it yourself (e.g. from ", /* @__PURE__ */ React.createElement("a", { href: "https://www.python.org/downloads/", className: "btn-inline", style: { color: "var(--bronze)" } }, "python.org"), " or ", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "winget install Python.Python.3.13"), ")."), /* @__PURE__ */ React.createElement("li", { className: "t-body" }, "An account at ", /* @__PURE__ */ React.createElement("a", { href: "https://brevitassystems.com", className: "btn-inline", style: { color: "var(--bronze)" } }, "brevitassystems.com"), " \u2014 you authorize it during setup and the device key is stored in your OS credential store (Keychain / Credential Manager / Secret Service).")), /* @__PURE__ */ React.createElement("p", { className: "t-body" }, "You do ", /* @__PURE__ */ React.createElement("strong", { style: { color: "var(--fg)" } }, "not"), " need a Go toolchain or a C compiler \u2014 every install path below ships a prebuilt binary.")), /* @__PURE__ */ React.createElement(Section, { id: "install", kicker: "INSTALL \xB7 01", title: /* @__PURE__ */ React.createElement(React.Fragment, null, "Install.") }, /* @__PURE__ */ React.createElement("p", { className: "t-body-lg" }, "Two ways in. The ", /* @__PURE__ */ React.createElement("strong", { style: { color: "var(--fg)" } }, "hosted gateway"), " is one command and is the path whose savings are verified and billable. The ", /* @__PURE__ */ React.createElement("span", { className: "mono bronze" }, "bvx"), " installer wires your local AI tools and codebases through Brevitas with no code changes \u2014 analytics, never billed."), /* @__PURE__ */ React.createElement("div", { className: "t-mono", style: { color: "var(--bronze)", fontSize: 11 } }, "// RECOMMENDED \xB7 METERED \xB7 BILLABLE \u2014 hosted gateway"), /* @__PURE__ */ React.createElement("p", { className: "t-body" }, "Install the CLI, then approve once in the browser. ", /* @__PURE__ */ React.createElement("span", { className: "mono bronze" }, "brevitas connect"), " opens the dashboard for approval, then hands back an organization service key scoped to your workspace \u2014 nothing to install on your servers and no background service. The only change in your app is the client base URL."), /* @__PURE__ */ React.createElement(CodeBlock, { lang: "sh", code: `pip install brevitas-systems     # installs the brevitas CLI
brevitas connect                 # approve in the browser, receive a scoped org key` }), /* @__PURE__ */ React.createElement("figure", { style: { margin: "4px 0 0", border: "1px solid var(--line)", borderRadius: 2, overflow: "hidden", background: "var(--ink-2)" } }, /* @__PURE__ */ React.createElement("img", { src: "/assets/docs/connect-dashboard.png", alt: "The Connect tab of the Brevitas dashboard showing the one-command quick start: pip install brevitas-systems, then brevitas connect.", style: { display: "block", width: "100%", height: "auto" } }), /* @__PURE__ */ React.createElement("figcaption", { className: "t-mono", style: { color: "var(--stone)", fontSize: 11, padding: "8px 12px", borderTop: "1px solid var(--line)" } }, "// brevitas connect opens this dashboard for approval, then hands back a scoped key")), /* @__PURE__ */ React.createElement("p", { className: "t-body" }, "Then point your client at the gateway \u2014 two headers, no other code changes:"), /* @__PURE__ */ React.createElement(CodeBlock, { lang: "python", code: `import os
from openai import OpenAI

client = OpenAI(
    base_url="https://api.brevitassystems.com/v1",
    api_key=os.environ["OPENAI_API_KEY"],
    default_headers={
        "X-Brevitas-Key": os.environ["BREVITAS_API_KEY"],
        "X-Brevitas-Customer-ID": "acme",   # required on every hosted request
    },
)` }), /* @__PURE__ */ React.createElement("div", { className: "t-mono", style: { color: "var(--stone)", fontSize: 11, marginTop: 20 } }, "// LOCAL TOOLING \u2014 bvx \xB7 macOS / Linux (Homebrew)"), /* @__PURE__ */ React.createElement(CodeBlock, { lang: "sh", code: `brew tap Brevitas-ai/brevitas
brew install bvx` }), /* @__PURE__ */ React.createElement("p", { className: "t-body" }, "Or as a single command:"), /* @__PURE__ */ React.createElement(CodeBlock, { lang: "sh", code: `brew install Brevitas-ai/brevitas/bvx` }), /* @__PURE__ */ React.createElement("p", { className: "t-body" }, "To build the latest ", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "main"), " from source instead of a release binary:"), /* @__PURE__ */ React.createElement(CodeBlock, { lang: "sh", code: `brew install --HEAD Brevitas-ai/brevitas/bvx` }), /* @__PURE__ */ React.createElement("div", { className: "t-mono", style: { color: "var(--stone)", fontSize: 11, marginTop: 12 } }, "// Windows (PowerShell)"), /* @__PURE__ */ React.createElement(CodeBlock, { lang: "powershell", code: `irm https://raw.githubusercontent.com/Brevitas-ai/brevitas/main/install.ps1 | iex` }), /* @__PURE__ */ React.createElement("p", { className: "t-body" }, "No GitHub account or API token is required; the installer resolves the latest release without GitHub's rate-limited REST API."), /* @__PURE__ */ React.createElement("p", { className: "t-body" }, "This downloads the prebuilt ", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "bvx.exe"), " for your architecture,", /* @__PURE__ */ React.createElement("strong", { style: { color: "var(--fg)" } }, " verifies its SHA-256"), " against the release", /* @__PURE__ */ React.createElement("span", { className: "mono" }, " checksums.txt"), ", installs it to ", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "%LOCALAPPDATA%\\Programs\\bvx"), ", and adds that folder to your user PATH. Open a ", /* @__PURE__ */ React.createElement("strong", { style: { color: "var(--fg)" } }, "new"), " terminal afterward so the updated PATH takes effect."), /* @__PURE__ */ React.createElement("p", { className: "t-body" }, "To pin a specific version, set ", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "$env:BVX_VERSION"), " before running:"), /* @__PURE__ */ React.createElement(CodeBlock, { lang: "powershell", code: `$env:BVX_VERSION = "0.1.22"
irm https://raw.githubusercontent.com/Brevitas-ai/brevitas/main/install.ps1 | iex` }), /* @__PURE__ */ React.createElement("div", { className: "t-mono", style: { color: "var(--stone)", fontSize: 11, marginTop: 12 } }, "// verify the binary is installed"), /* @__PURE__ */ React.createElement(CodeBlock, { lang: "sh", code: `bvx version` }), /* @__PURE__ */ React.createElement("p", { className: "t-body" }, "This only confirms the CLI is on your PATH \u2014 it does ", /* @__PURE__ */ React.createElement("strong", { style: { color: "var(--fg)" } }, "not"), " configure anything yet. That's the next step.")), /* @__PURE__ */ React.createElement(Section, { id: "setup", kicker: "INSTALL \xB7 02", title: /* @__PURE__ */ React.createElement(React.Fragment, null, "First-time setup.") }, /* @__PURE__ */ React.createElement("p", { className: "t-body" }, "Run the interactive installer once:"), /* @__PURE__ */ React.createElement(CodeBlock, { lang: "sh", code: `bvx install` }), /* @__PURE__ */ React.createElement("figure", { style: { margin: "4px 0 0", border: "1px solid var(--line)", borderRadius: 2, overflow: "hidden", background: "#0a0a0a" } }, /* @__PURE__ */ React.createElement("img", { src: "/assets/docs/bvx-home.png", alt: "The bvx terminal UI home screen: an Actions menu with Connect repository, Configure AI tools, System status, and more.", style: { display: "block", width: "100%", height: "auto" } }), /* @__PURE__ */ React.createElement("figcaption", { className: "t-mono", style: { color: "var(--stone)", fontSize: 11, padding: "8px 12px", borderTop: "1px solid var(--line)" } }, "// bvx opens an interactive menu \u2014 arrow keys to navigate, Enter to launch")), /* @__PURE__ */ React.createElement("p", { className: "t-body" }, "This is the same as ", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "bvx install ai"), ". Here's exactly what it does:"), /* @__PURE__ */ React.createElement("ol", { style: { margin: 0, paddingLeft: 18, display: "flex", flexDirection: "column", gap: 10 } }, /* @__PURE__ */ React.createElement("li", { className: "t-body" }, /* @__PURE__ */ React.createElement("strong", { style: { color: "var(--fg)" } }, "Scans"), " your system for supported AI tools (Claude Code, Codex CLI, Continue, Aider, \u2026)."), /* @__PURE__ */ React.createElement("li", { className: "t-body" }, /* @__PURE__ */ React.createElement("strong", { style: { color: "var(--fg)" } }, "Opens the Brevitas dashboard"), " for one-click account authorization."), /* @__PURE__ */ React.createElement("li", { className: "t-body" }, /* @__PURE__ */ React.createElement("strong", { style: { color: "var(--fg)" } }, "Stores"), " the dedicated device key in your OS credential store."), /* @__PURE__ */ React.createElement("li", { className: "t-body" }, /* @__PURE__ */ React.createElement("strong", { style: { color: "var(--fg)" } }, "Rewrites"), " each supported tool's documented config to route through ", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "http://127.0.0.1:8080"), " (backing up the original first)."), /* @__PURE__ */ React.createElement("li", { className: "t-body" }, /* @__PURE__ */ React.createElement("strong", { style: { color: "var(--fg)" } }, "Installs and starts"), " the background services (proxy + ", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "brevitas-systems"), " optimizer)."), /* @__PURE__ */ React.createElement("li", { className: "t-body" }, /* @__PURE__ */ React.createElement("strong", { style: { color: "var(--fg)" } }, "Runs diagnostics"), " and prints a summary.")), /* @__PURE__ */ React.createElement("div", { className: "t-mono", style: { color: "var(--stone)", fontSize: 11, marginTop: 8 } }, "// example output"), /* @__PURE__ */ React.createElement(CodeBlock, { lang: "text", code: `Scanning system...

  \u2713 Claude Code
  \u2713 Codex CLI
  \u2713 Continue
  \u2713 Aider
  \u26A0 Cursor (manual step required)
  \u26A0 GitHub Copilot \u2014 Unsupported

Detected 4 configurable tool(s), 1 manual, 1 unsupported.

Opening https://brevitassystems.com/dashboard#bvx=...
Waiting for approval... approved

Installing...

  \u2713 API key stored in macOS Keychain
  \u2713 Claude Code configured` }), /* @__PURE__ */ React.createElement("p", { className: "t-body" }, /* @__PURE__ */ React.createElement("strong", { style: { color: "var(--fg)" } }, "Wiring up a codebase instead."), " To route every LLM call in a project through Brevitas (instead of configuring interactive tools):"), /* @__PURE__ */ React.createElement(CodeBlock, { lang: "sh", code: `bvx install <repo>                 # scan + open the AI-call map
bvx install <repo> --apply         # write a .env.agentmap you can source
bvx install <repo> --apply --auto  # also rewrite hardcoded provider URLs` }), /* @__PURE__ */ React.createElement("p", { className: "t-body" }, "Choosing ", /* @__PURE__ */ React.createElement("strong", { style: { color: "var(--fg)" } }, "Connect repository"), " (or running ", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "bvx install"), " with no path) opens a picker so you can browse to the backend project folder to connect:"), /* @__PURE__ */ React.createElement("figure", { style: { margin: "4px 0 0", border: "1px solid var(--line)", borderRadius: 2, overflow: "hidden", background: "#0a0a0a" } }, /* @__PURE__ */ React.createElement("img", { src: "/assets/docs/bvx-repo-picker.png", alt: "The bvx repository picker: a file browser listing Documents, Downloads, Desktop, and GitHub folders with a preview pane.", style: { display: "block", width: "100%", height: "auto" } }), /* @__PURE__ */ React.createElement("figcaption", { className: "t-mono", style: { color: "var(--stone)", fontSize: 11, padding: "8px 12px", borderTop: "1px solid var(--line)" } }, "// bvx repository picker \u2014 pick the project folder, Enter to open, then scan + connect"))), /* @__PURE__ */ React.createElement(Section, { id: "verify", kicker: "MANAGE \xB7 01", title: /* @__PURE__ */ React.createElement(React.Fragment, null, "Verify it works.") }, /* @__PURE__ */ React.createElement(CodeBlock, { lang: "sh", code: `bvx status     # proxy, service, and provider status
bvx doctor     # full diagnostics across the installation` }), /* @__PURE__ */ React.createElement("p", { className: "t-body" }, "If something looks off, re-apply config and restart the service:"), /* @__PURE__ */ React.createElement(CodeBlock, { lang: "sh", code: `bvx repair` })), /* @__PURE__ */ React.createElement(Section, { id: "service", kicker: "MANAGE \xB7 02", title: /* @__PURE__ */ React.createElement(React.Fragment, null, "Background service.") }, /* @__PURE__ */ React.createElement("p", { className: "t-body" }, "Control the background proxy service directly:"), /* @__PURE__ */ React.createElement(CodeBlock, { lang: "sh", code: `bvx start      # start the proxy service
bvx stop       # stop it
bvx restart    # restart it
bvx logs       # print the proxy logs
bvx logs -f    # follow the logs live` })), /* @__PURE__ */ React.createElement(Section, { id: "update", kicker: "MANAGE \xB7 03", title: /* @__PURE__ */ React.createElement(React.Fragment, null, "Updating.") }, /* @__PURE__ */ React.createElement("p", { className: "t-body" }, "Upgrade the ", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "bvx"), " CLI itself with your package manager:"), /* @__PURE__ */ React.createElement(CodeBlock, { lang: "sh", code: `# macOS / Linux
brew upgrade bvx

# Windows \u2014 just re-run the installer; it fetches the latest release
irm https://raw.githubusercontent.com/Brevitas-ai/brevitas/main/install.ps1 | iex` }), /* @__PURE__ */ React.createElement("p", { className: "t-body" }, "Upgrade the optimization engine (", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "brevitas-systems"), "):"), /* @__PURE__ */ React.createElement(CodeBlock, { lang: "sh", code: `bvx update` })), /* @__PURE__ */ React.createElement(Section, { id: "uninstall", kicker: "MANAGE \xB7 04", title: /* @__PURE__ */ React.createElement(React.Fragment, null, "Uninstalling.") }, /* @__PURE__ */ React.createElement("p", { className: "t-body" }, "This restores every tool config from its backup and removes the background service:"), /* @__PURE__ */ React.createElement(CodeBlock, { lang: "sh", code: `bvx uninstall` }), /* @__PURE__ */ React.createElement("p", { className: "t-body" }, "Then remove the CLI itself:"), /* @__PURE__ */ React.createElement(CodeBlock, { lang: "sh", code: `# macOS / Linux
brew uninstall bvx

# Windows
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\\Programs\\bvx"
# and remove that folder from your user PATH (System Settings \u2192 Environment Variables)` })), /* @__PURE__ */ React.createElement(Section, { id: "commands", kicker: "REFERENCE \xB7 01", title: /* @__PURE__ */ React.createElement(React.Fragment, null, "Command reference.") }, /* @__PURE__ */ React.createElement("table", { className: "docs-table" }, /* @__PURE__ */ React.createElement("thead", null, /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("th", null, "Command"), /* @__PURE__ */ React.createElement("th", null, "Description"))), /* @__PURE__ */ React.createElement("tbody", null, /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("td", null, /* @__PURE__ */ React.createElement("span", { className: "mono" }, "bvx install")), /* @__PURE__ */ React.createElement("td", null, "Configure AI coding tools (", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "install ai"), ") or a codebase (", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "install <repo>"), ")")), /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("td", null, /* @__PURE__ */ React.createElement("span", { className: "mono" }, "bvx uninstall")), /* @__PURE__ */ React.createElement("td", null, "Restore all tool configs and remove the background service")), /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("td", null, /* @__PURE__ */ React.createElement("span", { className: "mono" }, "bvx status")), /* @__PURE__ */ React.createElement("td", null, "Show proxy, service, and provider status")), /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("td", null, /* @__PURE__ */ React.createElement("span", { className: "mono" }, "bvx stats")), /* @__PURE__ */ React.createElement("td", null, "Show cumulative token-savings metrics from the proxy")), /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("td", null, /* @__PURE__ */ React.createElement("span", { className: "mono" }, "bvx providers")), /* @__PURE__ */ React.createElement("td", null, "List supported providers and their detection/config state")), /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("td", null, /* @__PURE__ */ React.createElement("span", { className: "mono" }, "bvx doctor")), /* @__PURE__ */ React.createElement("td", null, "Run diagnostics across the whole installation")), /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("td", null, /* @__PURE__ */ React.createElement("span", { className: "mono" }, "bvx repair")), /* @__PURE__ */ React.createElement("td", null, "Re-apply configuration and restart the service")), /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("td", null, /* @__PURE__ */ React.createElement("span", { className: "mono" }, "bvx start"), " / ", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "stop"), " / ", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "restart")), /* @__PURE__ */ React.createElement("td", null, "Control the background proxy service")), /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("td", null, /* @__PURE__ */ React.createElement("span", { className: "mono" }, "bvx logs")), /* @__PURE__ */ React.createElement("td", null, "Print (or follow, with ", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "-f"), ") the proxy logs")), /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("td", null, /* @__PURE__ */ React.createElement("span", { className: "mono" }, "bvx config")), /* @__PURE__ */ React.createElement("td", null, "Print or edit Brevitas configuration")), /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("td", null, /* @__PURE__ */ React.createElement("span", { className: "mono" }, "bvx login"), " / ", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "logout")), /* @__PURE__ */ React.createElement("td", null, "Connect through the dashboard / remove the stored key")), /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("td", null, /* @__PURE__ */ React.createElement("span", { className: "mono" }, "bvx update")), /* @__PURE__ */ React.createElement("td", null, "Check for and upgrade the ", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "brevitas-systems"), " package")), /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("td", null, /* @__PURE__ */ React.createElement("span", { className: "mono" }, "bvx version")), /* @__PURE__ */ React.createElement("td", null, "Print version information")))), /* @__PURE__ */ React.createElement("p", { className: "t-body" }, "Run ", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "bvx help"), " to see the full list at any time.")), /* @__PURE__ */ React.createElement(Section, { id: "troubleshooting", kicker: "REFERENCE \xB7 02", title: /* @__PURE__ */ React.createElement(React.Fragment, null, "Troubleshooting.") }, /* @__PURE__ */ React.createElement("table", { className: "docs-table" }, /* @__PURE__ */ React.createElement("thead", null, /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("th", null, "Symptom"), /* @__PURE__ */ React.createElement("th", null, "Fix"))), /* @__PURE__ */ React.createElement("tbody", null, /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("td", null, /* @__PURE__ */ React.createElement("span", { className: "mono" }, "bvx: command not found"), " (Windows)"), /* @__PURE__ */ React.createElement("td", null, "Open a new terminal; PATH updates only apply to shells started after install.")), /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("td", null, "GitHub API rate limit (Windows)"), /* @__PURE__ */ React.createElement("td", null, "Download and run the current install command again. The current installer does not use GitHub's rate-limited REST API and does not require a GitHub token.")), /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("td", null, "A tool still hits the provider directly"), /* @__PURE__ */ React.createElement("td", null, "Run ", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "bvx status"), " to confirm it was configured, then ", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "bvx repair"), " to re-apply.")), /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("td", null, "Optimizer won't start"), /* @__PURE__ */ React.createElement("td", null, "Make sure Python 3.13+ is installed and on your PATH, then run ", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "bvx update"), " followed by ", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "bvx doctor"), ".")), /* @__PURE__ */ React.createElement("tr", null, /* @__PURE__ */ React.createElement("td", null, "Anything else"), /* @__PURE__ */ React.createElement("td", null, /* @__PURE__ */ React.createElement("span", { className: "mono" }, "bvx doctor"), " inspects the whole installation and points at the specific problem.")))), /* @__PURE__ */ React.createElement("p", { className: "t-body" }, "For how the proxy and optimizer communicate under the hood, see ", /* @__PURE__ */ React.createElement("span", { className: "mono" }, "PROTOCOL.md"), " in the repository.")), /* @__PURE__ */ React.createElement("div", { style: { borderTop: "1px solid var(--line)", marginTop: 80, paddingTop: 40, paddingBottom: 80 } }, /* @__PURE__ */ React.createElement("div", { className: "t-mono", style: { color: "var(--stone)", marginBottom: 16, fontSize: 11 } }, "WHAT'S NEXT"), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 32, flexWrap: "wrap" } }, /* @__PURE__ */ React.createElement("a", { href: "/benchmarks", className: "btn btn-ghost underline" }, "See the benchmarks \u2192"), /* @__PURE__ */ React.createElement("a", { href: "/product", className: "btn btn-ghost underline" }, "Explore the product \u2192"), /* @__PURE__ */ React.createElement("a", { href: "/login", className: "btn btn-primary" }, "Open dashboard")))))), /* @__PURE__ */ React.createElement(Footer, null), /* @__PURE__ */ React.createElement("style", null, `
        .docs-shell {
          display: grid;
          grid-template-columns: 260px 1fr;
          gap: 80px;
          max-width: 1280px;
          padding-top: 32px;
        }
        .docs-side {
          position: relative;
        }
        .docs-side-inner {
          position: sticky;
          top: 96px;
          padding-right: 16px;
        }
        .docs-toc-link {
          display: block;
          padding: 6px 10px;
          color: var(--stone-2);
          font-size: 14px;
          border-left: 1px solid transparent;
          text-decoration: none;
          transition: color 180ms, border-color 180ms;
        }
        .docs-toc-link:hover { color: var(--fg); }
        .docs-toc-link.active {
          color: var(--fg);
          border-left-color: var(--bronze);
        }
        .docs-main { max-width: 780px; padding-bottom: 80px; }
        .docs-table {
          width: 100%;
          border-collapse: collapse;
          margin: 20px 0;
          font-size: 14px;
        }
        .docs-table th, .docs-table td {
          text-align: left;
          padding: 12px 14px;
          border-bottom: 1px solid var(--line);
          vertical-align: top;
        }
        .docs-table th {
          font-family: 'JetBrains Mono', monospace;
          font-size: 11px;
          color: var(--stone);
          font-weight: 400;
          border-bottom-color: var(--stone-2);
        }
        .docs-table td { color: var(--bone-dim); }
        .endpoint-badge {
          display: inline-flex;
          align-items: center;
          gap: 10px;
          padding: 8px 14px;
          background: var(--ink-2);
          border: 1px solid var(--line);
          border-radius: 2px;
          margin-bottom: 16px;
        }
        .method-badge {
          font-family: 'JetBrains Mono', monospace;
          font-size: 10px;
          font-weight: 700;
          padding: 2px 6px;
          border-radius: 2px;
        }
        .method-POST { background: #3b4fd8; color: #fff; }
        .method-GET  { background: #1a8a6f; color: #fff; }
        .method-PUT  { background: #b45309; color: #fff; }
        .code-block-wrap {
          border-radius: 2px;
          overflow: hidden;
          margin: 12px 0;
        }
        .code-block-header {
          display: flex;
          justify-content: space-between;
          align-items: center;
          background: #0c0c0c;
          border-bottom: 1px solid #222;
          padding: 8px 16px;
        }
        .code-block-lang {
          font-family: 'JetBrains Mono', monospace;
          font-size: 10px;
          color: #555;
          text-transform: uppercase;
          letter-spacing: 0.1em;
        }
        .code-block-copy {
          font-family: 'JetBrains Mono', monospace;
          font-size: 10px;
          color: #555;
          background: none;
          border: none;
          cursor: pointer;
          transition: color 150ms;
        }
        .code-block-copy:hover { color: #aaa; }
        .code-block-pre {
          background: #0c0c0c;
          padding: 20px;
          margin: 0;
          font-family: 'JetBrains Mono', monospace;
          font-size: 12px;
          color: #ccc;
          overflow-x: auto;
          line-height: 1.7;
          white-space: pre;
        }
        @media (max-width: 900px) {
          .docs-shell { grid-template-columns: 1fr; gap: 32px; }
          .docs-side-inner { position: static; }
        }
      `));
}
function Section({ id, kicker, title, children }) {
  return /* @__PURE__ */ React.createElement("section", { id, "data-doc-section": true, style: { marginBottom: 80, scrollMarginTop: 100 } }, /* @__PURE__ */ React.createElement("div", { className: "t-mono", style: { color: "var(--bronze)", fontSize: 11, marginBottom: 14 } }, kicker), /* @__PURE__ */ React.createElement("h2", { className: "serif", style: { fontSize: "clamp(32px, 4vw, 44px)", fontWeight: 400, letterSpacing: "-0.015em", margin: "0 0 24px 0", lineHeight: 1.1 } }, title), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 18 } }, children));
}
function CodeBlock({ lang, code }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    navigator.clipboard.writeText(code);
    setCopied(true);
    setTimeout(() => setCopied(false), 2e3);
  };
  return /* @__PURE__ */ React.createElement("div", { className: "code-block-wrap" }, /* @__PURE__ */ React.createElement("div", { className: "code-block-header" }, /* @__PURE__ */ React.createElement("span", { className: "code-block-lang" }, lang), /* @__PURE__ */ React.createElement("button", { className: "code-block-copy", onClick: copy }, copied ? "copied!" : "copy")), /* @__PURE__ */ React.createElement("pre", { className: "code-block-pre" }, code));
}
ReactDOM.createRoot(document.getElementById("app")).render(/* @__PURE__ */ React.createElement(DocsPage, null));

})();
