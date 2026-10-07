"""Pure parsing of EWKit repository content: sample lists, configs and CODEOWNERS.

Nothing here talks to GitLab; the repository connector feeds it file contents.
"""

import posixpath
import re
from dataclasses import dataclass, field
from typing import Literal

import yaml

# r-tag -> MC campaign, as in EWKit's share/EWGridRun.py (outputDSFormatter) at 17c998c.
# sample_helpers.rtag_mc_map() has the same entries minus r14932.
RTAG_CAMPAIGNS: dict[str, str] = {
    "r13167": "mc20a",
    "r14859": "mc20a",
    "r13144": "mc20d",
    "r14860": "mc20d",
    "r13145": "mc20e",
    "r14861": "mc20e",
    "r14622": "mc23a",
    "r15540": "mc23a",
    "r14932": "mc23a",
    "r15224": "mc23d",
    "r15530": "mc23d",
    "r16083": "mc23e",
}

UNKNOWN_CAMPAIGN = "unknown"

# Folders under data/ that are not analyses
NON_ANALYSIS_FOLDERS = frozenset({"common"})

# Folders EWKit's CI runs on example files (.gitlab-ci.yml at 17c998c); the others are never exercised
CI_TESTED_FOLDERS = frozenset({"common", "CompressedSleptons", "RadiativeDecays", "DisplacedTrack"})

SAMPLE_FILE_RE = re.compile(r"^(?P<run>Run\d+)_(?P<sample>[A-Za-z0-9_]+)\.txt$")
CONFIG_FILE_RE = re.compile(r"^config[A-Za-z0-9_\-]*\.ya?ml$")

_MC_RE = re.compile(
    r"^(?P<project>mc\d+_\w+?)\.(?P<dsid>\d{6,})\.(?P<physics_short>[^.]+)"
    r"\.(?P<prod_step>[^.]+)\.(?P<format>[^.]+)\.(?P<tags>[^.]+)$"
)
_DATA_RE = re.compile(
    r"^(?P<project>data(?P<year>\d{2})_\w+?)\.(?P<period>[^.]+)\.(?P<stream>[^.]+)"
    r"\.(?P<prod_step>[^.]+)\.(?P<format>[^.]+)\.(?P<tags>[^.]+)$"
)


def campaign_sort_key(campaign: str) -> tuple[int, str]:
    """mc campaigns first, then data years, then anything unrecognised."""
    if campaign.startswith("mc"):
        return (0, campaign)
    if campaign.startswith("data"):
        return (1, campaign)
    return (2, campaign)


@dataclass(frozen=True)
class Dataset:
    name: str
    kind: Literal["mc", "data", "unknown"]
    project: str | None = None
    dsid: str | None = None
    physics_short: str | None = None
    format: str | None = None
    etag: str | None = None
    simtag: str | None = None
    rtags: tuple[str, ...] = ()
    ptag: str | None = None
    campaign: str = UNKNOWN_CAMPAIGN
    problem: str | None = None


def parse_dataset(line: str) -> Dataset:
    """Split a Rucio dataset name into DSID, short name, tags and campaign."""
    name = line.strip().rstrip("/")
    if ":" in name:  # drop a Rucio scope prefix
        name = name.split(":", 1)[1]

    if match := _MC_RE.match(name):
        tags = match["tags"].split("_")
        rtags = tuple(t for t in tags if re.fullmatch(r"r\d+", t))
        known = [RTAG_CAMPAIGNS[t] for t in rtags if t in RTAG_CAMPAIGNS]
        if known:
            campaign, problem = known[0], None
        elif rtags:
            campaign, problem = UNKNOWN_CAMPAIGN, f"Unknown r-tag {', '.join(rtags)}"
        else:
            campaign, problem = UNKNOWN_CAMPAIGN, "No r-tag in dataset name"
        return Dataset(
            name=name,
            kind="mc",
            project=match["project"],
            dsid=match["dsid"],
            physics_short=match["physics_short"],
            format=match["format"],
            etag=_first_tag(tags, r"e\d+"),
            simtag=_first_tag(tags, r"[as]\d+"),
            rtags=rtags,
            ptag=_last_tag(tags, r"p\d+"),
            campaign=campaign,
            problem=problem,
        )

    if match := _DATA_RE.match(name):
        tags = match["tags"].split("_")
        return Dataset(
            name=name,
            kind="data",
            project=match["project"],
            physics_short=match["period"],
            format=match["format"],
            rtags=tuple(t for t in tags if re.fullmatch(r"r\d+", t)),
            ptag=_last_tag(tags, r"p\d+"),
            campaign=f"data{match['year']}",
        )

    return Dataset(name=name, kind="unknown", problem="Not a recognised ATLAS dataset name")


def _first_tag(tags: list[str], pattern: str) -> str | None:
    return next((t for t in tags if re.fullmatch(pattern, t)), None)


def _last_tag(tags: list[str], pattern: str) -> str | None:
    return next((t for t in reversed(tags) if re.fullmatch(pattern, t)), None)


@dataclass
class DatasetGroup:
    heading: str | None
    datasets: list[Dataset] = field(default_factory=list)


def parse_sample_list(text: str) -> list[DatasetGroup]:
    """One dataset per line; '#' lines start a new named group."""
    groups: list[DatasetGroup] = [DatasetGroup(heading=None)]
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
            if heading:
                groups.append(DatasetGroup(heading=heading))
            continue
        groups[-1].datasets.append(parse_dataset(line))
    return [g for g in groups if g.datasets or g.heading is not None]


def sample_file_parts(filename: str) -> tuple[str, str] | None:
    """'Run3_bkg_syst.txt' -> ('Run3', 'bkg_syst')."""
    match = SAMPLE_FILE_RE.match(filename)
    return (match["run"], match["sample"]) if match else None


@dataclass
class ConfigSummary:
    extend: str | None = None
    blocks: list[str] = field(default_factory=list)
    added_block_modules: list[str] = field(default_factory=list)
    output_tree: str | None = None
    # runSystematics / saveCutBookkeepers wherever they appear in the config; None if not set here
    run_systematics: bool | None = None
    save_cutbookkeepers: bool | None = None
    parse_error: str | None = None


def summarize_config(text: str) -> ConfigSummary:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return ConfigSummary(parse_error=str(exc).splitlines()[0])
    if doc is None:
        return ConfigSummary()
    if not isinstance(doc, dict):
        return ConfigSummary(parse_error="Top level of the config is not a mapping")

    extend = doc.get("Extend")
    added = doc.get("AddConfigBlocks") or []
    output = doc.get("Output")
    return ConfigSummary(
        extend=extend if isinstance(extend, str) else None,
        blocks=[k for k in doc if k != "Extend"],
        added_block_modules=[
            b["modulePath"] for b in added if isinstance(b, dict) and isinstance(b.get("modulePath"), str)
        ],
        output_tree=output.get("treeName") if isinstance(output, dict) else None,
        run_systematics=_find_bool(doc, "runSystematics"),
        save_cutbookkeepers=_find_bool(doc, "saveCutBookkeepers"),
    )


def _find_bool(node: object, key: str) -> bool | None:
    """The first boolean value of `key` anywhere in a parsed YAML document."""
    if isinstance(node, dict):
        if isinstance(node.get(key), bool):
            return node[key]
        children = node.values()
    elif isinstance(node, list):
        children = node
    else:
        return None
    for child in children:
        if (found := _find_bool(child, key)) is not None:
            return found
    return None


def resolve_extend(config_path: str, target: str) -> str | None:
    """Resolve an Extend: target relative to the config's folder; None if it leaves the repo."""
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(config_path), target))
    return None if resolved.startswith("..") or resolved.startswith("/") else resolved


# Packages that config blocks may load but the standard EWKit image does not include
UNAVAILABLE_PACKAGES = frozenset({"TopCPToolkit"})


def unavailable_modules(modules: list[str]) -> list[str]:
    """Config block modules (AddConfigBlocks modulePath) the standard EWKit image cannot load."""
    return sorted({m for m in modules if m.split(".", 1)[0] in UNAVAILABLE_PACKAGES})


def parse_codeowners(text: str) -> list[tuple[str, list[str]]]:
    rules = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("[") or line.startswith("^["):
            continue
        pattern, *owners = line.split()
        rules.append((pattern, [o for o in owners if o.startswith("@")]))
    return rules


def owners_for_path(rules: list[tuple[str, list[str]]], path: str) -> list[str]:
    """Owners of a directory path such as 'data/RadiativeDecays'. The last matching rule wins."""
    owners: list[str] = []
    for pattern, rule_owners in rules:
        if pattern == "*":
            owners = rule_owners
            continue
        prefix = pattern.strip("/")
        if path == prefix or path.startswith(prefix + "/"):
            owners = rule_owners
    return owners


def ref_slug(ref: str) -> str:
    """GitLab's CI_COMMIT_REF_SLUG, which EWKit's CI uses as the image tag."""
    slug = re.sub(r"[^a-z0-9]", "-", ref.lower())[:63]
    return slug.strip("-")
