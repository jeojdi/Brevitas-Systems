(function(){
function PostPage() {
  useFadeUpReveal();
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(Nav, { current: "blog" }), /* @__PURE__ */ React.createElement("section", { className: "section", style: { paddingBottom: 0 } }, /* @__PURE__ */ React.createElement("div", { className: "container", style: { maxWidth: 760 } }, /* @__PURE__ */ React.createElement("div", { className: "t-mono fade-up in", style: { color: "var(--bronze)", fontSize: 11, marginBottom: 28 } }, "ENGINEERING \xB7 JUNE 2026"), /* @__PURE__ */ React.createElement("h1", { className: "serif fade-up in", style: {
    fontSize: "clamp(36px, 5.5vw, 64px)",
    fontWeight: 400,
    letterSpacing: "-0.025em",
    lineHeight: 1.06,
    margin: "0 0 28px"
  } }, "Fable 5 is built for agents. The token bill will show it."), /* @__PURE__ */ React.createElement("p", { className: "t-body-lg fade-up delay-1 in", style: { maxWidth: 640, marginBottom: 40, color: "var(--stone-2)" } }, "Anthropic's Mythos-class model is finally public. It's the best thing that's happened to multi-agent pipelines in a year. It's also the most expensive model most teams will have ever run at scale. The math changes fast."), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 20, paddingBottom: 48, borderBottom: "1px solid var(--line)" } }, /* @__PURE__ */ React.createElement("span", { className: "t-mono", style: { color: "var(--stone)", fontSize: 11 } }, "Jun 10, 2026"), /* @__PURE__ */ React.createElement("span", { className: "t-mono", style: { color: "var(--line)" } }, "\xB7"), /* @__PURE__ */ React.createElement("span", { className: "t-mono", style: { color: "var(--stone)", fontSize: 11 } }, "8 min read"), /* @__PURE__ */ React.createElement("span", { className: "t-mono", style: { color: "var(--line)" } }, "\xB7"), /* @__PURE__ */ React.createElement("span", { className: "t-mono", style: {
    fontSize: 10,
    color: "var(--bronze)",
    padding: "3px 8px",
    border: "1px solid var(--bronze)",
    borderRadius: 2,
    letterSpacing: "0.1em"
  } }, "ENGINEERING")))), /* @__PURE__ */ React.createElement("article", null, /* @__PURE__ */ React.createElement("section", { className: "section", style: { paddingTop: 56, paddingBottom: 0 } }, /* @__PURE__ */ React.createElement("div", { className: "container", style: { maxWidth: 760 } }, /* @__PURE__ */ React.createElement("p", { className: "t-body-lg fade-up", style: { marginBottom: 24 } }, "Anthropic shipped Claude Fable 5 on June 9th. It's the first Mythos-class model they've made broadly available \u2014 the same model family that was briefly restricted after concerns about its ability to identify and exploit zero-days in critical infrastructure. New safeguards block specific high-risk domains. Everything else is live."), /* @__PURE__ */ React.createElement("p", { className: "t-body fade-up", style: { marginBottom: 24 } }, "The benchmarks are consistent with what we saw in early Mythos Preview access: #1 across intelligence, coding, and agentic tasks across 377 evaluated models. 80.7 on the agentic benchmark. 80.3% on SWE-Bench Pro. Those numbers don't convey the qualitative shift \u2014 the model stays coherent across long task chains in a way that previous Sonnet-class models didn't. Reviewer agents that used to hallucinate findings from earlier in the pipeline now don't. Planner agents don't lose the thread at step 40."), /* @__PURE__ */ React.createElement("p", { className: "t-body fade-up", style: { marginBottom: 24 } }, "For multi-agent pipelines specifically, the long-horizon memory management improvement is the headline. Anthropic calls it out directly: this is the capability previous models would drop partway through complex, extended tasks. That was the dominant failure mode we saw in our own harness, and it's now largely gone."), /* @__PURE__ */ React.createElement("p", { className: "t-body fade-up" }, "There's one number that changes everything else about this launch: ", /* @__PURE__ */ React.createElement("span", { className: "mono", style: { color: "var(--signal)" } }, "$50 per million output tokens"), "."))), /* @__PURE__ */ React.createElement("section", { style: { padding: "56px 0" } }, /* @__PURE__ */ React.createElement("div", { className: "container", style: { maxWidth: 760 } }, /* @__PURE__ */ React.createElement("blockquote", { style: {
    borderLeft: "2px solid var(--bronze)",
    paddingLeft: 28,
    margin: "0"
  } }, /* @__PURE__ */ React.createElement("p", { className: "serif fade-up", style: {
    fontSize: "clamp(24px, 3.5vw, 34px)",
    fontWeight: 300,
    fontStyle: "italic",
    letterSpacing: "-0.015em",
    lineHeight: 1.28,
    color: "var(--fg)",
    margin: 0
  } }, `"In a multi-agent pipeline, both the input and output prices compound across hops in ways that single-call pricing benchmarks don't expose."`)))), /* @__PURE__ */ React.createElement("section", { className: "section", style: { paddingTop: 0, paddingBottom: 0 } }, /* @__PURE__ */ React.createElement("div", { className: "container", style: { maxWidth: 760 } }, /* @__PURE__ */ React.createElement("h2", { className: "serif fade-up", style: { fontSize: "clamp(28px, 3.8vw, 40px)", fontWeight: 400, letterSpacing: "-0.02em", marginBottom: 28 } }, "The pipeline compounding problem."), /* @__PURE__ */ React.createElement("p", { className: "t-body fade-up", style: { marginBottom: 24 } }, "Fable 5 is priced at $10/M input tokens and $50/M output tokens, with a 50% batch discount. That's roughly 3\u20134\xD7 what most teams were paying per token on Claude Sonnet 4.x for comparable workloads. For a single-call use case, you benchmark it against the quality improvement and make a call. For a multi-agent pipeline, the math is different."), /* @__PURE__ */ React.createElement("p", { className: "t-body fade-up", style: { marginBottom: 32 } }, "Here's the architecture we've been benchmarking on: a three-agent architect / builder / reviewer loop, sixty HumanEval+ coding tasks, three seeds each. The same harness from ", /* @__PURE__ */ React.createElement("a", { href: "/benchmarks", className: "link" }, "our benchmarks page"), ". In that pipeline, by the time the Reviewer sees its context, it's ingesting the task, the architect's design, and the builder's full code output \u2014 the entire history of the run. At baseline, that third hop is the most expensive call by a significant margin."), /* @__PURE__ */ React.createElement("div", { className: "fade-up", style: {
    border: "1px solid var(--line)",
    borderRadius: 4,
    overflow: "hidden",
    marginBottom: 32
  } }, /* @__PURE__ */ React.createElement("div", { className: "t-mono", style: {
    padding: "10px 20px",
    borderBottom: "1px solid var(--line)",
    color: "var(--stone-2)",
    fontSize: 11,
    background: "var(--ink-2)"
  } }, "TOKEN CONSUMPTION \xB7 BASELINE \xB7 THREE-AGENT CODING HARNESS"), /* @__PURE__ */ React.createElement("table", { style: {
    width: "100%",
    borderCollapse: "collapse",
    fontFamily: "JetBrains Mono, ui-monospace, monospace",
    fontSize: 13
  } }, [
    ["Architect", "~480", "~380", "~$0.000024"],
    ["Builder", "~860", "~590", "~$0.0000382"],
    ["Reviewer", "~1,580", "~410", "~$0.0000363"],
    ["Total per task", "~2,920", "~1,380", "~$0.0000985"]
  ].map(([hop, inp, out, cost], i) => /* @__PURE__ */ React.createElement("tr", { key: i, style: {
    borderBottom: i < 3 ? "1px solid var(--line)" : "none",
    background: i === 3 ? "var(--ink-2)" : "transparent"
  } }, /* @__PURE__ */ React.createElement("td", { style: { padding: "14px 20px", color: i === 3 ? "var(--fg)" : "var(--stone-2)" } }, hop), /* @__PURE__ */ React.createElement("td", { style: { padding: "14px 20px", color: "var(--bone-dim)", textAlign: "right" } }, inp, " in"), /* @__PURE__ */ React.createElement("td", { style: { padding: "14px 20px", color: "var(--bone-dim)", textAlign: "right" } }, out, " out"), /* @__PURE__ */ React.createElement("td", { style: { padding: "14px 20px", color: i === 3 ? "var(--bronze)" : "var(--stone)", textAlign: "right" } }, cost)))), /* @__PURE__ */ React.createElement("div", { className: "t-mono", style: { padding: "10px 20px", color: "var(--stone)", fontSize: 10, background: "var(--ink-2)", borderTop: "1px solid var(--line)" } }, "FABLE 5 PRICING \xB7 $10/M INPUT \xB7 $50/M OUTPUT \xB7 ILLUSTRATIVE PER-TASK COST")), /* @__PURE__ */ React.createElement("p", { className: "t-body fade-up", style: { marginBottom: 24 } }, "At scale, the Reviewer hop alone costs more per call than an entire 3-agent run would have on Claude 3.5 Haiku. This isn't an argument against running Fable 5 \u2014 the quality improvement is real and for many use cases it changes the economics of what you can automate. It's an argument for being deliberate about what tokens those agents actually need."))), /* @__PURE__ */ React.createElement("section", { className: "section", style: { paddingTop: 56, paddingBottom: 0 } }, /* @__PURE__ */ React.createElement("div", { className: "container", style: { maxWidth: 760 } }, /* @__PURE__ */ React.createElement("h2", { className: "serif fade-up", style: { fontSize: "clamp(28px, 3.8vw, 40px)", fontWeight: 400, letterSpacing: "-0.02em", marginBottom: 28 } }, "The 1M window will make things worse before it makes them better."), /* @__PURE__ */ React.createElement("p", { className: "t-body fade-up", style: { marginBottom: 24 } }, "Fable 5 ships with a 1 million token context window, up from 200K on most previous Sonnet-class models. For specific tasks \u2014 keeping a full codebase in context across a multi-day run, reasoning across an entire document corpus \u2014 this is a genuine capability unlock."), /* @__PURE__ */ React.createElement("p", { className: "t-body fade-up", style: { marginBottom: 24 } }, "It also changes how engineers design pipelines, and not always in a good direction. When the context limit stops being a practical constraint, agents default to loading everything. We've seen this on our own harness: when context limits expand, agents stop making decisions about what to include. The window is effectively infinite from the agent's perspective, and the agent acts accordingly."), /* @__PURE__ */ React.createElement("div", { className: "fade-up", style: {
    padding: "24px 28px",
    border: "1px solid var(--line)",
    background: "var(--ink-2)",
    borderRadius: 4,
    marginBottom: 28
  } }, /* @__PURE__ */ React.createElement("div", { className: "t-mono", style: { color: "var(--bronze)", fontSize: 10, marginBottom: 12 } }, "PATTERN TO WATCH"), /* @__PURE__ */ React.createElement("p", { className: "t-body", style: { margin: 0, fontSize: 15 } }, "Agents designed for 128K windows were forced to summarize, prioritize, and pass only relevant context downstream. Those constraints built frugality into the architecture. Redesigning the same pipeline for 1M windows and naively expanding the context sizes will cost 5\u201310\xD7 more with no quality improvement on most tasks.")), /* @__PURE__ */ React.createElement("p", { className: "t-body fade-up" }, "The 1M window matters a lot for specific task shapes. For most coding and knowledge-work pipelines today, 128K was already enough \u2014 the problem was redundancy, not capacity. Fable 5's long-horizon coherence improvements mean you can now use that capacity effectively when you need it. But you still need to be deliberate about when you actually need it."))), /* @__PURE__ */ React.createElement("section", { className: "section", style: { paddingTop: 56, paddingBottom: 0 } }, /* @__PURE__ */ React.createElement("div", { className: "container", style: { maxWidth: 760 } }, /* @__PURE__ */ React.createElement("h2", { className: "serif fade-up", style: { fontSize: "clamp(28px, 3.8vw, 40px)", fontWeight: 400, letterSpacing: "-0.02em", marginBottom: 28 } }, "Where Brevitas fits."), /* @__PURE__ */ React.createElement("p", { className: "t-body fade-up", style: { marginBottom: 24 } }, "Our benchmarks used Claude Sonnet 4.x as the baseline. At Fable 5's pricing, the absolute dollar savings from a 59.4% token reduction scale proportionally \u2014 the percentage stays the same, the dollars go up. On a pipeline running 50,000 tasks per month at Fable 5 prices, that difference is substantial enough to change the business case for running the model at all."), /* @__PURE__ */ React.createElement("p", { className: "t-body fade-up", style: { marginBottom: 32 } }, "Plugging Brevitas into a Fable 5 pipeline is the same two-line change. The model is specified at the orchestrator level; Brevitas operates at the communication layer between agents and is agnostic to which model is running each hop."), /* @__PURE__ */ React.createElement("div", { className: "fade-up", style: { marginBottom: 32 } }, /* @__PURE__ */ React.createElement(
    CodeBlockPy,
    {
      filename: "pipeline_fable5.py",
      copyable: true,
      source: `from brevitas import optimize
import anthropic

# Your existing Fable 5 agents \u2014 no changes needed
architect = create_agent(model="claude-fable-5", role="architect")
builder   = create_agent(model="claude-fable-5", role="builder")
reviewer  = create_agent(model="claude-fable-5", role="reviewer")

# Wrap the pipeline \u2014 Brevitas sits between agents, not inside them
pipeline = optimize([architect, builder, reviewer])

# Run as normal
result = pipeline.run(task)
report = pipeline.report()
# {
#   "tokens_in":  1,184,  "tokens_out":  562,
#   "cost_baseline": 0.0000985,
#   "cost_optimized": 0.0000400,
#   "savings_pct": 59.4,
#   "savings_by_technique": {
#     "compress": 0.41, "reference": 0.22, "delta": 0.11,
#     "prune": 0.08, "protocol": 0.04
#   }
# }`
    }
  )), /* @__PURE__ */ React.createElement("p", { className: "t-body fade-up", style: { marginBottom: 24 } }, "One thing that does change with Fable 5: the routing layer can be more aggressive with the reference technique. Because Fable 5 has better long-horizon coherence, agents are less likely to need an expanded verbatim copy of upstream context to perform well \u2014 they can work from the ", /* @__PURE__ */ React.createElement("span", { className: "mono", style: { color: "var(--signal)" } }, "mem://"), " reference plus a compact summary. That's worth testing explicitly in your harness before accepting the default router settings."), /* @__PURE__ */ React.createElement("div", { className: "fade-up", style: {
    display: "grid",
    gridTemplateColumns: "1fr 1fr",
    gap: 1,
    border: "1px solid var(--line)",
    borderRadius: 4,
    overflow: "hidden",
    marginBottom: 32
  } }, [
    {
      label: "BASELINE \xB7 FABLE 5",
      val: "~$0.0000985",
      sub: "per task, unoptimized",
      hi: false
    },
    {
      label: "WITH BREVITAS \xB7 FABLE 5",
      val: "~$0.0000400",
      sub: "per task, 59.4% fewer tokens",
      hi: true
    }
  ].map((x) => /* @__PURE__ */ React.createElement("div", { key: x.label, style: {
    padding: "32px 28px",
    background: x.hi ? "var(--ink-2)" : "transparent"
  } }, /* @__PURE__ */ React.createElement("div", { className: "t-mono", style: { color: "var(--stone)", fontSize: 10, marginBottom: 20 } }, x.label), /* @__PURE__ */ React.createElement("div", { className: "serif", style: {
    fontSize: "clamp(32px, 4vw, 44px)",
    fontWeight: 300,
    letterSpacing: "-0.02em",
    lineHeight: 1,
    color: x.hi ? "var(--bronze)" : "var(--fg)",
    marginBottom: 10
  } }, x.val), /* @__PURE__ */ React.createElement("div", { className: "t-mono", style: { color: "var(--stone)", fontSize: 11 } }, x.sub)))), /* @__PURE__ */ React.createElement("p", { className: "t-body fade-up", style: { marginBottom: 0 } }, "At 50,000 tasks per month, that's the difference between ~$4,925 and ~$2,000 in model spend on the pipeline alone. At 500,000 tasks, the monthly delta is ~$29,250. The percentage savings are the same as every model we've measured. The absolute number changes when you're running Mythos-class pricing."))), /* @__PURE__ */ React.createElement("section", { className: "section", style: { paddingTop: 56, paddingBottom: 0 } }, /* @__PURE__ */ React.createElement("div", { className: "container", style: { maxWidth: 760 } }, /* @__PURE__ */ React.createElement("h2", { className: "serif fade-up", style: { fontSize: "clamp(28px, 3.8vw, 40px)", fontWeight: 400, letterSpacing: "-0.02em", marginBottom: 28 } }, "Anthropic's own compaction feature."), /* @__PURE__ */ React.createElement("p", { className: "t-body fade-up", style: { marginBottom: 24 } }, "It's worth naming directly: Anthropic ships Fable 5 with a ", /* @__PURE__ */ React.createElement("em", { style: { fontStyle: "italic" } }, "compaction"), " feature in beta. Server-side summarization that automatically condenses earlier parts of a long-running conversation to keep the effective context from growing unbounded. It's available via the API and is a sensible default for agents running truly long tasks."), /* @__PURE__ */ React.createElement("p", { className: "t-body fade-up", style: { marginBottom: 24 } }, "We're complementary, not competitive, with this. Compaction addresses the within-conversation growth problem for single agents. Brevitas addresses the across-agent redundancy problem in multi-agent pipelines \u2014 the content that gets re-serialized at every hop because each agent starts with a fresh context. These are different problems; you can use both."), /* @__PURE__ */ React.createElement("div", { className: "fade-up", style: {
    padding: "24px 28px",
    border: "1px solid var(--line)",
    background: "var(--ink-2)",
    borderRadius: 4,
    marginBottom: 0
  } }, /* @__PURE__ */ React.createElement("div", { className: "t-mono", style: { color: "var(--bronze)", fontSize: 10, marginBottom: 16 } }, "COMPLEMENTARY LAYERS"), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 14 } }, [
    ["Anthropic Compaction", "Within a single agents conversation; condenses conversation history as it grows."],
    ["Brevitas", "Between agents in a pipeline \u2014 compresses, references, and de-duplicates what flows across hops."],
    ["Provider Prompt Cache", "Repeated identical prefixes within a single agent \u2014 cache hits for static system prompt content."]
  ].map(([name, desc], i) => /* @__PURE__ */ React.createElement("div", { key: i, style: { display: "flex", gap: 16 } }, /* @__PURE__ */ React.createElement("span", { className: "t-mono", style: { color: "var(--stone)", flexShrink: 0, paddingTop: 2 } }, "\u2192"), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("span", { className: "t-mono", style: { color: "var(--fg)", fontSize: 12 } }, name), /* @__PURE__ */ React.createElement("span", { className: "t-body", style: { color: "var(--stone-2)", fontSize: 14, marginLeft: 10 } }, desc)))))))), /* @__PURE__ */ React.createElement("section", { className: "section", style: { paddingTop: 56, paddingBottom: 56 } }, /* @__PURE__ */ React.createElement("div", { className: "container", style: { maxWidth: 760 } }, /* @__PURE__ */ React.createElement("h2", { className: "serif fade-up", style: { fontSize: "clamp(28px, 3.8vw, 40px)", fontWeight: 400, letterSpacing: "-0.02em", marginBottom: 28 } }, "What we're watching."), /* @__PURE__ */ React.createElement("p", { className: "t-body fade-up", style: { marginBottom: 24 } }, "We're rerunning our 60-task harness on Fable 5 this week. A few things we expect to see: the compression technique should hold at similar ratios \u2014 model capability doesn't change what the relevance model strips. The reference technique may show improved savings because Fable 5's coherence means it works better from compact references than previous models did. Delta mode should be largely unchanged; the delta is a diff of state, not a model capability question."), /* @__PURE__ */ React.createElement("p", { className: "t-body fade-up", style: { marginBottom: 24 } }, "The routing layer is the one to watch. Our router was trained on pipeline traces from Sonnet 4.x. Fable 5's output distribution is different enough that the per-call routing decisions may shift. We'll publish updated routing defaults once we've characterized it. If you're running Fable 5 in production and want to share pipeline traces for benchmarking, get in touch."), /* @__PURE__ */ React.createElement("div", { className: "fade-up", style: { display: "flex", flexDirection: "column", gap: 16, marginBottom: 40 } }, [
    ["Technique re-benchmark on Fable 5", "Running this week. Will post per-technique attribution data."],
    ["Router calibration", "New routing defaults for Fable 5 output distribution."],
    ["1M window case studies", "Documenting which pipeline shapes actually benefit from extended context."],
    ["Mythos 5 preview", "We have early access. Benchmarks pending NDA lift."]
  ].map(([h, b], i) => /* @__PURE__ */ React.createElement("div", { key: i, style: { display: "flex", gap: 20, paddingBottom: 16, borderBottom: "1px solid var(--line)" } }, /* @__PURE__ */ React.createElement("span", { className: "t-mono", style: { color: "var(--bronze)", flexShrink: 0, paddingTop: 3, fontSize: 12 } }, "0", i + 1), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("div", { className: "serif", style: { fontSize: 19, fontWeight: 400, marginBottom: 4 } }, h), /* @__PURE__ */ React.createElement("div", { className: "t-body", style: { color: "var(--stone-2)", fontSize: 14 } }, b))))), /* @__PURE__ */ React.createElement("p", { className: "t-body fade-up", style: { marginBottom: 0 } }, "The short version: Fable 5 is the right model for agents that need to stay coherent across complex tasks. The pricing means the optimization layer isn't optional \u2014 it's the difference between Fable 5 being economically viable at scale and not. We'll have updated numbers by end of week.")))), /* @__PURE__ */ React.createElement("section", { style: { borderTop: "1px solid var(--line)", padding: "32px 0" } }, /* @__PURE__ */ React.createElement("div", { className: "container", style: { maxWidth: 760 } }, /* @__PURE__ */ React.createElement("a", { href: "/blog", className: "t-mono link", style: { color: "var(--stone-2)", fontSize: 13 } }, "\u2190 Back to Notes"))), /* @__PURE__ */ React.createElement("section", { className: "section section--tight", style: { background: "var(--ink-2)", borderTop: "1px solid var(--line)", borderBottom: "1px solid var(--line)" } }, /* @__PURE__ */ React.createElement("div", { className: "container" }, /* @__PURE__ */ React.createElement("div", { style: { maxWidth: 720 } }, /* @__PURE__ */ React.createElement("h2", { className: "t-h2", style: { marginBottom: 16 } }, "Running Fable 5 in a multi-agent pipeline?"), /* @__PURE__ */ React.createElement("p", { className: "t-body-lg", style: { marginBottom: 32 } }, "Get early access to Brevitas and the updated Fable 5 benchmark report when it ships."), /* @__PURE__ */ React.createElement(WaitlistInput, { source: "blog-fable5" })))), /* @__PURE__ */ React.createElement(Footer, null));
}
const root = ReactDOM.createRoot(document.getElementById("app"));
root.render(/* @__PURE__ */ React.createElement(PostPage, null));

})();
