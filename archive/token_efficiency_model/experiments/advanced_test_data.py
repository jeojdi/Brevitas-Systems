"""Advanced Test Data Generator for Token Efficiency Testing

Generates complex, multi-modal scenarios that test edge cases and realistic
multi-agent communication patterns:
- Multi-turn conversations with state drift
- High-complexity reasoning tasks
- Domain-specific vocabularies  
- Cross-team communication patterns
- Time-series analysis scenarios
- Adversarial pruning cases
"""

import random
from typing import Dict, List, Any
from enum import Enum


class ScenarioType(Enum):
    """Types of advanced scenarios"""
    MULTI_TURN_STATEFUL = "multi_turn_stateful"
    HIGH_COMPLEXITY_REASONING = "high_complexity_reasoning"
    DOMAIN_SPECIFIC = "domain_specific"
    CROSS_TEAM_COMM = "cross_team_communication"
    TIMESERIES_ANALYSIS = "timeseries_analysis"
    ADVERSARIAL_PRUNING = "adversarial_pruning"
    CASCADING_DECISIONS = "cascading_decisions"
    EMERGENT_BEHAVIOR = "emergent_behavior"
    MATH_REASONING = "math_reasoning"
    MULTI_HOP_QA = "multi_hop_qa"
    LOGICAL_DEDUCTION = "logical_deduction"
    PLANNING = "planning"
    SWE_DEVELOPMENT = "swe_development"
    RESEARCH_TASK = "research_task"


class AdvancedTestDataGenerator:
    """Generates nuanced, realistic test scenarios"""
    
    def __init__(self, seed: int = 42):
        random.seed(seed)
        self.domains = {
            "finance": {
                "keywords": ["portfolio", "derivative", "arbitrage", "volatility", "correlation", 
                           "hedge", "liquidity", "spread", "risk-adjusted", "sharpe-ratio"],
                "entities": ["trader", "market-maker", "investor", "risk-officer", "compliance"],
                "actions": ["execute", "hedge", "rebalance", "liquidate", "analyze"]
            },
            "devops": {
                "keywords": ["deployment", "rollout", "canary", "bluegreen", "sidecar", "circuit-breaker",
                           "latency", "throughput", "observability", "instrumentation"],
                "entities": ["ops-team", "dev-team", "sre", "dba", "platform-engineer"],
                "actions": ["deploy", "monitor", "scale", "debug", "optimize"]
            },
            "biology": {
                "keywords": ["phenotype", "genotype", "mutation", "epistasis", "pleiotropy", "heritability",
                           "allele", "locus", "quantitative", "population-genetics"],
                "entities": ["researcher", "lab-tech", "bioinformatician", "clinician", "geneticist"],
                "actions": ["sequence", "analyze", "model", "validate", "predict"]
            },
            "mlops": {
                "keywords": ["model-drift", "feature-importance", "calibration", "ablation", 
                           "reproducibility", "lineage", "experimentation", "governance", "drift-detection"],
                "entities": ["ml-engineer", "data-scientist", "validator", "monitor", "auditor"],
                "actions": ["train", "validate", "deploy", "monitor", "remediate"]
            }
        }
    
    def _multi_turn_stateful(self, turn: int = 1, team_id: int = 1) -> Dict[str, Any]:
        """
        Multi-turn conversation with cumulative state drift.
        Simulates decision-making where context matters, and some details
        become less relevant over time.
        """
        base_task = f"Team-{team_id} Turn-{turn}: Review architecture proposal and identify risk vectors"

        # Build conversation history that compounds
        incoming_messages = [
            f"Agent-A: We analyzed 3 deployment patterns. Pattern-{i} has "
            f"tradeoff: lower latency but higher memory footprint."
            for i in range(random.randint(3, 6))
        ]

        # Add turn-specific complications
        if turn > 1:
            incoming_messages.extend([
                f"Agent-B: Context from Turn-{turn-1}: We approved Pattern-{turn-1}, "
                f"but measurements show {turn*15}% higher CPU than expected.",
                f"Agent-C: The approval was conditional on traffic staying <1Krps. "
                f"Traffic now at {800 + turn*200}rps."
            ])

        # Prior context accumulates but some details fade
        prior_context = [
            f"Decision-{j}: Earlier we agreed on SLA: P50<100ms, P99<500ms, "
            f"availability>99.95% (turn {turn})"
            for j in range(max(1, turn))
        ]

        prior_context.extend([
            f"Constraint-{j}: Team-{team_id} has budget for 8 instances max, "
            f"auto-scale trigger at 70% CPU."
            for j in range(2)
        ])

        # Old decisions become less critical
        if turn > 2:
            prior_context.extend([
                f"Deprecated-Decision: {turn} turns ago we considered approach X "
                f"(now superseded by new findings)"
            ])

        complexity = 0.5 + 0.15 * turn  # Complexity increases with turns
        urgency = 0.7 if turn < 3 else 0.4  # First 3 turns urgent
        context_load = min(1.0, (len(prior_context) + len(incoming_messages)) / 20.0)
        continuity = min(1.0, 0.6 + turn * 0.1)  # State continuity improves with turns

        must_keep_facts = [
            f"Team-{team_id}",
            f"Turn-{turn}",
            "P50<100ms",
            "P99<500ms",
            "99.95%",
            "8 instances",
            "70% CPU",
        ]
        if turn > 1:
            must_keep_facts.extend([
                f"Turn-{turn-1}",
                f"{turn*15}% higher CPU",
                f"{800 + turn*200}rps",
            ])

        return {
            "task_text": base_task,
            "incoming_messages": incoming_messages,
            "prior_context": prior_context,
            "complexity": complexity,
            "urgency": urgency,
            "context_load": context_load,
            "continuity": continuity,
            "thread_id": team_id,
            "scenario_type": ScenarioType.MULTI_TURN_STATEFUL,
            "turn": turn,
            "task_family": "operational",
            "must_keep_facts": must_keep_facts,
        }
    
    def _high_complexity_reasoning(self) -> Dict[str, Any]:
        """
        High-complexity reasoning task with many interdependent constraints.
        Tests whether pruning accidentally removes critical decision factors.
        """
        task_text = (
            "Given 12 microservices with interdependencies, optimize for: "
            "minimize cost, ensure P99<200ms, maintain <50% cross-zone traffic, "
            "support 10K concurrent users, enable canary deployments, "
            "collect full distributed traces. Propose architecture."
        )
        
        services = [
            "api-gateway", "auth-service", "order-service", "payment-processor",
            "inventory-manager", "shipping-coordinator", "notification-hub",
            "analytics-pipeline", "recommendation-engine", "cache-layer",
            "data-warehouse", "monitoring-system"
        ]
        
        # Messages with interdependencies
        incoming_messages = []
        for i, svc in enumerate(services):
            constraint = random.choice([
                f"{svc} must respond <50ms (downstream latency SLA)",
                f"{svc} needs <100MB memory, can scale to 5 replicas",
                f"{svc} generates 500 metrics/min, needs aggregation",
                f"{svc} calls {random.choice(services)} synchronously",
            ])
            incoming_messages.append(f"Architect-1: {constraint}")
        
        # Add conflicting requirements
        incoming_messages.extend([
            "Architect-2: Payment processor must be in-country for compliance, adds latency.",
            "Architect-3: But recommendations engine needs global cache for ML model.",
            "Ops-Lead: We have budget for 100 instances total, not unlimited scaling.",
            "SecOps: All inter-service traffic must be encrypted, increases network overhead.",
        ])
        
        # Prior context with many constraints
        prior_context = [
            f"SLA-Req: {svc} uptime >= {95 + random.randint(0, 4)}%, RPO <= {random.randint(1, 10)} min"
            for svc in services[:6]
        ]
        prior_context.extend([
            "Budget: $50K/month, must accommodate growth to 50K users",
            "Team-Capacity: 3 SREs available, spend <2hr/week on platform ops",
            "Compliance: HIPAA, SOC-2, must audit all access",
            "Timeline: 6-month migration window from monolith",
            "Disaster: RTO <= 1hr for critical services, RTO <= 4hr for others",
        ])

        must_keep_facts = [
            "P99<200ms",
            "10K concurrent users",
            "$50K/month",
            "50K users",
            "HIPAA",
            "SOC-2",
            "6-month",
            "RTO <= 1hr",
        ] + services[:6]

        return {
            "task_text": task_text,
            "incoming_messages": incoming_messages,
            "prior_context": prior_context,
            "complexity": 0.85,
            "urgency": 0.75,
            "context_load": 0.95,
            "continuity": 0.5,
            "thread_id": 1,
            "scenario_type": ScenarioType.HIGH_COMPLEXITY_REASONING,
            "task_family": "operational",
            "must_keep_facts": must_keep_facts,
        }
    
    def _domain_specific(self, domain: str = None) -> Dict[str, Any]:
        """
        Domain-specific vocabulary and patterns (finance, devops, biology, ml-ops)
        Tests compression and routing with specialized terminology
        """
        if domain is None:
            domain = random.choice(list(self.domains.keys()))
        
        d = self.domains[domain]
        
        task_text = f"[{domain.upper()}] Design solution for {random.choice(d['actions'])}: "
        task_phrase = random.choice([
            "Implement robust strategy for critical business requirement",
            "Optimize workflow to reduce waste and improve outcomes",
            "Design resilient system to handle edge cases",
            "Create framework for ongoing governance and compliance",
        ])
        task_text += task_phrase
        
        # Domain-specific messages with jargon
        incoming_messages = [
            f"{random.choice(d['entities'])}: We need to {random.choice(d['actions'])} "
            f"considering {random.choice(d['keywords'])} across {random.choice(d['keywords'])}."
            for _ in range(random.randint(4, 8))
        ]
        
        # Conflicting domain perspectives
        incoming_messages.extend([
            f"{d['entities'][0]}: Prioritize {random.choice(d['keywords'])}, "
            f"even if it increases complexity.",
            f"{d['entities'][1]}: Counter-view: {random.choice(d['keywords'])} "
            f"isn't worth the operational burden.",
        ])
        
        prior_context = [
            f"Policy[{domain}]: All {d['actions'][0]} must account for "
            f"{random.choice(d['keywords'])}, established after 2022 incident.",
            f"Pattern[{domain}]: Best practice is to decouple "
            f"{random.choice(d['keywords'])} from {random.choice(d['keywords'])}.",
            f"Metric[{domain}]: KPI targets: {random.randint(85, 99)}% for "
            f"{random.choice(d['keywords'])}, threshold {random.randint(20, 50)} for "
            f"{random.choice(d['keywords'])}.",
        ]

        must_keep_facts = [
            f"[{domain.upper()}]",
            d['actions'][0],
            d['entities'][0],
            d['entities'][1],
            d['keywords'][0],
            d['keywords'][1],
        ]

        return {
            "task_text": task_text,
            "incoming_messages": incoming_messages,
            "prior_context": prior_context,
            "complexity": 0.7,
            "urgency": 0.6,
            "context_load": 0.7,
            "continuity": 0.55,
            "thread_id": hash(domain) % 10,
            "scenario_type": ScenarioType.DOMAIN_SPECIFIC,
            "domain": domain,
            "task_family": "operational",
            "must_keep_facts": must_keep_facts,
        }
    
    def _cross_team_communication(self) -> Dict[str, Any]:
        """
        Cross-team coordination with multiple viewpoints.
        Each team has different priorities and language; tests routing.
        """
        teams = ["frontend", "backend", "data", "platform"]
        primary_team = random.choice(teams)
        
        task_text = (
            f"Coordinate across {teams} to plan Q2 initiative: "
            f"implement new feature with minimal regression risk"
        )
        
        incoming_messages = []
        priorities = {
            "frontend": "user experience, time-to-interactive, bundle-size",
            "backend": "API contract stability, backward compatibility, migration path",
            "data": "data lineage, reproducibility of training, feature drift",
            "platform": "resource efficiency, observability, disaster recovery",
        }
        
        for team in teams:
            incoming_messages.append(
                f"{team.title()}-Lead: Our priorities are {priorities[team]}. "
                f"We need commitment on these fronts."
            )
        
        # Add cross-team dependency messages
        incoming_messages.extend([
            "Backend: Frontend's new API calls require new database indexes—data team needs to review.",
            "Data: But we need full lineage transparency; backend can't use hardcoded queries.",
            "Platform: Both of you need to commit to error budgets. Can't deploy without SLI agreement.",
        ])
        
        prior_context = [
            f"Team-Charter({team}): Owns {priorities[team]}"
            for team in teams
        ]
        prior_context.extend([
            "Org-Policy: All features must pass cross-team review, documented in RFC.",
            "Last-Initiative: Took 3 months because of unaligned requirements; use async RFC reviews this time.",
            "Risk-Register: Q1 had 2 incidents due to data inconsistency. New feature must not reintroduce.",
        ])

        must_keep_facts = [
            "frontend",
            "backend",
            "data",
            "platform",
            "RFC",
            "error budgets",
            "SLI",
            "data lineage",
            "Q1 had 2 incidents",
        ]

        return {
            "task_text": task_text,
            "incoming_messages": incoming_messages,
            "prior_context": prior_context,
            "complexity": 0.72,
            "urgency": 0.65,
            "context_load": 0.75,
            "continuity": 0.6,
            "thread_id": hash(primary_team) % 10,
            "scenario_type": ScenarioType.CROSS_TEAM_COMM,
            "teams": teams,
            "task_family": "operational",
            "must_keep_facts": must_keep_facts,
        }
    
    def _timeseries_analysis(self) -> Dict[str, Any]:
        """
        Time-series analysis with seasonal patterns, anomalies, confounders.
        Tests if smart pruning preserves temporal relationships.
        """
        task_text = (
            "Analyze 90-day metrics: identify root cause of performance degradation "
            "in P99 latency, accounting for seasonal load, deployments, and infrastructure changes."
        )
        
        incoming_messages = [
            f"Data-Scientist: Week {w}: observed anomaly on day {3*w + random.randint(0,3)}, "
            f"P99 jumped {random.randint(20, 150)}ms, then recovered after {random.randint(2, 8)} hours."
            for w in range(1, 13)
        ]
        
        incoming_messages.extend([
            "DevOps: We deployed version-X on day 43, causing 15% CPU increase across fleet.",
            "Infra: Day 60-70: experienced extended VM churn (maintenance window), network jitter observed.",
            "Analyst: Day 75: marketing campaign launched, traffic volume +200%, but setup was OK.",
        ])
        
        prior_context = [
            "Baseline: Normal P99 is 45-55ms, seasonal peak is 100-120ms in Dec/Jan.",
            "Pattern: Deployments historically cause 5-15ms temp degradation, recovered <1hr.",
            "Alert-Tuning: Currently alert on P99>150ms for 5min, but false-positive rate is high.",
            "Incident-History: Two incidents in past 90d traced to resource contention.",
            "Infrastructure: We have 3 regions; latencies spike when rebalancing happens.",
            "SLA-Window: Peak hours 2PM-6PM UTC, need P99<100ms then; off-peak <80ms.",
        ]

        must_keep_facts = [
            "day 43",
            "day 60-70",
            "day 75",
            "version-X",
            "15% CPU",
            "+200%",
            "P99<100ms",
            "P99>150ms",
            "45-55ms",
            "100-120ms",
        ]

        return {
            "task_text": task_text,
            "incoming_messages": incoming_messages,
            "prior_context": prior_context,
            "complexity": 0.78,
            "urgency": 0.55,
            "context_load": 0.8,
            "continuity": 0.7,
            "thread_id": 1,
            "scenario_type": ScenarioType.TIMESERIES_ANALYSIS,
            "task_family": "operational",
            "must_keep_facts": must_keep_facts,
        }
    
    def _adversarial_pruning(self) -> Dict[str, Any]:
        """
        Adversarial scenario: similar-sounding contexts that naive pruning might remove
        but are actually critical to understanding.
        """
        task_text = (
            "We have two failed rollouts with similar error signatures. "
            "Differentiate root causes (infra vs code) and recommend solution."
        )
        
        # Contexts that sound similar but are crucial
        incoming_messages = [
            "Incident-Report-1: Rollout on 2024-01-15: timeout@100ms in service-A calling service-B",
            "Incident-Report-2: Rollout on 2024-01-22: timeout@100ms in service-B calling service-C",
            "DevOps-A: Rollout 1: we increased instance size for service-A that day",
            "DevOps-B: Rollout 2: we decreased instance size for service-B that day (cost savings)",
            "Analysis-1: Rollout 1 error: 'broken pipe' in logs, suggests network issue",
            "Analysis-2: Rollout 2 error: SIGKILL in logs, suggests OOM issue",
        ]
        
        prior_context = [
            "Context-A: Service-A talks to service-B; service-B talks to service-C (chain dependency)",
            "Context-B: Service-A uses HTTP/1.1; service-B uses gRPC (different timeout semantics)",
            "Context-C: Service-B has 64GB memory baseline; rollout 2 reduced to 32GB",
            "Context-D: Network path 1->2 goes through load-balancer-X; path 2->3 goes through LB-Y",
            "Critical-Fix-1: For incident-1, we need to upgrade network MTU on LB-X",
            "Critical-Fix-2: For incident-2, we need to restore memory to 64GB for service-B",
            "Conflicting-View: Some say both incidents are the same; history shows they're not",
        ]

        must_keep_facts = [
            "2024-01-15",
            "2024-01-22",
            "broken pipe",
            "SIGKILL",
            "32GB",
            "64GB",
            "LB-X",
            "LB-Y",
            "MTU",
            "service-A",
            "service-B",
            "service-C",
        ]

        return {
            "task_text": task_text,
            "incoming_messages": incoming_messages,
            "prior_context": prior_context,
            "complexity": 0.8,
            "urgency": 0.9,
            "context_load": 0.65,
            "continuity": 0.4,
            "thread_id": 1,
            "scenario_type": ScenarioType.ADVERSARIAL_PRUNING,
            "task_family": "operational",
            "must_keep_facts": must_keep_facts,
        }
    
    def _cascading_decisions(self) -> Dict[str, Any]:
        """
        Cascading decisions where early choice constrains later options.
        Tests if pruning removes options needed for downstream reasoning.
        """
        task_text = (
            "Plan microservices architecture migration: decide on (1) DB strategy, "
            "(2) comm protocol, (3) deployment model—in that order."
        )
        
        incoming_messages = [
            "Architect-1: If we choose multi-region DB, we need eventual-consistency semantics.",
            "Architect-2: But if consistency is needed, single-region is only option.",
            "Arch-DB: Multi-region DB choice unlocks global-scale but requires async replication.",
            "Arch-Comm: If async replication, we can't use RPC; must use event-sourcing.",
            "Arch-Deploy: Event-sourcing + event-driven forces serverless or event-mesh deployment.",
            "Ops: Serverless costs are predictable but cold-start adds latency.",
            "Ops2: Event-mesh requires new monitoring and requires team training.",
            "Architect-3: OK so our early DB choice cascades to 4 downstream implications.",
        ]
        
        prior_context = [
            "Decision-Tree: 3 DB paths (single-region, multi-region async, multi-region sync)",
            "Decision-Tree: For each, 2-3 comm options (RPC, msg-bus, pub-sub)",
            "Decision-Tree: For each combo, 2-3 deployment models",
            "Constraint-1: Team wants <6mo migration; serverless has learning curve",
            "Constraint-2: All decisions must be recorded and reversible (within reason)",
            "Precedent: Last migration chose Path-A; this decision affects new hires' learning",
        ]

        must_keep_facts = [
            "multi-region DB",
            "single-region",
            "eventual-consistency",
            "async replication",
            "event-sourcing",
            "serverless",
            "event-mesh",
            "<6mo migration",
            "Path-A",
        ]

        return {
            "task_text": task_text,
            "incoming_messages": incoming_messages,
            "prior_context": prior_context,
            "complexity": 0.85,
            "urgency": 0.7,
            "context_load": 0.72,
            "continuity": 0.5,
            "thread_id": 1,
            "scenario_type": ScenarioType.CASCADING_DECISIONS,
            "task_family": "operational",
            "must_keep_facts": must_keep_facts,
        }
    
    def _emergent_behavior(self, agent_count: int = 5) -> Dict[str, Any]:
        """
        Emergent behavior from many agents with local rules.
        Tests if context pruning loses critical interaction patterns.
        """
        task_text = (
            f"Monitor {agent_count} agents in distributed optimization task. "
            f"One agent behaving oddly; diagnose if it's local bug or system-wide issue."
        )
        
        incoming_messages = []
        for i in range(agent_count):
            status = random.choice([
                f"Agent-{i}: state={random.randint(0, 100)}, converging normally",
                f"Agent-{i}: attempting consensus with {random.randint(1, 3)} neighbors",
                f"Agent-{i}: received update from Agent-{(i+1) % agent_count}, adjusted accordingly",
            ])
            incoming_messages.append(status)
        
        # The "odd" agent
        odd_agent = random.randint(0, agent_count - 1)
        incoming_messages.append(
            f"Agent-{odd_agent}: WARN: loss-rate={random.randint(5, 30)}%, "
            f"messages from neighbors dropped, state diverging"
        )
        
        incoming_messages.extend([
            f"Monitor: Agent-{odd_agent}'s loss-rate is {random.randint(15, 25)}% higher than others",
            f"Network-Team: No network anomalies detected on path to Agent-{odd_agent}",
            f"Hypothesis-1: Agent-{odd_agent} has CPU overload, causing message queue to back up",
            f"Hypothesis-2: Communication protocol timing issue endemic to all (but manifests in Agent-{odd_agent} first)",
        ])
        
        prior_context = [
            f"Agent-Arch: {agent_count} replicas, gossip protocol, 10-turn consensus expected",
            "Expectation: All agents converge to same state within 15 turns (normal)",
            f"Anomaly: Agent-{odd_agent} still divergent after 20 turns (not normal)",
            "Previous-Incident: Year ago, we had cascading failures when one agent diverged.",
            "Critical: Determine if this is isolated or signals system-wide protocol issue.",
        ]

        must_keep_facts = [
            f"Agent-{odd_agent}",
            "gossip protocol",
            "10-turn consensus",
            "20 turns",
            "cascading failures",
            "loss-rate",
        ]

        return {
            "task_text": task_text,
            "incoming_messages": incoming_messages,
            "prior_context": prior_context,
            "complexity": 0.75,
            "urgency": 0.75,
            "context_load": 0.68,
            "continuity": 0.65,
            "thread_id": 1,
            "scenario_type": ScenarioType.EMERGENT_BEHAVIOR,
            "agent_count": agent_count,
            "task_family": "operational",
            "must_keep_facts": must_keep_facts,
        }

    def _math_reasoning(self) -> Dict[str, Any]:
        """
        Arithmetic and compound-interest word problem.
        Tests if pruning removes critical operands or intermediate calculations.
        """
        principal = random.randint(1000, 50000)
        rate = random.uniform(0.02, 0.08)
        years = random.randint(1, 30)

        task_text = (
            f"A principal amount of ${principal} is invested at {rate*100:.1f}% annual "
            f"interest compounded quarterly for {years} years. Calculate final amount. "
            f"Then compute weighted average return across 3 scenarios with different rates."
        )

        a = principal
        b = rate
        c = years
        compound_result = principal * ((1 + rate/4) ** (4*years))
        weighted_avg = (compound_result + principal * 0.05 * years) / 2

        incoming_messages = [
            f"Analyst-1: Quarterly compounding means n=4 periods per year",
            f"Analyst-2: Formula is A = P(1 + r/n)^(nt), where P={principal}, r={rate:.4f}, n=4, t={years}",
            f"Analyst-3: Intermediate calculation: {a}*{b}={a*b:.0f}",
            f"Analyst-4: Result before weighting: {compound_result:.2f}",
        ]

        prior_context = [
            f"Scenario-1: Standard calculation with rate={rate*100:.1f}%",
            f"Scenario-2: Alternative weighting assumes {principal*0.05}$ annual flat return",
            f"Cross-check: a*b={a*b:.0f}, a-c={a-c}",
            f"Final weighted average should be approximately {weighted_avg:.2f}",
        ]

        must_keep_facts = [
            str(principal),
            f"{rate:.4f}",
            str(years),
            f"{a}",
            f"{b:.4f}",
            f"{c}",
            f"{a*b:.0f}",
            f"{a-c}",
            f"{compound_result:.2f}",
            f"{weighted_avg:.2f}",
        ]

        return {
            "task_text": task_text,
            "incoming_messages": incoming_messages,
            "prior_context": prior_context,
            "complexity": 0.65,
            "urgency": 0.5,
            "context_load": 0.6,
            "continuity": 0.7,
            "thread_id": 1,
            "scenario_type": ScenarioType.MATH_REASONING,
            "task_family": "math",
            "must_keep_facts": must_keep_facts,
            "expected_answers": {
                "compound": compound_result,
                "weighted_avg": weighted_avg,
            }
        }

    def _multi_hop_qa(self) -> Dict[str, Any]:
        """
        Entity-chain QA: person -> org -> artifact -> city -> decade.
        Tests if pruning loses intermediate entities in reasoning chain.
        """
        person = random.choice(["Alice", "Bob", "Charlie", "Diana"])
        org = random.choice(["TechCorp", "FinanceHub", "ResearchLab", "StartupX"])
        artifact = random.choice(["Dataset-2024", "Framework-v3", "Model-Artifact", "Report-Q4"])
        city = random.choice(["San Francisco", "New York", "London", "Tokyo"])
        decade = random.choice(["1990s", "2000s", "2010s", "2020s"])

        task_text = (
            f"Who is {person}? Where does {person} work? What artifact does {org} produce? "
            f"In what city is {org} headquartered? What decade was the {artifact} published? "
            f"Link these entities and answer the final question."
        )

        incoming_messages = [
            f"Source-1: {person} is a principal engineer at {org}",
            f"Source-2: {org} is located in {city}",
            f"Source-3: {org} published {artifact} in the {decade}",
            f"Reasoning: Following the chain: {person} -> {org} -> {artifact} -> {city}/{decade}",
        ]

        prior_context = [
            f"Entity-Graph: {person} works-at {org}",
            f"Entity-Graph: {org} located-in {city}",
            f"Entity-Graph: {org} produces {artifact}",
            f"Timeline: {artifact} created-in {decade}",
        ]

        must_keep_facts = [
            person,
            org,
            artifact,
            city,
            decade,
        ]

        return {
            "task_text": task_text,
            "incoming_messages": incoming_messages,
            "prior_context": prior_context,
            "complexity": 0.6,
            "urgency": 0.5,
            "context_load": 0.55,
            "continuity": 0.75,
            "thread_id": 1,
            "scenario_type": ScenarioType.MULTI_HOP_QA,
            "task_family": "multihop",
            "must_keep_facts": must_keep_facts,
        }

    def _logical_deduction(self) -> Dict[str, Any]:
        """
        Modus-ponens chain over 4 named individuals.
        Tests if pruning removes logical connectives or premises.
        """
        names = [random.choice(["Alice", "Bob", "Charlie", "Diana", "Eve"]) for _ in range(4)]
        a, b, c, d = names

        task_text = (
            f"Given: {a} is a researcher. If someone works in research, then they publish papers. "
            f"If someone publishes papers, then they attend conferences. "
            f"If someone attends conferences, then they collaborate with peers. "
            f"Therefore, does {a} collaborate with peers? Use modus ponens."
        )

        incoming_messages = [
            f"Logic-Step-1: {a} works in research (given)",
            f"Logic-Step-2: {a} works in research implies {a} publishes papers (modus ponens)",
            f"Logic-Step-3: {a} publishes papers implies {a} attends conferences (modus ponens)",
            f"Logic-Step-4: {a} attends conferences implies {a} collaborates with peers (modus ponens)",
            f"Conclusion: Therefore, {a} collaborates with peers",
        ]

        prior_context = [
            f"Premise-1: {a} is a researcher",
            f"Premise-2: All researchers publish papers",
            f"Premise-3: All who publish papers attend conferences",
            f"Premise-4: All who attend conferences collaborate with peers",
            "Rule: Modus ponens: if P then Q, P is true, therefore Q is true",
        ]

        must_keep_facts = [
            a,
            "if",
            "then",
            "therefore",
            "implies",
            "modus ponens",
            "works in research",
        ]

        return {
            "task_text": task_text,
            "incoming_messages": incoming_messages,
            "prior_context": prior_context,
            "complexity": 0.68,
            "urgency": 0.55,
            "context_load": 0.62,
            "continuity": 0.78,
            "thread_id": 1,
            "scenario_type": ScenarioType.LOGICAL_DEDUCTION,
            "task_family": "logical",
            "must_keep_facts": must_keep_facts,
        }

    def _planning(self) -> Dict[str, Any]:
        """
        5-step plan with preconditions and ordering markers.
        Tests if pruning removes step-order information.
        """
        goal = random.choice([
            "deploy new feature to production",
            "migrate database to new schema",
            "refactor authentication system",
            "optimize query performance",
        ])

        task_text = (
            f"Create a 5-step plan to {goal}. Each step must include preconditions. "
            f"The plan must be executed in order. Identify all dependencies and constraints."
        )

        incoming_messages = [
            f"Step 1: Code review and testing. Precondition: All PR comments addressed.",
            f"Step 2: Staging deployment. Precondition: Step 1 complete, smoke tests pass.",
            f"Step 3: Canary rollout to 5% traffic. Precondition: Step 2 successful.",
            f"Step 4: Monitor canary metrics for 2 hours. Precondition: Step 3 started.",
            f"Step 5: Full production rollout. Precondition: Step 4 shows no errors.",
        ]

        prior_context = [
            f"Goal: {goal}",
            "first: implement and review code changes",
            "then: test in staging environment",
            "next: gradual production rollout with canary",
            "finally: full deployment with monitoring",
            "Constraint: Cannot skip to Step 5 without completing Steps 1-4",
            "Constraint: Rollback available at any step",
        ]

        must_keep_facts = [
            "Step 1",
            "Step 2",
            "Step 3",
            "Step 4",
            "Step 5",
            "first",
            "then",
            "next",
            "finally",
            "precondition",
        ]

        return {
            "task_text": task_text,
            "incoming_messages": incoming_messages,
            "prior_context": prior_context,
            "complexity": 0.7,
            "urgency": 0.65,
            "context_load": 0.68,
            "continuity": 0.8,
            "thread_id": 1,
            "scenario_type": ScenarioType.PLANNING,
            "task_family": "planning",
            "must_keep_facts": must_keep_facts,
        }

    def _swe_development(self) -> Dict[str, Any]:
        """
        Bug-fix / code-review / refactor task with code snippets and error details.
        Tests if pruning removes critical file/line/error context.
        """
        file_name = random.choice(["utils.py", "handlers.py", "models.py", "services.py"])
        line_num = random.randint(20, 200)
        func_name = random.choice(["process_data", "validate_input", "fetch_config", "handle_request"])
        error_type = random.choice(["ValueError", "TypeError", "AttributeError", "RuntimeError"])

        task_text = (
            f"Review and fix bug in file {file_name} at line {line_num}. "
            f"Function {func_name} raises {error_type}. Provide refactored code and test."
        )

        incoming_messages = [
            f"Issue: {error_type} in {file_name}:{line_num} when calling {func_name}",
            f"Stack trace shows: from module.errors import {error_type}",
            f"Current code: def {func_name}(data): return process(data)",
            f"Fix: Add type checking and error handling before processing",
            f"Test: Must add tests/test_{file_name[:-3]}.py with coverage",
        ]

        prior_context = [
            f"File: {file_name}",
            f"Function: def {func_name}(...)",
            f"Error: {error_type} raised on line {line_num}",
            f"Import: from module.errors import {error_type}",
            f"Tests: tests/test_{file_name[:-3]}.py covers {func_name}",
            "Refactor goal: cleaner code with explicit error handling",
        ]

        must_keep_facts = [
            file_name,
            f"{line_num}",
            func_name,
            error_type,
            f"def {func_name}",
            "from module.errors import",
            f"tests/test_{file_name[:-3]}.py",
        ]

        return {
            "task_text": task_text,
            "incoming_messages": incoming_messages,
            "prior_context": prior_context,
            "complexity": 0.72,
            "urgency": 0.7,
            "context_load": 0.7,
            "continuity": 0.65,
            "thread_id": 1,
            "scenario_type": ScenarioType.SWE_DEVELOPMENT,
            "task_family": "swe",
            "must_keep_facts": must_keep_facts,
        }

    def _research_task(self) -> Dict[str, Any]:
        """
        Literature synthesis with citations, findings, and statistical measures.
        Tests if pruning removes citations or statistical evidence.
        """
        author1 = random.choice(["Smith", "Johnson", "Williams", "Brown"])
        author2 = random.choice(["Chen", "Rodriguez", "Kumar", "O'Brien"])
        year1 = random.randint(2015, 2023)
        year2 = random.randint(2015, 2023)
        pvalue = random.uniform(0.001, 0.05)
        n_samples = random.randint(1000, 10000)
        improvement = random.randint(5, 40)

        task_text = (
            f"Synthesize findings from recent literature on model optimization. "
            f"Review ({author1}, {year1}) and ({author2}, {year2}). "
            f"Compare effect sizes (p={pvalue:.4f}, n={n_samples}). "
            f"Report {improvement}% improvement in benchmark."
        )

        incoming_messages = [
            f"Source-1: ({author1}, {year1}) found significant effect (p={pvalue:.4f})",
            f"Source-2: ({author2}, {year2}) reported {improvement}% improvement vs baseline",
            f"Source-3: Sample size n={n_samples} across both studies",
            f"Analysis: Effect sizes are consistent; recommend adoption",
        ]

        prior_context = [
            f"Citation: {author1}, Year={year1}, p-value={pvalue:.4f}",
            f"Citation: {author2}, Year={year2}, improvement={improvement}%",
            f"Meta-analysis: n={n_samples} total participants",
            f"Quote: '{author1} et al. demonstrate statistically significant improvement'",
            "Conclusion: Sufficient evidence for recommendation",
        ]

        must_keep_facts = [
            f"({author1}, {year1})",
            f"({author2}, {year2})",
            f"p={pvalue:.4f}",
            f"n={n_samples}",
            f"{improvement}%",
        ]

        return {
            "task_text": task_text,
            "incoming_messages": incoming_messages,
            "prior_context": prior_context,
            "complexity": 0.68,
            "urgency": 0.5,
            "context_load": 0.65,
            "continuity": 0.72,
            "thread_id": 1,
            "scenario_type": ScenarioType.RESEARCH_TASK,
            "task_family": "research",
            "must_keep_facts": must_keep_facts,
        }

    def generate_advanced_scenario(self, 
                                  scenario_type: ScenarioType = None) -> Dict[str, Any]:
        """
        Generate a single advanced scenario
        """
        if scenario_type is None:
            scenario_type = random.choice(list(ScenarioType))
        
        if scenario_type == ScenarioType.MULTI_TURN_STATEFUL:
            turn = random.randint(1, 5)
            team_id = random.randint(1, 3)
            return self._multi_turn_stateful(turn=turn, team_id=team_id)
        elif scenario_type == ScenarioType.HIGH_COMPLEXITY_REASONING:
            return self._high_complexity_reasoning()
        elif scenario_type == ScenarioType.DOMAIN_SPECIFIC:
            domain = random.choice(list(self.domains.keys()))
            return self._domain_specific(domain=domain)
        elif scenario_type == ScenarioType.CROSS_TEAM_COMM:
            return self._cross_team_communication()
        elif scenario_type == ScenarioType.TIMESERIES_ANALYSIS:
            return self._timeseries_analysis()
        elif scenario_type == ScenarioType.ADVERSARIAL_PRUNING:
            return self._adversarial_pruning()
        elif scenario_type == ScenarioType.CASCADING_DECISIONS:
            return self._cascading_decisions()
        elif scenario_type == ScenarioType.EMERGENT_BEHAVIOR:
            agent_count = random.randint(5, 15)
            return self._emergent_behavior(agent_count=agent_count)
        elif scenario_type == ScenarioType.MATH_REASONING:
            return self._math_reasoning()
        elif scenario_type == ScenarioType.MULTI_HOP_QA:
            return self._multi_hop_qa()
        elif scenario_type == ScenarioType.LOGICAL_DEDUCTION:
            return self._logical_deduction()
        elif scenario_type == ScenarioType.PLANNING:
            return self._planning()
        elif scenario_type == ScenarioType.SWE_DEVELOPMENT:
            return self._swe_development()
        elif scenario_type == ScenarioType.RESEARCH_TASK:
            return self._research_task()
        else:
            raise ValueError(f"Unknown scenario type: {scenario_type}")
    
    def generate_workload(self, count: int = 100,
                         scenario_distribution: Dict[ScenarioType, float] = None,
                         mix: str = None,
                         num_scenarios: int = None):
        """
        Generate a realistic workload distribution across scenario types.

        Args:
            count: Total number of scenarios (deprecated, use num_scenarios)
            scenario_distribution: Dict mapping ScenarioType to proportion
            mix: Named mix ("reasoning") or None for default
            num_scenarios: Total scenarios (preferred over count)
        """
        # Handle num_scenarios parameter
        if num_scenarios is not None:
            count = num_scenarios

        if scenario_distribution is None:
            # Handle named mixes
            if mix == "reasoning":
                # Evenly distribute reasoning experts
                scenario_distribution = {
                    ScenarioType.MATH_REASONING: 1.0 / 6,
                    ScenarioType.MULTI_HOP_QA: 1.0 / 6,
                    ScenarioType.LOGICAL_DEDUCTION: 1.0 / 6,
                    ScenarioType.PLANNING: 1.0 / 6,
                    ScenarioType.SWE_DEVELOPMENT: 1.0 / 6,
                    ScenarioType.RESEARCH_TASK: 1.0 / 6,
                }
            else:
                # Default realistic distribution: ~63% operational, ~25% reasoning, ~12% dev/research
                scenario_distribution = {
                    ScenarioType.MULTI_TURN_STATEFUL: 0.17,
                    ScenarioType.HIGH_COMPLEXITY_REASONING: 0.13,
                    ScenarioType.DOMAIN_SPECIFIC: 0.13,
                    ScenarioType.CROSS_TEAM_COMM: 0.10,
                    ScenarioType.TIMESERIES_ANALYSIS: 0.05,
                    ScenarioType.ADVERSARIAL_PRUNING: 0.03,
                    ScenarioType.CASCADING_DECISIONS: 0.01,
                    ScenarioType.EMERGENT_BEHAVIOR: 0.01,
                    ScenarioType.MATH_REASONING: 0.065,
                    ScenarioType.MULTI_HOP_QA: 0.065,
                    ScenarioType.LOGICAL_DEDUCTION: 0.055,
                    ScenarioType.PLANNING: 0.055,
                    ScenarioType.SWE_DEVELOPMENT: 0.060,
                    ScenarioType.RESEARCH_TASK: 0.060,
                }

        scenarios = []
        for scenario_type, proportion in scenario_distribution.items():
            count_for_type = int(count * proportion)
            for _ in range(count_for_type):
                scenarios.append(self.generate_advanced_scenario(scenario_type))

        return scenarios
