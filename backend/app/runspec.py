"""The RunSpec (the options a production passes to EWRun.py) and what the run setup produces around it.

The RunSpec mirrors EWGridRun.py's options. Its field descriptions double as the schema the
builder agent fills through structured output, so keep them short and accurate.
"""

from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["blocking", "warning", "info"]
Systematics = Literal["do", "no", "config-default"]
Purpose = Literal["production", "test"]


class RunSpec(BaseModel):
    analysis: str = Field(description="Analysis folder under data/, e.g. RadiativeDecays (--analysis)")
    ref: str = Field(description="EWKit branch, tag or commit as the person named it; code resolves it to a SHA")
    config: str = Field(default="config.yaml", description="Config file in the folder (--configName)")
    runs: list[str] = Field(default_factory=list, description="Runs to produce, e.g. ['Run2', 'Run3'] (--runs)")
    sample_lists: list[str] = Field(
        default_factory=list,
        description="Sample list names as found in DAODs/Run*_<name>.txt, e.g. ['bkg', 'data'] (--samples)",
    )
    systematics: Systematics = Field(
        default="config-default",
        description="'do' adds --doSys, 'no' adds --noSys, 'config-default' leaves the config's runSystematics",
    )
    no_filter: bool = Field(default=False, description="--noFilter: turn off the event filter")
    no_thinning: bool = Field(default=False, description="--noThinning: keep all objects")
    include: list[str] = Field(
        default_factory=list, description="Regexes (-w); only datasets matching one of them are run"
    )
    skip: list[str] = Field(default_factory=list, description="Regexes (--skip); datasets matching any are left out")
    purpose: Purpose = Field(
        default="production",
        description="'production' needs a git tag; 'test' allows branches and commits, with a test suffix",
    )
    memory_mb: int = Field(default=2000, ge=500, le=64000, description="Memory per job in MB (EWGridRun's default 2000)")
    files_per_job: int | None = Field(
        default=None, ge=1, description="Input files per job; null to work it out at Gate 1 from file sizes"
    )


class RunSpecPatch(BaseModel):
    """A change to a RunSpec. Each field that is set replaces the whole field, lists included."""

    analysis: str | None = None
    ref: str | None = None
    config: str | None = None
    runs: list[str] | None = None
    sample_lists: list[str] | None = None
    systematics: Systematics | None = None
    no_filter: bool | None = None
    no_thinning: bool | None = None
    include: list[str] | None = None
    skip: list[str] | None = None
    purpose: Purpose | None = None
    memory_mb: int | None = None
    files_per_job: int | None = None


def apply_patch(spec: RunSpec, patch: RunSpecPatch) -> RunSpec:
    """The spec with the patch applied. Raises pydantic.ValidationError if the result is invalid."""
    return RunSpec.model_validate({**spec.model_dump(), **patch.model_dump(exclude_none=True)})


class Evidence(BaseModel):
    path: str
    web_url: str | None = None
    # Excerpts of the file, each with its 1-based line number when known
    lines: list[str] = []
    line_numbers: list[int] = []


class Finding(BaseModel):
    """One result of a deterministic check. The validator decides; no model does."""

    id: str
    code: str
    severity: Severity
    message: str
    field: str | None = None
    evidence: list[Evidence] = []
    # A mechanical fix worked out by code (RunSpecPatch fields), offered when no model proposes one
    suggestion: dict | None = None


class SelectionRow(BaseModel):
    """One sample list the spec selects, and how many of its datasets the patterns keep."""

    run: str
    sample: str
    file: str
    exists: bool
    datasets: int = 0
    selected: int = 0
    campaigns: dict[str, int] = {}


class ValidationReport(BaseModel):
    commit: str
    ref_kind: Literal["tag", "branch", "commit"]
    findings: list[Finding]
    selection: list[SelectionRow]

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "blocking"]


class FieldReason(BaseModel):
    field: str = Field(description="RunSpec field name")
    reason: str = Field(description="One line on why it has this value")


class BuilderDraft(BaseModel):
    """What the builder returns: a RunSpec plus the reasoning the person needs to check it."""

    spec: RunSpec
    reasons: list[FieldReason] = Field(default_factory=list, description="One entry per field you set")
    assumptions: list[str] = Field(default_factory=list, description="Choices the request did not settle")
    questions: list[str] = Field(
        default_factory=list, description="Questions for the person where the request is ambiguous"
    )


class EvidenceCitation(BaseModel):
    path: str = Field(description="Repository path, e.g. data/DiTauStudies/DAODs/Run3_bkg.txt")
    line: int | None = Field(default=None, description="1-based line number, if one line is meant")
    excerpt: str | None = Field(default=None, description="Short excerpt of that file")


class ReviewerIssue(BaseModel):
    severity: Severity
    title: str = Field(description="One short sentence")
    explanation: str = Field(description="Plain-language explanation for a physicist")
    finding_codes: list[str] = Field(
        default_factory=list, description="Codes of the validator findings this card is about, e.g. UNKNOWN_RTAG"
    )
    evidence: list[EvidenceCitation] = Field(default_factory=list, description="Files and lines that show the problem")
    patch: RunSpecPatch | None = Field(
        default=None, description="RunSpec fields to replace to fix it; lists are replaced whole. Omit if no spec change helps"
    )
    if_declined: str | None = Field(default=None, description="What happens if the person does not accept the patch")


class ReviewerOutput(BaseModel):
    issues: list[ReviewerIssue] = Field(default_factory=list)


class PatchEffect(BaseModel):
    clears: list[str] = []
    introduces: list[str] = []


class IssueCard(BaseModel):
    """A reviewer issue after code has checked its evidence and dry-run its patch."""

    id: str
    # 'validator' cards are added by code for blocking findings the reviewer left out
    source: Literal["reviewer", "validator"] = "reviewer"
    severity: Severity
    title: str
    explanation: str
    finding_codes: list[str]
    evidence: list[Evidence]
    patch: dict | None = None
    patch_error: str | None = None
    effect: PatchEffect | None = None
    if_declined: str | None = None
    owners: list[str] = []
