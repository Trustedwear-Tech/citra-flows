# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""
Real LLM integration test for Workflow 1: Government Application Intake.

This script:
  1. Uses hardcoded realistic government job applicant data (10 applicants)
  2. Calls the REAL LLM (LLM) with qualification rules
  3. Logs every LLM response to a file for analysis
  4. Runs through the full Workflow 1 DAG with real LLM + mocked SQL/SFTP

Run:
  cd c:\Github\Citra-AI\Citra-Service
  myenv\Scripts\activate
  python -m pytest tests/test_e2e_real_llm.py -v -s 2>&1 | tee tests/llm_test_output.log
"""

import sys
import os
import json
import logging
import pytest
from unittest.mock import AsyncMock, patch
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

# Load .env so XAI_API_KEY is available
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

os.environ["DISABLE_AUTH"] = "true"

from citra_workflow.models import (
    ExecutionStatus, NodeExecutionStatus, NodeType,
    WorkflowDefinition, NodeDefinition, EdgeDefinition,
    NodeExecutionResult,
)
from citra_workflow.nodes import NodeContext
from citra_workflow.nodes.processors import LLMProcessorNode

from tests.conftest_e2e import make_run_result, make_mock_node

# ============================================================================
# Logging Setup — write to file + console
# ============================================================================

LOG_DIR = Path(__file__).parent
LOG_FILE = LOG_DIR / "llm_evaluation_results.log"

logger = logging.getLogger("llm_e2e_test")
logger.setLevel(logging.DEBUG)
logger.handlers.clear()

# File handler — detailed
fh = logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")
fh.setLevel(logging.DEBUG)
fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
logger.addHandler(fh)

# Console handler — summary
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(ch)


# ============================================================================
# Realistic Government Job Applicant Data (10 applicants)
# ============================================================================

APPLICANTS = [
    {
        "id": "GOV-2026-001",
        "name": "Maria Gonzalez",
        "email": "maria.gonzalez@email.com",
        "position_applied": "Senior Policy Analyst",
        "education": "Master's in Public Policy, Georgetown University",
        "experience_years": 12,
        "gpa": 3.85,
        "certifications": ["PMP", "Lean Six Sigma Green Belt"],
        "previous_employer": "U.S. Department of Health and Human Services",
        "skills": ["policy analysis", "data-driven decision making", "stakeholder engagement", "federal budgeting"],
        "cover_letter_summary": "12 years of experience in federal policy development. Led cross-agency initiatives reducing processing backlogs by 35%. Proficient in evidence-based policy frameworks."
    },
    {
        "id": "GOV-2026-002",
        "name": "James Chen",
        "email": "james.chen@email.com",
        "position_applied": "Senior Policy Analyst",
        "education": "Bachelor's in Political Science, State University",
        "experience_years": 2,
        "gpa": 2.90,
        "certifications": [],
        "previous_employer": "Local county administration (intern)",
        "skills": ["basic research", "MS Office"],
        "cover_letter_summary": "Recent graduate looking for opportunities in government. Completed a 6-month internship filing documents at county office."
    },
    {
        "id": "GOV-2026-003",
        "name": "Dr. Aisha Patel",
        "email": "aisha.patel@email.com",
        "position_applied": "Senior Policy Analyst",
        "education": "PhD in Economics, MIT",
        "experience_years": 8,
        "gpa": 3.92,
        "certifications": ["CFA Level III", "Advanced Data Analytics"],
        "previous_employer": "World Bank - Policy Research Division",
        "skills": ["econometric modeling", "impact evaluation", "R/Python", "policy briefs", "multilateral coordination"],
        "cover_letter_summary": "Published researcher with 15 peer-reviewed papers on public finance. Designed evaluation frameworks adopted by 3 developing nations. Expert in quantitative policy analysis."
    },
    {
        "id": "GOV-2026-004",
        "name": "Robert Williams",
        "email": "robert.w@email.com",
        "position_applied": "Senior Policy Analyst",
        "education": "High School Diploma",
        "experience_years": 0,
        "gpa": 2.10,
        "certifications": [],
        "previous_employer": "N/A",
        "skills": ["typing"],
        "cover_letter_summary": "I am very enthusiastic and a quick learner. I believe I can do this job well despite having no experience."
    },
    {
        "id": "GOV-2026-005",
        "name": "Sarah Kim",
        "email": "sarah.kim@email.com",
        "position_applied": "Senior Policy Analyst",
        "education": "Master's in Public Administration, Harvard Kennedy School",
        "experience_years": 6,
        "gpa": 3.70,
        "certifications": ["CGFM (Certified Government Financial Manager)"],
        "previous_employer": "Government Accountability Office (GAO)",
        "skills": ["program evaluation", "performance auditing", "SQL", "Tableau", "congressional testimony preparation"],
        "cover_letter_summary": "6 years at GAO auditing federal programs. Authored 4 reports presented to Congress. Strong financial analysis and communication skills."
    },
    {
        "id": "GOV-2026-006",
        "name": "Mohammed Al-Rashid",
        "email": "m.alrashid@email.com",
        "position_applied": "Senior Policy Analyst",
        "education": "JD, Yale Law School + Master's in International Relations, Johns Hopkins SAIS",
        "experience_years": 15,
        "gpa": 3.88,
        "certifications": ["Bar Admission (DC, NY)", "Mediation Certification"],
        "previous_employer": "U.S. State Department — Office of Policy Planning",
        "skills": ["international law", "treaty negotiation", "legislative drafting", "interagency coordination", "crisis management"],
        "cover_letter_summary": "15 years in federal policy with dual legal and IR expertise. Negotiated 3 bilateral agreements. Led State Department's pandemic response policy framework."
    },
    {
        "id": "GOV-2026-007",
        "name": "Emily Thompson",
        "email": "emily.t@email.com",
        "position_applied": "Senior Policy Analyst",
        "education": "Bachelor's in Communications, Online University",
        "experience_years": 3,
        "gpa": 3.10,
        "certifications": ["Google Analytics"],
        "previous_employer": "Marketing agency (social media coordinator)",
        "skills": ["social media management", "content writing", "basic Excel"],
        "cover_letter_summary": "3 years in social media marketing. Looking to transition into government work. Strong communicator and team player."
    },
    {
        "id": "GOV-2026-008",
        "name": "David Okonkwo",
        "email": "d.okonkwo@email.com",
        "position_applied": "Senior Policy Analyst",
        "education": "Master's in Data Science, Columbia University + Bachelor's in Statistics",
        "experience_years": 5,
        "gpa": 3.65,
        "certifications": ["AWS Solutions Architect", "Certified Analytics Professional"],
        "previous_employer": "Census Bureau — Data Analytics Division",
        "skills": ["machine learning", "Python", "R", "GIS mapping", "survey methodology", "statistical modeling"],
        "cover_letter_summary": "5 years at Census Bureau building predictive models for population forecasting. Modernized legacy reporting pipelines saving $2M/year. Strong quantitative foundation."
    },
    {
        "id": "GOV-2026-009",
        "name": "Lisa Martinez",
        "email": "lisa.m@email.com",
        "position_applied": "Senior Policy Analyst",
        "education": "Bachelor's in Business Administration, Community College",
        "experience_years": 1,
        "gpa": 2.50,
        "certifications": [],
        "previous_employer": "Retail store manager",
        "skills": ["customer service", "inventory management", "scheduling"],
        "cover_letter_summary": "Managed a team of 8 at a retail store. Looking for a more stable government position. I'm organized and dependable."
    },
    {
        "id": "GOV-2026-010",
        "name": "Dr. Thomas Wright",
        "email": "t.wright@email.com",
        "position_applied": "Senior Policy Analyst",
        "education": "PhD in Public Health, Johns Hopkins + MPP, University of Chicago",
        "experience_years": 10,
        "gpa": 3.95,
        "certifications": ["Certified in Public Health (CPH)", "Project Management Professional (PMP)"],
        "previous_employer": "CDC — Center for Policy and Strategy",
        "skills": ["epidemiological analysis", "health policy", "regulatory impact assessment", "grant management", "stakeholder facilitation"],
        "cover_letter_summary": "10 years designing evidence-based health policy at CDC. Led COVID-19 policy coordination across 12 agencies. Published extensively on regulatory impact analysis."
    },
]

# Resume content per applicant (simulates SFTP file content)
RESUMES = {
    a["id"]: (
        f"RESUME: {a['name']}\n"
        f"Position: {a['position_applied']}\n"
        f"Education: {a['education']}\n"
        f"Experience: {a['experience_years']} years\n"
        f"Previous: {a['previous_employer']}\n"
        f"Skills: {', '.join(a['skills'])}\n"
        f"Certifications: {', '.join(a['certifications']) or 'None'}\n"
        f"\n{a['cover_letter_summary']}"
    )
    for a in APPLICANTS
}

APPLICANT_BY_ID = {a["id"]: a for a in APPLICANTS}

# Qualification rules the LLM will evaluate against
QUALIFICATION_RULES = """
GOVERNMENT POSITION: Senior Policy Analyst (GS-13/14 equivalent)

QUALIFICATION CRITERIA — evaluate each applicant against ALL of these:

1. EDUCATION (Required): Minimum Master's degree in a relevant field (Public Policy, 
   Public Administration, Economics, Law, Data Science, or related). PhD is preferred.
   Minimum GPA: 3.0 on a 4.0 scale.

2. EXPERIENCE (Required): Minimum 5 years of professional experience in policy analysis, 
   government operations, research, or a closely related field. Federal government 
   experience is strongly preferred.

3. SKILLS (Required): Must demonstrate at least 3 of the following:
   - Quantitative analysis / data analysis
   - Policy writing / legislative drafting
   - Stakeholder engagement / interagency coordination
   - Program evaluation / performance auditing
   - Advanced tools (Python, R, SQL, Tableau, or equivalent)

4. CERTIFICATIONS (Preferred, bonus points): PMP, CFA, CGFM, CPH, or similar 
   professional certifications add 5 bonus points each (max 10 bonus).

SCORING RUBRIC:
- Education: 0-25 points (25 = PhD relevant field, 20 = Master's relevant, 10 = Bachelor's relevant, 0 = no degree/irrelevant)
- Experience: 0-30 points (30 = 10+ years federal, 25 = 5-10 years federal, 20 = 5+ years non-federal, 10 = 2-5 years, 0 = <2 years)
- Skills match: 0-25 points (25 = 5+ relevant skills, 20 = 3-4 skills, 10 = 1-2 skills, 0 = none)
- Cover letter quality: 0-10 points (10 = compelling with measurable achievements, 5 = adequate, 0 = poor/irrelevant)
- Certifications bonus: 0-10 points

TOTAL: 0-100 points
QUALIFIED THRESHOLD: 65 points or higher

For EACH applicant, return a JSON object with:
{
    "applicant_id": "...",
    "name": "...",
    "score": <integer 0-100>,
    "qualified": <boolean>,
    "education_score": <0-25>,
    "experience_score": <0-30>,
    "skills_score": <0-25>,
    "cover_letter_score": <0-10>,
    "certification_bonus": <0-10>,
    "reasoning": "<2-3 sentence justification>"
}
"""


# ============================================================================
# Test: Real LLM Call — Process Each Applicant
# ============================================================================

@pytest.mark.integration
class TestRealLLMEvaluation:
    """Call the real LLM to evaluate each applicant with qualification rules.

    Marked `integration` like `TestWorkflow1RealLLM` below: both bill a live LLM
    provider. Without the marker this ran in the default unit suite and failed on
    any machine without a configured key — a fresh clone included.
    """

    @pytest.mark.asyncio
    async def test_evaluate_all_applicants_with_real_llm(self):
        """Send all 10 applicants to real LLM in 'each' mode, log every response."""
        logger.info("=" * 80)
        logger.info("REAL LLM EVALUATION TEST — Government Application Intake")
        logger.info("=" * 80)
        logger.info("Model: configured LLM")
        logger.info(f"Applicants: {len(APPLICANTS)}")
        logger.info(f"Log file: {LOG_FILE}")
        logger.info("")

        node = LLMProcessorNode()
        results = []

        for i, applicant in enumerate(APPLICANTS):
            app_id = applicant["id"]
            resume = RESUMES[app_id]

            # Merge applicant data + resume (like real workflow would)
            combined_data = {
                **applicant,
                "resume_content": resume,
            }

            logger.info(f"--- Applicant {i+1}/{len(APPLICANTS)}: {applicant['name']} ({app_id}) ---")
            logger.debug(f"INPUT DATA:\n{json.dumps(combined_data, indent=2)}")

            ctx = NodeContext(
                node_id=f"llm_eval_{app_id}",
                node_config={
                    "system_prompt": QUALIFICATION_RULES,
                    "user_prompt": (
                        "Evaluate this applicant for the Senior Policy Analyst position.\n\n"
                        "APPLICANT DATA:\n{{data}}\n\n"
                        "Return ONLY a single JSON object with the evaluation. No markdown, no code blocks."
                    ),
                    "model": None,
                    "processing_mode": "all",
                    "max_retries": 1,
                    "retry_delay_seconds": 2,
                },
                input_data={"records": [combined_data]},
                variables={"applicant_id": app_id},
                user_id="test-evaluator",
                execution_id=f"eval-test-{datetime.now().strftime('%Y%m%d%H%M%S')}",
                environment="test",
            )

            # REAL LLM CALL
            result = await node.run(ctx)

            status = result.status
            if hasattr(status, "value"):
                status = status.value

            logger.info(f"  Status: {status}")
            logger.info(f"  Duration: {result.duration_ms}ms")

            if status == "completed":
                output = result.output_data
                llm_result = output.get("items", [{}])[0] if output.get("items") else output.get("result", {})

                if isinstance(llm_result, dict):
                    score = llm_result.get("score", "N/A")
                    qualified = llm_result.get("qualified", "N/A")
                    reasoning = llm_result.get("reasoning", "N/A")
                    logger.info(f"  Score: {score}/100")
                    logger.info(f"  Qualified: {qualified}")
                    logger.info(f"  Reasoning: {reasoning}")

                    # Log detailed breakdown
                    logger.debug(f"  Education Score: {llm_result.get('education_score', 'N/A')}/25")
                    logger.debug(f"  Experience Score: {llm_result.get('experience_score', 'N/A')}/30")
                    logger.debug(f"  Skills Score: {llm_result.get('skills_score', 'N/A')}/25")
                    logger.debug(f"  Cover Letter Score: {llm_result.get('cover_letter_score', 'N/A')}/10")
                    logger.debug(f"  Certification Bonus: {llm_result.get('certification_bonus', 'N/A')}/10")

                    results.append({
                        "applicant_id": app_id,
                        "name": applicant["name"],
                        "llm_response": llm_result,
                        "duration_ms": result.duration_ms,
                        "status": "completed",
                    })
                else:
                    logger.warning(f"  LLM returned non-dict: {type(llm_result)}")
                    logger.warning(f"  Raw response: {llm_result}")
                    if output.get("parse_warning"):
                        logger.warning(f"  Parse warning: {output['parse_warning']}")
                    results.append({
                        "applicant_id": app_id,
                        "name": applicant["name"],
                        "llm_response": str(llm_result),
                        "duration_ms": result.duration_ms,
                        "status": "parse_error",
                    })
            else:
                logger.error(f"  FAILED: {result.error}")
                results.append({
                    "applicant_id": app_id,
                    "name": applicant["name"],
                    "error": result.error,
                    "duration_ms": result.duration_ms,
                    "status": "failed",
                })

            logger.info("")

        # ============================================================
        # Summary
        # ============================================================
        logger.info("=" * 80)
        logger.info("EVALUATION SUMMARY")
        logger.info("=" * 80)

        completed = [r for r in results if r["status"] == "completed"]
        failed = [r for r in results if r["status"] == "failed"]
        parse_errors = [r for r in results if r["status"] == "parse_error"]

        logger.info(f"Total: {len(results)} | Completed: {len(completed)} | Failed: {len(failed)} | Parse Errors: {len(parse_errors)}")
        logger.info("")

        if completed:
            # Sort by score descending
            scored = []
            for r in completed:
                resp = r["llm_response"]
                if isinstance(resp, dict):
                    scored.append({
                        "id": r["applicant_id"],
                        "name": r["name"],
                        "score": resp.get("score", 0),
                        "qualified": resp.get("qualified", False),
                        "reasoning": resp.get("reasoning", ""),
                    })

            scored.sort(key=lambda x: x["score"], reverse=True)

            logger.info(f"{'Rank':<5} {'ID':<16} {'Name':<25} {'Score':<8} {'Qualified':<12} {'Reasoning'}")
            logger.info("-" * 120)
            for rank, s in enumerate(scored, 1):
                q_str = "YES" if s["qualified"] else "NO"
                logger.info(f"{rank:<5} {s['id']:<16} {s['name']:<25} {s['score']:<8} {q_str:<12} {s['reasoning'][:60]}")

            qualified_list = [s for s in scored if s["qualified"]]
            unqualified_list = [s for s in scored if not s["qualified"]]

            logger.info("")
            logger.info(f"Qualified: {len(qualified_list)} applicants")
            logger.info(f"Unqualified: {len(unqualified_list)} applicants")

            if qualified_list:
                avg_q_score = sum(s["score"] for s in qualified_list) / len(qualified_list)
                logger.info(f"Average qualified score: {avg_q_score:.1f}")
            if unqualified_list:
                avg_u_score = sum(s["score"] for s in unqualified_list) / len(unqualified_list)
                logger.info(f"Average unqualified score: {avg_u_score:.1f}")

            # Write full JSON results for analysis
            json_output = LOG_DIR / "llm_evaluation_results.json"
            with open(json_output, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2, default=str)
            logger.info(f"\nFull JSON results saved to: {json_output}")

        # Assertions — basic sanity checks
        assert len(completed) >= 8, f"Expected at least 8/10 successful LLM calls, got {len(completed)}"

        for r in completed:
            resp = r["llm_response"]
            if isinstance(resp, dict):
                assert "score" in resp, f"Missing 'score' for {r['applicant_id']}"
                assert "qualified" in resp, f"Missing 'qualified' for {r['applicant_id']}"
                assert isinstance(resp["score"], (int, float)), f"Score should be numeric for {r['applicant_id']}"
                assert 0 <= resp["score"] <= 100, f"Score out of range for {r['applicant_id']}: {resp['score']}"


# ============================================================================
# Test: Full Workflow 1 with Real LLM (other nodes mocked)
# ============================================================================

@pytest.mark.integration
class TestWorkflow1RealLLM:
    """Run the complete intake workflow DAG with real LLM, mocked SQL/SFTP.

    Requires LLM_BASE_URL/LLM_API_KEY/LLM_MODEL to point at a real
    provider; the test issues an actual chat-completion request. Skipped
    in the unit-test run via the `integration` marker.
    """

    @pytest.mark.asyncio
    async def test_full_intake_workflow_real_llm(self, mock_executor, make_node, make_edge):
        """Workflow 1 E2E: webhook → sql(mock) → sftp(mock) → llm(REAL) → condition → sql_writer(mock)."""
        applicant = APPLICANTS[0]  # Maria Gonzalez — strong candidate
        app_id = applicant["id"]

        logger.info("")
        logger.info("=" * 80)
        logger.info(f"FULL WORKFLOW 1 TEST — Real LLM — Applicant: {applicant['name']}")
        logger.info("=" * 80)

        # Build the workflow DAG
        workflow = WorkflowDefinition(
            workflow_id="wf-real-llm-intake",
            user_id="test-user",
            name="Real LLM Intake Test",
            variables={"applicant_id": ""},
            nodes=[
                make_node("webhook", NodeType.WEBHOOK_TRIGGER, config={
                    "input_schema": [{"name": "applicant_id", "type": "string"}],
                }),
                make_node("sql_fetch", NodeType.SQL_SOURCE, config={
                    "connection_id": "test-sql",
                    "query": "SELECT * FROM applications WHERE id = '{{applicant_id}}'",
                }),
                make_node("sftp_fetch", NodeType.SFTP_SOURCE, config={
                    "connection_id": "test-sftp",
                    "remote_path": "/applications/{{applicant_id}}/resume.pdf",
                    "file_type": "text",
                }),
                make_node("llm_eval", NodeType.LLM_PROCESSOR, config={
                    "system_prompt": QUALIFICATION_RULES,
                    "user_prompt": (
                        "Evaluate this applicant for the Senior Policy Analyst position.\n\n"
                        "APPLICANT DATA:\n{{data}}\n\n"
                        "Return ONLY a single JSON object with the evaluation. No markdown, no code blocks."
                    ),
                    "model": None,
                    "processing_mode": "all",
                }),
                make_node("check_qualified", NodeType.CONDITION, config={
                    "field": "qualified",
                    "operator": "==",
                    "value": "true",
                }),
                make_node("store_result", NodeType.SQL_WRITER, config={
                    "connection_id": "test-sql",
                    "table": "evaluated_applicants",
                    "mode": "append",
                }),
                make_node("mark_rejected", NodeType.SET_VARIABLE, config={
                    "assignments": [{"name": "last_rejected", "value": "{{applicant_id}}"}],
                }),
            ],
            edges=[
                make_edge("webhook", "sql_fetch"),
                make_edge("sql_fetch", "sftp_fetch"),
                make_edge("sftp_fetch", "llm_eval"),
                make_edge("llm_eval", "check_qualified"),
                make_edge("check_qualified", "store_result", source_handle="true"),
                make_edge("check_qualified", "mark_rejected", source_handle="false"),
            ],
        )

        # Mock side effects — SQL, SFTP, condition, writer are mocked; LLM is REAL
        combined_data = {**applicant, "resume_content": RESUMES[app_id]}

        def webhook_se(ctx):
            return {
                "triggered": True, "trigger_type": "webhook",
                "payload": {"applicant_id": app_id},
                "variables": {"applicant_id": app_id},
            }

        def sql_se(ctx):
            return {"items": [applicant], "records": [applicant], "count": 1}

        def sftp_se(ctx):
            resume = RESUMES[app_id]
            return {"items": [{"content": resume}], "records": [{"content": resume}],
                    "count": 1, "source_type": "text", "content": resume}

        def condition_se(ctx):
            # Use actual LLM output to determine branch
            input_data = ctx.input_data or {}
            # LLM output is envelope: {"items": [parsed], "meta": {...}}
            items = input_data.get("items", [])
            result = items[0] if items else input_data.get("result", {})
            qualified = result.get("qualified", False) if isinstance(result, dict) else False
            return {
                "items": items,
                "meta": {
                    "condition_result": qualified,
                    "branch": "true" if qualified else "false",
                },
            }

        def writer_se(ctx):
            return {"written": 1, "table": "evaluated_applicants"}

        def set_var_se(ctx):
            return {"variables": {"last_rejected": app_id}}

        # LLM node uses the REAL implementation
        real_llm_node = LLMProcessorNode()

        dispatch_mock = {
            "webhook": webhook_se,
            "sql_fetch": sql_se,
            "sftp_fetch": sftp_se,
            "check_qualified": condition_se,
            "store_result": writer_se,
            "mark_rejected": set_var_se,
        }

        def node_factory(node_type):
            if node_type == NodeType.LLM_PROCESSOR:
                # Return the REAL LLM node
                return real_llm_node
            # All other nodes are mocked
            return make_mock_node(
                side_effect_fn=lambda ctx: dispatch_mock.get(ctx.node_id, lambda c: {})(ctx)
            )

        with patch("citra_workflow.executor.get_node", side_effect=node_factory):
            result = await mock_executor.execute(
                workflow,
                trigger_data={"applicant_id": app_id},
            )

        # Log results
        logger.info(f"Workflow Status: {result.status}")
        for nid, nr in result.node_results.items():
            status = nr["status"] if isinstance(nr, dict) else nr.status
            if hasattr(status, "value"):
                status = status.value
            output = nr["output_data"] if isinstance(nr, dict) else nr.output_data
            logger.info(f"  Node '{nid}': {status}")
            if nid == "llm_eval" and status == "completed":
                llm_result = output.get("items", [{}])[0] if output.get("items") else output.get("result", {})
                logger.info(f"    LLM Score: {llm_result.get('score', 'N/A')}")
                logger.info(f"    LLM Qualified: {llm_result.get('qualified', 'N/A')}")
                logger.info(f"    LLM Reasoning: {llm_result.get('reasoning', 'N/A')}")
                logger.debug(f"    Full LLM output: {json.dumps(llm_result, indent=2)}")
            if nid == "check_qualified" and status == "completed":
                logger.info(f"    Branch taken: {output.get('meta', {}).get('branch', 'N/A')}")

        # Assert workflow completed
        assert result.status == ExecutionStatus.COMPLETED, f"Workflow failed: {result.error}"

        # Assert LLM node completed
        llm_nr = result.node_results["llm_eval"]
        llm_status = llm_nr["status"] if isinstance(llm_nr, dict) else llm_nr.status
        if hasattr(llm_status, "value"):
            llm_status = llm_status.value
        assert llm_status == "completed", f"LLM node failed: {llm_nr.get('error', '')}"

        # Maria Gonzalez (12yr exp, Master's, PMP) should be qualified
        llm_output = llm_nr["output_data"] if isinstance(llm_nr, dict) else llm_nr.output_data
        llm_result = llm_output.get("items", [{}])[0] if llm_output.get("items") else llm_output.get("result", {})
        if isinstance(llm_result, dict):
            assert llm_result.get("score", 0) > 50, "Strong candidate should score > 50"
            logger.info(f"\nMaria Gonzalez final score: {llm_result.get('score')}/100, Qualified: {llm_result.get('qualified')}")
