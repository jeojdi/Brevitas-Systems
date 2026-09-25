(function(){
function NotFoundPage() {
  const [tokens, setTokens] = useState([]);
  useEffect(() => {
    const words = ["context", "prompt", "agent", "message", "token", "memory", "state", "route", "call", "trace"];
    setTokens(Array.from({ length: 28 }).map((_, i) => ({
      id: i,
      w: words[i % words.length],
      left: Math.random() * 90 + 5,
      top: Math.random() * 90 + 5,
      delay: Math.random() * 1.5,
      dropped: Math.random() < 0.75
    })));
  }, []);
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(Nav, { current: "" }), /* @__PURE__ */ React.createElement("section", { style: {
    minHeight: "calc(100vh - 80px)",
    display: "flex",
    alignItems: "center",
    padding: "clamp(40px, 8vh, 120px) 0",
    position: "relative",
    overflow: "hidden"
  } }, /* @__PURE__ */ React.createElement("div", { style: { position: "absolute", inset: 0, pointerEvents: "none" } }, tokens.map((t) => /* @__PURE__ */ React.createElement(
    "span",
    {
      key: t.id,
      className: `nf-token ${t.dropped ? "dropped" : ""}`,
      style: {
        position: "absolute",
        left: `${t.left}%`,
        top: `${t.top}%`,
        animationDelay: `${t.delay}s`
      }
    },
    t.w
  ))), /* @__PURE__ */ React.createElement("div", { className: "container", style: { position: "relative", zIndex: 1 } }, /* @__PURE__ */ React.createElement("div", { style: { maxWidth: 720 } }, /* @__PURE__ */ React.createElement("div", { className: "t-mono", style: { color: "var(--bronze)", marginBottom: 24, fontSize: 13 } }, "STATUS 404 \xB7 ROUTE NOT FOUND"), /* @__PURE__ */ React.createElement("h1", { className: "serif", style: {
    fontSize: "clamp(96px, 18vw, 220px)",
    fontWeight: 300,
    letterSpacing: "-0.04em",
    lineHeight: 0.9,
    margin: "0 0 24px 0"
  } }, "4", /* @__PURE__ */ React.createElement("em", { style: { fontStyle: "italic", color: "var(--bronze)" } }, "0"), "4"), /* @__PURE__ */ React.createElement("h2", { className: "serif", style: {
    fontSize: "clamp(28px, 3.6vw, 42px)",
    fontWeight: 400,
    letterSpacing: "-0.015em",
    lineHeight: 1.15,
    margin: "0 0 24px 0",
    maxWidth: 640
  } }, /* @__PURE__ */ React.createElement("em", { style: { fontStyle: "italic" } }, "This page was compressed a little too aggressively.")), /* @__PURE__ */ React.createElement("p", { className: "t-body-lg", style: { marginBottom: 48, maxWidth: 520 } }, "The relevance model decided this route wasn't meaning-bearing. That was probably a mistake. Here are some routes that definitely are:"), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexWrap: "wrap", gap: 16, marginBottom: 48 } }, /* @__PURE__ */ React.createElement("a", { href: "/", className: "btn btn-primary" }, "Back to home"), /* @__PURE__ */ React.createElement("a", { href: "/product", className: "btn btn-ghost underline" }, "Product"), /* @__PURE__ */ React.createElement("a", { href: "/benchmarks", className: "btn btn-ghost underline" }, "Benchmarks"), /* @__PURE__ */ React.createElement("a", { href: "mailto:james@brevitassystems.com", className: "btn btn-ghost underline" }, "Docs")), /* @__PURE__ */ React.createElement("div", { style: {
    padding: "20px 24px",
    border: "1px solid var(--line)",
    background: "var(--ink-2)",
    borderLeft: "2px solid var(--bronze)",
    maxWidth: 560,
    fontFamily: "JetBrains Mono, monospace",
    fontSize: 12,
    color: "var(--stone-2)"
  } }, /* @__PURE__ */ React.createElement("div", { style: { color: "var(--stone)", marginBottom: 6 } }, "# trace"), /* @__PURE__ */ React.createElement("div", null, 'route = "', typeof window !== "undefined" ? window.location.pathname : "/unknown", '"'), /* @__PURE__ */ React.createElement("div", { style: { color: "var(--bronze)" } }, "router.decide(route) \u2192 dropped (relevance: 0.03)"), /* @__PURE__ */ React.createElement("div", null, "\u2192 ", /* @__PURE__ */ React.createElement("span", { style: { color: "var(--signal)" } }, "redirect recommended: /")))))), /* @__PURE__ */ React.createElement(Footer, null), /* @__PURE__ */ React.createElement("style", null, `
        .nf-token {
          font-family: 'JetBrains Mono', monospace;
          font-size: 13px;
          color: var(--stone);
          opacity: 0.22;
          animation: nfDrift 8s ease-in-out infinite;
        }
        .nf-token.dropped {
          opacity: 0;
          text-decoration: line-through;
          animation: nfDrop 6s ease-in-out infinite;
        }
        @keyframes nfDrift {
          0%, 100% { transform: translate(0, 0); }
          50% { transform: translate(8px, -4px); }
        }
        @keyframes nfDrop {
          0% { opacity: 0.28; transform: translateY(0); text-decoration: none; }
          40% { opacity: 0.18; text-decoration: line-through; }
          70% { opacity: 0; transform: translateY(12px); }
          100% { opacity: 0; }
        }
      `));
}
ReactDOM.createRoot(document.getElementById("app")).render(/* @__PURE__ */ React.createElement(NotFoundPage, null));

})();
