(function(){
const CALENDLY = "https://calendly.com/anish-brevitassystems/30min";
const STAGES = [
  {
    id: "assess",
    n: "01 \xB7 Assess",
    h: "Benchmark your workload.",
    lead: "Before anything is deployed, we measure where your pipeline spends its inference. Which edits your agents make, how much they re-prefill, and what an in-place edit would save on your own models and hardware.",
    props: [
      ["Profiled on your traffic.", "Real sessions, not synthetic prompts, so the numbers reflect what you actually run."],
      ["Per-edit and all-in.", "Steady-state speed-up and the setup cost charged honestly, side by side."],
      ["A re-runnable report.", "Every result carries its certificate: pooled p95 KL against a fresh re-ingest."]
    ],
    card: { kind: "Benchmarks", h: "Splice benchmarks", b: "How we measure, and the results on text, video, image and audio.", href: "/benchmarks", read: "View benchmarks", img: "/assets/ascii-magic-5-poster.jpg" }
  },
  {
    id: "integrate",
    n: "02 \xB7 Integrate",
    h: "Into SGLang or vLLM.",
    lead: "Splice runs as a cache-edit path inside your serving engine, not as a separate service. Edits arrive over a socket, repair is batched by block, and the surrounding cache is reused untouched.",
    props: [
      ["Radix-cache backend.", "Built into SGLang today, with vLLM support for direct KV-cache access."],
      ["Per-model exactness tests.", "Each model is verified before an edit type is enabled in production."],
      ["Byte-identical repair.", "Block-batched repair reproduces per-row repair exactly, at engine speed."]
    ],
    card: { kind: "Research paper", h: "Exact position surgery", b: "Re-rotating a rotary cache after an edit is exact, and how it extends to multi-axis M-RoPE.", href: "/blog/splice-rerotation", read: "Read the paper", img: "/assets/ascii-magic-3-poster.jpg" }
  },
  {
    id: "certify",
    n: "03 \xB7 Certify",
    h: "Prove every edit.",
    lead: "An edit is only served if it matches the answer a clean recompute would have produced. We calibrate that gate for your models and your edit types, so correctness is a property of the system, not a hope.",
    props: [
      ["Teacher-forced against re-ingest.", "Pooled p95 KL under 0.05, with a control arm that must read zero."],
      ["A conservative runtime threshold.", "Edits that certify are served; the rest fall back to a clean re-prefill."],
      ["Tuned per edit type.", "Delete, same-length replace and reorder are each calibrated separately."]
    ],
    card: { kind: "Research paper", h: "The correctness certificate", b: "A teacher-forced KL gate calibrated into a runtime threshold that decides what ships.", href: "/blog/splice-certificate", read: "Read the paper", img: "/assets/ascii-magic-4-poster.jpg" }
  },
  {
    id: "operate",
    n: "04 \xB7 Operate",
    h: "Run it in production.",
    lead: "A forward-deployed engineer works alongside your team to put the edit path into your production loop, watch it under real load, and widen coverage as your workloads and models change.",
    props: [
      ["Forward-deployed.", "We integrate against your stack and stay through rollout, not just a handoff."],
      ["Admission-aware.", "Scheduling that keeps the edit path safe under shared KV pressure."],
      ["Coverage that grows.", "As the runtime proxy improves, more safe edits move onto the fast path."]
    ],
    card: { kind: "Research paper", h: "Dependency-ranked repair", b: "Which rows an edit actually breaks, and why recomputing a scattered few percent is enough.", href: "/blog/splice-repair", read: "Read the paper", img: "/assets/enterprise-operate.jpg" }
  }
];
function EnterprisePage() {
  useFadeUpReveal();
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(Nav, { current: "enterprise" }), /* @__PURE__ */ React.createElement("section", { className: "section", style: { paddingBottom: "clamp(24px, 4vh, 48px)" } }, /* @__PURE__ */ React.createElement("div", { className: "container" }, /* @__PURE__ */ React.createElement("h1", { className: "ent-h1 fade-up in" }, "Splice, deployed into your inference stack."), /* @__PURE__ */ React.createElement("p", { className: "ent-sub fade-up delay-1 in" }, "We partner with teams running self-hosted models across the whole arc of a deployment: measuring where your pipeline rebuilds memory it could edit, integrating the cache-edit path into your serving engine, certifying every edit against a fresh re-ingest, and running it in production alongside your team."), /* @__PURE__ */ React.createElement("div", { className: "fade-up delay-2 in" }, /* @__PURE__ */ React.createElement(Button, { variant: "primary", href: CALENDLY, target: "_blank", className: "hero-btn hero-cta" }, "Book a call")))), STAGES.map((s, i) => /* @__PURE__ */ React.createElement("section", { key: s.id, id: s.id, className: "section", style: { borderTop: "1px solid var(--line)", background: i % 2 ? "var(--ink-2)" : "transparent" } }, /* @__PURE__ */ React.createElement("div", { className: "container" }, /* @__PURE__ */ React.createElement("div", { className: `ent-sec${i % 2 ? " ent-sec--rev" : ""}` }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("div", { className: "ent-num fade-up" }, s.n), /* @__PURE__ */ React.createElement("h2", { className: "fade-up" }, s.h), /* @__PURE__ */ React.createElement("p", { className: "lead fade-up" }, s.lead), /* @__PURE__ */ React.createElement("ul", { className: "ent-props fade-up" }, s.props.map(([b, t], i2) => /* @__PURE__ */ React.createElement("li", { key: i2 }, /* @__PURE__ */ React.createElement("b", null, b), " ", t)))), /* @__PURE__ */ React.createElement("a", { href: s.card.href, className: "ent-card fade-up" }, /* @__PURE__ */ React.createElement("img", { src: s.card.img, alt: "", loading: "lazy" }), /* @__PURE__ */ React.createElement("div", { className: "kind" }, s.card.kind), /* @__PURE__ */ React.createElement("h3", null, s.card.h), /* @__PURE__ */ React.createElement("p", null, s.card.b), /* @__PURE__ */ React.createElement("span", { className: "read" }, s.card.read)))))), /* @__PURE__ */ React.createElement("section", { className: "section", style: { borderTop: "1px solid var(--line)" } }, /* @__PURE__ */ React.createElement("div", { className: "container", style: { maxWidth: 820 } }, /* @__PURE__ */ React.createElement("div", { className: "ent-num fade-up" }, "What we need from you"), /* @__PURE__ */ React.createElement("h2", { className: "t-h2 fade-up", style: { margin: "0 0 28px" } }, "Requirements for setup."), /* @__PURE__ */ React.createElement("dl", { className: "ent-reqs fade-up" }, /* @__PURE__ */ React.createElement("dt", null, "Model access"), /* @__PURE__ */ React.createElement("dd", null, "Self-hosted open models on SGLang or vLLM, or a platform hosting them, so the edit path has direct KV-cache access."), /* @__PURE__ */ React.createElement("dt", null, "Replicas"), /* @__PURE__ */ React.createElement("dd", null, "Dedicated or lightly shared replicas for the edit path."), /* @__PURE__ */ React.createElement("dt", null, "Position schedule"), /* @__PURE__ */ React.createElement("dd", null, "Standard RoPE or M-RoPE with a fixed schedule."), /* @__PURE__ */ React.createElement("dt", null, "Cache precision"), /* @__PURE__ */ React.createElement("dd", null, "fp16, bf16 or int8 keys and values."), /* @__PURE__ */ React.createElement("dt", null, "Per-model test"), /* @__PURE__ */ React.createElement("dd", null, "A per-model exactness test before enabling an edit type in production.")))), /* @__PURE__ */ React.createElement("section", { className: "section", style: { background: "var(--ink-2)", borderTop: "1px solid var(--line)", borderBottom: "1px solid var(--line)" } }, /* @__PURE__ */ React.createElement("div", { className: "container" }, /* @__PURE__ */ React.createElement("div", { style: { maxWidth: 820 } }, /* @__PURE__ */ React.createElement("h2", { className: "t-h1", style: { marginTop: 0, marginBottom: 20 } }, "Collaborate with us."), /* @__PURE__ */ React.createElement("p", { className: "t-body-lg", style: { marginBottom: 40, maxWidth: 680 } }, "Tell us what you run and where it hurts. We will benchmark Splice on your workload and share the certificate numbers, then scope the integration from there."), /* @__PURE__ */ React.createElement(Button, { variant: "primary", href: CALENDLY, target: "_blank", className: "hero-btn hero-cta" }, "Book a call")))), /* @__PURE__ */ React.createElement(Footer, null));
}
ReactDOM.createRoot(document.getElementById("app")).render(/* @__PURE__ */ React.createElement(EnterprisePage, null));

})();
