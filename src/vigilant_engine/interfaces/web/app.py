"""Local web UI for the intake agent.

Same engine as the CLI: the planner, quality gate, Jira publisher and evidence log are the
library code the CLI uses, through `intake.service`. The UI adds no shortcuts: gate errors
block publishing, approval is required, and every step lands in the run's event log.

Local-only safety:
* binds to 127.0.0.1 (see `serve`), never a network interface;
* rejects requests whose Host header isn't the local address (DNS-rebinding protection);
* every POST carries a per-process CSRF token, so other websites can't drive it;
* templates auto-escape, so model output can't inject markup.
"""

from __future__ import annotations

import contextlib
import getpass
import json
import os
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic
import jinja2
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from ...evidence import EvidenceError, RunStore, digest
from ...intake import Backlog, BacklogPlanner, IdeaBrief, Limits, PlannerError, service
from ...intake.models import AcceptanceCriterion, ClarifyingQuestions
from ...intake.prompts import DEFAULT_PROMPT_VERSION, PROMPTS
from ...intake.render import display_ids
from ...integrations.jira import (
    JiraClient,
    JiraError,
    JiraPublisher,
    PublishError,
    PublishOptions,
    PublishPlan,
)
from ...llm import LLMError, StructuredLLM
from .jobs import JobRunner, RunBusyError

HERE = Path(__file__).parent
RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")
STANDARD_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
CONTEXT_FIELDS = [  # the idea-context template as form fields (context-guidelines.md §2)
    ("users", "Users", "Who it is for, roughly how many, and who it is not for"),
    ("problem", "Problem today", "What happens now, how often, what it costs"),
    ("in_scope", "In scope", "Capabilities that must be in the backlog"),
    ("out_of_scope", "Out of scope / later", "What this backlog should not cover"),
    ("systems", "Systems", "Named systems it must integrate with"),
    ("constraints", "Constraints", "Regulations, data classification, accessibility, deadlines"),
    ("decided", "Already decided", "Vendors, architecture or policy choices that are settled"),
    ("success", "Success measures", "Target and today's baseline"),
    ("glossary", "Glossary", "Terms your organisation uses differently"),
]
STEP_LABELS = {"clarify": "Preparing clarifying questions",
               "decompose": "Drafting the backlog",
               "repair": "Fixing quality issues"}


@dataclass
class WebConfig:
    runs_dir: Path = Path("runs")
    standards_dir: Path = Path("standards")
    port: int = 8765
    llm_factory: Callable[[], StructuredLLM] | None = None
    jira_factory: Callable[[], JiraClient | None] | None = None
    jira_base_url: str = field(default_factory=lambda: os.environ.get("JIRA_BASE_URL", ""))
    inline_jobs: bool = False
    allowed_hosts: set[str] = field(default_factory=set)


def create_app(config: WebConfig | None = None) -> FastAPI:
    config = config or WebConfig()
    config.runs_dir.mkdir(parents=True, exist_ok=True)
    allowed_hosts = config.allowed_hosts or {f"127.0.0.1:{config.port}",
                                             f"localhost:{config.port}"}
    csrf_token = secrets.token_urlsafe(32)
    jobs = JobRunner(inline=config.inline_jobs)

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(HERE / "templates"),
                             autoescape=True, trim_blocks=True, lstrip_blocks=True)
    templates = Jinja2Templates(env=env)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        jobs.shutdown()

    app = FastAPI(title="vigilant-engine", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.state.jobs = jobs

    # --- safety middleware --------------------------------------------------------------

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if request.headers.get("host", "") not in allowed_hosts:
            return HTMLResponse("This app only answers on its local address.", status_code=403)
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and origin.split("://", 1)[-1] not in allowed_hosts:
                return HTMLResponse("Cross-site request refused.", status_code=403)
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'")
        return response

    async def checked_form(request: Request):
        """Read a POSTed form and require this process's CSRF token (every POST uses this)."""
        form = await request.form()
        if not secrets.compare_digest(str(form.get("csrf", "")), csrf_token):
            raise HTTPException(403, "Form expired. Go back, reload the page and try again.")
        return form

    # --- helpers --------------------------------------------------------------------------

    def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {
            "csrf": csrf_token, "claude_ready": _claude_configured(),
            "jira_ready": bool(config.jira_base_url), **ctx})

    def open_run(run_id: str) -> RunStore:
        if not RUN_ID_RE.match(run_id) or not (config.runs_dir / run_id).is_dir():
            raise HTTPException(404, "Run not found")
        return RunStore.open(config.runs_dir / run_id)

    def make_llm() -> StructuredLLM:
        if config.llm_factory is not None:
            return config.llm_factory()
        from ...llm import ClaudeLLM
        return ClaudeLLM()

    def make_jira() -> JiraClient | None:
        if config.jira_factory is not None:
            return config.jira_factory()
        from ...integrations.jira import JiraConfig
        try:
            return JiraClient(JiraConfig.from_env())
        except JiraError:
            return None

    def standards() -> list[dict[str, str]]:
        found = []
        for path in sorted(config.standards_dir.glob("*.md")):
            if path.stem.upper() == "TEMPLATE":
                continue
            text = path.read_text(encoding="utf-8")
            title = next((line.lstrip("# ").strip() for line in text.splitlines()
                          if line.startswith("# ")), path.stem)
            title = re.sub(r"^standards:\s*", "", title, flags=re.I).capitalize()
            found.append({"name": path.stem, "title": title, "text": text})
        return found

    def busy(run_id: str) -> HTMLResponse:
        return HTMLResponse("This run is busy. Wait for the current step to finish.",
                            status_code=409)

    # --- background work ----------------------------------------------------------------

    def plan_job(run_id: str, brief: IdeaBrief) -> None:
        store = RunStore.open(config.runs_dir / run_id)
        limits = service.load_limits(store)
        request_data = _read_json(store, "request.json")

        def save_attempt(attempt: int, backlog: Backlog, findings: list) -> None:
            store.write_json(f"attempts/attempt-{attempt}.json", backlog.model_dump())

        try:
            planner = BacklogPlanner(
                make_llm(), limits=limits, events=store.events, on_attempt=save_attempt,
                effort=request_data.get("effort", "high"),
                prompt_version=request_data.get("prompt_version", DEFAULT_PROMPT_VERSION))
            result = planner.plan(brief)
        except (LLMError, PlannerError, anthropic.AnthropicError) as exc:
            store.events.append("run.failed", actor="system:intake", payload={
                "error": service.claude_error_message(exc), "kind": type(exc).__name__})
            return
        service.save_plan_outputs(store, brief, result)

    def clarify_job(run_id: str, brief: IdeaBrief) -> None:
        store = RunStore.open(config.runs_dir / run_id)
        try:
            questions = BacklogPlanner(make_llm(), events=store.events).clarify(brief)
        except (LLMError, PlannerError, anthropic.AnthropicError) as exc:
            store.events.append("run.failed", actor="system:intake", payload={
                "error": service.claude_error_message(exc), "kind": type(exc).__name__})
            return
        if not questions.questions:  # nothing to ask: go straight to planning
            plan_job(run_id, brief)
            return
        store.write_json("questions.json", questions.model_dump())

    def publish_job(run_id: str, plan: PublishPlan, anchor: str) -> None:
        store = RunStore.open(config.runs_dir / run_id)
        client = make_jira()
        if client is None:
            store.events.append("jira.failed", actor="system:jira-publisher",
                                payload={"error": "Jira is not configured."})
            return
        try:
            publisher = JiraPublisher(client, store.path(service.MANIFEST_FILE),
                                      events=store.events)
            publisher.execute(plan, anchor=anchor)
        except (PublishError, JiraError) as exc:
            store.events.append("jira.failed", actor="system:jira-publisher",
                                payload={"error": str(exc)})
        finally:
            client.close()

    # --- pages ----------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def runs_page(request: Request):
        runs = []
        for path in sorted(config.runs_dir.iterdir(), reverse=True):
            if path.is_dir() and RUN_ID_RE.match(path.name):
                summary = service.run_summary(RunStore.open(path))
                if jobs.running(path.name):
                    summary["state"] = "working"
                runs.append(summary)
        return render(request, "runs.html", runs=runs)

    @app.get("/new", response_class=HTMLResponse)
    def new_page(request: Request, source: str | None = None):
        values: dict[str, Any] = {"standards": [], "clarify": True}
        if source and RUN_ID_RE.match(source) and (config.runs_dir / source).is_dir():
            values = _read_json(RunStore.open(config.runs_dir / source), "request.json") \
                or values
        return render(request, "new.html", values=values, fields=CONTEXT_FIELDS,
                      standards=standards(), prompt_versions=sorted(PROMPTS),
                      default_prompt=DEFAULT_PROMPT_VERSION, limits=Limits(), error=None)

    @app.post("/new")
    async def start_run(request: Request):
        form = await checked_form(request)
        values = {k: str(v).strip() for k, v in form.items()
                  if k not in ("csrf", "standards")}
        values["standards"] = [s for s in form.getlist("standards")
                               if STANDARD_RE.match(str(s))]
        values["clarify"] = form.get("clarify") == "on"
        idea = values.get("idea", "")
        known = {s["name"]: s for s in standards()}
        try:
            limits = Limits(*(int(values.get(k) or getattr(Limits, k)) for k in
                              ("max_epics", "max_features_per_epic",
                               "max_stories_per_feature", "max_total_issues")))
            if min(limits.__dict__.values()) < 1:
                raise ValueError
        except ValueError:
            limits = None
        error = None
        if not idea:
            error = "Describe the idea first."
        elif limits is None:
            error = "Limits must be whole numbers of 1 or more."
        elif values.get("prompt_version", DEFAULT_PROMPT_VERSION) not in PROMPTS:
            error = "Unknown prompt version."
        elif values.get("effort", "high") not in ("low", "medium", "high", "xhigh", "max"):
            error = "Unknown effort level."
        elif not _claude_configured() and config.llm_factory is None:
            error = ("No Claude API key found. Close the app, set ANTHROPIC_API_KEY in the "
                     "same Terminal window, and start it again.")
        if error:
            return render(request, "new.html", values=values, fields=CONTEXT_FIELDS,
                          standards=standards(), prompt_versions=sorted(PROMPTS),
                          default_prompt=DEFAULT_PROMPT_VERSION, limits=Limits(), error=error)

        context_parts = [known[name]["text"] for name in values["standards"] if name in known]
        idea_context = _compose_idea_context(values)
        if idea_context:
            context_parts.append(idea_context)
        brief = IdeaBrief(idea=idea, context=service.compose_context(context_parts))

        store = RunStore(config.runs_dir)
        store.write_text("idea.md", idea)
        store.write_json("request.json", values)
        service.save_limits(store, limits)
        store.events.append("run.started", actor=f"human:{getpass.getuser()}", payload={
            "source": "web", "prompt_version": values.get("prompt_version",
                                                          DEFAULT_PROMPT_VERSION),
            "standards": values["standards"], "limits": limits.__dict__})
        store.write_json("brief-draft.json", brief.model_dump())
        if values["clarify"]:
            jobs.submit(store.run_id, "clarify", lambda: clarify_job(store.run_id, brief))
        else:
            jobs.submit(store.run_id, "plan", lambda: plan_job(store.run_id, brief))
        return RedirectResponse(f"/runs/{store.run_id}", status_code=303)

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: str, notice: str | None = None):
        return render(request, "run.html", **_run_context(run_id), notice=notice)

    def _run_context(run_id: str, **extra: Any) -> dict[str, Any]:
        store = open_run(run_id)
        summary = service.run_summary(store)
        job = jobs.get(run_id)
        ctx: dict[str, Any] = {"run_id": run_id, "summary": summary, "job": job,
                               "working": jobs.running(run_id), "step": _step(store, job),
                               "jira_base_url": config.jira_base_url.rstrip("/")}
        ctx["questions"] = _read_json(store, "questions.json").get("questions", [])
        if store.path("backlog.json").exists() and summary["state"] != "invalid backlog":
            backlog, _, findings = service.load_backlog(store)
            ctx.update(backlog=backlog, findings=findings, ids=display_ids(backlog))
            plan_data = _read_json(store, service.PLAN_FILE)
            if plan_data:
                plan = PublishPlan.from_dict(plan_data)
                ctx["plan"] = plan
                ctx["plan_current"] = plan.backlog_digest == digest(backlog.model_dump())
        approvals = [e for e in _events(store) if e.get("type") == "intake.approved"]
        ctx["approval"] = approvals[-1] if approvals else None
        failures = [e for e in _events(store) if e.get("type") == "jira.failed"]
        ctx["jira_error"] = failures[-1]["payload"]["error"] if failures else None
        ctx.update(extra)
        return ctx

    @app.get("/runs/{run_id}/status")
    def run_status(run_id: str):
        store = open_run(run_id)
        job = jobs.get(run_id)
        return JSONResponse({"working": jobs.running(run_id), "step": _step(store, job),
                             "elapsed": job.elapsed if job else 0,
                             "state": service.run_summary(store)["state"]})

    @app.post("/runs/{run_id}/answers")
    async def answer_questions(request: Request, run_id: str):
        store = open_run(run_id)
        if jobs.running(run_id):
            return busy(run_id)
        form = await checked_form(request)
        questions = ClarifyingQuestions.model_validate(_read_json(store, "questions.json"))
        draft = IdeaBrief.model_validate(_read_json(store, "brief-draft.json"))
        answers = []
        for i, q in enumerate(questions.questions):
            reply = str(form.get(f"answer_{i}", "")).strip()
            answers.append((q.question, reply or f"(assumed) {q.default_assumption}"))
        brief = IdeaBrief(idea=draft.idea, context=draft.context, answers=answers)
        store.events.append("intake.clarified", actor=f"human:{getpass.getuser()}",
                            payload={"answers": answers})
        store.path("questions.json").unlink(missing_ok=True)
        jobs.submit(run_id, "plan", lambda: plan_job(run_id, brief))
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.post("/runs/{run_id}/stories/{ref}")
    async def edit_story(request: Request, run_id: str, ref: str):
        store = open_run(run_id)
        if jobs.running(run_id):
            return busy(run_id)
        form = await checked_form(request)
        backlog, _, _ = service.load_backlog(store)
        story = next((s for _, _, s in backlog.iter_stories() if s.ref == ref), None)
        if story is None:
            raise HTTPException(404, "Story not found")
        try:
            criteria = _parse_criteria(str(form.get("criteria", "")))
            story.title = str(form.get("title", story.title)).strip()
            story.user_story = str(form.get("user_story", story.user_story)).strip()
            story.description = str(form.get("description", story.description)).strip()
            story.story_points = int(form.get("story_points", story.story_points))
            story.priority = str(form.get("priority", story.priority))
            story.acceptance_criteria = criteria
            backlog = Backlog.model_validate(backlog.model_dump())  # re-validate the whole
        except (ValueError, ValidationError) as exc:
            return render(request, "run.html", **_run_context(run_id),
                          notice=f"Not saved: {_short_error(exc)}")
        service.save_backlog(store, backlog)
        store.path(service.PLAN_FILE).unlink(missing_ok=True)  # any preview is now stale
        return RedirectResponse(f"/runs/{run_id}?notice=Saved+{ref}#story-{ref}",
                                status_code=303)

    @app.get("/runs/{run_id}/json", response_class=HTMLResponse)
    def json_page(request: Request, run_id: str):
        store = open_run(run_id)
        text = store.path("backlog.json").read_text(encoding="utf-8")
        return render(request, "json.html", run_id=run_id, text=text, error=None)

    @app.post("/runs/{run_id}/json")
    async def save_json(request: Request, run_id: str):
        store = open_run(run_id)
        if jobs.running(run_id):
            return busy(run_id)
        text = str((await checked_form(request)).get("backlog", ""))
        try:
            backlog = Backlog.model_validate_json(text)
        except (ValidationError, ValueError) as exc:
            return render(request, "json.html", run_id=run_id, text=text,
                          error=service.validation_summary(exc))
        service.save_backlog(store, backlog)
        store.path(service.PLAN_FILE).unlink(missing_ok=True)
        return RedirectResponse(f"/runs/{run_id}?notice=Backlog+saved", status_code=303)

    @app.post("/runs/{run_id}/preview")
    async def preview(request: Request, run_id: str):
        store = open_run(run_id)
        if jobs.running(run_id):
            return busy(run_id)
        form = await checked_form(request)
        backlog, _, findings = service.load_backlog(store)
        if any(f.severity == "error" for f in findings):
            return render(request, "run.html", **_run_context(run_id),
                          notice="Fix the quality-gate errors before previewing.")
        client = make_jira()
        if client is None:
            return render(request, "run.html", **_run_context(run_id),
                          notice="Jira is not configured. Set JIRA_BASE_URL, JIRA_EMAIL and "
                                 "JIRA_API_TOKEN, then restart the app.")
        opts = PublishOptions(
            project_key=str(form.get("project", "")).strip().upper(), run_id=run_id,
            epic_type=str(form.get("epic_type") or "Epic"),
            feature_type=str(form.get("feature_type") or "Feature"),
            story_type=str(form.get("story_type") or "Story"),
            feature_mode=str(form.get("feature_mode") or "auto"),  # type: ignore[arg-type]
            story_points_field=str(form.get("story_points_field") or "") or None)
        try:
            if not re.match(r"^[A-Z][A-Z0-9_]{0,19}$", opts.project_key):
                raise PublishError("Enter a Jira project key, for example SAM1.")
            service.record_edit_if_changed(store, backlog, f"human:{getpass.getuser()}")
            plan = JiraPublisher(client, store.path(service.MANIFEST_FILE),
                                 events=store.events).prepare(backlog, opts)
        except (PublishError, JiraError) as exc:
            return render(request, "run.html", **_run_context(run_id),
                          notice=f"Preview failed: {exc}")
        finally:
            client.close()
        store.write_json(service.PLAN_FILE, plan.to_dict())
        return RedirectResponse(f"/runs/{run_id}#publish", status_code=303)

    @app.post("/runs/{run_id}/approve")
    async def approve(request: Request, run_id: str):
        store = open_run(run_id)
        if jobs.running(run_id):
            return busy(run_id)
        form = await checked_form(request)
        plan_data = _read_json(store, service.PLAN_FILE)
        if not plan_data:
            return render(request, "run.html", **_run_context(run_id),
                          notice="Preview the Jira plan first.")
        plan = PublishPlan.from_dict(plan_data)
        backlog, _, findings = service.load_backlog(store)
        problems = []
        if str(form.get("plan_digest")) != plan.digest:
            problems.append("the plan changed since you previewed it")
        if plan.backlog_digest != digest(backlog.model_dump()):
            problems.append("the backlog was edited after the preview")
        if any(f.severity == "error" for f in findings):
            problems.append("the backlog has quality-gate errors")
        approver = str(form.get("approver", "")).strip()
        if not approver:
            problems.append("enter your name as the approver")
        if str(form.get("confirm_project", "")).strip().upper() != plan.project_key:
            problems.append(f"type the project key {plan.project_key} to confirm")
        if problems:
            return render(request, "run.html", **_run_context(run_id),
                          notice="Not approved: " + "; ".join(problems) + ".")
        approval = service.record_approval(
            store, plan_digest=plan.digest, project=plan.project_key, issues=len(plan.issues),
            approver=approver, method="web form, typed project key",
            os_user=getpass.getuser(), interactive=True,
            backlog_digest=digest(backlog.model_dump()))
        try:
            jobs.submit(run_id, "publish", lambda: publish_job(run_id, plan, approval["hash"]))
        except RunBusyError:
            return busy(run_id)
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.post("/runs/{run_id}/verify", response_class=HTMLResponse)
    async def verify(request: Request, run_id: str):
        store = open_run(run_id)
        form = await checked_form(request)
        expect = str(form.get("expect_head", "")).strip() or None
        ok, message = store.events.verify(expect_head=expect)
        return render(request, "run.html", **_run_context(run_id),
                      notice=("Audit check passed: " if ok else "Audit check FAILED: ") + message)

    @app.get("/standards", response_class=HTMLResponse)
    def standards_page(request: Request):
        return render(request, "standards.html", standards=standards())

    @app.exception_handler(EvidenceError)
    async def damaged_log(request: Request, exc: EvidenceError):
        return HTMLResponse(f"Stopped: this run's audit log is damaged ({exc}).",
                            status_code=409)

    return app


# --- small helpers ------------------------------------------------------------------------

def _claude_configured() -> bool:
    return any(os.environ.get(v) for v in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                                           "ANTHROPIC_FEDERATION_RULE_ID"))


def _read_json(store: RunStore, name: str) -> dict[str, Any]:
    path = store.path(name)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _events(store: RunStore) -> list[dict[str, Any]]:
    try:
        return list(store.events.events())
    except EvidenceError:
        return []


def _step(store: RunStore, job: Any) -> str:
    """A plain-language description of what the current job is doing."""
    if job is None or job.done:
        return ""
    if job.kind == "publish":
        created = sum(1 for e in _events(store) if e.get("type") == "jira.issue_created")
        total = len(_read_json(store, service.PLAN_FILE).get("issues", []))
        return f"Creating Jira issues ({created} of {total})"
    step = ""
    for event in _events(store):
        if event.get("type") == "model.request":
            step = STEP_LABELS.get(event["payload"].get("step"), "Working")
        elif event.get("type") == "intake.lint":
            step = "Checking quality"
    return step or "Starting"


def _compose_idea_context(values: dict[str, Any]) -> str:
    lines = [f"**{label}:** {values[key]}" for key, label, _ in CONTEXT_FIELDS
             if str(values.get(key, "")).strip()]
    return "# Idea context\n\n" + "\n\n".join(lines) if lines else ""


def _parse_criteria(text: str) -> list[AcceptanceCriterion]:
    """One criterion per line: 'Given ... | When ... | Then ...'."""
    criteria = []
    for n, line in enumerate((ln for ln in text.splitlines() if ln.strip()), 1):
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 3:
            raise ValueError(f"criterion {n} needs three parts: Given ... | When ... | Then ...")
        given, when, then = (re.sub(rf"^{word}\s+", "", part, flags=re.I)
                             for word, part in zip(("given", "when", "then"), parts,
                                                   strict=True))
        criteria.append(AcceptanceCriterion(given=given, when=when, then=then))
    return criteria


def _short_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        err = exc.errors()[0]
        return f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
    return str(exc)


def serve(port: int = 8765, runs_dir: Path = Path("runs"), open_browser: bool = True) -> None:
    """Start the UI on the loopback address only."""
    import threading
    import webbrowser

    import uvicorn

    app = create_app(WebConfig(runs_dir=runs_dir, port=port))
    url = f"http://127.0.0.1:{port}/"
    print(f"vigilant-engine UI: {url}  (press Ctrl+C to stop)")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
